"""Synthetic fixture inputs only; no bundled experimental labels."""
import csv
import json

import pytest

from proteinrsi.goal import available_landscapes, load_replay
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.ssmula import LANDSCAPES, prepare


def fixture(tmp_path, monkeypatch, rows=None):
    source = tmp_path / "data" / "GB1"
    (source / "fitness_landscape").mkdir(parents=True)
    fasta = source / "GB1.fasta"
    fasta.write_text(">synthetic test parent\nACDE\n")
    table = source / "fitness_landscape" / "GB1.csv"
    with table.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Variants", "Fitness"])
        writer.writerows(rows if rows is not None else [["AC", 1], ["CC", 4321.25]])
    monkeypatch.setitem(LANDSCAPES, "GB1", {"positions": [1, 2], "parent_sites": "AC",
        "length": 4, "sequence_column": "Variants", "value_column": "Fitness",
        "csv_sha256": file_sha256(table), "fasta_sha256": file_sha256(fasta), "unit": "test-only"})
    return table


def test_prepared_inputs_load_and_expose_only_parent(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    result = prepare(tmp_path, "GB1")
    assert result["rows"] == 2 and result["labels_printed"] is False
    assert "4321" not in json.dumps(result)
    catalog = available_landscapes(tmp_path)
    assert catalog["GB1"]["reference_sequence"] == "ACDE"
    assert "4321" not in json.dumps(catalog)
    raw, dataset = load_replay(tmp_path, "GB1")
    assert raw["candidates"] == [] and raw["candidate_access"] == "open"
    assert raw["initial_parent_measurement"]["value"] == 1
    assert "4321" not in json.dumps(raw)
    with dataset.open() as handle:
        rows = list(csv.DictReader(handle))
    assert float(rows[1]["value"]) == 4321.25  # unchanged original units
    assert rows[1]["sequence"] == "CCDE"
    with pytest.raises(FileExistsError):
        prepare(tmp_path, "GB1")


def test_changed_source_rejected_before_preparing(tmp_path, monkeypatch):
    source = fixture(tmp_path, monkeypatch)
    source.write_text(source.read_text() + "DC,2\n")
    with pytest.raises(ValueError, match="SHA256"):
        prepare(tmp_path, "GB1")
    assert not (tmp_path / "processed").exists()


@pytest.mark.parametrize("rows", [
    [["AC", 1], ["AC", 2]], [["AC", 1], ["*C", 2]],
    [["AC", 1], ["CC", "nan"]], [["AC", 1], ["CC", ""]], [["CC", 2]],
])
def test_invalid_source_never_imputed_filtered_or_aggregated(tmp_path, monkeypatch, rows):
    fixture(tmp_path, monkeypatch, rows)
    with pytest.raises(ValueError):
        prepare(tmp_path, "GB1")
    assert not (tmp_path / "processed").exists()


def test_parent_metadata_mismatch_rejected(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    LANDSCAPES["GB1"]["parent_sites"] = "CC"
    with pytest.raises(ValueError, match="parent sequence"):
        prepare(tmp_path, "GB1")
