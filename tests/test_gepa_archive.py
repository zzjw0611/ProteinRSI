"""Offline GEPA result-contract mocks; no optimizer/provider efficacy claim."""
import sys
from types import SimpleNamespace

import pytest

from proteinrsi.contracts import Workflow
from proteinrsi.integrations.gepa import ProteinWorkflowAdapter, encode_workflow, optimize_workflow
from proteinrsi.storage import Store


def result():
    first = encode_workflow(Workflow())
    second = encode_workflow(Workflow(exploration=0.4))
    core = {"candidates": [first, second], "parents": [[None], [0]],
            "val_aggregate_scores": [0.1, 0.2], "val_subscores": [{"v": 0.1}, {"v": 0.2}]}
    return SimpleNamespace(best_candidate=second, to_dict=lambda: core,
        eval_log=[{"case_id": "v", "score": 0.2}], metadata={"total_metric_calls": 2})


def test_full_pool_lineage_and_runtime_logs_are_persisted(tmp_path, monkeypatch):
    upstream = result()
    monkeypatch.setitem(sys.modules, "gepa", SimpleNamespace(optimize=lambda **kwargs: upstream))
    store = Store(tmp_path)
    adapter = ProteinWorkflowAdapter(lambda case, workflow: (0, {}), store=store)
    best = optimize_workflow(Workflow(), adapter, [{"case_id": "t"}], [{"case_id": "v"}], reflection_lm=object())
    assert best.exploration == 0.4 and adapter.last_result is upstream
    record = store.get("gepa_results", adapter.last_archive_ref)
    assert len(record["result"]["candidates"]) == 2
    assert record["result"]["parents"] == [[None], [0]]
    assert record["eval_log"] == upstream.eval_log and record["metadata"] == upstream.metadata
    assert record["publication_authority"] is False
    assert store.get("campaign", "state") is None
    assert store.get("gepa_attempts", adapter.last_archive_ref)["state"] == "completed"


def test_existing_in_memory_api_keeps_result_available(monkeypatch):
    upstream = result()
    monkeypatch.setitem(sys.modules, "gepa", SimpleNamespace(optimize=lambda **kwargs: upstream))
    adapter = ProteinWorkflowAdapter(lambda case, workflow: (0, {}))
    best = optimize_workflow(Workflow(), adapter, [{"case_id": "t"}], [{"case_id": "v"}], reflection_lm=object())
    assert isinstance(best, Workflow) and adapter.last_result is upstream
    assert adapter.last_archive_ref is None


def test_optimizer_failure_is_preserved_without_a_best_candidate(tmp_path, monkeypatch):
    def fail(**kwargs):
        raise ValueError("Synthetic optimization failure")
    monkeypatch.setitem(sys.modules, "gepa", SimpleNamespace(optimize=fail))
    store = Store(tmp_path)
    adapter = ProteinWorkflowAdapter(lambda case, workflow: (0, {}), store=store)
    with pytest.raises(ValueError):
        optimize_workflow(Workflow(), adapter, [{"case_id": "t"}], [{"case_id": "v"}], reflection_lm=object())
    assert next(iter(store.all("gepa_attempts").values()))["state"] == "failed"
    assert next(iter(store.all("gepa_failures").values()))["error_type"] == "ValueError"
    assert not store.all("gepa_results")


def test_non_json_audit_is_not_silently_dropped(tmp_path, monkeypatch):
    upstream = result()
    upstream.metadata = {"not_serializable": object()}
    monkeypatch.setitem(sys.modules, "gepa", SimpleNamespace(optimize=lambda **kwargs: upstream))
    store = Store(tmp_path)
    adapter = ProteinWorkflowAdapter(lambda case, workflow: (0, {}), store=store)
    with pytest.raises(TypeError):
        optimize_workflow(Workflow(), adapter, [{"case_id": "t"}], [{"case_id": "v"}], reflection_lm=object())
    assert not store.all("gepa_results")
    assert store.all("gepa_failures")
