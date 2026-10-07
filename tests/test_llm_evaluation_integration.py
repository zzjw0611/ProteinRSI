"""Synthetic LLM-adjudication integration checks; no live or scientific evidence.

The HTTP fixture scripts E's decisions independently of the numeric measurements.
This deliberately catches a controller that substitutes a numerical gate for E.
"""

from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from proteinrsi.agents import Team
from proteinrsi.contracts import Candidate, GatePolicy, GateResult, MetaPolicy, Observation, Patch
from proteinrsi.evaluation import evaluate_meta
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, Store
from test_rsi import ScriptedOffspringTeam, make_meta_cases


PLAN = {
    "top_ns": [1, 2],
    "criteria": ["Consider measured best, top-N means, average, and evidence quality together."],
    "rationale": "Synthetic model-defined criteria for the bounded comparison.",
    "tradeoff_handling": "A lower best is permissible when the observed panel tradeoff is justified.",
    "missing_evidence_handling": "Evaluate missing observations explicitly without an automatic veto.",
}


class EvaluationHTTP:
    """Actual JSONLLM transport/caching, with explicitly artificial E responses."""

    def __init__(self, store, *, decision="accepted", pause_verdict=False, invalid=None, plan=None):
        self.store = store
        self.decision = decision
        self.pause_verdict = pause_verdict
        self.invalid = invalid
        self.plan = deepcopy(plan or PLAN)
        self.requests = {"plan": [], "verdict": []}
        self.contexts = {"plan": [], "verdict": []}
        self.inspect = None

    def __call__(self, request):
        # A remote call inside SQLite's publication transaction can roll back
        # paid receipts or deadlock another controller. Check at the actual I/O.
        assert getattr(self.store._transaction, "connection", None) is None
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        schema = json.loads(system.split("matching this schema:\n", 1)[1])
        phase = "verdict" if "supporting_evidence_refs" in schema["properties"] else "plan"
        assert phase == "verdict" or "criteria" in schema["properties"], system
        context = json.loads(payload["messages"][1]["content"])
        self.requests[phase].append(payload)
        self.contexts[phase].append(context)
        if self.inspect:
            self.inspect(phase, context)
        if phase == "verdict" and self.pause_verdict and len(self.requests[phase]) == 1:
            return httpx.Response(503)
        if phase == "plan":
            value = deepcopy(self.plan)
        else:
            value = {
                "plan_ref": context["plan_ref"],
                "decision": self.decision,
                "reason": "Synthetic E decision: evaluated the frozen criteria and the disclosed tradeoff.",
                "supporting_evidence_refs": list(context["evidence"]),
                "tradeoff_label": "Synthetic model judgment, not controller arithmetic",
            }
        if self.invalid:
            value = self.invalid(phase, value)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 11, "total_tokens": 28},
        })

    def team(self, store):
        self.store = store
        llm = JSONLLM(store, model="synthetic-evaluator", base_url="https://example.invalid/v1",
                      api_key="not-a-real-key", max_attempts=1,
                      transport=httpx.MockTransport(self))
        return Team(store, llm)


def make_campaign(campaign, monkeypatch, *, target="workflow", direction="maximize",
                  source="synthetic", llm_limit=None, **provider):
    task = campaign.view().task.model_copy(update={
        "batch_size": 6, "controls_per_batch": 0, "direction": direction,
        "feedback_source": source,
    })
    if llm_limit is not None:
        task = task.model_copy(update={"budget": task.budget.model_copy(update={"llm_calls": llm_limit})})
    campaign = Campaign.initialize(str(campaign.store.root / "llm-evaluation"), task,
        workflow=campaign.view().workflow, meta=MetaPolicy(min_observations=100),
        gate=GatePolicy(criterion="llm_adjudicated_v1"))
    transport = EvaluationHTTP(campaign.store, **provider)
    campaign.team = transport.team(campaign.store)
    # This file tests evaluation, not unrelated post-feedback C and M calls.
    monkeypatch.setattr(campaign, "consider_improvement", lambda: None)
    runs = []
    original = campaign.view().workflow.version
    choices = [s for s in task.candidates if s != task.reference_sequence]

    def run(view):
        runs.append(view.workflow.version)
        selected = choices[:3] if view.workflow.version == original else choices[3:6]
        return [Candidate(sequence=s) for s in selected]

    monkeypatch.setattr(campaign.team, "run", run)
    if target == "meta":
        state = campaign.state
        state["observations"] = [o.model_dump(mode="json")
                                  for o in make_meta_cases(campaign)[0].initial]
        campaign.store.put("campaign", "state", state)
    view = campaign.view()
    base = view.workflow.version if target == "workflow" else view.meta.version
    patch = Patch(target=target, base_version=base,
        changes={"strategy": "pairwise"} if target == "workflow" else {"min_observations": 2},
        task_kind=task.kind, hypothesis="Synthetic LLM evaluation integration only")
    campaign.stage_patch(patch)
    return campaign, transport, runs, base, patch


