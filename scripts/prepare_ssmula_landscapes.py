#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare reviewed original-score SSMuLA inputs outside the frozen runtime.

Trusted administration only: do not expose this process, its data paths, or its
measurement files to experiment actors. Only metadata is returned or printed.
No replay, LLM request, label statistics, active labels or candidate catalogue.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from proteinrsi.contracts import AMINO_ACIDS, TaskSpec
from proteinrsi.goal import available_landscapes, load_replay
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.tasks import validate_task


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "configs/ssmula.original_inputs.json"
PINNED_MANIFEST_SHA256 = "647e5dada818b2b52265d9c01e345b65f5a09dcbba4dccd0d476ebe0fa0b2e55"
# The first 14-landscape preparation preceded the primary-source TEV review.
# Keep those immutable artifacts valid; verification still compares their entire
# task, every measurement, source pins and exclusions against the current spec.
PREVIOUS_PREPARATION_MANIFEST_SHA256 = {
    "0b2e099fc6e90022d02595bd68443ebd59cf63f5fe7a501c56f7c95bfdc36da1",
}
FIELDS = ["sequence", "value", "qc", "source", "label_kind"]
LEGACY_LANDSCAPES = {"GB1", "ParD3"}


class PreparationError(ValueError):
    """Safe diagnostic: never include a source row, score or active label."""


def load_manifest() -> dict:
    """No runtime flag can bypass the reviewed source/manifest pins."""
    if file_sha256(MANIFEST_PATH) != PINNED_MANIFEST_SHA256:
        raise PreparationError("Pinned manifest SHA256 mismatch; separate review required")
    manifest = json.loads(MANIFEST_PATH.read_text())
    if manifest.get("schema_version") != 1:
        raise PreparationError("Unsupported pinned manifest version")
    return manifest


def _spec(manifest: dict, landscape: str) -> dict:
    spec = manifest["landscapes"].get(landscape)
    if spec is None:
        raise PreparationError("Landscape is not in the reviewed manifest")
    if spec["status"] != "eligible":
        raise PreparationError("Quarantined landscape: " + "; ".join(spec["quarantine_reasons"]))
    return spec


def _source_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise PreparationError("Source path escapes the data root")
    return path


def _check_source_hashes(root: Path, spec: dict) -> tuple[Path, Path]:
    source = _source_path(root, spec["source_csv"])
    fasta = _source_path(root, spec["source_fasta"])
    pinned = [(source, spec["source_csv_sha256"]), (fasta, spec["source_fasta_sha256"])]
    if construct := spec.get("assay_construct"):
        bundled = construct["bundled_reference"]
        pinned += [(_source_path(root, construct["masked_fasta"]), construct["masked_fasta_sha256"]),
                   (_source_path(root, bundled["source_fasta"]), bundled["source_fasta_sha256"])]
    for path, digest in pinned:
        if file_sha256(path) != digest:
            raise PreparationError("Pinned source SHA256 mismatch; separate review required")
    return source, fasta


