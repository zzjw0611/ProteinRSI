"""Synthetic post-assay C checkpoints, independent of the method-improvement gate."""
from copy import deepcopy
import json

import httpx
import pytest

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import Candidate, Observation, digest
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.replay.broker import GuardedMetaAgent, GuardedTeam, dispatch
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, SponsoredStore, Store
from proteinrsi.trajectory import read_trace
from test_feedback_improvement_recovery import interrupted_feedback


def pending_feedback(tmp_path, monkeypatch, *, paused=False):
    interrupted_feedback(tmp_path, monkeypatch, RuntimeError)
    campaign = Campaign(Store(tmp_path))
    if paused:
        state = campaign.state
        state["method_governance"].update(paused=True, consecutive_failures=3,
                                           pause_reason="Synthetic governance pause")
        campaign.store.put("campaign", "state", state)
    return campaign


def attach_provider(campaign, monkeypatch, requests, *, fail_role=None, error=None, guarded=False):
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)

    def respond(request):
        payload = json.loads(request.content)
        role = "C-feedback" if "# C — experimental feedback" in payload["messages"][0]["content"] else "M"
        requests.append((role, payload))
        if role == fail_role and sum(r == role for r, _ in requests) == 1:
            if error is not None:
                raise error("Synthetic unknown completion")
            return httpx.Response(503)
        result = {"summary": "Synthetic measured-evidence feedback"} if role == "C-feedback" else {
            "reason": "Synthetic no-change method decision"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    llm = JSONLLM(campaign.store, model="synthetic-checkpoint", base_url="https://example.invalid",
                  api_key="fake", max_attempts=1, transport=httpx.MockTransport(respond))
    campaign.team = Team(campaign.store, llm)
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()["available"]:
            pytest.skip("Guarded worker sandbox unavailable")
        campaign.team = GuardedTeam.from_team(campaign.team)
        campaign.meta_agent = GuardedMetaAgent(campaign.team)
    else:
        campaign.meta_agent = MetaAgent(llm, campaign.store)
    monkeypatch.setattr(campaign.team, "run", lambda view: [Candidate(sequence=s)
        for s in view.task.candidates if s not in {o.sequence for o in view.observations}])


def finish_synthetic_batch(campaign, batch):
    task = campaign.view().task
    campaign.approve(batch.batch_id, operator="synthetic-test")
    observations = [Observation(sample_id=s.sample_id, batch_id=batch.batch_id,
        sequence=s.candidate.sequence, value=1.0, metric=task.metric, unit=task.unit,
        source="synthetic", assay_protocol=task.assay_protocol) for s in batch.samples]
    campaign.ingest(observations)
    return observations


def roles(requests):
    return [role for role, _ in requests]


@pytest.mark.parametrize("guarded", [False, True])
def test_governance_pause_keeps_one_c_per_evidence_and_resume_reuses_it(tmp_path, monkeypatch, guarded):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests, guarded=guarded)
    batch = campaign.prepare()
    assert roles(requests) == ["C-feedback"]
    assert campaign.state["considered_round"] != 1
    assert campaign.methods.paused(campaign.state)
    assert campaign.state["history"][-1]["analyst_feedback"]["summary"]
    assert not campaign.store.all("patches")
    assert campaign.prepare() == batch
    campaign = Campaign(Store(tmp_path))
    attach_provider(campaign, monkeypatch, requests, guarded=guarded)
    assert campaign.prepare() == batch
    campaign.cancel_prepared(batch.batch_id, operator="synthetic-test")
    batch = campaign.prepare()
    assert roles(requests) == ["C-feedback"]
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    finish_synthetic_batch(campaign, batch)
    assert roles(requests) == ["C-feedback", "C-feedback"]
    assert len(campaign.store.all("feedback_results")) == 2
    assert campaign.state["considered_round"] != 2
    assert campaign.methods.paused(campaign.state)
    campaign.methods.resume(operator="synthetic-test", reason="Synthetic reviewed pause")
    campaign.consider_improvement()
    assert roles(requests) == ["C-feedback", "C-feedback", "M"]
    assert campaign.state["considered_round"] == 2
    batch = campaign.prepare()
    assert campaign.prepare() == batch
    assert campaign.store.usage()["llm_calls"]["committed"] == 3
    assert not campaign.store.all("method_proposal_failures")


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("fail_role,paused", [("C-feedback", True), ("C-feedback", False), ("M", False)])
def test_provider_pause_restarts_exact_stage_without_repeating_successful_c(
        tmp_path, monkeypatch, fail_role, paused, guarded):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=paused)
    requests = []
    attach_provider(campaign, monkeypatch, requests, fail_role=fail_role, guarded=guarded)
    with pytest.raises(ProviderPaused):
        campaign.prepare()
    if fail_role == "M":
        assert campaign.state["history"][-1]["analyst_feedback"]["summary"]
        assert len(campaign.store.all("feedback_results")) == 1
    else:
        assert "analyst_feedback" not in campaign.state["history"][-1]
        assert not campaign.store.all("feedback_results")
    first_requests = deepcopy(requests)
    assert campaign.state["considered_round"] != 1
    assert not campaign.store.all("method_proposal_failures")
    campaign = Campaign(Store(tmp_path))
    attach_provider(campaign, monkeypatch, requests, fail_role=fail_role, guarded=guarded)
    if fail_role == "M":
        monkeypatch.setattr(campaign.team.analyst, "feedback", lambda _: pytest.fail("C already committed"))
    with pytest.raises(ProviderPaused):
        campaign.prepare()
    assert requests == first_requests
    key = next(k for k, v in campaign.store.all("llm").items() if v["role"] == fail_role)
    authorize_retry(campaign.store, key, operator="synthetic-test", reason="Synthetic provider recovery")
    assert campaign.prepare().round_index == 1
    attempted = [payload for role, payload in requests if role == fail_role]
    assert attempted[0] == attempted[1]
    assert len(requests) == (2 if paused else 3)
    assert roles(requests).count("C-feedback") == (2 if fail_role == "C-feedback" else 1)
    assert campaign.store.usage()["llm_calls"]["committed"] == len(requests)
    assert not campaign.store.all("method_proposal_failures")
    assert campaign.methods.paused(campaign.state) is paused


