"""Synthetic fixtures only. Real scores/data are never packaged or printed."""
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import pytest

from proteinrsi.goal import available_landscapes, load_replay
from proteinrsi.localtools.artifacts import file_sha256


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_ssmula_landscapes.py"
spec = importlib.util.spec_from_file_location("prepare_ssmula_landscapes", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def fixture(tmp_path, monkeypatch, rows=None, *, family="TrpB", name="TrpB3A"):
    """Only tests can replace the manifest pin; the CLI has no override flag."""
    original = adapter.load_manifest()
    root = tmp_path / "external-data"
    directory = root / "data" / family
    (directory / "fitness_landscape").mkdir(parents=True)
    fasta = directory / f"{family}.fasta"
    fasta.write_text(">synthetic parent only\nACDE\n")
    source = directory / "fitness_landscape" / f"{name}.csv"
    header = ["AAs", "AA1", "AA2", "fitness", "active"] if family == "TrpB" else ["AAs", "fitness"]
    values = rows if rows is not None else [
        ["CC", "987654.12300"], ["A*", "-4321.125"], ["AC", "0.12500"], ["CA", "-765.43200"]]
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for aa, value in values:
            writer.writerow([aa, *(list(aa[:2]) + [value, "PRIVATE_ACTIVE_SENTINEL"]
                                  if family == "TrpB" else [value])])
    manifest = {**original, "landscapes": {name: {
        **original["landscapes"]["TrpB3A"], "family": family,
        "source_csv": str(source.relative_to(root)), "source_csv_sha256": file_sha256(source),
        "source_fasta": str(fasta.relative_to(root)), "source_fasta_sha256": file_sha256(fasta),
        "parent_sequence_sha256": hashlib.sha256(b"ACDE").hexdigest(),
        "parent_length": 4, "positions": [1, 2], "parent_sites": "AC",
        "sequence_column": "AAs", "value_column": "fitness", "csv_header": header,
        "stop_policy": "exclude_stop_tuples_only" if family == "TrpB" else "reject",
        "expected_counts": {"source_rows": len(values),
            "excluded_stop_rows": sum("*" in aa for aa, _ in values),
            "prepared_rows": sum("*" not in aa for aa, _ in values)},
    }, "DHFR": {**original["landscapes"]["DHFR"], "status": "quarantined",
        "quarantine_reasons": original["landscapes"]["DHFR"].get("previous_quarantine_reasons",
            original["landscapes"]["DHFR"]["quarantine_reasons"])}, "TEV": {
        **original["landscapes"]["TEV"], "status": "quarantined",
        "quarantine_reasons": original["landscapes"]["TEV"]["previous_quarantine_reasons"],
    }}}
    path = tmp_path / "synthetic-pins.json"
    monkeypatch.setattr(adapter, "MANIFEST_PATH", path)
    repin(monkeypatch, manifest)
    return root, source, manifest


def repin(monkeypatch, manifest):
    adapter.MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    monkeypatch.setattr(adapter, "PINNED_MANIFEST_SHA256", file_sha256(adapter.MANIFEST_PATH))


def test_reviewed_manifest_has_exact_scope_and_hard_bounds():
    manifest = adapter.load_manifest()
    eligible = [name for name, spec in manifest["landscapes"].items() if spec["status"] == "eligible"]
    assert eligible == ["DHFR", "GB1", "ParD2", "ParD3", "T7", "TEV", *[f"TrpB3{x}" for x in "ABCDEFGHI"], "TrpB4"]
    defaults = manifest["launch_defaults"]
    assert defaults["max_rounds"] == 20 and defaults["batch_size"] == 100
    assert defaults["experimental_wells"] == 2000
    assert defaults["llm_calls"] == 600 and defaults["tool_calls"] == 400
    assert defaults["seed"] == 17 and defaults["parent_query_cost"] == 0
    dhfr = manifest["landscapes"]["DHFR"]
    assert "mean(exp(source fitness))" in " ".join(dhfr["previous_quarantine_reasons"])
    assert dhfr["label_kind"] == "reviewed_experimental_score_aggregate"
    assert dhfr["parent_length"] == 159
    assert dhfr["expected_counts"]["parent_synonymous_codon_rows"] == 42
    tev = manifest["landscapes"]["TEV"]
    assert tev["parent_length"] == 233 and tev["positions"] == [143, 145, 164, 167]
    assert tev["assay_construct"]["bundled_reference"]["source_fasta_length"] == 236
    assert "233" in " ".join(tev["previous_quarantine_reasons"])


def test_exact_scores_sequence_only_exclusions_and_no_catalogue(tmp_path, monkeypatch, capsys):
    root, _, _ = fixture(tmp_path, monkeypatch)
    result = adapter.prepare(root, "TrpB3A")
    assert result["source_rows"] == 4 and result["prepared_rows"] == 3
    assert result["excluded_stop_rows"] == 1
    directory = root / "processed/TrpB3A"
    raw = json.loads((directory / "task.json").read_text())
    assert raw["candidates"] == [] and raw["candidate_access"] == "open"
    assert raw["initial_parent_measurement"] is None
    assert raw["initial_observation_policy"] == "none"
    assert raw["max_rounds"] == 20 and raw["batch_size"] == 100
    assert raw["budget"] == {"experimental_wells": 2000, "llm_calls": 600, "tool_calls": 400}
    assert raw["batch_fill_policy"] == "full_plate"
    with (directory / "measurements.csv").open() as handle:
        records = list(csv.DictReader(handle))
    assert [r["sequence"] for r in records] == ["ACDE", "CADE", "CCDE"]
    assert [r["value"] for r in records] == ["0.12500", "-765.43200", "987654.12300"]
    assert "active" not in records[0]
    catalog = available_landscapes(root)
    loaded, _ = load_replay(root, "TrpB3A")
    assert loaded["initial_parent_measurement"]["value"] == 0.125
    assert loaded["candidate_access"] == "open" and not loaded["candidates"]
    inventory = adapter.launch_inventory(root, verified={"TrpB3A": result})
    assert inventory["prepared_verified_count"] == 1
    assert inventory["quarantined_count"] == 2
    assert not inventory["scientific_replay_executed"]
    public = json.dumps([result, raw, catalog, loaded, inventory]) + (directory / "provenance.json").read_text()
    assert "987654" not in public and "765.432" not in public
    assert "PRIVATE_ACTIVE_SENTINEL" not in public
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("rows,reason", [
    ([["AC", "1"], ["AC", "2"]], "Duplicate"),
    ([["AC", "1"], ["A*", "2"], ["A*", "3"]], "Duplicate"),
    ([["AC", "1"], ["XC", "2"]], "symbols"),
    ([["AC", "1"], ["X*", "2"]], "symbols"),
    ([["AC", "1"], ["aC", "2"]], "symbols"),
    ([["AC", "1"], ["CC", "NaN"]], "Nonfinite"),
    ([["AC", "1"], ["CC", "inf"]], "Nonfinite"),
    ([["AC", "1"], ["CC", ""]], "unparseable"),
    ([["AC", "1"], ["CC", "PRIVATE_UNPARSEABLE_SENTINEL"]], "unparseable"),
    ([["AC", "1"], ["A*", "nan"]], "Nonfinite"),
    ([["CC", "2"]], "parent"),
])
def test_invalid_rows_fail_closed_without_printing_values(tmp_path, monkeypatch, rows, reason):
    root, _, _ = fixture(tmp_path, monkeypatch, rows)
    with pytest.raises(adapter.PreparationError, match=reason) as error:
        adapter.prepare(root, "TrpB3A")
    assert "PRIVATE_UNPARSEABLE_SENTINEL" not in str(error.value)
    assert not (root / "processed").exists()


def test_non_trpb_stop_is_rejected_and_negative_t7_score_unchanged(tmp_path, monkeypatch):
    root, source, manifest = fixture(tmp_path, monkeypatch, [["AC", "1"], ["A*", "2"]], family="T7", name="T7")
    with pytest.raises(adapter.PreparationError, match="Noncanonical"):
        adapter.prepare(root, "T7")
    source.write_text("AAs,fitness\nAC,1.000\nCC,-987.65400\n")
    spec = manifest["landscapes"]["T7"]
    spec["source_csv_sha256"] = file_sha256(source)
    spec["expected_counts"] = {"source_rows": 2, "excluded_stop_rows": 0, "prepared_rows": 2}
    repin(monkeypatch, manifest)
    adapter.prepare(root, "T7")
    with (root / "processed/T7/measurements.csv").open() as handle:
        assert list(csv.DictReader(handle))[1]["value"] == "-987.65400"


@pytest.mark.parametrize("change", ["source", "fasta", "manifest"])
def test_changed_source_and_manifest_pins_rejected(tmp_path, monkeypatch, change):
    root, source, manifest = fixture(tmp_path, monkeypatch)
    path = {"source": source, "fasta": root / manifest["landscapes"]["TrpB3A"]["source_fasta"],
            "manifest": adapter.MANIFEST_PATH}[change]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(adapter.PreparationError, match="SHA256"):
        adapter.prepare(root, "TrpB3A")
    assert not (root / "processed").exists()


@pytest.mark.parametrize("field,value", [("parent_sites", "CC"), ("positions", [1, 1]),
                                        ("positions", [1, 9]), ("parent_length", 7)])
def test_parent_metadata_mismatch_rejected(tmp_path, monkeypatch, field, value):
    root, _, manifest = fixture(tmp_path, monkeypatch)
    manifest["landscapes"]["TrpB3A"][field] = value
    repin(monkeypatch, manifest)
    with pytest.raises(adapter.PreparationError, match="parent sequence"):
        adapter.prepare(root, "TrpB3A")


@pytest.mark.parametrize("row", ["AC,A,C,1", "AC,A,C,1,ignored,extra", "AC,C,A,1,ignored"])
def test_malformed_and_contradictory_rows_fail(tmp_path, monkeypatch, row):
    root, source, manifest = fixture(tmp_path, monkeypatch)
    source.write_text("AAs,AA1,AA2,fitness,active\n" + row + "\n")
    manifest["landscapes"]["TrpB3A"]["source_csv_sha256"] = file_sha256(source)
    repin(monkeypatch, manifest)
    with pytest.raises(adapter.PreparationError):
        adapter.prepare(root, "TrpB3A")
    assert not (root / "processed").exists()


def test_existing_inputs_are_verified_never_overwritten(tmp_path, monkeypatch):
    root, _, _ = fixture(tmp_path, monkeypatch)
    adapter.prepare(root, "TrpB3A")
    directory = root / "processed/TrpB3A"
    before = {p.name: (file_sha256(p), p.stat().st_mtime_ns) for p in directory.iterdir()}
    with pytest.raises(FileExistsError):
        adapter.prepare(root, "TrpB3A")
    adapter.verify(root, "TrpB3A")
    assert adapter.main(["--data-root", str(root), "--all"]) == 0
    assert before == {p.name: (file_sha256(p), p.stat().st_mtime_ns) for p in directory.iterdir()}


@pytest.mark.parametrize("target", ["measurements.csv", "task.json", "provenance.json"])
def test_tampered_preparation_rejected(tmp_path, monkeypatch, target):
    root, _, _ = fixture(tmp_path, monkeypatch)
    adapter.prepare(root, "TrpB3A")
    path = root / "processed/TrpB3A" / target
    if target == "measurements.csv":
        path.write_text(path.read_text().replace("987654.12300", "876543.21000"))
    elif target == "task.json":
        raw = json.loads(path.read_text())
        raw["max_rounds"] = 3
        path.write_text(json.dumps(raw))
    else:
        raw = json.loads(path.read_text())
        raw["sequence_eligibility"]["excluded_stop_rows"] = 0
        path.write_text(json.dumps(raw))
    with pytest.raises(adapter.PreparationError):
        adapter.verify(root, "TrpB3A")


def test_quarantine_blocks_before_source_read_or_partial_preparation(tmp_path, monkeypatch, capsys):
    root, _, _ = fixture(tmp_path, monkeypatch)
    assert adapter.main(["--data-root", str(root), "--landscape", "TrpB3A", "TEV"]) == 1
    assert not (root / "processed").exists()
    assert "Quarantined" in capsys.readouterr().out
    with pytest.raises(adapter.PreparationError, match="Quarantined"):
        adapter.prepare(root, "DHFR")


def test_cli_metadata_inventory_and_safe_parse_errors(tmp_path, monkeypatch, capsys):
    root, _, _ = fixture(tmp_path, monkeypatch)
    path = tmp_path / "launch.json"
    assert adapter.main(["--data-root", str(root), "--all", "--launch-inventory", str(path)]) == 0
    output = capsys.readouterr().out
    assert "987654" not in output and "PRIVATE_ACTIVE" not in output
    assert json.loads(path.read_text())["prepared_verified_count"] == 1
    assert adapter.main(["--data-root", str(root), "--all", "--verify-only", "--launch-inventory", str(path)]) == 1
    assert "failed_closed" in capsys.readouterr().out


def tev_fixture(tmp_path, monkeypatch):
    root, source, manifest = fixture(tmp_path, monkeypatch, [["AC", "1"], ["CC", "-987.65400"]],
                                     family="T7", name="T7")
    spec = manifest["landscapes"].pop("T7")
    manifest["landscapes"]["TEV"] = spec
    spec.update(parent_sites="MC", complete_sequence_column="Complete Sequence",
                csv_header=["AAs", "fitness", "Complete Sequence"],
                parent_sequence_sha256=hashlib.sha256(b"MCDE").hexdigest())
    fasta = root / spec["source_fasta"]
    fasta.write_text(">synthetic primary construct\nMCDE\n")
    spec["source_fasta_sha256"] = file_sha256(fasta)
    source.write_text("AAs,fitness,Complete Sequence\nMC,1.000,MCDE\nCC,-987.65400,CCDE\n")
    spec["source_csv_sha256"] = file_sha256(source)
    supplied = source.parent / "bundled.fasta"
    supplied.write_text(">synthetic bundled reference\nGESLCDE\n")
    masked = source.parent / "masked.fasta"
    masked.write_text(">synthetic primary mask\nXXDE\n")
    spec["assay_construct"] = {
        "review_status": "resolved_original_assay_construct",
        "masked_fasta": str(masked.relative_to(root)), "masked_fasta_sha256": file_sha256(masked),
        "bundled_reference": {"source_fasta": str(supplied.relative_to(root)),
            "source_fasta_sha256": file_sha256(supplied), "source_fasta_length": 7,
            "source_fasta_sequence_sha256": hashlib.sha256(b"GESLCDE").hexdigest(),
            "mutable_positions": [4, 5]},
    }
    repin(monkeypatch, manifest)
    return root, source, manifest


def test_tev_primary_measured_construct_and_exact_full_sequence_validation(tmp_path, monkeypatch):
    root, _, _ = tev_fixture(tmp_path, monkeypatch)
    result = adapter.prepare(root, "TEV")
    assert result["prepared_rows"] == 2
    loaded, dataset = load_replay(root, "TEV")
    assert loaded["reference_sequence"] == "MCDE"
    with dataset.open() as handle:
        assert {r["sequence"]: r["value"] for r in csv.DictReader(handle)} == {
            "MCDE": "1.000", "CCDE": "-987.65400"}


@pytest.mark.parametrize("change", ["complete", "mask", "bundled", "numbering"])
def test_tev_construct_mismatch_fails_without_implicit_repair(tmp_path, monkeypatch, change):
    root, source, manifest = tev_fixture(tmp_path, monkeypatch)
    spec = manifest["landscapes"]["TEV"]
    construct = spec["assay_construct"]
    if change == "complete":
        source.write_text(source.read_text().replace(",CCDE", ",CCDF"))
        spec["source_csv_sha256"] = file_sha256(source)
    elif change == "mask":
        path = root / construct["masked_fasta"]
        path.write_text(">wrong synthetic mask\nMXDE\n")
        construct["masked_fasta_sha256"] = file_sha256(path)
    elif change == "bundled":
        bundled = construct["bundled_reference"]
        path = root / bundled["source_fasta"]
        path.write_text(">wrong synthetic background\nGESLCDF\n")
        bundled["source_fasta_sha256"] = file_sha256(path)
        bundled["source_fasta_sequence_sha256"] = hashlib.sha256(b"GESLCDF").hexdigest()
    else:
        construct["bundled_reference"]["mutable_positions"] = [1, 2]
    repin(monkeypatch, manifest)
    with pytest.raises(adapter.PreparationError):
        adapter.prepare(root, "TEV")
    assert not (root / "processed").exists()


def dhfr_fixture(tmp_path, monkeypatch, rows=None):
    root, source, manifest = fixture(tmp_path, monkeypatch, [["AC", "1"]], family="T7", name="T7")
    spec = manifest["landscapes"].pop("T7")
    manifest["landscapes"]["DHFR"] = spec
    values = rows if rows is not None else [
        ["GCCGATCTC", "0"], ["GCTGATCTC", repr(math.log(9))],
        ["GCCGATTAA", "-765432.125"], ["TGCGATCTC", repr(math.log(3))]]
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["seq", "fitness"])
        writer.writerows(values)
    spec.update(family="DHFR", parent_sites="ADL", positions=[1, 2, 3],
        parent_sequence_sha256=hashlib.sha256(b"ADLA").hexdigest(),
        sequence_column="seq", csv_header=["seq", "fitness"], source_csv_sha256=file_sha256(source),
        label_kind="reviewed_experimental_score_aggregate", metric="mean_exponentiated_source_fitness",
        unit="mean_exp_original_source_fitness", stop_policy="exclude_stop_translations_only",
        assay_protocol="ssmula-zenodo-15203754-DHFR-canonical159-mean-exp-aa-v1",
        reference_caveat="Synthetic canonical-reference reconstruction, not measured full assay sequence",
        score_semantics={"transform": "mean_of_natural_exp_over_observed_synonymous_codons",
            "reference_scope": "canonical_reference_reconstruction_not_literal_full_assay_sequence"},
        expected_counts={"source_rows": 4, "excluded_stop_rows": 1, "prepared_rows": 2,
            "observed_synonymous_codon_rows": 3, "parent_synonymous_codon_rows": 2})
    fasta = root / spec["source_fasta"]
    fasta.write_text(">synthetic canonical reference\nADLA\n")
    spec["source_fasta_sha256"] = file_sha256(fasta)
    fragment = source.parent / "fragment.fasta"
    fragment.write_text(">synthetic DNA fragment\nGCCGATCTCGCC\n")
    pdb = source.parent / "reference.pdb"
    pdb.write_text("DBREF  6XG5 A 1 4 UNP P0ABQ4 DYR_ECOLI 1 4\n" +
        "".join(f"SEQADV 6XG5 HIS A {i} UNP P0ABQ4 EXPRESSION TAG\n" for i in range(5, 11)) +
        "SEQRES 1 A 10 ALA ASP LEU ALA HIS HIS HIS HIS HIS HIS\n")
    spec["aggregate_reference"] = {"fragment_fasta": str(fragment.relative_to(root)),
        "fragment_fasta_sha256": file_sha256(fragment), "fragment_nt_length": 12,
        "fragment_translated_length": 4, "pdb": str(pdb.relative_to(root)),
        "pdb_sha256": file_sha256(pdb), "pdb_id": "6XG5", "chain": "A",
        "canonical_accession": "P0ABQ4", "expression_tag": "HHHHHH"}
    repin(monkeypatch, manifest)
    return root, source, manifest


