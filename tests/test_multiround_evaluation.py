"""Synthetic mechanism tests; not GB1 or biological efficacy measurements."""
from copy import deepcopy

import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import BudgetSpec, Candidate, GatePolicy, Observation, Patch, TaskSpec
from proteinrsi.evaluation import MetaCase
from proteinrsi.harness_evaluation import evaluate_workflow
from proteinrsi.llm import ProviderPaused
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.storage import BudgetExceeded, Conflict, SponsoredStore, Store

SEQUENCES = [x + "AAA" for x in "CDEFGHIKLMNPQRSTVWY"]


class LearningTeam(Team):
    seen = []
    pause_once = False

    def run(self, view):
        self.seen.append((view.workflow.strategy, view.round_index, len(view.observations)))
        if self.store.get("configuration", "evaluation_branch"):
            assert not self.store.get("campaign", "state")["pending_patch"]
        if self.pause_once and view.workflow.strategy == "pairwise" and view.round_index == 1:
            type(self).pause_once = False
            raise ProviderPaused("synthetic pause")
        self.store.event("task_harness_invoked", {"workflow": view.workflow.version,
            "meta": view.meta.version, "evidence_version": view.evidence_version})
        observed = {o.sequence for o in view.observations}
        choices = sorted(set(view.task.candidates) - observed)
        if view.workflow.strategy == "pairwise":
            choices.reverse()
        return [Candidate(sequence=s) for s in choices]


@pytest.fixture
def root(tmp_path):
    task = TaskSpec(name="mechanism", reference_sequence="AAAA", mutable_positions=[1],
        max_mutations=1, candidates=SEQUENCES, controls_per_batch=0, batch_size=2,
        max_rounds=3, budget=BudgetSpec(experimental_wells=64, llm_calls=200, tool_calls=200))
    c = Campaign.initialize(str(tmp_path / "root"), task, gate=GatePolicy(min_per_arm=2),
        research_config=ResearchConfig(enabled=False, workflow_validation="independent",
            evaluation_protocol="task-loop-v1", auto_evaluation={"main_reserve_wells": 2, "llm_calls_per_arm": 8, "tool_calls_per_arm": 8}))
    LearningTeam.seen = []
    LearningTeam.pause_once = False
    c.team = LearningTeam(c.store)
    return c


def stage(c):
    p = Patch(target="workflow", base_version=c.view().workflow.version,
        changes={"strategy": "pairwise"}, task_kind="variant_design",
        hypothesis="Synthetic ordering change for controlled tests")
    c.stage_patch(p)
    return p


def cases(c, split="validation"):
    labels = {s: float(i) for i, s in enumerate(sorted(SEQUENCES))}
    result = []
    for i in range(2):
        task = c.view().task.model_copy(deep=True)
        task.budget = BudgetSpec(experimental_wells=4, llm_calls=8, tool_calls=8)
        task.max_rounds = 2
        seq = SEQUENCES[5+i]
        initial = [Observation(sample_id="initial", batch_id="prestudy", sequence=seq,
            value=labels[seq], metric=task.metric, unit=task.unit, source=task.feedback_source,
            assay_protocol=task.assay_protocol)]
        result.append(MetaCase(case_id=f"case{i}", group_id=f"group{i}", split=split,
            task=task, initial=initial, labels=labels, query_budget=4))
    return result


def test_full_feedback_replanning_free_initial_and_frozen_methods(root, monkeypatch):
    p = stage(root)
    monkeypatch.setattr("proteinrsi.agents.MetaAgent.propose", lambda *a, **k: pytest.fail("Improver invoked in fixed-H branch"))
    report = evaluate_workflow(root, cases(root), promote=True, team_factory=LearningTeam)
    assert report["promoted"]
    assert root.store.usage()["experimental_wells"]["committed"] == 16
    assert root.store.remaining("experimental_wells") == 48
    assert not root.state["observations"]
    for pair in report["traces"]:
        for trace in pair.values():
            assert trace["completed_new_rounds"] == 2
            assert len(trace["history"]) == 2
            assert all(h.get("analyst_feedback") for h in trace["history"])
            assert trace["task_reports"]
            assert trace["branch"]["free_initial_observations"] == 1
    assert any(round_ == 1 and n == 3 for _, round_, n in LearningTeam.seen)
    before = deepcopy(root.store.usage())
    assert evaluate_workflow(root, cases(root), promote=True, team_factory=LearningTeam) == report
    assert root.store.usage() == before
    with pytest.raises(ValueError, match="confirmation|Already revealed"):
        evaluate_workflow(root, cases(root, "test"), patch_id=p.patch_id, team_factory=LearningTeam)
    batch = root.prepare()
    assert all(sample.workflow_version == report["challenger"] for sample in batch.samples)
    assert any(event["kind"] == "task_harness_invoked" and event["payload"]["workflow"] == report["challenger"]
               for event in root.store.events())
    root.cancel_prepared(batch.batch_id, operator="fixture")
    root.methods.rollback("workflow", report["baseline"], operator="fixture", reason="test rollback")
    assert root.view().workflow.version == report["baseline"]
    assert root.store.usage() == before


