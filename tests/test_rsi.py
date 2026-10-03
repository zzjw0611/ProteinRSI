import pytest

from proteinrsi.agents import MetaAgent
from proteinrsi.contracts import Candidate, GatePolicy, MetaPolicy, Observation, Patch
from proteinrsi.evaluation import MetaCase, evaluate_meta
from proteinrsi.improvement import compare_scores


def test_gate_requires_evidence_and_reports_inconclusive():
    policy = GatePolicy(min_per_arm=3, bootstrap_samples=100)
    assert compare_scores([1], [100], policy).decision == "inconclusive"
    assert compare_scores([1] * 4, [1] * 4, policy).decision == "inconclusive"
    assert compare_scores([1] * 4, [2] * 4, policy).decision == "accepted"
    assert compare_scores([2] * 4, [1] * 4, policy).decision == "rejected"


def make_meta_cases(campaign, split="validation"):
    task = campaign.view().task
    sequences = task.candidates[:12]
    cases = []
    for i in range(4):
        labels = {s: float(j) for j, s in enumerate(sequences)}
        initial = [Observation(sample_id=f"s-{j}", sequence=s, value=labels[s], metric=task.metric,
            unit=task.unit, source="synthetic", batch_id="initial", assay_protocol=task.assay_protocol)
            for j, s in enumerate(sequences[:2])]
        small_task = task.model_copy(update={"candidates": sequences})
        cases.append(MetaCase(case_id=f"case-{i}", group_id=f"artificial-group-{i}", split=split,
            task=small_task, initial=initial, labels=labels, query_budget=2))
    return cases


class ScriptedOffspringTeam:
    """Artificial ranking oracle for testing the gate's mechanics, NOT scientific evaluation."""
    llm = None
    def __init__(self, store):
        self.store = store
    def run(self, view):
        seqs = list(view.task.candidates)
        if view.workflow.strategy == "pairwise":
            seqs.reverse()
        return [Candidate(sequence=s) for s in seqs]


def stage_meta(campaign):
    state = campaign.state
    # Trusted initial policy setting, before any experiment; the successor is a staged patch.
    state["meta"] = MetaPolicy(min_observations=100).model_dump()
    campaign.store.put("campaign", "state", state)
    base = MetaPolicy.model_validate(state["meta"])
    patch = Patch(target="meta", base_version=base.version, changes={"min_observations": 2},
        task_kind="variant_design", hypothesis="Use the available evidence earlier to propose a workflow trial",
        author_backend="synthetic-test")
    campaign.stage_patch(patch)
    return base


def test_meta_changes_are_evaluated_on_descendants_and_successor_is_loaded(campaign):
    base = stage_meta(campaign)
    assert campaign.view().meta.version == base.version
    report = evaluate_meta(campaign, make_meta_cases(campaign), promote=True,
                           team_factory=ScriptedOffspringTeam)
    assert report["promoted"] is True
    assert campaign.view().meta.version == report["candidate_meta"] != base.version
    assert campaign.state["pending_meta"] is None
    case = make_meta_cases(campaign)[0]
    view = campaign.view()
    view.observations = case.initial
    view.round_index = 1
    successor_decision = MetaAgent().propose(view)
    assert successor_decision.patch.target == "workflow"
    view.meta = base
    assert MetaAgent().propose(view).patch is None


def test_final_test_cannot_select_a_meta_version(campaign):
    stage_meta(campaign)
    with pytest.raises(ValueError):
        evaluate_meta(campaign, make_meta_cases(campaign, "test"), promote=True,
                      team_factory=ScriptedOffspringTeam)


def test_related_cases_do_not_inflate_meta_sample_size(campaign):
    stage_meta(campaign)
    cases = make_meta_cases(campaign)
    for case in cases:
        case.group_id = "same-protein"
    result = evaluate_meta(campaign, cases, promote=True, team_factory=ScriptedOffspringTeam)
    assert not result["promoted"]
    assert result["gate"]["decision"] == "inconclusive"
    assert result["gate"]["n_baseline"] == 1
