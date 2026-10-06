"""Read-only reporting tests with invented observations, never real landscapes."""
import importlib.util
from pathlib import Path

import pytest

from proteinrsi.contracts import digest
from proteinrsi.storage import Store

spec = importlib.util.spec_from_file_location("round_audit",
    Path(__file__).parents[1] / "scripts" / "summarize_replay_study.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def fixture(tmp_path):
    store = Store(tmp_path)
    artifact = {"predictions": [{"sequence": "AC", "predicted_value": 2.0, "uncertainty": None},
                                {"sequence": "AD", "predicted_value": 4.0, "uncertainty": None}],
                "evidence_kind": "proxy", "status": "predicted", "evidence_version": "e0",
                "workflow": "w0", "metric": "fitness", "unit": "fixture"}
    key = digest(artifact)
    store.put("task_predictions", key, artifact, immutable=True)
    ref = "task_predictions/" + key
    samples = [
        {"sample_id": "s0", "arm": "research", "workflow_version": "w0", "candidate": {
            "sequence": "AC", "predicted_value": 2.0, "prediction_ref": ref,
            "uncertainty": None, "evidence_kind": "proxy"}},
        {"sample_id": "s1", "arm": "research", "workflow_version": "w0", "candidate": {
            "sequence": "AE", "predicted_value": None, "prediction_ref": None,
            "uncertainty": None, "evidence_kind": "none"}},
        {"sample_id": "s2", "arm": "research", "workflow_version": "w0", "candidate": {
            "sequence": "AD", "predicted_value": 4.0, "prediction_ref": ref,
            "uncertainty": None, "evidence_kind": "proxy"}},
    ]
    store.put("batches", "b0", {"batch_id": "b0", "round_index": 0, "evidence_version": "e0",
                                "samples": samples})
    store.put("measurements", "b0", [{"sample_id": "s0", "sequence": "AC", "qc": "valid", "value": 3.0},
        {"sample_id": "s1", "sequence": "AE", "qc": "valid", "value": 1.0},
        {"sample_id": "s2", "sequence": "AD", "qc": "unavailable", "value": None}])
    store.put("campaign", "state", {"campaign_id": "fixture", "status": "complete", "round_index": 1,
        "task": {"metric": "fitness", "unit": "fixture"}, "history": [{"batch_id": "b0"}]})
    store.put("research_runs", "r0", {"run_id": "r0", "evidence_version": "e0", "round": 0,
        "workflow_version": "w0", "status": "complete", "completed": ["design", "rank"],
        "plan": {"steps": [{"step_id": "design", "operation": "agent:propose"},
                            {"step_id": "rank", "operation": "agent:rank"}]}})
    store.configure_budget({"experimental_wells": 3})
    store.reserve("b0", "experimental_wells", 3, {})
    store.settle("b0")
    store.event("tool_completed", {"tool": "research_fit_predict", "key": "j0"})
    store.event("validation_event", {"branch": "challenger", "kind": "tool_completed",
        "payload": {"tool": "research_evidence_summary", "key": "j1"}})
    store.event("batch_prepared", {"batch_id": "b0"})
    return store


def test_correct_denominator_and_pipeline_without_writes(tmp_path):
    store = fixture(tmp_path)
    state, charges, events = store.get("campaign", "state"), store.usage(), store.events()
    result = audit.summarize(tmp_path)
    row = result["rounds"][0]
    assert row["submitted"] == 3 and row["valid"] == 2 and row["unavailable"] == 1
    assert row["frozen_numeric_predictions"] == row["verified_prediction_refs"] == 2
    assert row["valid_with_prediction"] == row["valid_without_prediction"] == 1
    assert row["unavailable_with_prediction"] == 1
    assert row["frozen_valid_prediction_error_n"] == 1
    assert row["frozen_valid_prediction_mae"] == 1.0
    assert row["pipelines"][0]["completed_operations"] == ["agent:propose", "agent:rank"]
    assert len(row["tool_executions_since_previous_prepared_batch"]) == 2
    assert (store.get("campaign", "state"), store.usage(), store.events()) == (state, charges, events)


def test_tampered_frozen_value_not_silently_reported(tmp_path):
    store = fixture(tmp_path)
    batch = store.get("batches", "b0")
    batch["samples"][0]["candidate"]["predicted_value"] = 123.0
    store.put("batches", "b0", batch)
    with pytest.raises(ValueError, match="differs from protected artifact"):
        audit.summarize(tmp_path)


def test_missing_state_never_creates_a_campaign(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        audit.summarize(tmp_path)
    assert list(tmp_path.iterdir()) == []