def test_pause_resume_does_not_repeat_completed_round_or_charge(root):
    stage(root)
    LearningTeam.pause_once = True
    with pytest.raises(ProviderPaused):
        evaluate_workflow(root, cases(root), promote=True, team_factory=LearningTeam)
    cost = root.store.usage()["experimental_wells"]["committed"]
    assert cost == 6  # First arm done, first round of the second arm committed.
    assert root.store.usage()["experimental_wells"]["allocated_unspent"] == 10
    r = evaluate_workflow(root, cases(root), promote=True, team_factory=LearningTeam)
    assert r["promoted"]
    assert root.store.usage()["experimental_wells"]["committed"] == 16
    assert LearningTeam.seen.count(("additive", 0, 1)) == 2  # One per case, not repeated on resume.


def test_branch_memory_and_evidence_are_private(root):
    stage(root)
    root.store.put("experience", "shared", {"scope": {"task_kind": "variant_design"}, "status": "local_support", "text": "same initial memory"})
    class Checking(LearningTeam):
        def run(self, view):
            assert self.store.get("experience", "shared")
            # Deliberately write branch-local notes. The next arm cannot see them.
            key = "only-" + view.workflow.strategy
            other = "only-" + ("pairwise" if view.workflow.strategy == "additive" else "additive")
            assert self.store.get("research_notes", other) is None
            self.store.put("research_notes", key, {"content": "private"})
            assert "labels" not in view.model_dump()
            return super().run(view)
    evaluate_workflow(root, cases(root), team_factory=Checking)
    assert root.store.get("research_notes", "only-additive") is None


def test_budget_rejected_before_execution(root):
    stage(root)
    data = cases(root)
    for case in data:
        case.query_budget = 20
        case.task.budget.experimental_wells = 20
    before = deepcopy(root.store.usage())
    with pytest.raises(BudgetExceeded):
        evaluate_workflow(root, data, promote=True, team_factory=LearningTeam)
    assert root.store.usage() == before
    assert not LearningTeam.seen
    assert root.state["pending_patch"]
    assert not root.store.all("harness_attempts")
    assert root.prepare() is not None


def test_whole_plate_case_budget_is_checked(root):
    stage(root)
    data = cases(root)
    data[0].task.batch_fill_policy = "full_plate"
    data[0].query_budget = 3
    with pytest.raises(ValueError, match="whole-plate"):
        evaluate_workflow(root, data, team_factory=LearningTeam)


def test_allocations_atomic_idempotent_and_actual_charges(tmp_path):
    sponsor = Store(tmp_path / "sponsor")
    sponsor.configure_budget({"experimental_wells": 12, "llm_calls": 8, "tool_calls": 8})
    limits = {"experimental_wells": 4, "llm_calls": 2, "tool_calls": 2}
    pair = {"pair/base": limits, "pair/new": limits}
    sponsor.reserve_allocations("pair", pair, keep_free={"experimental_wells": 4})
    sponsor.reserve_allocations("pair", pair, keep_free={"experimental_wells": 4})
    assert sponsor.remaining("experimental_wells") == 4
    child = SponsoredStore(tmp_path / "arm", sponsor, "pair/base", allocation_id="pair/base")
    child.configure_budget(limits)
    child.reserve("batch", "experimental_wells", 2, {"request": 1})
    child.settle("batch")
    child.reserve("batch", "experimental_wells", 2, {"request": 1})
    child.settle("batch")
    assert sponsor.usage()["experimental_wells"]["committed"] == 2
    assert sponsor.remaining("experimental_wells") == 4
    sponsor.release_allocations("pair")
    sponsor.release_allocations("pair")
    assert sponsor.remaining("experimental_wells") == 10
    with pytest.raises(Conflict):
        sponsor.reserve_allocations("pair", pair, keep_free={"experimental_wells": 4})
    assert sponsor.usage()["experimental_wells"]["committed"] == 2