def measurements(campaign, batch, old, new):
    task = campaign.view().task
    values = {"baseline": iter(old), "challenger": iter(new)}
    result = []
    for sample in batch.samples:
        value = next(values[sample.arm])
        result.append(Observation(sample_id=sample.sample_id, sequence=sample.candidate.sequence,
            value=value, qc="unavailable" if value is None else "valid", metric=task.metric,
            unit=task.unit, source=task.feedback_source, batch_id=batch.batch_id,
            assay_protocol=task.assay_protocol))
    return result


def trial_facts(transport):
    evidence = transport.contexts["verdict"][-1]["evidence"]
    return next(value for ref, value in evidence.items() if ref.startswith("trial_metrics/"))


@pytest.mark.parametrize("field,value", [
    ("min_per_arm", 4), ("min_effect", 0), ("confidence", 0.95),
    ("bootstrap_samples", 2000), ("max_qc_failure_fraction", 0.25),
    ("top_ns", [5, 10]), ("absolute_tolerances", {}), ("improvement_margins", {}),
])
def test_llm_policy_refuses_legacy_acceptance_knobs(field, value):
    with pytest.raises(ValidationError):
        GatePolicy(criterion="llm_adjudicated_v1", **{field: value})
    policy = GatePolicy(criterion="llm_adjudicated_v1")
    assert policy.model_dump() == {"criterion": "llm_adjudicated_v1"}
    assert GatePolicy.model_validate(policy.model_dump()) == policy


@pytest.mark.parametrize("direction,old,new,decision", [
    ("maximize", [0, 0, 10], [5, 5, 5], "accepted"),
    ("maximize", [0, 1, 2], [1, 2, 3], "rejected"),
    ("maximize", [0, 0, 0], [3, 3, 3], "inconclusive"),
    ("minimize", [4, 5, 6], [1, 2, 3], "accepted"),
])
def test_workflow_adoption_follows_e_not_a_numeric_gate(
        campaign, monkeypatch, direction, old, new, decision):
    campaign, transport, runs, base, patch = make_campaign(
        campaign, monkeypatch, direction=direction, decision=decision)
    batch = campaign.prepare()
    assert batch.patch_id == patch.patch_id
    trial = campaign.store.get("trials", batch.batch_id)
    plans = deepcopy(campaign.store.all("evaluation_plans"))
    assert len(plans) == 1 and trial["evaluation_plan_ref"].startswith("evaluation_plans/")
    assert not campaign.store.all("measurements")
    assert not transport.requests["verdict"]
    campaign.approve(batch.batch_id, operator="synthetic-test")
    rows = measurements(campaign, batch, old, new)
    campaign.ingest(rows)
    assert (campaign.view().workflow.version != base) == (decision == "accepted")
    assert campaign.store.get("trial_results", batch.batch_id)["decision"] == decision
    facts = trial_facts(transport)
    for arm, values in (("baseline", old), ("challenger", new)):
        assert facts["arms"][arm]["metrics"]["avg"] == pytest.approx(sum(values) / len(values))
        best = min(values) if direction == "minimize" else max(values)
        assert facts["arms"][arm]["metrics"]["best"] == best
        assert facts["arms"][arm]["maximum"] == max(values)
    if old == [0, 0, 10]:
        assert facts["metrics"]["best"]["delta"] < 0 < facts["metrics"]["avg"]["delta"]
    if direction == "minimize":
        assert facts["metrics"]["best"]["delta"] > 0
    before = campaign.store.usage()
    campaign.ingest(rows)
    assert campaign.store.usage() == before
    assert campaign.store.all("evaluation_plans") == plans
    assert len(transport.requests["plan"]) == len(transport.requests["verdict"]) == 1
    assert len(runs) == 2


