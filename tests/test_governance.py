"""Artificial governance/fault-injection tests, not protein efficacy evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from conftest import finish_round
from proteinrsi.cli import main
from proteinrsi.contracts import MetaPolicy, Patch, canonical, digest
from proteinrsi.evaluation import evaluate_meta
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, Store
from proteinrsi.trajectory import read_trace
from test_rsi import make_meta_cases, ScriptedOffspringTeam


def stage_workflow(campaign, **changes):
    base = campaign.view().workflow
    patch = Patch(target="workflow", base_version=base.version,
        changes=changes or {"strategy": "pairwise"}, task_kind=campaign.view().task.kind,
        hypothesis="Artificial check of an independently validated candidate")
    campaign.stage_patch(patch)
    return base, patch


def accept_workflow(campaign, oracle):
    finish_round(campaign, oracle)
    base, patch = stage_workflow(campaign)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    observations = oracle.measure(batch)
    arms = {s.sample_id: s.arm for s in batch.samples}
    for item in observations:
        item.value = 10 if arms[item.sample_id] == "challenger" else 0
    campaign.ingest(observations)
    return base, patch, batch, observations


def test_snapshots_are_full_immutable_and_parent_linked(campaign):
    base, patch = stage_workflow(campaign)
    record = campaign.store.get("method_candidates", patch.patch_id)
    assert record["parent_version"] == base.version
    assert record["evidence_version"] == campaign.view().evidence_version
    assert record["validation_plan"]["gate"] == campaign.state["gate"]
    for name in ("base_snapshot_ref", "candidate_snapshot_ref"):
        ref = record[name]
        snapshot = campaign.store.get("method_snapshots", ref)
        assert "method-"+digest(snapshot) == ref
        assert snapshot["configuration"]["prompt_bundle"]["templates"]
        assert snapshot["contracts"]["method"]["properties"]
        source = campaign.store.get("method_assets", snapshot["source_ref"])
        assert digest(source) == snapshot["source_ref"]
        assert "dataflow/protocol.py" in source["files"]
        assert "research/analysis.py" in source["files"]
        assert not any(p.startswith(("experiments/", ".env")) for p in source["files"])
        with pytest.raises(Conflict):
            campaign.store.put("method_snapshots", ref, {}, immutable=True)
    assert len(campaign.store.all("method_assets")) == 1
    assert campaign.view().workflow == base  # Staging does not publish anything.


def test_definite_failure_continues_baseline_and_keeps_charges(campaign, monkeypatch):
    base, patch = stage_workflow(campaign)
    original = campaign.team.run
    def run(view):
        if view.workflow.version != base.version:
            campaign.store.reserve("failed-candidate-call", "tool_calls", 1, {})
            campaign.store.settle("failed-candidate-call")
            raise ValueError("Candidate contract failed")
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    batch = campaign.prepare()
    assert batch.patch_id is None
    assert campaign.state["pending_patch"] is None
    assert all(s.workflow_version == base.version for s in batch.samples)
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "failed"
    assert campaign.store.usage()["tool_calls"]["committed"] == 1
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
    assert campaign.prepare() == batch
    assert campaign.state["method_governance"]["consecutive_failures"] == 1


@pytest.mark.parametrize("error_type,recovers", [("ValueError", True), ("RuntimeError", False)])
def test_complete_remote_failure_is_not_confused_with_transport_failure(campaign, monkeypatch, error_type, recovers):
    from proteinrsi.replay.broker import WorkerExecutionError
    base, patch = stage_workflow(campaign)
    original = campaign.team.run
    def run(view):
        if view.workflow.version != base.version:
            raise WorkerExecutionError(error_type, "synthetic failure receipt")
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    if recovers:
        assert campaign.prepare().patch_id is None
    else:
        with pytest.raises(WorkerExecutionError):
            campaign.prepare()
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == ("failed" if recovers else "blocked")


@pytest.mark.parametrize("error", [TimeoutError("unknown external completion"), RuntimeError("unknown bug")])
def test_unknown_completion_blocks_restart_without_recharging(campaign, monkeypatch, error):
    base, patch = stage_workflow(campaign)
    original = campaign.team.run
    calls = []
    def run(view):
        if view.workflow.version != base.version:
            calls.append(1)
            campaign.store.reserve("uncertain-call", "tool_calls", 1, {})
            campaign.store.settle("uncertain-call")
            raise error
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    with pytest.raises(type(error)):
        campaign.prepare()
    usage = campaign.store.usage()
    restarted = Campaign(Store(campaign.store.root))
    with pytest.raises(Conflict, match="uncertain"):
        restarted.prepare()
    assert len(calls) == 1 and restarted.store.usage() == usage
    assert restarted.store.get("method_candidate_states", patch.patch_id)["status"] == "blocked"
    with pytest.raises(Conflict, match="reconciled"):
        restarted.methods.abandon(patch.patch_id, operator="test", reason="reviewed")
    restarted.methods.abandon(patch.patch_id, operator="test", reason="External work reconciled",
                              acknowledge_uncertain=True)
    assert restarted.prepare().patch_id is None
    assert restarted.store.usage()["tool_calls"]["committed"] == 1


def test_started_call_receipt_overrides_a_value_error(campaign, monkeypatch):
    base, patch = stage_workflow(campaign)
    original = campaign.team.run
    def run(view):
        if view.workflow.version != base.version:
            campaign.store.put("tool_jobs", "in-flight", {"state": "started"})
            raise ValueError("Cannot decode an incomplete result")
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    with pytest.raises(ValueError):
        campaign.prepare()
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "blocked"


def test_unrelated_old_receipt_does_not_misclassify_new_failure(campaign, monkeypatch):
    campaign.store.put("tool_jobs", "historical", {"state": "started"})
    base, patch = stage_workflow(campaign)
    original = campaign.team.run
    def run(view):
        if view.workflow.version != base.version:
            raise ValueError("New invalid sequence, no external call")
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    assert campaign.prepare().patch_id is None
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "failed"


def test_rollback_changes_only_method_and_preserves_facts(campaign, oracle):
    base, patch, batch, observations = accept_workflow(campaign, oracle)
    before = deepcopy(campaign.state)
    usage = campaign.store.usage()
    measurements = campaign.store.all("measurements")
    batch_record = campaign.store.get("batches", batch.batch_id)
    bindings = campaign.store.get("batch_method_bindings", batch.batch_id)
    assert len(bindings["workflow_snapshots"]) == 2
    campaign.methods.rollback("workflow", base.version, operator="test", reason="Regression observed")
    assert campaign.view().workflow == base
    assert campaign.state["observations"] == before["observations"]
    assert campaign.state["history"] == before["history"]
    assert campaign.state["round_index"] == before["round_index"]
    assert campaign.store.usage() == usage
    assert campaign.store.all("measurements") == measurements
    assert campaign.store.get("batches", batch.batch_id) == batch_record
    assert campaign.store.get("batch_method_bindings", batch.batch_id) == bindings
    assert [x["action"] for x in campaign.methods.report()["switches"]] == ["adopt", "rollback"]
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "accepted"
    campaign.ingest(observations)  # Reimport does not undo the rollback or consume queries.
    assert campaign.view().workflow == base and campaign.store.usage() == usage
    next_batch = campaign.prepare()
    assert all(s.workflow_version == base.version for s in next_batch.samples)
    assert next_batch.evidence_version != batch.evidence_version


@pytest.mark.parametrize("prepared", [False, True])
def test_pending_candidates_and_batches_block_rollback(campaign, prepared):
    base, _ = stage_workflow(campaign)
    if prepared:
        campaign.prepare()
    with pytest.raises(Conflict, match="idle"):
        campaign.methods.rollback("workflow", base.version, operator="test", reason="review")


def test_unvalidated_snapshot_cannot_be_activated(campaign):
    _, patch = stage_workflow(campaign)
    ref = campaign.store.get("method_candidates", patch.patch_id)["candidate_snapshot_ref"]
    version = campaign.store.get("method_snapshots", ref)["version"]
    campaign.methods.abandon(patch.patch_id, operator="test", reason="Not validated")
    with pytest.raises(Conflict, match="previously active"):
        campaign.methods.rollback("workflow", version, operator="test", reason="Do not bypass validation")


def test_atomic_promotion_retains_imported_measurements_on_audit_failure(campaign, oracle, monkeypatch):
    finish_round(campaign, oracle)
    base, patch = stage_workflow(campaign)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    obs = oracle.measure(batch)
    arms = {s.sample_id: s.arm for s in batch.samples}
    for item in obs:
        item.value = 10 if arms[item.sample_id] == "challenger" else 0
    before_state, before_usage = campaign.state, campaign.store.usage()
    original = campaign.store.put
    def crash(namespace, *args, **kwargs):
        if namespace == "method_switches":
            raise OSError("Simulated storage failure")
        return original(namespace, *args, **kwargs)
    monkeypatch.setattr(campaign.store, "put", crash)
    with pytest.raises(OSError):
        campaign.ingest(obs)
    assert campaign.state == before_state
    assert campaign.view().workflow == base
    assert campaign.store.get("measurements", batch.batch_id)  # Actual facts were not erased.
    assert campaign.store.usage() == before_usage
    assert not campaign.methods.report()["switches"]
    assert not any(e["kind"] == "method_version_switched" for e in campaign.store.events())
    monkeypatch.setattr(campaign.store, "put", original)
    campaign.ingest(obs)
    assert campaign.view().workflow.strategy == "pairwise"
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "accepted"
    assert campaign.store.usage() == before_usage


def test_repeated_failures_pause_and_operator_resume_keeps_meta_disabled(campaign, monkeypatch):
    original = campaign.team.run
    def run(view):
        if view.workflow.exploration != 0.2:
            raise ValueError("A failed candidate")
        return original(view)
    monkeypatch.setattr(campaign.team, "run", run)
    for value in (0.3, 0.4, 0.5):
        stage_workflow(campaign, exploration=value)
        batch = campaign.prepare()
        campaign.cancel_prepared(batch.batch_id, operator="test")
    assert campaign.state["method_governance"]["paused"]
    with pytest.raises(Conflict, match="paused"):
        stage_workflow(campaign, exploration=0.6)
    def must_not_propose(*args):
        raise AssertionError("Paused M must not make a model call")
    monkeypatch.setattr(campaign.meta_agent, "propose", must_not_propose)
    campaign.consider_improvement()
    campaign.methods.resume(operator="test", reason="Reviewed failures")
    assert not campaign.methods.paused(campaign.state)
    assert not campaign.view().meta.enabled


def test_invalid_automatic_proposals_are_counted(campaign):
    for round_index in range(3):
        state = campaign.state
        state["round_index"] = round_index
        campaign.methods.rejected_proposal(state, ValueError("Invalid proposal"))
        campaign.methods.rejected_proposal(state, ValueError("Same failed attempt"))
    assert campaign.state["method_governance"]["consecutive_failures"] == 3
    assert campaign.methods.paused(campaign.state)


def test_deferrals_are_bounded_by_distinct_round_not_restarts(campaign, oracle):
    state = campaign.state
    state["gate"]["min_per_arm"] = 100
    campaign.store.put("campaign", "state", state)
    _, patch = stage_workflow(campaign)
    batch = campaign.prepare()
    assert campaign.state["pending_patch"]
    campaign.cancel_prepared(batch.batch_id, operator="test")
    batch = campaign.prepare()
    assert campaign.state["pending_patch"]  # Same round did not use the second deferral.
    campaign.approve(batch.batch_id, operator="test")
    campaign.ingest(oracle.measure(batch))
    assert campaign.prepare().patch_id is None
    assert campaign.state["pending_patch"] is None
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "inconclusive"


def test_cancelled_unsubmitted_trial_can_be_replanned(campaign, oracle):
    finish_round(campaign, oracle)
    _, patch = stage_workflow(campaign)
    batch = campaign.prepare()
    campaign.cancel_prepared(batch.batch_id, operator="test")
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "staged"
    replacement = campaign.prepare()
    assert replacement.batch_id != batch.batch_id and replacement.patch_id == patch.patch_id
    assert campaign.store.get("batches", batch.batch_id)


def test_only_small_patches_are_staged(campaign):
    with pytest.raises(ValueError, match="too many"):
        stage_workflow(campaign, strategy="pairwise", exploration=0.5, ridge_alpha=2, design_tool_rounds=5)
    assert campaign.state["pending_patch"] is None
    assert not campaign.store.all("method_candidates")


def test_source_change_blocks_execution_and_rollback(campaign, oracle, monkeypatch):
    base, _, _, _ = accept_workflow(campaign, oracle)
    import proteinrsi.governance as module
    changed = deepcopy(module._packaged_source())
    changed["files"]["runtime.py"] += "\n# modified executable\n"
    monkeypatch.setattr(module, "_packaged_source", lambda: changed)
    with pytest.raises(Conflict, match="Executable"):
        campaign.prepare()
    with pytest.raises(Conflict, match="Executable"):
        campaign.methods.rollback("workflow", base.version, operator="test", reason="Incompatible environment")


def test_legacy_campaign_is_readable_but_has_no_invented_rollback(campaign):
    with campaign.store.connect() as con:
        con.execute("DELETE FROM kv WHERE namespace='configuration' AND key='method_governance'")
    assert campaign.methods.report()["enabled"] is False
    with pytest.raises(Conflict, match="predates"):
        campaign.methods.rollback("workflow", campaign.view().workflow.version, operator="test", reason="review")


def test_offline_meta_failure_is_durable_and_keeps_spending(campaign):
    base = campaign.view().meta
    patch = Patch(target="meta", base_version=base.version, changes={"mode": "diagnostic"},
        task_kind=campaign.view().task.kind, hypothesis="An artificial failed validation")
    campaign.stage_patch(patch)
    class FailedTeam(ScriptedOffspringTeam):
        def run(self, view):
            raise ValueError("Invalid descendant")
    with pytest.raises(ValueError, match="Invalid descendant"):
        evaluate_meta(campaign, make_meta_cases(campaign), promote=True, team_factory=FailedTeam)
    assert campaign.store.usage()["experimental_wells"]["committed"] == 2
    assert campaign.state["pending_meta"] is None
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "failed"
    assert next(iter(campaign.store.all("meta_attempts").values()))["state"] == "failed"
    assert campaign.view().meta == base
    assert campaign.prepare().patch_id is None


def test_meta_adoption_and_rollback_share_the_same_audit(tmp_path, fixture_data):
    task, _ = fixture_data
    campaign = Campaign.initialize(str(tmp_path/"meta"), task, meta=MetaPolicy(min_observations=100))
    base = campaign.view().meta
    campaign.stage_patch(Patch(target="meta", base_version=base.version, changes={"min_observations": 2},
        task_kind=task.kind, hypothesis="Artificial descendant comparison"))
    report = evaluate_meta(campaign, make_meta_cases(campaign), promote=True, team_factory=ScriptedOffspringTeam)
    assert report["promoted"]
    usage = campaign.store.usage()
    campaign.methods.rollback("meta", base.version, operator="test", reason="Operator-selected rollback")
    assert campaign.view().meta == base
    assert campaign.store.usage() == usage
    assert [r["target"] for r in campaign.methods.report()["switches"]] == ["meta", "meta"]


def test_running_offline_evaluation_blocks_normal_planning(campaign):
    base = campaign.view().meta
    patch = Patch(target="meta", base_version=base.version, changes={"mode": "diagnostic"},
        task_kind=campaign.view().task.kind, hypothesis="Artificial interrupted evaluation")
    campaign.stage_patch(patch)
    campaign.methods.begin(campaign.state, patch)
    campaign.store.put("meta_attempts", "interrupted", {"state": "started", "patch_id": patch.patch_id})
    with pytest.raises(Conflict):
        Campaign(Store(campaign.store.root)).prepare()


def test_method_records_are_not_worker_capabilities(campaign):
    from proteinrsi.replay.broker import dispatch
    for namespace in ("method_snapshots", "method_assets", "method_activations", "method_switches", "method_candidates"):
        for operation in ("get", "put"):
            with pytest.raises(PermissionError):
                dispatch(campaign.team, campaign.team.tools, campaign.view(), [],
                    {"rpc": operation, "namespace": namespace, "key": "x", "value": {}})


def test_methods_cli_and_offline_trace(campaign, oracle, capsys, tmp_path):
    base, patch, _, _ = accept_workflow(campaign, oracle)
    args = ["--campaign", str(campaign.store.root)]
    main(["methods", "status", *args])
    status = json.loads(capsys.readouterr().out)
    assert status["candidate_states"][patch.patch_id]["status"] == "accepted"
    main(["methods", "rollback", *args, "--target", "workflow", "--version", base.version,
          "--operator", "test", "--reason", "Reviewed regression"])
    assert json.loads(capsys.readouterr().out)["switches"][-1]["action"] == "rollback"
    trace = read_trace(campaign.store.root)
    assert trace["records"]["method_candidates"][patch.patch_id]
    assert trace["records"]["method_switches"]
    absent = tmp_path/"does-not-exist"
    with pytest.raises(SystemExit):
        main(["methods", "status", "--campaign", str(absent)])
    assert not absent.exists()


def test_method_receipts_never_serialize_client_secrets(campaign):
    base = campaign.view().workflow
    batch = campaign.prepare()
    # A client identity is recorded, but never the client object or API key.
    state = campaign.state
    other = batch.model_copy(update={"batch_id": "test-receipt-only"})
    campaign.team.llm = SimpleNamespace(model="test", base_url="https://example.invalid", api_key="DO-NOT-STORE-THIS-KEY")
    campaign.methods.bind_batch(state, other)
    text = canonical(campaign.store.get("batch_method_bindings", other.batch_id))
    assert base.version in text
    assert "DO-NOT-STORE-THIS-KEY" not in text


def test_live_candidate_is_not_leaked_into_comparison_history(campaign):
    _, patch = stage_workflow(campaign)
    assert campaign.methods.visible_history() == []
    state = campaign.state
    campaign.methods.begin(state, patch)
    assert campaign.methods.visible_history() == []
    campaign.methods.finish(state, patch, "failed", detail={"reason": "Synthetic contract failure"})
    history = campaign.view().research_context["method_history"]
    assert history[0]["patch_id"] == patch.patch_id
    assert history[0]["status"] == "failed" and history[0]["transfer_validated"] is False


def test_active_definition_cannot_drift_from_snapshot(campaign):
    state = campaign.state
    state["workflow"]["exploration"] = 0.9
    campaign.store.put("campaign", "state", state)
    with pytest.raises(Conflict, match="differs from its snapshot"):
        campaign.prepare()
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


@pytest.mark.parametrize("promote", [False, True])
@pytest.mark.parametrize("namespace,status", [
    ("meta_attempts", "started"), ("meta_attempts", "blocked"),
    ("meta_online_attempts", "started"), ("meta_online_attempts", "planned"),
    ("meta_online_attempts", "blocked"),
])
def test_offline_meta_cannot_bypass_unfinished_candidate(campaign, namespace, status, promote):
    patch = Patch(target="meta", base_version=campaign.view().meta.version,
        changes={"mode": "diagnostic"}, task_kind=campaign.view().task.kind,
        hypothesis="Artificial interrupted candidate validation")
    campaign.stage_patch(patch)
    campaign.methods.begin(campaign.state, patch)
    # A different manifest produces a different evaluation ID, but it is still
    # the same candidate. Reopen the database to exercise restart protection.
    campaign.store.put(namespace, "another-manifest", {"state": status, "patch_id": patch.patch_id})
    before_state, before_usage = campaign.state, campaign.store.usage()
    before_events = campaign.store.events()
    restarted = Campaign(Store(campaign.store.root))
    def must_not_execute(store):
        pytest.fail("An unfinished candidate must be rejected before child execution")
    with pytest.raises(Conflict, match="uncertain|unfinished"):
        evaluate_meta(restarted, make_meta_cases(campaign), promote=promote, team_factory=must_not_execute)
    assert restarted.state == before_state
    assert restarted.store.usage() == before_usage
    assert restarted.store.events() == before_events
    assert restarted.store.get(namespace, "another-manifest")["state"] == status
    assert not restarted.store.all("meta_evaluations")
    assert not restarted.store.all("method_switches")


def test_offline_meta_checks_frozen_configuration_before_spending(campaign):
    campaign.stage_patch(Patch(target="meta", base_version=campaign.view().meta.version,
        changes={"mode": "diagnostic"}, task_kind=campaign.view().task.kind,
        hypothesis="Artificial snapshot drift check"))
    state = campaign.state
    state["workflow"]["exploration"] = 0.9
    campaign.store.put("campaign", "state", state)
    before = campaign.store.usage()
    with pytest.raises(Conflict, match="differs from its snapshot"):
        evaluate_meta(campaign, make_meta_cases(campaign), team_factory=ScriptedOffspringTeam)
    assert campaign.store.usage() == before
    assert not campaign.store.all("meta_attempts")


def test_completed_report_allows_new_manifest_validation(campaign):
    campaign.stage_patch(Patch(target="meta", base_version=campaign.view().meta.version,
        changes={"mode": "diagnostic"}, task_kind=campaign.view().task.kind,
        hypothesis="Artificial sequential report-only validations"))
    cases = make_meta_cases(campaign)[:2]
    evaluate_meta(campaign, cases, team_factory=ScriptedOffspringTeam)
    for case in cases:
        case.case_id += "-second"
    evaluate_meta(campaign, cases, team_factory=ScriptedOffspringTeam)
    attempts = campaign.store.all("meta_attempts")
    assert len(attempts) == 2
    assert all(attempt["state"] == "completed" for attempt in attempts.values())
    assert campaign.store.usage()["experimental_wells"]["committed"] == 32
    assert not campaign.store.all("method_switches")


def test_concurrent_offline_evaluators_claim_candidate_once(campaign):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    campaign.stage_patch(Patch(target="meta", base_version=campaign.view().meta.version,
        changes={"mode": "diagnostic"}, task_kind=campaign.view().task.kind,
        hypothesis="Artificial concurrent validation check"))
    entered, release = Event(), Event()
    class WaitingTeam(ScriptedOffspringTeam):
        def run(self, view):
            entered.set()
            assert release.wait(15), "Test did not release the first evaluator"
            return super().run(view)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(evaluate_meta, campaign, make_meta_cases(campaign), team_factory=WaitingTeam)
        try:
            assert entered.wait(15), "First evaluator never reached child execution"
            usage = campaign.store.usage()
            second_cases = make_meta_cases(campaign)
            for case in second_cases:
                case.case_id += "-concurrent"
            with pytest.raises(Conflict, match="uncertain|unfinished"):
                evaluate_meta(Campaign(Store(campaign.store.root)), second_cases,
                              promote=True, team_factory=ScriptedOffspringTeam)
            assert campaign.store.usage() == usage
            assert len(campaign.store.all("meta_attempts")) == 1
        finally:
            release.set()
        assert first.result(timeout=15)["promoted"] is False
    assert campaign.store.usage()["experimental_wells"]["committed"] == 32
