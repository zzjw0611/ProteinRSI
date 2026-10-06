"""Nonadaptive comparator tests using fabricated scores only."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

from proteinrsi.contracts import TaskSpec
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.storage import Conflict, Store

spec = importlib.util.spec_from_file_location("baseline_script",
    Path(__file__).parents[1] / "scripts" / "replay_baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def prepared(tmp_path):
    directory = tmp_path / "processed" / "fixture"
    directory.mkdir(parents=True)
    task = TaskSpec(name="Synthetic comparator fixture", reference_sequence="ACDE",
        mutable_positions=[1, 2], max_mutations=2, controls_per_batch=0,
        candidate_access="open", feedback_source="measured_replay")
    (directory / "task.json").write_text(task.model_dump_json())
    data = directory / "measurements.csv"
    with data.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sequence", "value", "qc", "source", "label_kind"])
        writer.writeheader()
        writer.writerow({"sequence": "ACDE", "value": 1.0, "qc": "valid",
            "source": "measured_replay", "label_kind": "reported_experimental_assay_score"})
    (directory / "provenance.json").write_text(json.dumps({
        "status": "ready_strict_measured_replay", "landscape": "fixture",
        "parent": {"sequence": "ACDE"},
        "prepared_assets": {"measurements_csv_sha256": file_sha256(data)}}))
    return task


@pytest.mark.parametrize("policy", ["uniform", "single-first"])
def test_proposals_label_free_unique_reproducible(tmp_path, policy):
    task = prepared(tmp_path)
    selected = baseline.proposals(task, 40, 17, policy)
    assert selected == baseline.proposals(task, 40, 17, policy)
    assert len(set(selected)) == 40 and task.reference_sequence not in selected
    assert all(sequence[2:] == "DE" for sequence in selected)
    if policy == "single-first":
        assert all(sum(a != b for a, b in zip(sequence, task.reference_sequence)) == 1
                   for sequence in selected[:38])


def test_missing_queries_charged_without_resampling_and_restart_idempotent(tmp_path):
    prepared(tmp_path / "data")
    kwargs = dict(rounds=2, batch_size=4, seed=17, policy="uniform")
    first = baseline.run(tmp_path / "data", "fixture", tmp_path / "run", **kwargs)
    second = baseline.run(tmp_path / "data", "fixture", tmp_path / "run", **kwargs)
    assert first == second
    assert first["queries"] == 8 and first["repeat_queries"] == 0
    assert first["budget"]["experimental_wells"] == {"limit": 8, "committed": 8, "reserved": 0}
    assert first["curve"][-1]["unavailable_queries"] == 8
    assert first["best_measured_value"] == 1.0
    with pytest.raises(Conflict):
        baseline.run(tmp_path / "data", "fixture", tmp_path / "run", **{**kwargs, "seed": 18})


def test_interrupted_measurement_resumes_original_charge(tmp_path, monkeypatch):
    prepared(tmp_path / "data")
    original = baseline.CSVOracle.measure
    def interrupt(self, batch):
        raise RuntimeError("Synthetic interruption after committed reservation")
    monkeypatch.setattr(baseline.CSVOracle, "measure", interrupt)
    kwargs = dict(rounds=2, batch_size=4, seed=17, policy="single-first")
    with pytest.raises(RuntimeError):
        baseline.run(tmp_path / "data", "fixture", tmp_path / "run", **kwargs)
    assert Store(tmp_path / "run").usage()["experimental_wells"]["committed"] == 4
    monkeypatch.setattr(baseline.CSVOracle, "measure", original)
    report = baseline.run(tmp_path / "data", "fixture", tmp_path / "run", **kwargs)
    assert report["budget"]["experimental_wells"]["committed"] == 8


def test_proposal_order_persisted_before_loading_labels(tmp_path, monkeypatch):
    prepared(tmp_path / "data")
    def observe_order(root, landscape):
        store = Store(tmp_path / "run")
        assert len(store.get("baseline", "plan")["sequences"]) == 8
        assert store.usage()["experimental_wells"]["committed"] == 0
        raise RuntimeError("Stop before labels are loaded")
    monkeypatch.setattr(baseline, "load_replay", observe_order)
    with pytest.raises(RuntimeError, match="before labels"):
        baseline.run(tmp_path / "data", "fixture", tmp_path / "run",
                     rounds=2, batch_size=4, seed=17, policy="uniform")