@pytest.mark.parametrize("values,valid", [([None, None, 10], 1), ([None, None, None], 0)])
def test_missing_coverage_is_disclosed_to_e_without_automatic_veto(campaign, monkeypatch, values, valid):
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, source="measured_replay")
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    campaign.ingest(measurements(campaign, batch, [0, 0, 0], values))
    facts = trial_facts(transport)
    arm = facts["arms"]["challenger"]
    assert arm["denominators"]["submitted"] == 3
    assert arm["denominators"]["unavailable"] == 3 - valid
    assert arm["denominators"]["unique_valid"] == valid
    assert arm["metrics"]["top2mean"] is None
    assert arm["top_n"]["top2mean"] == {"required": 2, "effective": valid, "complete": False}
    assert campaign.view().workflow.version != base
    assert campaign.store.get("trial_results", batch.batch_id)["decision"] == "accepted"


@pytest.mark.parametrize("target", ["workflow", "meta"])
def test_measured_trial_provider_pause_resumes_without_reexecuting_arms(
        campaign, monkeypatch, target):
    campaign, transport, runs, base, patch = make_campaign(
        campaign, monkeypatch, target=target, pause_verdict=True)
    if target == "meta":
        class CountingTeam(ScriptedOffspringTeam):
            def run(self, view):
                runs.append(view.workflow.version)
                return super().run(view)

        monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                            lambda campaign, store: CountingTeam(store))
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    batch = campaign.prepare()
    assert batch.patch_id == patch.patch_id
    plans = deepcopy(campaign.store.all("evaluation_plans"))
    campaign.approve(batch.batch_id, operator="synthetic-test")
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    with pytest.raises(ProviderPaused):
        campaign.ingest(rows)
    # Read via another connection to prove these facts survive a process restart.
    reopened = Store(campaign.store.root)
    saved_rows = reopened.get("measurements", batch.batch_id)
    evidence = reopened.all("evaluation_evidence")
    assert len(saved_rows) == len(batch.samples) and evidence
    assert reopened.get("campaign", "state")["status"] == "awaiting_results"
    assert not reopened.all("trial_results")
    assert not reopened.all("evaluation_verdicts")
    active = campaign.view().workflow.version if target == "workflow" else campaign.view().meta.version
    assert active == base
    usage = reopened.usage()
    assert usage["experimental_wells"]["committed"] == len(batch.samples)
    assert usage["llm_calls"]["committed"] == 2
    assert len(runs) == 2
    resumed = Campaign(reopened, transport.team(reopened))
    monkeypatch.setattr(resumed, "consider_improvement", lambda: None)
    monkeypatch.setattr(resumed.team, "run", lambda _: pytest.fail("Measured arms must not run again"))
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            resumed.ingest(rows)
    assert len(transport.requests["verdict"]) == 1
    assert reopened.usage() == usage
    key = next(key for key, value in reopened.all("llm").items() if value["state"] == "failed")
    failed = deepcopy(reopened.get("llm", key))
    authorize_retry(reopened, key, operator="synthetic-test", reason="Synthetic provider restored")
    resumed.ingest(rows)
    assert reopened.get("llm_attempts", key + "/attempt-1") == failed
    assert transport.requests["verdict"][0] == transport.requests["verdict"][1]
    assert reopened.all("evaluation_plans") == plans
    assert reopened.all("evaluation_evidence") == evidence
    assert reopened.get("measurements", batch.batch_id) == saved_rows
    assert reopened.usage()["experimental_wells"] == usage["experimental_wells"]
    assert reopened.usage()["llm_calls"]["committed"] == 3
    assert len(runs) == 2 and len(transport.requests["plan"]) == 1
    assert len(resumed.state["observations"]) == len(rows) + (2 if target == "meta" else 0)
    assert resumed.state["pending_meta" if target == "meta" else "pending_patch"] is None
    if target == "meta":
        assert resumed.view().meta.version != base
        report = next(iter(reopened.all("meta_evaluations").values()))
        assert report["scope"] == "current_task_only" and report["transfer_validated"] is False
    else:
        assert resumed.view().workflow.version != base