@pytest.mark.parametrize("fail_role", ["C-feedback", "M"])
def test_unknown_completion_keeps_exact_request_and_never_charges_it_again(tmp_path, monkeypatch, fail_role):
    campaign = pending_feedback(tmp_path, monkeypatch)
    requests = []
    attach_provider(campaign, monkeypatch, requests, fail_role=fail_role, error=RuntimeError)
    with pytest.raises(RuntimeError, match="Synthetic unknown completion"):
        campaign.prepare()
    usage = campaign.store.usage()
    assert campaign.state["considered_round"] != 1
    call = next(v for v in campaign.store.all("llm").values() if v["role"] == fail_role)
    assert call["state"] == "started"
    if fail_role == "M":
        assert campaign.state["history"][-1]["analyst_feedback"]
    campaign = Campaign(Store(tmp_path))
    attach_provider(campaign, monkeypatch, requests, fail_role=fail_role)
    campaign.consider_improvement()
    # Preserve existing LLMError handling: the uncertain receipt is not retried,
    # no result is fabricated, and the failed decision remains in the audit.
    assert campaign.store.usage() == usage
    assert len(requests) == (1 if fail_role == "C-feedback" else 2)
    assert campaign.state["considered_round"] == 1
    assert len(campaign.store.all("method_proposal_failures")) == 1
    assert bool(campaign.store.all("feedback_results")) is (fail_role == "M")


def test_legacy_success_and_decision_markers_are_not_backfilled(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch)
    state = campaign.state
    state["history"][-1]["analyst_feedback"] = {"summary": "Actual prior synthetic C result"}
    campaign.store.put("campaign", "state", state)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    campaign.consider_improvement()
    assert roles(requests) == ["M"]
    assert not campaign.store.all("feedback_inputs")
    assert not campaign.store.all("feedback_results")
    # A previously considered legacy round without saved C is also left alone.
    state = campaign.state
    del state["history"][-1]["analyst_feedback"]
    campaign.store.put("campaign", "state", state)
    campaign.consider_improvement()
    assert roles(requests) == ["M"]
    assert "analyst_feedback" not in campaign.state["history"][-1]


