"""Reporting-only synthetic panels: no source landscape or evaluator labels."""
import importlib.util
from pathlib import Path

import pytest

from proteinrsi.metrics import summarize_metrics
from proteinrsi.reporting_metrics import campaign_metrics, charged_metrics, sample_metrics
from proteinrsi.storage import Store


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit, baseline = script("summarize_replay_study"), script("replay_baseline")


def panel(bid, entries, *, round_index=0):
    samples, observations = [], []
    for i, (sequence, arm, value, qc) in enumerate(entries):
        identity = f"{bid}-{i}"
        samples.append({"sample_id": identity, "arm": arm, "workflow_version": "w0",
                        "candidate": {"sequence": sequence}})
        if qc is not None:
            observations.append({"sample_id": identity, "sequence": sequence, "value": value, "qc": qc})
    return {"batch_id": bid, "round_index": round_index, "evidence_version": "e0", "samples": samples}, observations


def charge(bid, amount, *, state="committed"):
    return {"key": "lab-" + bid, "resource": "experimental_wells", "amount": amount,
            "state": state, "fingerprint": "fixture"}


def parent(value=100):
    return [{"sample_id": "provided-parent", "sequence": "AA", "value": value, "qc": "valid"}]


def test_parent_excluded_from_round_and_arms_included_once_in_campaign():
    batch, observed = panel("b0", [("AC", "baseline", 9, "valid"), ("AC", "baseline", 7, "valid"),
        ("AD", "baseline", 8, "valid"), ("AE", "challenger", 10, "valid"),
        ("AF", "challenger", 0, "valid"), ("AA", "control", 100, "valid")])
    result = sample_metrics(batch["samples"], observed, reference="AA", top_ns=(2, 5))
    assert result["metrics"] == {"best": 10, "top2mean": 9, "top5mean": None, "avg": 6.5}
    assert result["denominators"]["unique_valid"] == 4
    assert result["denominators"]["technical_repeats"] == 1
    assert result["denominators"]["submitted"] == 5
    assert result["submitted_denominators"]["submitted"] == 6
    assert result["parent"] == {"included": False, "excluded_submissions": 1, "excluded_returned": 1}
    accumulated = campaign_metrics(observed, submitted=6, initial=parent(), reference="AA", top_ns=(2, 5))
    assert accumulated["metrics"] == {"best": 100, "top2mean": 55, "top5mean": 25.2, "avg": 25.2}
    assert accumulated["denominators"]["valid"] == 6
    assert accumulated["evidence_denominators"]["valid"] == 7
    assert accumulated["evidence_denominators"]["unique_valid"] == 5
    assert accumulated["parent"]["unique_valid_sequences"] == 1
    assert accumulated["parent"]["valid_observations"] == 2


@pytest.mark.parametrize("direction,best,top2", [("maximize", 8, 8), ("minimize", -4, 2)])
def test_ties_direction_and_replicate_order_independence(direction, best, top2):
    _, observed = panel("b", [("AC", "research", 10, "valid"), ("AC", "research", 6, "valid"),
                              ("AD", "research", 8, "valid"), ("AE", "research", -4, "valid")])
    result = summarize_metrics(observed, direction=direction, top_ns=(2, 4))
    assert result == summarize_metrics(list(reversed(observed)), direction=direction, top_ns=(2, 4))
    assert result["metrics"] == {"best": best, "top2mean": top2, "top4mean": None, "avg": 4}
    assert result["top_n"]["top4mean"] == {"required": 4, "effective": 3, "complete": False}


def test_empty_missing_failed_and_unreturned_are_not_zero():
    batch, observed = panel("b", [("AC", "research", None, "unavailable"),
        ("AD", "research", None, "failed"), ("AE", "research", None, "inconclusive"),
        ("AF", "research", None, None)])
    result = sample_metrics(batch["samples"], observed)
    assert all(v is None for v in result["metrics"].values())
    assert result["denominators"] == {"submitted": 4, "returned": 3, "valid": 0,
        "unavailable": 1, "other_nonvalid": 2, "nonvalid": 3, "not_returned": 1,
        "unique_valid": 0, "technical_repeats": 0}
    assert result["top_n"]["top5mean"]["effective"] == 0