def test_dhfr_standard_translation_and_mean_of_exp_not_exp_of_mean(tmp_path, monkeypatch):
    assert len(adapter.STANDARD_CODE) == 64
    assert adapter._translate_dna("GCCGATCTC") == "ADL"
    assert adapter._translate_dna("GCTGATCTC") == "ADL"
    assert adapter._translate_dna("GCCGATTAA") == "AD*"
    root, _, _ = dhfr_fixture(tmp_path, monkeypatch)
    result = adapter.prepare(root, "DHFR")
    assert result["prepared_rows"] == 2 and result["parent_synonymous_codon_rows"] == 2
    raw, dataset = load_replay(root, "DHFR")
    assert raw["initial_parent_measurement"]["value"] == pytest.approx(5)
    assert raw["initial_parent_measurement"]["value"] != pytest.approx(3)
    assert "aggregate" in raw["initial_parent_measurement"]["source_ref"]
    assert "canonical-reference" in raw["initial_parent_measurement"]["source_ref"]
    assert raw["candidate_access"] == "open" and not raw["candidates"]
    with dataset.open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r["sequence"] for r in rows] == ["ADLA", "CDLA"]
    assert all(r["label_kind"] == "reviewed_experimental_score_aggregate" for r in rows)
    assert "765432.125" not in json.dumps(result)