@pytest.mark.parametrize("invalid", ["wrong_plan", "wrong_evidence", "missing_refs", "missing_decision"])
def test_invalid_e_verdict_pauses_without_adoption_or_numeric_fallback(campaign, monkeypatch, invalid):
    from proteinrsi.llm_evaluation import MAX_RESPONSE_REPAIRS

    def mutate(phase, value):
        if phase == "plan":
            return value
        if invalid == "wrong_plan":
            value["plan_ref"] = "evaluation_plans/" + "0" * 64
        elif invalid == "wrong_evidence":
            value["supporting_evidence_refs"] = ["trial_metrics/an-unrelated-batch"]
        elif invalid == "missing_refs":
            value.pop("supporting_evidence_refs")
        else:
            value.pop("decision")
        return value

    campaign, transport, _, base, patch = make_campaign(campaign, monkeypatch, invalid=mutate)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    rows = measurements(campaign, batch, [0, 0, 0], [10, 10, 10])
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            campaign.ingest(rows)
        assert len(transport.requests["verdict"]) == MAX_RESPONSE_REPAIRS + 1
    assert campaign.view().workflow.version == base
    assert campaign.state["pending_patch"]["base_version"] == patch.base_version
    assert campaign.state["pending_batch"] == batch.batch_id
    assert campaign.store.get("measurements", batch.batch_id)
    assert not campaign.store.all("evaluation_verdicts")
    assert not campaign.store.all("trial_results")
    assert campaign.store.usage()["llm_calls"]["committed"] == MAX_RESPONSE_REPAIRS + 2
    original = transport.contexts["verdict"][0]
    for context in transport.contexts["verdict"][1:]:
        assert {key: value for key, value in context.items() if key != "response_repair"} == original


def test_format_repair_keeps_original_plan_evidence_and_adopts_valid_model_answer(campaign, monkeypatch):
    verdict_calls = []

    def omit_first_reference(phase, value):
        if phase == "verdict":
            verdict_calls.append(value.copy())
            if len(verdict_calls) == 1:
                value.pop("supporting_evidence_refs")
        return value

    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, invalid=omit_first_reference)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    campaign.ingest(rows)
    assert campaign.view().workflow.version != base
    assert len(transport.requests["plan"]) == 1 and len(transport.requests["verdict"]) == 2
    original, repaired = transport.contexts["verdict"]
    assert {key: value for key, value in repaired.items() if key != "response_repair"} == original
    assert campaign.store.usage()["llm_calls"]["committed"] == 3
    before = campaign.store.usage()
    campaign.ingest(rows)
    assert campaign.store.usage() == before


def test_model_selected_top_n_above_capacity_is_missing_evidence_not_a_veto(campaign, monkeypatch):
    plan = {**PLAN, "top_ns": [1, 5]}
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, plan=plan)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    campaign.ingest(measurements(campaign, batch, [0, 0, 10], [5, 5, 5]))
    assert campaign.view().workflow.version != base
    for arm in trial_facts(transport)["arms"].values():
        assert arm["metrics"]["top5mean"] is None
        assert arm["top_n"]["top5mean"] == {"required": 5, "effective": 3, "complete": False}
    assert len(transport.requests["plan"]) == len(transport.requests["verdict"]) == 1


@pytest.mark.parametrize("decision", ["accepted", "rejected", "inconclusive"])
def test_online_meta_adoption_honors_e_even_with_identical_descendants(campaign, monkeypatch, decision):
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, target="meta", decision=decision)
    # With no prior observations neither improver creates a new descendant. E,
    # not a deterministic "identical descendant" efficacy rule, judges the trial.
    state = campaign.state
    state["observations"] = []
    campaign.store.put("campaign", "state", state)
    monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                        lambda campaign, store: ScriptedOffspringTeam(store))
    batch = campaign.prepare()
    assert batch.patch_id is not None
    trial = campaign.store.get("trials", batch.batch_id)
    assert trial["descendants"]["baseline"] == trial["descendants"]["challenger"]
    campaign.approve(batch.batch_id, operator="synthetic-test")
    campaign.ingest(measurements(campaign, batch, [0, 1, 2], [3, 4, 5]))
    assert (campaign.view().meta.version != base) == (decision == "accepted")
    report = next(iter(campaign.store.all("meta_evaluations").values()))
    assert report["result"]["decision"] == decision
    assert report["transfer_validated"] is False
    assert len(transport.requests["plan"]) == len(transport.requests["verdict"]) == 1