def test_successful_provider_cache_recovers_a_missing_controller_checkpoint(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    # The provider succeeds, but an old/stopped controller has not saved C in state.
    campaign.team.analyst.feedback(campaign.view())
    assert not campaign.store.all("feedback_inputs")
    campaign.consider_improvement()
    assert roles(requests) == ["C-feedback", "M"]
    assert campaign.store.usage()["llm_calls"]["committed"] == 2
    assert len(campaign.store.all("feedback_results")) == 1
    assert any(e["kind"] == "llm_cache_hit" for e in campaign.store.events())


def test_checkpoint_commit_failure_recovers_from_same_provider_receipt(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    original = campaign.store.put

    def fail_state(namespace, key, value, **kwargs):
        if namespace == "campaign" and value["history"][-1].get("analyst_feedback"):
            raise RuntimeError("Synthetic checkpoint commit interruption")
        return original(namespace, key, value, **kwargs)

    monkeypatch.setattr(campaign.store, "put", fail_state)
    with pytest.raises(RuntimeError, match="checkpoint commit"):
        campaign.consider_improvement()
    assert not campaign.store.all("feedback_results")
    assert "analyst_feedback" not in campaign.state["history"][-1]
    assert not any(e["kind"] == "analyst_feedback_completed" for e in campaign.store.events())
    campaign = Campaign(Store(tmp_path))
    attach_provider(campaign, monkeypatch, requests)
    campaign.consider_improvement()
    assert roles(requests) == ["C-feedback"]
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert len(campaign.store.all("feedback_results")) == 1


def test_terminal_replay_does_not_backfill_feedback(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    for _ in range(3):
        batch = campaign.prepare()
        observations = finish_synthetic_batch(campaign, batch)
    assert campaign.state["status"] == "complete"
    assert roles(requests) == ["C-feedback", "C-feedback", "C-feedback"]
    assert "analyst_feedback" not in campaign.state["history"][-1]
    usage = campaign.store.usage()
    records = campaign.store.all("feedback_results")
    campaign = Campaign(Store(tmp_path))
    attach_provider(campaign, monkeypatch, requests)
    assert campaign.prepare() is None
    campaign.ingest(observations)
    campaign.consider_improvement()
    assert campaign.store.usage() == usage
    assert campaign.store.all("feedback_results") == records
    assert len(requests) == 3


def test_frozen_engine_is_checked_before_direct_feedback(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    from proteinrsi import governance
    source = deepcopy(governance._packaged_source())
    source["files"]["runtime.py"] += "\n# Synthetic mismatched engine\n"
    monkeypatch.setattr(governance, "_packaged_source", lambda: source)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    with pytest.raises(Conflict, match="silent migration"):
        campaign.consider_improvement()
    assert not requests
    assert not campaign.store.all("feedback_inputs")


@pytest.mark.parametrize("setting,value", [("model", "different-synthetic-model"),
    ("max_tokens", 8192), ("reasoning_effort", "high"), ("api_protocol", "responses")])
def test_unfinished_feedback_rejects_changed_provider(tmp_path, monkeypatch, setting, value):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests, fail_role="C-feedback")
    with pytest.raises(ProviderPaused):
        campaign.consider_improvement()
    setattr(campaign.team.llm, setting, value)
    with pytest.raises(Conflict, match="original method and provider"):
        campaign.consider_improvement()
    assert len(requests) == 1
    assert not campaign.store.all("feedback_results")


def test_checkpoint_is_immutable_auditable_and_worker_protected(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    campaign.consider_improvement()
    key, saved = next(iter(campaign.store.all("feedback_inputs").items()))
    result = campaign.store.get("feedback_results", key)
    assert result["input_digest"] == digest(saved)
    assert saved["provenance"]["method_snapshots"] == campaign.state["method_governance"]["active"]
    assert "analyst_feedback" not in saved["view"]["history"][-1]
    trace = read_trace(tmp_path)
    event = next(e for e in trace["events"] if e["kind"] == "analyst_feedback_completed")
    assert event["details"]["feedback_inputs"] == saved
    assert event["details"]["feedback_results"] == result
    for namespace in ("feedback_inputs", "feedback_results"):
        with pytest.raises(Conflict, match="immutable"):
            campaign.store.put(namespace, key, {}, immutable=True)
        with pytest.raises(PermissionError, match="Protected namespace"):
            dispatch(campaign.team, campaign.team.tools, campaign.view(), [],
                {"rpc": "put", "namespace": namespace, "key": key, "value": {}})
    state = campaign.state
    state["history"][-1]["analyst_feedback"]["summary"] = "Synthetic conflicting state"
    campaign.store.put("campaign", "state", state)
    with pytest.raises(Conflict, match="committed history"):
        campaign.consider_improvement()
    assert len(requests) == 1


def test_sponsored_feedback_keeps_charge_and_exact_operator_audit(tmp_path, monkeypatch):
    child_root = tmp_path / "child"
    campaign = pending_feedback(child_root, monkeypatch, paused=True)
    sponsor = Store(tmp_path / "sponsor")
    sponsor.configure_budget({key: value["limit"] for key, value in campaign.store.usage().items()})
    campaign = Campaign(SponsoredStore(child_root, sponsor, "synthetic/arm"))
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    campaign.consider_improvement()
    campaign = Campaign(SponsoredStore(child_root, sponsor, "synthetic/arm"))
    attach_provider(campaign, monkeypatch, requests)
    campaign.consider_improvement()
    assert roles(requests) == ["C-feedback"]
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert sponsor.usage()["llm_calls"]["committed"] == 1
    for namespace in ("feedback_inputs", "feedback_results"):
        key, record = next(iter(campaign.store.all(namespace).items()))
        assert sponsor.get("validation_" + namespace, "synthetic/arm/" + key) == {
            "branch": "synthetic/arm", **record}
    event = next(e for e in read_trace(sponsor.root)["events"]
                 if e["kind"] == "validation_event" and e["payload"]["kind"] == "analyst_feedback_completed")
    assert event["details"]["feedback_results"]["feedback"]["summary"]


@pytest.mark.parametrize("fail_role", ["C-feedback", "M"])
def test_guarded_pause_receipt_keeps_stage_checkpoint_without_new_worker_c(tmp_path, monkeypatch, fail_role):
    from proteinrsi.agents import FeedbackAnalysis, MetaResponse
    from proteinrsi.replay.broker import WorkerExecutionError

    campaign = pending_feedback(tmp_path, monkeypatch)
    campaign.team = GuardedTeam.from_team(campaign.team)
    campaign.meta_agent = GuardedMetaAgent(campaign.team)
    operations = []
    recovered = False

    def invoke(team, view, operation, **kwargs):
        operations.append(operation)
        if ("C-feedback" if operation == "feedback" else "M") == fail_role and not recovered:
            raise WorkerExecutionError("ProviderPaused", "Synthetic guarded pause receipt")
        return (FeedbackAnalysis(summary="Synthetic guarded feedback").model_dump()
                if operation == "feedback" else MetaResponse(reason="Synthetic no change").model_dump())

    monkeypatch.setattr("proteinrsi.replay.broker.invoke_worker", invoke)
    with pytest.raises(WorkerExecutionError, match="ProviderPaused"):
        campaign.consider_improvement()
    assert campaign.state["considered_round"] != 1
    assert not campaign.store.all("method_proposal_failures")
    campaign = Campaign(Store(tmp_path))
    campaign.team = GuardedTeam.from_team(campaign.team)
    campaign.meta_agent = GuardedMetaAgent(campaign.team)
    recovered = True
    campaign.consider_improvement()
    assert operations == (["feedback", "feedback", "meta"] if fail_role == "C-feedback" else
                           ["feedback", "meta", "meta"])
    assert campaign.state["considered_round"] == 1
    assert len(campaign.store.all("feedback_results")) == 1


def test_admin_disabled_meta_still_gets_c_without_method_proposal(campaign, monkeypatch):
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    finish_synthetic_batch(campaign, campaign.prepare())
    assert roles(requests) == ["C-feedback"]
    assert not campaign.view().meta.enabled
    assert campaign.state["considered_round"] == 1
    assert len(campaign.store.all("feedback_results")) == 1


def test_paused_c_uses_frozen_input_when_non_evidence_context_changes(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests, fail_role="C-feedback")
    with pytest.raises(ProviderPaused):
        campaign.consider_improvement()
    campaign.store.put("artifacts", "synthetic-later-artifact", {"note": "Synthetic later context"})
    key = next(iter(campaign.store.all("llm")))
    authorize_retry(campaign.store, key, operator="synthetic-test", reason="Synthetic retry")
    campaign.consider_improvement()
    assert requests[0] == requests[1]
    assert campaign.store.usage()["llm_calls"]["committed"] == 2
    saved = next(iter(campaign.store.all("feedback_inputs").values()))
    assert not saved["view"]["artifacts"]


@pytest.mark.parametrize("fail_role", ["C-feedback", "M"])
def test_definite_provider_failure_preserves_existing_decision_semantics(tmp_path, monkeypatch, fail_role):
    campaign = pending_feedback(tmp_path, monkeypatch)
    requests = []
    attach_provider(campaign, monkeypatch, requests, fail_role=fail_role, error=ValueError)
    campaign.consider_improvement()
    assert campaign.state["considered_round"] == 1
    assert campaign.state["method_governance"]["consecutive_failures"] == 1
    assert bool(campaign.store.all("feedback_results")) is (fail_role == "M")
    assert ("analyst_feedback" in campaign.state["history"][-1]) is (fail_role == "M")
    campaign.consider_improvement()
    assert roles(requests) == (["C-feedback"] if fail_role == "C-feedback" else ["C-feedback", "M"])
    assert campaign.store.usage()["llm_calls"]["committed"] == len(requests)


def test_local_preflight_cap_can_be_corrected_without_changing_c_request(tmp_path, monkeypatch):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    campaign.team.llm.max_request_bytes = 1
    with pytest.raises(ProviderPaused, match="preflight"):
        campaign.consider_improvement()
    assert not requests
    assert campaign.store.usage()["llm_calls"]["committed"] == 0
    rejected = next(iter(campaign.store.all("llm_preflights").values()))
    campaign.team.llm.max_request_bytes = rejected["input_preflight"]["request_bytes"]
    campaign.consider_improvement()
    assert requests[0][1] == rejected["request"]
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert list(campaign.store.all("llm")) == [rejected["request_key"]]


@pytest.mark.parametrize("field", ["model", "base_url"])
def test_checkpoint_redacts_misplaced_secret_without_losing_semantic_identity(tmp_path, monkeypatch, field):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests)
    secret = "SYNTHETIC_SECRET_ONE"
    llm = campaign.team.llm
    llm.api_key = secret
    value = secret if field == "model" else "https://example.invalid/" + secret
    setattr(llm, field, value)
    llm.max_request_bytes = 1
    with pytest.raises(ProviderPaused, match="preflight"):
        campaign.consider_improvement()
    key, saved = next(iter(campaign.store.all("feedback_inputs").items()))
    assert secret not in json.dumps(saved)
    display_field = "url" if field == "base_url" else field
    assert "[REDACTED]" in saved["provenance"][display_field]
    original_semantics = {**saved["provenance"], display_field: value}
    assert saved["provenance_digest"] == digest(original_semantics)
    assert secret not in json.dumps(read_trace(tmp_path)["records"])
    # Different semantic providers can have the same redacted display. Compare
    # their unredacted fingerprints rather than treating both as [REDACTED].
    changed_secret = "SYNTHETIC_SECRET_TWO"
    llm.api_key = changed_secret
    setattr(llm, field, value.replace(secret, changed_secret))
    with pytest.raises(Conflict, match="original method and provider"):
        campaign.consider_improvement()
    assert campaign.store.get("feedback_inputs", key) == saved
    assert campaign.store.usage()["llm_calls"]["committed"] == 0
    assert not requests


@pytest.mark.parametrize("pause", ["preflight", "retryable"])
def test_normal_credential_rotation_preserves_c_continuation(tmp_path, monkeypatch, pause):
    campaign = pending_feedback(tmp_path, monkeypatch, paused=True)
    requests = []
    attach_provider(campaign, monkeypatch, requests,
                    fail_role="C-feedback" if pause == "retryable" else None)
    campaign.team.llm.api_key = "SYNTHETIC_OLD_CREDENTIAL"
    if pause == "preflight":
        campaign.team.llm.max_request_bytes = 1
    with pytest.raises(ProviderPaused):
        campaign.consider_improvement()
    key, saved = next(iter(campaign.store.all("feedback_inputs").items()))
    assert "SYNTHETIC_OLD_CREDENTIAL" not in json.dumps(saved)
    assert saved["provenance_digest"] == digest(saved["provenance"])
    if pause == "preflight":
        rejected = next(iter(campaign.store.all("llm_preflights").values()))
        request_key = rejected["request_key"]
        original_payload = rejected["request"]
        campaign.team.llm.max_request_bytes = rejected["input_preflight"]["request_bytes"]
    else:
        request_key, call = next(iter(campaign.store.all("llm").items()))
        original_payload = call["request"]
        authorize_retry(campaign.store, request_key, operator="synthetic-test", reason="Synthetic retry")
    campaign.team.llm.api_key = "SYNTHETIC_ROTATED_CREDENTIAL"
    campaign.consider_improvement()
    assert requests[-1][1] == original_payload
    assert list(campaign.store.all("llm")) == [request_key]
    assert campaign.store.get("feedback_inputs", key) == saved
    assert campaign.store.usage()["llm_calls"]["committed"] == (1 if pause == "preflight" else 2)
    assert len(campaign.store.all("feedback_results")) == 1