def test_automatic_same_snapshot_comparison_needs_no_case_json(root):
    from proteinrsi.evaluation_scheduler import schedule_evaluation
    stage(root)
    class Oracle:
        _labels = {s: (float(i), "valid") for i, s in enumerate(sorted(SEQUENCES))}
    report = schedule_evaluation(root, Oracle(), team_factory=LearningTeam)
    assert report["protocol"] == "task-loop-v1"
    assert report["scope"] == "same_snapshot"
    assert report["gate"]["decision"] == "inconclusive"  # One start is not several independent groups.
    assert all(t["completed_new_rounds"] == 2 for t in report["traces"][0].values())
    assert root.store.usage()["experimental_wells"]["committed"] == 8
    assert root.state["pending_patch"] is None


def test_known_budget_shortage_defers_and_main_keeps_running(root):
    from proteinrsi.evaluation_scheduler import schedule_evaluation
    stage(root)
    root.store.reserve("prior-work", "experimental_wells", 58, {"operator": "fixture"})
    root.store.settle("prior-work")
    class Oracle:
        _labels = {s: (float(i), "valid") for i, s in enumerate(sorted(SEQUENCES))}
    assert schedule_evaluation(root, Oracle(), team_factory=LearningTeam) is None
    assert root.state["pending_patch"] and root.store.all("evaluation_deferrals")
    assert not root.store.all("harness_attempts")
    assert root.prepare() is not None


def test_complete_replay_uses_shared_loop_and_analysis_when_meta_disabled(root, tmp_path, monkeypatch):
    import csv
    from proteinrsi.replay.controller import run_replay
    monkeypatch.setattr(root, "consider_improvement", lambda: None)
    path = tmp_path / "measurements.csv"
    with path.open("w") as f:
        writer = csv.writer(f)
        writer.writerow(["sequence", "value"])
        writer.writerows([(s, i) for i, s in enumerate(SEQUENCES)])
    report = run_replay(root, path, guarded=False)
    assert report["loop_result"]["status"] == "completed"
    assert report["completed_rounds"] == 3
    assert len(root.state["history"]) == 3
    assert all(h.get("analyst_feedback") for h in root.state["history"])
    assert root.store.all("task_reports")
    cost = root.store.usage()
    run_replay(root, path, guarded=False)
    assert cost == root.store.usage()


def test_manual_feedback_boundary_and_resume(root):
    from proteinrsi.task_loop import run_task_loop
    result = run_task_loop(root)
    assert result.status == "waiting_approval"
    root.approve(result.pending_batch, operator="fixture")
    result = run_task_loop(root)
    assert result.status == "waiting_feedback"
    assert root.store.usage()["experimental_wells"]["committed"] == 2


def test_allocation_failure_is_atomic_and_uncertain_calls_are_not_refunded(tmp_path):
    s = Store(tmp_path / "store")
    s.configure_budget({"experimental_wells": 8, "tool_calls": 4})
    pair = {"a": {"experimental_wells": 4}, "b": {"experimental_wells": 4}}
    with pytest.raises(BudgetExceeded):
        s.reserve_allocations("pair", pair, keep_free={"experimental_wells": 2})
    assert s.remaining("experimental_wells") == 8 and not s.all("budget_allocation_groups")
    s.reserve_allocations("pair", pair)
    s.reserve_allocated("a", "request", "experimental_wells", 2, {"id": 1})
    with pytest.raises(Conflict, match="Reconcile"):
        s.release_allocations("pair")
    assert s.remaining("experimental_wells") == 0
    s.settle("request")
    s.release_allocations("pair")
    assert s.remaining("experimental_wells") == 6