def test_missing_frozen_trial_plan_cannot_be_recreated_after_measurement(campaign, monkeypatch):
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    trial = campaign.store.get("trials", batch.batch_id)
    trial.pop("evaluation_plan_ref")
    campaign.store.put("trials", batch.batch_id, trial)
    with pytest.raises(Conflict, match="premeasurement"):
        campaign.ingest(measurements(campaign, batch, [0, 0, 0], [10, 10, 10]))
    assert campaign.view().workflow.version == base
    assert campaign.store.get("measurements", batch.batch_id)
    assert not campaign.store.all("trial_results")
    assert len(transport.requests["plan"]) == 1 and not transport.requests["verdict"]


@pytest.mark.parametrize("details", [None, {}, {
    "criterion": "llm_adjudicated_v1",
    "evaluation_plan_ref": "evaluation_plans/" + "0" * 64,
    "evaluation_verdict_ref": "evaluation_verdicts/" + "1" * 64,
}])
def test_fabricated_controller_gate_cannot_adopt_a_method(campaign, monkeypatch, details):
    from proteinrsi.improvement import apply_patch

    campaign, transport, _, base, patch = make_campaign(campaign, monkeypatch)
    forged = GateResult(decision="accepted", reason="Synthetic fabricated arithmetic verdict",
                        n_baseline=100, n_challenger=100, details=details)
    state_before = campaign.state
    child = apply_patch(campaign.view().workflow, patch)
    with pytest.raises(Conflict):
        campaign.methods.complete(campaign.state, patch, forged, child,
                                  evaluation_ref="trial_results/fabricated")
    assert campaign.state == state_before
    assert campaign.view().workflow.version == base
    assert not transport.requests["plan"] and not transport.requests["verdict"]


def test_no_llm_does_not_synthesize_evaluation_plan(campaign, monkeypatch):
    campaign, _, _, base, _ = make_campaign(campaign, monkeypatch)
    campaign.team.llm = None
    with pytest.raises(ProviderPaused):
        campaign.prepare()
    assert campaign.view().workflow.version == base
    assert campaign.state["pending_patch"]
    assert not campaign.store.all("evaluation_plans")
    assert not campaign.store.all("batches")
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


def test_workflow_approval_requires_e_headroom_before_settling_measurement(campaign, monkeypatch):
    campaign, transport, _, _, _ = make_campaign(campaign, monkeypatch, llm_limit=3)
    batch = campaign.prepare()
    before = campaign.store.usage()
    assert before["llm_calls"]["committed"] == 1
    assert before["experimental_wells"]["reserved"] == len(batch.samples)
    with pytest.raises(ProviderPaused):
        campaign.approve(batch.batch_id, operator="synthetic-test")
    assert campaign.store.usage() == before
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
    assert campaign.state["status"] == "awaiting_approval"
    assert campaign.state["pending_batch"] == batch.batch_id
    assert len(transport.requests["plan"]) == 1 and not transport.requests["verdict"]


def test_online_meta_defers_when_descendants_would_consume_e_headroom(campaign, monkeypatch):
    campaign, _, _, _, _ = make_campaign(campaign, monkeypatch, target="meta", llm_limit=6)
    monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                        lambda *args: pytest.fail("No descendant can fit alongside bounded E headroom"))
    batch = campaign.prepare()
    assert batch.patch_id is None
    assert campaign.state["pending_meta"]
    assert not campaign.store.all("meta_online_attempts")
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