@pytest.mark.parametrize("rows,reason", [
    ([["GCCGATCTC", "0"], ["GCCGATCTC", "1"]], "Duplicate"),
    ([["GCCGATCTC", "0"], ["GCCGATTAA", "nan"]], "Nonfinite"),
    ([["GCCGATCTC", "0"], ["GCCGATXXX", "1"]], "DNA"),
    ([["GCCGATCTC", "0"], ["gccgatctc", "1"]], "DNA"),
    ([["GCCGATCTC", "0"], ["GCTGATCTC", "1000"]], "overflow"),
    ([["GCCGATCTC", "0"], ["GCTGATCTC", "-1000"]], "underflow"),
    ([["TGCGATCTC", "0"]], "parent"),
])
def test_dhfr_never_reweights_imputes_or_renormalizes(tmp_path, monkeypatch, rows, reason):
    root, _, _ = dhfr_fixture(tmp_path, monkeypatch, rows)
    with pytest.raises(adapter.PreparationError, match=reason):
        adapter.prepare(root, "DHFR")
    assert not (root / "processed").exists()


@pytest.mark.parametrize("field", ["fragment", "dbref", "tag", "seqres"])
def test_dhfr_reference_requires_fragment_mapping_and_explicit_tag_evidence(tmp_path, monkeypatch, field):
    root, _, manifest = dhfr_fixture(tmp_path, monkeypatch)
    evidence = manifest["landscapes"]["DHFR"]["aggregate_reference"]
    if field == "fragment":
        path = root / evidence["fragment_fasta"]
        path.write_text(path.read_text().replace("GCCGATCTCGCC", "GCCGATCTGTGC"))
        evidence["fragment_fasta_sha256"] = file_sha256(path)
    else:
        path = root / evidence["pdb"]
        changes = {"dbref": ("DYR_ECOLI 1 4", "DYR_ECOLI 2 5"),
                   "tag": ("EXPRESSION TAG", "UNREVIEWED TAG"),
                   "seqres": ("ALA ASP LEU ALA", "ALA ASP LEU CYS")}
        path.write_text(path.read_text().replace(*changes[field]))
        evidence["pdb_sha256"] = file_sha256(path)
    repin(monkeypatch, manifest)
    with pytest.raises(adapter.PreparationError):
        adapter.prepare(root, "DHFR")
    assert not (root / "processed").exists()