def test_generated_metric_plan_and_llm_verdict_use_reserved_e_budget(root, monkeypatch):
    from test_custom_metric_integration import custom_plan, _synthetic_output, CODE
    from test_llm_evaluation_integration import EvaluationHTTP
    from proteinrsi.contracts import digest
    # Fresh gate/config snapshots; never change the initialized parent in place.
    campaign = Campaign.initialize(str(root.store.root / "llm"), root.view().task,
        gate=GatePolicy(criterion="llm_adjudicated_v1"),
        research_config=ResearchConfig(enabled=False, workflow_validation="independent",
            evaluation_protocol="task-loop-v1", auto_evaluation={"main_reserve_wells": 2}))
    provider = EvaluationHTTP(campaign.store, plan=custom_plan())
    campaign.team = provider.team(campaign.store)
    def metric_worker(store, view, args, *, pure=False):
        assert pure and view is None and args["code"] == CODE
        return {"status": "ok", "execution_backend": "landlock_seccomp_generated_v1",
                "code_sha256": digest(CODE), "output": _synthetic_output(args["inputs"])}
    monkeypatch.setattr("proteinrsi.evaluation_metrics.execute_code", metric_worker)
    monkeypatch.setattr("proteinrsi.replay.sandbox.probe", lambda: {"available": True})
    stage(campaign)
    def factory(store):
        from copy import copy
        llm = copy(campaign.team.llm)
        llm.store = store
        team = LearningTeam(store, llm)
        team.analyst.llm = None  # Scripted task run; only E uses the artificial HTTP transport.
        return team
    report = evaluate_workflow(campaign, cases(campaign), promote=True, team_factory=factory)
    assert report["promoted"]
    assert len(provider.requests["plan"]) == len(provider.requests["verdict"]) == 1
    assert campaign.store.usage()["llm_calls"]["committed"] == 2
    assert not campaign.store.usage()["llm_calls"].get("allocated_unspent")
    spec = campaign.store.get("evaluation_specs", report["spec_ref"])
    assert spec["metric_plan_ref"]
    assert spec["stopping_conditions"]
    assert "labels" not in str(provider.contexts["plan"][0])
    assert any("cost" in key for key in provider.contexts["verdict"][0]["evidence"])


def test_fork_checks_model_settings_and_disallows_method_writes(root):
    from types import SimpleNamespace
    stage(root)
    class WrongProvider(LearningTeam):
        def __init__(self, store):
            super().__init__(store)
            self.llm = SimpleNamespace(model="different", base_url="fixture", cache_settings={})
    with pytest.raises(Conflict, match="provider"):
        evaluate_workflow(root, cases(root), team_factory=WrongProvider)
    assert root.store.usage()["experimental_wells"]["committed"] == 0


def test_renamed_confirmation_cannot_hide_selection_measurements(root):
    patch = stage(root)
    evaluate_workflow(root, cases(root), promote=True, team_factory=LearningTeam)
    data = cases(root, "test")
    for i, case in enumerate(data):
        seq = SEQUENCES[8+i]
        case.initial[0] = case.initial[0].model_copy(update={"sequence": seq, "value": case.labels[seq]})
    with pytest.raises(ValueError, match="Already revealed"):
        evaluate_workflow(root, data, patch_id=patch.patch_id, team_factory=LearningTeam)


def test_budget_exhaustion_releases_unused_compute_without_adopting(root):
    stage(root)
    class Expensive(LearningTeam):
        def run(self, view):
            key = "expensive-" + str(view.round_index)
            self.store.reserve(key, "tool_calls", 100, {"round": view.round_index})
            return super().run(view)
    report = evaluate_workflow(root, cases(root), promote=True, team_factory=Expensive)
    assert report["gate"]["decision"] == "inconclusive"
    assert not report["promoted"]
    assert root.store.usage()["experimental_wells"]["committed"] == 0
    assert all(not values.get("allocated_unspent") for values in root.store.usage().values())


def test_rejected_method_keeps_old_version_and_no_cost_refund(root):
    stage(root)
    class Reversed(LearningTeam):
        def run(self, view):
            observed = {o.sequence for o in view.observations}
            choices = sorted(set(view.task.candidates) - observed, reverse=view.workflow.strategy == "additive")
            return [Candidate(sequence=s) for s in choices]
    old = root.view().workflow.version
    report = evaluate_workflow(root, cases(root), promote=True, team_factory=Reversed)
    assert report["gate"]["decision"] == "rejected"
    assert root.view().workflow.version == old and not root.state["pending_patch"]
    assert root.store.usage()["experimental_wells"]["committed"] == 16