def test_online_meta_preserves_call_budget_for_verdict_and_bounded_repairs(campaign, monkeypatch):
    malformed = []

    def require_two_repairs(phase, value):
        if phase == "verdict":
            malformed.append(1)
            if len(malformed) < 3:
                value.pop("supporting_evidence_refs")
        return value

    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, target="meta",
        llm_limit=8, invalid=require_two_repairs)
    branch_limits = []

    class BudgetedTeam(ScriptedOffspringTeam):
        def run(self, view):
            branch_limits.append(self.store.usage()["llm_calls"]["limit"])
            # Exercise real sponsored JSONLLM accounting. These are synthetic
            # computation receipts, separate from E's scientific decision.
            llm = JSONLLM(self.store, model="synthetic-descendant",
                base_url="https://example.invalid/v1", api_key="not-a-real-key", max_attempts=1,
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
                    "choices": [{"message": {"content": '{"ok": true}'}}]})))
            for index in range(2):
                llm.complete("synthetic-descendant", "Synthetic budget accounting call",
                             {"index": index}, {"type": "object"})
            return super().run(view)

    monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                        lambda campaign, store: BudgetedTeam(store))
    batch = campaign.prepare()
    assert batch.patch_id is not None and branch_limits == [2, 2]
    assert campaign.store.remaining("llm_calls") == 3
    campaign.approve(batch.batch_id, operator="synthetic-test")
    campaign.ingest(measurements(campaign, batch, [0, 0, 10], [5, 5, 5]))
    assert campaign.view().meta.version != base
    assert campaign.store.usage()["llm_calls"]["committed"] == 8
    assert campaign.store.remaining("llm_calls") == 0
    assert len(transport.requests["plan"]) == 1 and len(transport.requests["verdict"]) == 3


@pytest.mark.parametrize("target", ["workflow", "meta"])
def test_adoption_commit_failure_reuses_paid_verdict_and_measurements(campaign, monkeypatch, target):
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, target=target)
    if target == "meta":
        monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                            lambda campaign, store: ScriptedOffspringTeam(store))
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])

    def fail_commit(*args, **kwargs):
        raise RuntimeError("Synthetic interrupted adoption transaction")

    monkeypatch.setattr(campaign.methods, "complete", fail_commit)
    with pytest.raises(RuntimeError, match="interrupted adoption"):
        campaign.ingest(rows)
    reopened = Store(campaign.store.root)
    assert reopened.get("measurements", batch.batch_id)
    assert reopened.get("trial_results", batch.batch_id)["decision"] == "accepted"
    verdicts = reopened.all("evaluation_verdicts")
    assert len(verdicts) == 1
    assert campaign.state["pending_batch"] == batch.batch_id
    assert len(campaign.state["observations"]) == (2 if target == "meta" else 0)
    usage = reopened.usage()
    resumed = Campaign(reopened, transport.team(reopened))
    monkeypatch.setattr(resumed, "consider_improvement", lambda: None)
    monkeypatch.setattr(resumed.team.llm, "complete",
                        lambda *args: pytest.fail("Completed E verdict must not be requested again"))
    resumed.ingest(rows)
    assert reopened.all("evaluation_verdicts") == verdicts
    assert reopened.usage() == usage
    assert resumed.state["pending_batch"] is None
    assert len(resumed.state["observations"]) == len(rows) + (2 if target == "meta" else 0)
    active = resumed.view().meta.version if target == "meta" else resumed.view().workflow.version
    assert active != base


