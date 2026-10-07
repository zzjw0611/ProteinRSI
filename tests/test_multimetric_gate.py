"""Synthetic observed-Pareto mechanics, not experimental protein efficacy evidence."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from proteinrsi.contracts import (Batch, Candidate, GatePolicy, GateResult, MetaPolicy,
                                  Observation, Patch, Sample)
from proteinrsi.evaluation import compare_grouped_metrics, evaluate_meta
from proteinrsi.improvement import compare_metric_vectors, compare_scores, evaluate_trial
from proteinrsi.metrics import summarize_metrics
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, Store
from test_rsi import ScriptedOffspringTeam, make_meta_cases


def policy(**kwargs):
    return GatePolicy(criterion="observed_pareto_v1", min_per_arm=2, top_ns=[2, 3], **kwargs)


def summary(values, direction="maximize", *, sequences=None, top_ns=(2, 3)):
    return summarize_metrics([{"sequence": s, "value": v, "qc": "valid"}
        for s, v in zip(sequences or ["A" * (i + 1) for i in range(len(values))], values)],
        direction=direction, top_ns=top_ns)


def compare(old, new, gate=None):
    return compare_metric_vectors(summary(old)["signed_metrics"], summary(new)["signed_metrics"],
        gate or policy(), n_baseline=len(old), n_challenger=len(new))


@pytest.mark.parametrize("old,new,decision,outcome", [
    ([0, 1, 2], [1, 2, 3], "accepted", "dominates"),
    ([1, 2, 3], [0, 1, 2], "rejected", "dominated"),
    ([0, 1, 10], [5, 5, 5], "inconclusive", "tradeoff"),  # better avg, worse max
    ([5, 5, 5], [0, 1, 10], "inconclusive", "tradeoff"),  # better max, worse avg
    ([1, 1, 1], [1, 1, 1], "inconclusive", "tie_or_below_margin"),
    ([1, 2], [2, 3], "inconclusive", "missing_metrics"),
    ([1], [100], "inconclusive", "insufficient_units"),
])
def test_frozen_pareto_decisions(old, new, decision, outcome):
    result = compare(old, new)
    assert result.decision == decision
    assert result.details["outcome"] == outcome
    assert result.effect is None and result.interval is None
    assert "no statistical significance" in result.details["evidence"]


def test_no_scalar_gate_bypass():
    with pytest.raises(ValueError, match="metric vectors"):
        compare_scores([0] * 20, [100] * 20, policy())


def test_all_default_metrics_and_configurable_n():
    gate = GatePolicy(criterion="observed_pareto_v1")
    assert gate.required_per_arm == 10
    assert set(gate.absolute_tolerances) == {"best", "avg", "top5mean", "top10mean"}
    assert all(v == 0 for v in gate.improvement_margins.values())
    result = summary(range(12), top_ns=gate.top_ns)
    assert result["metrics"] == {"best": 11, "top5mean": 9, "top10mean": 6.5, "avg": 5.5}
    custom = GatePolicy(criterion="observed_pareto_v1", top_ns=[1, 7, 12])
    assert custom.required_per_arm == 12


@pytest.mark.parametrize("kwargs", [
    {"top_ns": []}, {"top_ns": [0]}, {"top_ns": [2, 2]}, {"top_ns": [True]},
    {"top_ns": [2.5]}, {"absolute_tolerances": {"median": 0}},
    {"absolute_tolerances": {"avg": -1}}, {"improvement_margins": {"avg": float("nan")}},
    {"improvement_margins": {"avg": float("inf")}}, {"min_effect": 1},
])
def test_bad_multi_metric_config_rejected(kwargs):
    with pytest.raises(ValidationError):
        GatePolicy(criterion="observed_pareto_v1", **kwargs)


def test_declared_absolute_tolerance_and_strict_margin():
    old = {name: 1.0 for name in policy().absolute_tolerances}
    new = {**old, "avg": 1.5, "best": 0.75}
    gate = policy(absolute_tolerances={"best": 0.25}, improvement_margins={"avg": 0.5})
    result = compare_metric_vectors(old, new, gate, n_baseline=3, n_challenger=3)
    assert result.decision == "inconclusive"  # Equal to margin does not suffice.
    new["avg"] = 1.6
    assert compare_metric_vectors(old, new, gate, n_baseline=3, n_challenger=3).decision == "accepted"
    new["best"] = 0.74
    assert compare_metric_vectors(old, new, gate, n_baseline=3, n_challenger=3).details["outcome"] == "tradeoff"


def test_raw_and_signed_minimization_and_unique_repeat_means():
    result = summary([100, 0, 4, 8], direction="minimize", sequences=["AA", "AA", "AC", "AD"])
    assert result["metrics"] == {"best": 4, "top2mean": 6, "top3mean": pytest.approx(62 / 3), "avg": pytest.approx(62 / 3)}
    assert result["signed_metrics"]["best"] == -4
    assert result["denominators"]["unique_valid"] == 3
    assert result["denominators"]["technical_repeats"] == 1
    assert result == summary([8, 0, 4, 100], direction="minimize", sequences=["AD", "AA", "AC", "AA"])


def test_denominators_missing_returns_and_top_n_not_silently_shrunk():
    result = summarize_metrics([
        {"sequence": "AA", "value": 2, "qc": "valid"},
        {"sequence": "AC", "value": None, "qc": "unavailable"},
        {"sequence": "AD", "value": None, "qc": "failed"}], top_ns=(2, 3), submitted=5)
    assert result["metrics"] == {"best": 2, "top2mean": None, "top3mean": None, "avg": 2}
    assert result["denominators"] == {"submitted": 5, "returned": 3, "valid": 1,
        "unavailable": 1, "other_nonvalid": 1, "not_returned": 2, "unique_valid": 1,
        "technical_repeats": 0, "nonvalid": 2}
    assert result["top_n"]["top3mean"] == {"required": 3, "effective": 1, "complete": False}


def batch_rows(old, new, *, parent=False, unavailable=False, repeats=False):
    samples, rows = [], []
    for arm, values, prefix in [("baseline", old, "A"), ("challenger", new, "C")]:
        for i, value in enumerate(values):
            sequence = prefix + "D" * (1 if repeats else i + 1)
            sid = f"{arm}-{i}"
            samples.append(Sample(sample_id=sid, candidate=Candidate(sequence=sequence), arm=arm,
                                  workflow_version="w-test"))
            missing = unavailable and arm == "challenger" and i == 0
            rows.append(Observation(sample_id=sid, sequence=sequence, value=None if missing else value,
                metric="fitness", unit="a.u.", source="measured_replay", qc="unavailable" if missing else "valid",
                batch_id="b-test", assay_protocol="test"))
        if parent:
            sid = arm + "-parent"
            samples.append(Sample(sample_id=sid, candidate=Candidate(sequence="EEE"), arm=arm, workflow_version="w-test"))
            rows.append(Observation(sample_id=sid, sequence="EEE", value=1e9, metric="fitness", unit="a.u.",
                source="measured_replay", batch_id="b-test", assay_protocol="test"))
    # A huge non-arm value can never change the gate.
    samples.append(Sample(sample_id="control", candidate=Candidate(sequence="FFF"), arm="control", workflow_version="w-test"))
    rows.append(Observation(sample_id="control", sequence="FFF", value=1e10, metric="fitness", unit="a.u.",
        source="measured_replay", batch_id="b-test", assay_protocol="test"))
    return Batch(batch_id="b-test", campaign_id="c", round_index=1, evidence_version="e",
                 meta_version="m", samples=samples), rows


@pytest.mark.parametrize("direction,old,new", [("maximize", [0, 1, 2], [1, 2, 3]),
                                              ("minimize", [1, 2, 3], [0, 1, 2])])
def test_trial_direction_controls_and_parent_excluded(direction, old, new):
    batch, rows = batch_rows(old, new, parent=True)
    result = evaluate_trial(batch, rows, policy(), direction=direction, reference_sequence="EEE")
    assert result.decision == "accepted"
    assert result.n_baseline == result.n_challenger == 3
    assert result.details["diagnostics"]["parent_excluded"] == {"baseline": 1, "challenger": 1}


def test_unavailable_or_repeated_units_never_promote():
    batch, rows = batch_rows([0] * 10, [10] * 10, unavailable=True)
    result = evaluate_trial(batch, rows, policy(), direction="maximize")
    assert result.details["outcome"] == "missing_coverage"
    assert result.details["diagnostics"]["arms"]["challenger"]["denominators"]["unavailable"] == 1
    batch, rows = batch_rows([0] * 10, [10] * 10, repeats=True)
    result = evaluate_trial(batch, rows, policy(), direction="maximize")
    assert result.decision == "inconclusive" and result.n_challenger == 1


def pair(case, group, old, new):
    return {arm: {"case_id": case, "group_id": group, "metric_summary": summary(values)}
            for arm, values in [("baseline", old), ("challenger", new)]}


def test_meta_uses_all_metrics_and_counts_groups_not_seeds():
    pairs = [pair(str(i), "same-protein", [0, 1, 10], [5, 5, 5]) for i in range(10)]
    result = compare_grouped_metrics(pairs, policy())
    assert result.n_baseline == 1 and result.details["outcome"] == "insufficient_units"
    pairs += [pair("other", "other-protein", [0, 1, 10], [5, 5, 5])]
    result = compare_grouped_metrics(pairs, policy())
    assert result.n_baseline == 2 and result.details["outcome"] == "tradeoff"
    assert result.details["metrics"]["best"]["delta"] == -5
    assert result.details["metrics"]["avg"]["delta"] > 0


def test_meta_aligned_case_metrics_not_max_across_groups():
    pairs = [pair("one", "g1", [0, 1, 10], [0, 1, 11]),
             pair("two", "g2", [0, 1, 100], [0, 1, 100])]
    result = compare_grouped_metrics(pairs, policy())
    assert result.decision == "accepted"
    assert result.details["metrics"]["best"]["delta"] == 0.5
    misaligned = deepcopy(pairs)
    misaligned[0]["challenger"]["case_id"] = "wrong"
    with pytest.raises(ValueError, match="aligned"):
        compare_grouped_metrics(misaligned, policy())
    pairs[0]["challenger"]["metric_summary"] = summary([1, 2])
    pairs[0]["baseline"]["metric_summary"] = summary([0, 1])
    assert compare_grouped_metrics(pairs, policy()).details["outcome"] == "missing_metrics"


def new_campaign(campaign, name, gate=None):
    return Campaign.initialize(str(campaign.store.root / name), campaign.view().task,
        workflow=campaign.view().workflow, meta=MetaPolicy(min_observations=100),
        gate=gate or policy())


def stage_meta(campaign):
    campaign.stage_patch(Patch(target="meta", base_version=campaign.view().meta.version,
        changes={"min_observations": 2}, task_kind="variant_design",
        hypothesis="Synthetic test of multi-metric descendant policy"))


def test_offline_meta_integrates_vectors_without_scalar_fallback(campaign):
    campaign = new_campaign(campaign, "offline", GatePolicy(criterion="observed_pareto_v1", top_ns=[1, 2], min_per_arm=2))
    stage_meta(campaign)
    report = evaluate_meta(campaign, make_meta_cases(campaign), promote=True, team_factory=ScriptedOffspringTeam)
    assert report["promoted"]
    assert report["gate"]["details"]["metrics"]["best"]["delta"] > 0
    assert all("metric_summary" in p["baseline"] for p in report["traces"])
    assert report["gate"]["n_baseline"] == 4


def test_online_meta_promotes_only_joint_improvement(campaign, monkeypatch):
    campaign = new_campaign(campaign, "online")
    state = campaign.state
    state["observations"] = [o.model_dump(mode="json") for o in make_meta_cases(campaign)[0].initial]
    campaign.store.put("campaign", "state", state)
    stage_meta(campaign)
    monkeypatch.setattr("proteinrsi.online_meta.make_validation_team", lambda campaign, store: ScriptedOffspringTeam(store))
    base = campaign.view().meta.version
    batch = campaign.prepare()
    assert batch.patch_id
    campaign.approve(batch.batch_id, operator="test")
    campaign.ingest([Observation(sample_id=s.sample_id, sequence=s.candidate.sequence,
        value=10 if s.arm == "challenger" else 0, metric=campaign.view().task.metric, unit=campaign.view().task.unit, source="synthetic",
        batch_id=batch.batch_id, assay_protocol=campaign.view().task.assay_protocol) for s in batch.samples])
    report = next(iter(campaign.store.all("meta_evaluations").values()))
    assert report["result"]["decision"] == "accepted"
    assert set(report["result"]["details"]["metrics"]) == {"best", "top2mean", "top3mean", "avg"}
    assert campaign.view().meta.version != base


def test_policy_is_frozen_exposed_and_cannot_reclassify_old_campaign(campaign):
    old_gate = campaign.state["gate"]
    assert GatePolicy.model_validate(old_gate).model_dump() == old_gate
    assert "criterion" not in old_gate
    assert GateResult(decision="inconclusive", reason="old", n_baseline=0, n_challenger=0).model_dump() == {
        "decision": "inconclusive", "effect": None, "interval": None, "reason": "old", "n_baseline": 0, "n_challenger": 0}
    fresh = new_campaign(campaign, "frozen")
    assert fresh.view().research_context["acceptance_policy"] == fresh.state["gate"]
    snapshot = next(iter(fresh.store.all("method_snapshots").values()))
    assert snapshot["acceptance_policy"] == fresh.state["gate"]
    state = fresh.state
    state["gate"]["absolute_tolerances"]["avg"] = 1
    fresh.store.put("campaign", "state", state)
    with pytest.raises(Conflict, match="frozen study"):
        Campaign(Store(fresh.store.root)).view()
    state = campaign.state
    state["gate"] = policy().model_dump()
    campaign.store.put("campaign", "state", state)
    with pytest.raises(Conflict, match="new study"):
        campaign.view()


def test_top5_and_top10_are_independent_vetoes():
    gate = GatePolicy(criterion="observed_pareto_v1", min_per_arm=2)
    cases = [([10, 9, 9, 9, 9] + [0] * 7, [10] + [8] * 11, "top5mean"),
             ([10] * 10 + [0] * 10, [11] * 5 + [6] * 15, "top10mean")]
    for old, new, veto in cases:
        result = compare_metric_vectors(summary(old, top_ns=(5, 10))["signed_metrics"],
            summary(new, top_ns=(5, 10))["signed_metrics"], gate,
            n_baseline=len(old), n_challenger=len(new))
        assert result.decision == "inconclusive" and result.details["outcome"] == "tradeoff"
        assert result.details["metrics"][veto]["worsened"]
        assert result.details["metrics"]["avg"]["improved"]


def test_workflow_adoption_uses_full_vector_and_freezes_plan(campaign, monkeypatch):
    campaign = new_campaign(campaign, "workflow")
    base = campaign.view().workflow.version
    patch = Patch(target="workflow", base_version=base, changes={"strategy": "pairwise"},
        task_kind="variant_design", hypothesis="Synthetic observed Pareto workflow comparison")
    campaign.stage_patch(patch)
    choices = campaign.view().task.candidates
    monkeypatch.setattr(campaign.team, "run", lambda view: [Candidate(sequence=s) for s in
        (choices[:5] if view.workflow.version == base else choices[5:10])])
    batch = campaign.prepare()
    assert batch.patch_id == patch.patch_id
    plan = campaign.store.get("method_candidates", patch.patch_id)["validation_plan"]
    assert plan["metric"] == "direction_adjusted_best_topN_avg"
    assert plan["gate"] == campaign.state["gate"]
    campaign.approve(batch.batch_id, operator="test")
    task = campaign.view().task
    campaign.ingest([Observation(sample_id=s.sample_id, sequence=s.candidate.sequence,
        value=1 if s.arm == "challenger" else 0, metric=task.metric, unit=task.unit,
        source=task.feedback_source, batch_id=batch.batch_id, assay_protocol=task.assay_protocol)
        for s in batch.samples])
    assert campaign.view().workflow.version != base
    result = campaign.store.get("trial_results", batch.batch_id)
    assert result["decision"] == "accepted" and result["details"]["outcome"] == "dominates"


def test_offline_missing_historical_label_is_explicit_inconclusive(campaign):
    campaign = new_campaign(campaign, "offline-missing", GatePolicy(
        criterion="observed_pareto_v1", top_ns=[1, 2], min_per_arm=2))
    stage_meta(campaign)
    cases = make_meta_cases(campaign)
    del cases[0].labels[cases[0].task.candidates[-1]]
    report = evaluate_meta(campaign, cases, promote=True, team_factory=ScriptedOffspringTeam)
    assert not report["promoted"]
    assert report["gate"]["details"]["outcome"] == "missing_coverage"
    missing = report["traces"][0]["challenger"]["metric_summary"]["denominators"]
    assert missing["unavailable"] == 1 and missing["submitted"] == 2
    assert report["budget_after"]["experimental_wells"]["committed"] == report["required_query_slots"]


def test_top_n_capacity_defers_before_validation_spending(campaign):
    campaign = new_campaign(campaign, "capacity", GatePolicy(criterion="observed_pareto_v1"))
    stage_meta(campaign)
    batch = campaign.prepare()
    assert batch.patch_id is None
    assert campaign.state["pending_meta"] is not None
    assert not campaign.store.all("meta_online_attempts")
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