def test_exact_100_charged_query_windows_cross_rounds_include_missing_and_control():
    batches, measurements = {}, {}
    for r in range(3):
        entries = [(f"S{r*60+i:03}", "research", r * 60 + i, "valid") for i in range(60)]
        if r == 0:
            entries[0] = ("AA", "control", 1, "valid")
            entries[1] = ("AC", "research", None, "unavailable")
        batch, observed = panel(f"b{r}", entries, round_index=r)
        batches[batch["batch_id"]], measurements[batch["batch_id"]] = batch, observed
    ledger = [charge(f"b{i}", 60) for i in range(3)]
    ledger += [charge("reserved", 20, state="reserved"), charge("released", 10, state="released")]
    result = charged_metrics(batches, measurements, ledger, initial=parent(1), reference="AA")
    first, last = result["equal_query_curve"]
    assert (first["query_start"], first["query_end"], first["charged_queries"]) == (1, 100, 100)
    assert first["complete_bin"] is True
    assert first["window_metrics"]["submitted_denominators"]["unavailable"] == 1
    assert first["campaign_metrics"]["denominators"]["submitted"] == 100
    assert first["campaign_metrics"]["metrics"]["best"] == 99
    assert (last["query_start"], last["query_end"], last["charged_queries"]) == (101, 180, 80)
    assert last["complete_bin"] is False
    assert last["window_metrics"]["metrics"]["best"] == 179
    assert last["campaign_metrics"]["denominators"]["submitted"] == result["charged_queries"] == 180
    assert result["batch_endpoints"]["b1"]["cumulative_charged_queries"] == 120


def test_unreconstructable_sponsored_charge_costs_preserved_curve_withheld():
    batch, observed = panel("b", [("AC", "research", 3, "valid"), ("AD", "research", 5, "valid")])
    result = charged_metrics({"b": batch}, {"b": observed}, [charge("offline-meta", 3), charge("b", 2)],
                             initial=parent(), reference="AA", top_ns=(2,))
    assert result["charged_queries"] == 5 and result["unreconstructed_charged_queries"] == 3
    assert result["campaign_metrics"]["denominators"]["not_returned"] == 3
    assert result["equal_query_curve"] == []
    assert result["equal_query_curve_status"] == "withheld_unreconstructed_charges"
    assert result["batch_endpoints"]["b"]["cumulative_charged_queries"] == 5


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "mismatch"])
def test_invalid_saved_identity_rejected(mutation):
    batch, observed = panel("b", [("AC", "research", 1, "valid")])
    if mutation == "duplicate":
        observed *= 2
    elif mutation == "unknown":
        observed[0]["sample_id"] = "foreign"
    else:
        observed[0]["sequence"] = "AD"
    with pytest.raises(ValueError, match="identity"):
        sample_metrics(batch["samples"], observed)


def test_old_study_loading_is_readonly_and_keeps_saved_trial_decision(tmp_path):
    store = Store(tmp_path)
    batch, observed = panel("b", [("AC", "baseline", 2, "valid"), ("AD", "challenger", 4, "valid")])
    historical_trial = {"decision": "accepted", "reason": "Historical criterion", "effect": 2}
    state = {"campaign_id": "old-study", "status": "complete", "round_index": 1,
        "task": {"metric": "fitness", "unit": "synthetic", "reference_sequence": "AA", "direction": "maximize"},
        "gate": {"min_effect": 0}, "history": [{"batch_id": "b", "trial": historical_trial}]}
    store.put("campaign", "state", state)
    store.put("batches", "b", batch)
    store.put("measurements", "b", observed)
    store.put("configuration", "provided_initial_evidence", {"observations": parent(1)})
    store.configure_budget({"experimental_wells": 2})
    store.reserve("lab-b", "experimental_wells", 2, batch)
    store.settle("lab-b")
    before = (store.get("campaign", "state"), store.events(), store.usage())
    result = audit.summarize(tmp_path, top_ns=(2,), query_bin_size=1)
    assert result["rounds"][0]["trial"] == historical_trial
    assert result["gate_policy_as_recorded"] == state["gate"]
    assert result["rounds"][0]["arm_metrics"]["challenger"]["metrics"]["top2mean"] is None
    assert result["campaign_metrics"]["metrics"]["top2mean"] == 3
    assert result["campaign_metrics"]["parent"]["unique_valid_sequences"] == 1
    assert before == (store.get("campaign", "state"), store.events(), store.usage())
    assert store.get("campaign", "state").get("execution_semantics") is None