def test_offline_meta_pause_preserves_arm_results_and_scoped_evidence(campaign, monkeypatch):
    campaign, transport, _, base, patch = make_campaign(
        campaign, monkeypatch, target="meta", pause_verdict=True)
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    cases = make_meta_cases(campaign)
    # This unrevealed value must never reach E or the research teams. It is not
    # selected by either scripted arm, and is not a global-extremum benchmark.
    sentinel = 987654321.125
    for case in cases:
        case.labels[case.task.candidates[6]] = sentinel
        case.group_id = "same-independent-group"
    runs, views = [], []

    class CountingTeam(ScriptedOffspringTeam):
        def run(self, view):
            runs.append((str(self.store.root), view.workflow.version))
            views.append(view.model_dump(mode="json"))
            assert campaign.store.all("evaluation_plans"), "Criteria must precede arm execution"
            return super().run(view)

    def inspect(phase, context):
        assert str(sentinel) not in json.dumps(context)
        if phase == "plan":
            assert not campaign.store.all("meta_arm_results")
            assert not campaign.store.all("evaluation_evidence")
        else:
            assert len(Store(campaign.store.root).all("meta_arm_results")) == 2 * len(cases)

    transport.inspect = inspect
    with pytest.raises(ProviderPaused):
        evaluate_meta(campaign, cases, promote=True, team_factory=CountingTeam)
    reopened = Store(campaign.store.root)
    evaluation_id, attempt = next(iter(reopened.all("meta_attempts").items()))
    assert attempt["state"] == "paused_provider" and attempt["patch_id"] == patch.patch_id
    assert campaign.view().meta.version == base
    assert campaign.state["pending_meta"] and not reopened.all("meta_evaluations")
    plans = reopened.all("evaluation_plans")
    arms = reopened.all("meta_arm_results")
    evidence = reopened.all("evaluation_evidence")
    usage = reopened.usage()
    assert len(arms) == len(runs) == 2 * len(cases)
    assert usage["experimental_wells"]["committed"] == sum(
        2 * (len(c.initial) + c.query_budget) for c in cases)
    assert usage["llm_calls"]["committed"] == 2
    assert str(sentinel) not in json.dumps(views)
    assert str(sentinel) not in json.dumps(evidence)
    resumed = Campaign(reopened, transport.team(reopened))

    def no_more_arms(store):
        pytest.fail("Resume must load durable offline arm results, not reconstruct a team")

    with pytest.raises(ProviderPaused):
        evaluate_meta(resumed, cases, promote=True, team_factory=no_more_arms)
    assert reopened.usage() == usage and len(transport.requests["verdict"]) == 1
    # A retry cannot switch the manifest or turn the existing promotion into a
    # report-only attempt under a fresh budget or different evidence.
    with pytest.raises(Conflict):
        evaluate_meta(resumed, cases, promote=False, team_factory=no_more_arms)
    changed = deepcopy(cases)
    changed[0].labels[changed[0].task.candidates[6]] = sentinel + 1
    with pytest.raises(Conflict):
        evaluate_meta(resumed, changed, promote=True, team_factory=no_more_arms)
    key = next(k for k, value in reopened.all("llm").items() if value["state"] == "failed")
    failed = deepcopy(reopened.get("llm", key))
    authorize_retry(reopened, key, operator="synthetic-test", reason="Synthetic provider restored")
    report = evaluate_meta(resumed, cases, promote=True, team_factory=no_more_arms)
    assert report["promoted"] and resumed.view().meta.version != base
    assert report["gate"]["n_baseline"] == report["gate"]["n_challenger"] == 1
    assert report["metric_facts"]["n_cases"] == len(cases)
    assert report["metric_facts"]["n_groups"] == 1
    assert reopened.get("meta_attempts", evaluation_id)["state"] == "completed"
    assert reopened.get("llm_attempts", key + "/attempt-1") == failed
    assert reopened.all("meta_arm_results") == arms
    assert reopened.all("evaluation_plans") == plans
    assert reopened.all("evaluation_evidence") == evidence
    assert reopened.usage()["experimental_wells"] == usage["experimental_wells"]
    assert reopened.usage()["llm_calls"]["committed"] == 3
    assert transport.requests["verdict"][0] == transport.requests["verdict"][1]
    after = reopened.usage()
    assert evaluate_meta(resumed, cases, promote=True, team_factory=no_more_arms) == report
    assert reopened.usage() == after
    assert len(transport.requests["plan"]) == 1 and len(transport.requests["verdict"]) == 2


@pytest.mark.parametrize("missing,decision", [(False, "rejected"), (True, "accepted")])
def test_offline_meta_uses_model_decision_for_dominance_and_missing_data(
        campaign, monkeypatch, missing, decision):
    campaign, transport, _, base, _ = make_campaign(
        campaign, monkeypatch, target="meta", decision=decision)
    cases = make_meta_cases(campaign)
    if missing:
        del cases[0].labels[cases[0].task.candidates[-1]]
    report = evaluate_meta(campaign, cases, promote=True, team_factory=ScriptedOffspringTeam)
    facts = report["metric_facts"]
    assert report["gate"]["decision"] == decision
    assert report["promoted"] == (decision == "accepted")
    assert (campaign.view().meta.version != base) == (decision == "accepted")
    if missing:
        partial = facts["cases"][0]["challenger"]["metric_summary"]
        assert partial["denominators"]["unavailable"] == 1
        assert partial["metrics"]["top2mean"] is None
        assert facts["arms"]["challenger"]["top2mean"] is None
    else:
        assert all(facts["arms"]["challenger"][key] > value
                   for key, value in facts["arms"]["baseline"].items())
    assert len(transport.requests["plan"]) == len(transport.requests["verdict"]) == 1