@pytest.mark.parametrize("field,value", [
    ("label_kind", "unreviewed_aggregate"), ("label_kind", "reported_experimental_assay_score"),
    ("transform", "exp_of_mean"), ("reference_scope", "literal_measured_full_sequence"),
    ("metric", "fitness"), ("unit", "a.u."), ("assay_protocol", "unreviewed-v1")])
def test_loader_rejects_unreviewed_or_mislabeled_aggregates(tmp_path, monkeypatch, field, value):
    root, _, _ = dhfr_fixture(tmp_path, monkeypatch)
    adapter.prepare(root, "DHFR")
    directory = root / "processed/DHFR"
    name = "task.json" if field in {"metric", "unit", "assay_protocol"} else "provenance.json"
    path = directory / name
    raw = json.loads(path.read_text())
    if field in {"transform", "reference_scope"}:
        raw["score_semantics"][field] = value
    else:
        raw[field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_replay(root, "DHFR")


def test_loader_cannot_reclassify_dhfr_aggregate_as_unchanged_reported_score(tmp_path, monkeypatch):
    root, _, _ = dhfr_fixture(tmp_path, monkeypatch)
    adapter.prepare(root, "DHFR")
    directory = root / "processed/DHFR"
    measurements = directory / "measurements.csv"
    measurements.write_text(measurements.read_text().replace(
        "reviewed_experimental_score_aggregate", "reported_experimental_assay_score"))
    path = directory / "provenance.json"
    raw = json.loads(path.read_text())
    raw["label_kind"] = "reported_experimental_assay_score"
    raw["prepared_assets"]["measurements_csv_sha256"] = file_sha256(measurements)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="requires its reviewed aggregate"):
        load_replay(root, "DHFR")


def test_loader_rejects_malformed_aggregate_semantics(tmp_path, monkeypatch):
    root, _, _ = dhfr_fixture(tmp_path, monkeypatch)
    adapter.prepare(root, "DHFR")
    path = root / "processed/DHFR/provenance.json"
    raw = json.loads(path.read_text())
    raw["score_semantics"] = "not a reviewed semantics object"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Unreviewed aggregate"):
        load_replay(root, "DHFR")