def test_old_comparator_report_only_never_opens_oracle_or_backfills(tmp_path, monkeypatch):
    store = Store(tmp_path)
    batch, observed = panel("b", [("AC", "baseline", 2, "valid"), ("AD", "baseline", None, "unavailable")])
    frozen = {"schema_version": 1, "task": {"metric": "fitness", "unit": "fixture", "reference_sequence": "AA"},
              "sequences": ["AC", "AD"]}
    old_report = {"provided_parent": 1, "best_measured_value": 2, "curve": [{"round": 1}]}
    store.put("baseline", "plan", frozen, immutable=True)
    store.put("baseline", "report", old_report)
    store.put("batches", "b", batch)
    store.put("measurements", "b", observed)
    store.configure_budget({"experimental_wells": 2})
    store.reserve("b", "experimental_wells", 2, batch)
    store.settle("b")
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only reporting must not open the oracle")
    monkeypatch.setattr(baseline, "load_replay", forbidden)
    result = baseline.summarize(tmp_path, top_ns=(2,), query_bin_size=1)
    assert result["campaign_metrics"]["metrics"]["top2mean"] == 1.5
    assert result["round_metrics"][0]["round_metrics"]["metrics"]["top2mean"] is None
    assert result["equal_query_curve"][-1]["campaign_metrics"]["denominators"]["unavailable"] == 1
    assert store.get("baseline", "plan") == frozen
    assert store.get("baseline", "report") == old_report
    assert not (tmp_path / "report.json").exists()


def test_comparator_and_campaign_have_identical_multimetric_semantics():
    batch, observed = panel("b", [("AC", "baseline", 2, "valid"), ("AC", "baseline", 4, "valid"),
                                ("AD", "baseline", 1, "valid"), ("AE", "baseline", None, "unavailable")])
    task = {"metric": "fitness", "unit": "fixture", "reference_sequence": "AA", "direction": "minimize"}
    ledger = [charge("b", 4)]
    result = baseline.metric_report({"task": task}, {"b": batch}, {"b": observed}, ledger, 10,
                                    top_ns=(2,), query_bin_size=2)
    expected = charged_metrics({"b": batch}, {"b": observed}, ledger, initial=parent(10), reference="AA",
                               direction="minimize", top_ns=(2,), query_bin_size=2)
    assert result["campaign_metrics"] == expected["campaign_metrics"]
    assert result["equal_query_curve"] == expected["equal_query_curve"]
    assert result["campaign_metrics"]["metrics"] == {"best": 1, "top2mean": 2, "avg": 14 / 3}


def test_primary_campaign_report_exposes_same_metrics_without_mutation(campaign, oracle):
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="pytest")
    campaign.ingest(oracle.measure(batch))
    before = (campaign.state, campaign.store.events(), campaign.store.usage())
    report = campaign.report()
    read_only = audit.summarize(campaign.store.root)
    assert report["metric_display_names"]["best"] == "max"
    assert report["campaign_metrics"] == read_only["campaign_metrics"]
    assert report["equal_query_curve"] == read_only["equal_query_curve"]
    assert report["round_progress"][0]["round_metrics"] == read_only["rounds"][0]["round_metrics"]
    assert report["round_progress"][0]["arm_metrics"] == read_only["rounds"][0]["arm_metrics"]
    assert before == (campaign.state, campaign.store.events(), campaign.store.usage())


def test_reporting_configuration_validation_precedes_oracle_access(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid reporting arguments must not open the oracle")
    monkeypatch.setattr(baseline, "load_replay", forbidden)
    with pytest.raises(ValueError, match="top_ns"):
        baseline.run(tmp_path / "not-needed", "synthetic", tmp_path / "run",
                     rounds=1, batch_size=2, policy="uniform", seed=17, top_ns=(5, 5))
    assert not (tmp_path / "run").exists()