def test_offline_adoption_commit_failure_does_not_recharge_or_rejudge(campaign, monkeypatch):
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, target="meta")
    cases = make_meta_cases(campaign)

    def fail_commit(*args, **kwargs):
        raise RuntimeError("Synthetic interrupted offline adoption transaction")

    monkeypatch.setattr(campaign.methods, "complete", fail_commit)
    with pytest.raises(RuntimeError, match="interrupted offline adoption"):
        evaluate_meta(campaign, cases, promote=True, team_factory=ScriptedOffspringTeam)
    reopened = Store(campaign.store.root)
    assert campaign.view().meta.version == base and campaign.state["pending_meta"]
    assert not reopened.all("meta_evaluations")
    assert next(iter(reopened.all("meta_attempts").values()))["state"] == "measurements_committed"
    verdicts, evidence = reopened.all("evaluation_verdicts"), reopened.all("evaluation_evidence")
    arms, usage = reopened.all("meta_arm_results"), reopened.usage()
    assert len(verdicts) == 1 and len(arms) == 2 * len(cases)
    resumed = Campaign(reopened, transport.team(reopened))
    monkeypatch.setattr(resumed.team.llm, "complete",
                        lambda *args: pytest.fail("Already paid offline E verdict must be reused"))

    def no_more_arms(store):
        pytest.fail("Completed arm execution must not be repeated after adoption interruption")

    report = evaluate_meta(resumed, cases, promote=True, team_factory=no_more_arms)
    assert report["promoted"] and resumed.view().meta.version != base
    assert reopened.all("evaluation_verdicts") == verdicts
    assert reopened.all("evaluation_evidence") == evidence
    assert reopened.all("meta_arm_results") == arms and reopened.usage() == usage
    assert len(transport.requests["plan"]) == len(transport.requests["verdict"]) == 1


def test_missing_plan_response_is_frozen_as_a_pause_not_replaced(campaign, monkeypatch):
    campaign, transport, _, base, _ = make_campaign(
        campaign, monkeypatch, invalid=lambda phase, value: None if phase == "plan" else value)
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            campaign.prepare()
    assert campaign.view().workflow.version == base
    assert not campaign.store.all("evaluation_plans")
    assert not campaign.store.all("batches")
    assert len(transport.requests["plan"]) == 1
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


@pytest.mark.parametrize("namespace", [
    "evaluation_requests", "evaluation_plans", "evaluation_bindings",
    "evaluation_evidence", "evaluation_verdicts", "evaluation_trial_inputs", "meta_arm_results",
])
def test_research_worker_cannot_read_or_forge_operator_evaluation(campaign, namespace):
    from proteinrsi.replay.broker import dispatch

    for operation in ("get", "all", "put"):
        with pytest.raises(PermissionError):
            dispatch(campaign.team, campaign.team.tools, campaign.view(), [],
                {"rpc": operation, "namespace": namespace, "key": "any", "value": {}})


def test_legacy_gate_serialization_and_workflow_decision_are_unchanged(campaign, monkeypatch):
    assert "criterion" not in campaign.state["gate"]
    assert GatePolicy.model_validate(campaign.state["gate"]).model_dump() == campaign.state["gate"]
    base = campaign.view().workflow.version
    campaign.stage_patch(Patch(target="workflow", base_version=base,
        changes={"strategy": "pairwise"}, task_kind=campaign.view().task.kind,
        hypothesis="Synthetic legacy compatibility"))
    choices = [s for s in campaign.view().task.candidates if s != campaign.view().task.reference_sequence]
    monkeypatch.setattr(campaign.team, "run", lambda view: [Candidate(sequence=s) for s in
        (choices[:5] if view.workflow.version == base else choices[5:10])])
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="synthetic-test")
    task = campaign.view().task
    rows = [Observation(sample_id=s.sample_id, sequence=s.candidate.sequence,
        value=10 if s.arm == "challenger" else 0, metric=task.metric, unit=task.unit,
        source=task.feedback_source, batch_id=batch.batch_id, assay_protocol=task.assay_protocol)
        for s in batch.samples]
    campaign.ingest(rows)
    assert campaign.view().workflow.version != base
    assert campaign.store.get("trial_results", batch.batch_id)["decision"] == "accepted"
    assert not campaign.store.all("evaluation_plans")
    assert not campaign.store.all("evaluation_verdicts")