def _fasta_sequence(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith(">") or sum(
            line.startswith(">") for line in lines) != 1:
        raise PreparationError("Expected exactly one parent FASTA record")
    return "".join(line.strip() for line in lines[1:])


def _check_measured_construct(root: Path, spec: dict, reference: str) -> None:
    """Explicit primary-source TEV mapping; never infer a construct from labels."""
    construct = spec["assay_construct"]
    if construct["review_status"] != "resolved_original_assay_construct":
        raise PreparationError("Unreviewed assay construct")
    bundled = construct["bundled_reference"]
    supplied = _fasta_sequence(_source_path(root, bundled["source_fasta"]))
    if (len(supplied) != bundled["source_fasta_length"]
            or hashlib.sha256(supplied.encode()).hexdigest() != bundled["source_fasta_sequence_sha256"]
            or not supplied.startswith("GESL") or "M" + supplied[4:] != reference
            or bundled["mutable_positions"] != [p + 3 for p in spec["positions"]]):
        raise PreparationError("Bundled reference differs from reviewed primary assay construct")
    masked = _fasta_sequence(_source_path(root, construct["masked_fasta"]))
    expected = list(reference)
    for position in spec["positions"]:
        expected[position - 1] = "X"
    if masked != "".join(expected):
        raise PreparationError("Primary masked FASTA differs from reviewed measured sites")


def _finite_score(value: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise PreparationError("Missing/unparseable source score; no imputation") from None
    if not math.isfinite(number):
        raise PreparationError("Nonfinite source score; no imputation")
    return number


def _read_originals(root: Path, spec: dict) -> tuple[str, dict[str, str], dict]:
    """Read private scores only to validate/copy; preserve their exact CSV text."""
    source, fasta = _check_source_hashes(root, spec)
    reference = _fasta_sequence(fasta)
    positions = spec["positions"]
    if (len(reference) != spec["parent_length"] or set(reference) - AMINO_ACIDS
            or hashlib.sha256(reference.encode()).hexdigest() != spec["parent_sequence_sha256"]
            or not positions or len(set(positions)) != len(positions)
            or any(type(p) is not int or not 1 <= p <= len(reference) for p in positions)
            or "".join(reference[p - 1] for p in positions) != spec["parent_sites"]):
        raise PreparationError("Reviewed parent sequence/positions do not match")
    if spec.get("assay_construct"):
        _check_measured_construct(root, spec, reference)
    rows, seen = {}, set()
    counts = {"source_rows": 0, "excluded_stop_rows": 0, "prepared_rows": 0}
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        header = next(reader, None)
        if header != spec["csv_header"] or len(set(header)) != len(header):
            raise PreparationError("Source CSV header differs from reviewed schema")
        sequence_index = header.index(spec["sequence_column"])
        value_index = header.index(spec["value_column"])
        complete_index = (header.index(spec["complete_sequence_column"])
                          if spec.get("complete_sequence_column") else None)
        # Auxiliary sequence columns can validate identity. Activity, stop-count,
        # variance, read-count and other phenotype columns are never consulted.
        split_indices = ([header.index(f"AA{i + 1}") for i in range(len(positions))]
                         if spec["family"] == "TrpB" else [])
        for record in reader:
            counts["source_rows"] += 1
            if len(record) != len(header):
                raise PreparationError("Malformed source CSV row")
            sites = record[sequence_index]
            if len(sites) != len(positions) or set(sites) - (AMINO_ACIDS | {"*"}):
                raise PreparationError("Unexpected source sequence symbols or length")
            if sites in seen:
                raise PreparationError("Duplicate source sequence; no aggregation")
            seen.add(sites)
            if split_indices and "".join(record[i] for i in split_indices) != sites:
                raise PreparationError("Source amino-acid columns disagree")
            # Validate all scores, even sequence-ineligible stop records. Never
            # use a score, active flag or source stop-count to decide eligibility.
            value = record[value_index]
            _finite_score(value)
            if "*" in sites:
                if spec["stop_policy"] != "exclude_stop_tuples_only":
                    raise PreparationError("Noncanonical source sequence; no implicit filtering")
                counts["excluded_stop_rows"] += 1
                continue
            sequence = list(reference)
            for position, residue in zip(positions, sites):
                sequence[position - 1] = residue
            full = "".join(sequence)
            if complete_index is not None and record[complete_index] != full:
                raise PreparationError("Source complete sequence differs from measured assay construct")
            if full in rows:
                raise PreparationError("Duplicate expanded protein; no aggregation")
            rows[full] = value
    counts["prepared_rows"] = len(rows)
    if not rows or reference not in rows:
        raise PreparationError("Missing original parent measurement")
    if counts != spec["expected_counts"]:
        raise PreparationError("Source integrity counts differ from reviewed manifest")
    _check_source_hashes(root, spec)  # Reject mutation during preparation.
    return reference, rows, counts


def _task(landscape: str, spec: dict, reference: str, defaults: dict) -> TaskSpec:
    task = TaskSpec(name=spec.get("task_name", f"{landscape} original-score measured replay"),
        reference_sequence=reference, mutable_positions=spec["positions"],
        max_mutations=len(spec["positions"]), candidates=[], candidate_access="open",
        feedback_source="measured_replay", metric="fitness", unit=spec["unit"],
        controls_per_batch=0, initial_observation_policy="none",
        assay_protocol=spec.get("assay_protocol", f"ssmula-zenodo-15203754-{landscape}-original-score-v1"),
        max_rounds=defaults["max_rounds"], batch_size=defaults["batch_size"],
        batch_fill_policy=defaults["batch_fill_policy"], seed=defaults["seed"],
        repeat_policy=defaults["repeat_policy"], budget={
            k: defaults[k] for k in ("experimental_wells", "llm_calls", "tool_calls")})
    validate_task(task)
    return task


def _write_json(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def prepare(data_root: str | Path, landscape: str) -> dict:
    """Write a new dataset atomically; refuse to overwrite existing preparation."""
    manifest = load_manifest()
    spec = _spec(manifest, landscape)
    root = Path(data_root).resolve(strict=True)
    out = root / "processed" / landscape
    if out.exists():
        raise FileExistsError("Existing preparation must be verified, never overwritten")
    reference, rows, counts = _read_originals(root, spec)
    task = _task(landscape, spec, reference, manifest["launch_defaults"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{landscape}-", dir=out.parent) as temporary:
        stage = Path(temporary) / landscape
        stage.mkdir()
        measurement = stage / "measurements.csv"
        with measurement.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            # Sequence-only order; never sort by scores or preserve source ranking.
            for sequence in sorted(rows):
                writer.writerow({"sequence": sequence, "value": rows[sequence], "qc": "valid",
                    "source": "measured_replay", "label_kind": "reported_experimental_assay_score"})
        _write_json(stage / "task.json", task.model_dump(mode="json"))
        _write_json(stage / "provenance.json", {
            "status": "ready_strict_measured_replay", "landscape": landscape,
            "source_release": manifest["source_release"],
            "original_study": f"Original source table supplied in {manifest['source_release']}",
            "metadata_source": manifest["metadata_source"],
            "source_processing_references": spec["source_processing_references"],
            "source_relative_path": spec["source_csv"],
            "source_sha256": spec["source_csv_sha256"],
            "parent_fasta_relative_path": spec["source_fasta"],
            "parent_fasta_sha256": spec["source_fasta_sha256"],
            "reviewed_manifest_sha256": PINNED_MANIFEST_SHA256,
            "parent": {"sequence": reference, "mutable_positions_1based": spec["positions"]},
            "original_score_definition": spec["original_score_definition"],
            "assay_construct": spec.get("assay_construct"),
            "preprocessing": "Source score text unchanged; no scaling, clipping, ranking, "
                "aggregation, imputation, activity labels or score-based filtering",
            "sequence_eligibility": {"canonical_amino_acids_only": True,
                "stop_policy": spec["stop_policy"], "excluded_stop_rows": counts["excluded_stop_rows"],
                "other_sequence_exclusions": 0},
            "source_rows": counts["source_rows"], "rows": counts["prepared_rows"],
            "row_order": "lexicographic full protein sequence; not source order",
            "prepared_assets": {"measurements_csv_sha256": file_sha256(measurement),
                "task_json_sha256": file_sha256(stage / "task.json")},
            "parent_disclosure": "Task contains no measurement; trusted launcher alone "
                "loads the single supplied parent outside the 2000 new-query budget",
            "validation_scope": "Input preparation only; scientific replay has not run",
        })
        # rename is atomic on this filesystem; no partially ready directory.
        if out.exists():
            raise FileExistsError("Another preparation already exists")
        os.rename(stage, out)
    return verify(data_root, landscape)


def verify(data_root: str | Path, landscape: str) -> dict:
    """Read-only, independent source/contract/loader verification; return no labels."""
    manifest = load_manifest()
    spec = _spec(manifest, landscape)
    root = Path(data_root).resolve(strict=True)
    reference, expected_rows, counts = _read_originals(root, spec)
    out = root / "processed" / landscape
    task_path, measurement = out / "task.json", out / "measurements.csv"
    raw = json.loads(task_path.read_text())
    provenance = json.loads((out / "provenance.json").read_text())
    expected_task = _task(landscape, spec, reference, manifest["launch_defaults"]).model_dump()
    for key in ("reference_sequence", "mutable_positions", "max_mutations", "candidates",
                "candidate_access", "feedback_source", "metric", "unit", "direction",
                "controls_per_batch", "initial_observation_policy", "initial_parent_measurement",
                "assay_protocol", "allow_indels", "repeat_policy"):
        if raw.get(key) != expected_task[key]:
            raise PreparationError("Prepared task differs from reviewed open-design contract")
    try:
        validate_task(TaskSpec.model_validate(raw))
    except ValueError:
        raise PreparationError("Prepared task contract is invalid") from None
    if (provenance.get("status") != "ready_strict_measured_replay"
            or provenance.get("landscape") != landscape
            or provenance.get("source_sha256") != spec["source_csv_sha256"]
            or provenance.get("parent_fasta_sha256") != spec["source_fasta_sha256"]
            or provenance.get("parent") != {
                "sequence": reference, "mutable_positions_1based": spec["positions"]}
            or provenance.get("rows") != counts["prepared_rows"]
            or provenance.get("prepared_assets", {}).get("measurements_csv_sha256")
            != file_sha256(measurement)):
        raise PreparationError("Prepared provenance or measurement digest mismatch")
    exact_text = landscape not in LEGACY_LANDSCAPES
    if exact_text:
        approved_pins = {PINNED_MANIFEST_SHA256}
        if not spec.get("assay_construct"):
            approved_pins |= PREVIOUS_PREPARATION_MANIFEST_SHA256
        if (provenance.get("reviewed_manifest_sha256") not in approved_pins
                or provenance.get("prepared_assets", {}).get("task_json_sha256")
                != file_sha256(task_path)
                or provenance.get("sequence_eligibility", {}).get("excluded_stop_rows")
                != counts["excluded_stop_rows"]):
            raise PreparationError("Prepared manifest/task/exclusion provenance mismatch")
        if provenance.get("assay_construct") != spec.get("assay_construct"):
            raise PreparationError("Prepared assay construct provenance mismatch")
    with measurement.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDS:
            raise PreparationError("Prepared measurement schema differs")
        for record in reader:
            if (set(record) != set(FIELDS) or record["qc"] != "valid"
                    or record["source"] != "measured_replay"
                    or record["label_kind"] != "reported_experimental_assay_score"):
                raise PreparationError("Prepared measurement provenance differs")
            original = expected_rows.pop(record["sequence"], None)
            if original is None:
                raise PreparationError("Prepared measurement key is unknown or duplicated")
            if (record["value"] != original if exact_text else
                    _finite_score(record["value"]) != _finite_score(original)):
                raise PreparationError("Prepared score differs from its original source")
        if expected_rows:
            raise PreparationError("Prepared measurements are missing reviewed candidates")
    if landscape not in available_landscapes(root):
        raise PreparationError("Runtime catalogue did not accept preparation")
    # The trusted loader reveals the parent into this controller only; nothing
    # from loaded['initial_parent_measurement'] is returned, logged or serialized.
    try:
        loaded, dataset = load_replay(root, landscape)
        validate_task(TaskSpec.model_validate(loaded))
    except ValueError:
        raise PreparationError("Runtime replay loader rejected preparation") from None
    if (loaded["candidates"] or loaded["candidate_access"] != "open"
            or loaded["initial_observation_policy"] != "provided_parent"
            or dataset != measurement):
        raise PreparationError("Runtime loader changed the open-design/parent contract")
    return {"landscape": landscape, "status": "prepared_verified",
        "directory": str(out), **counts, "source_sha256": spec["source_csv_sha256"],
        "parent_fasta_sha256": spec["source_fasta_sha256"],
        "prepared_assets": {p.name: file_sha256(p) for p in (
            task_path, measurement, out / "provenance.json")},
        "available_landscapes_verified": True, "load_replay_verified": True,
        "labels_printed": False, "scientific_replay_executed": False}


def launch_inventory(data_root: str | Path, *, verified: dict[str, dict]) -> dict:
    """Separate preparation evidence from proposed, still-unexecuted campaigns."""
    manifest = load_manifest()
    entries = []
    for name, spec in manifest["landscapes"].items():
        if spec["status"] == "quarantined":
            entries.append({"landscape": name, "status": "quarantined",
                "reasons": spec["quarantine_reasons"], "launch_allowed": False})
            continue
        evidence = verified.get(name)
        entries.append({"landscape": name,
            "status": "prepared_verified" if evidence else "not_verified_in_this_invocation",
            "preparation_evidence": evidence, "launch_defaults": manifest["launch_defaults"],
            "scientific_validation": "not_executed_by_preparation",
            "launch_allowed": False,
            "launch_gate": "Preparation-only gate: a separate orchestrator records explicit run "
                "authorization, compute configuration, and guarded enforcement OR separately "
                "explicit-approved trusted inprocess debugging with its weaker isolation label; "
                "input preparation never infers launch authorization"})
    return {"schema_version": 1, "data_root": str(Path(data_root).resolve()),
        "source_release": manifest["source_release"],
        "reviewed_manifest_sha256": PINNED_MANIFEST_SHA256,
        "launch_defaults": manifest["launch_defaults"],
        "budget_scope": "Per campaign: 2000 new historical queries plus one provided parent; "
            "600 LLM calls and 400 tool calls are hard proposed caps, not usage/currency estimates",
        "actor_boundary": "Open design: no candidate catalogue, source paths, source row order, "
            "active labels, global label quantities or unqueried measurements",
        "prepared_verified_count": len(verified),
        "quarantined_count": sum(e["status"] == "quarantined" for e in entries),
        "scientific_replay_executed": False, "landscapes": entries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True,
                        help="Root containing extracted data/ and controller-only processed/")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="All eligible reviewed landscapes")
    selection.add_argument("--landscape", nargs="+", help="Exact reviewed landscape names")
    parser.add_argument("--verify-only", action="store_true", help="Never create prepared inputs")
    parser.add_argument("--launch-inventory", help="New metadata-only JSON file; never overwritten")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        names = ([name for name, spec in manifest["landscapes"].items()
                  if spec["status"] == "eligible"] if args.all else args.landscape)
        # Reject an explicit quarantined request before creating any artifacts.
        for name in names:
            _spec(manifest, name)
        verified = {}
        for name in names:
            exists = (Path(args.data_root) / "processed" / name).exists()
            result = (verify(args.data_root, name) if args.verify_only or exists
                      else prepare(args.data_root, name))
            verified[name] = result
            print(json.dumps(result, sort_keys=True))
        if args.launch_inventory:
            _write_json(Path(args.launch_inventory), launch_inventory(args.data_root, verified=verified))
            print(json.dumps({"launch_inventory": str(Path(args.launch_inventory).resolve()),
                "prepared_verified_count": len(verified), "labels_printed": False}))
    except PreparationError as exc:
        print(json.dumps({"status": "failed_closed", "reason": str(exc), "labels_printed": False}))
        return 1
    except (OSError, ValueError, csv.Error, KeyError, TypeError):
        # Raw parser/OS exceptions may include a private source value; omit them.
        print(json.dumps({"status": "failed_closed", "reason": "Input/output or schema error; "
            "inspect file presence, permissions and pinned schema without printing source rows",
            "labels_printed": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
