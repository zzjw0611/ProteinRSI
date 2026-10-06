# SPDX-License-Identifier: MIT
"""Trusted, pinned SSMuLA input preparation; never a research-agent tool.

The source tables stay outside the package and campaign. No label statistics or
candidate menu are returned to the operator or LLM by this preparation command.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from proteinrsi.contracts import AMINO_ACIDS, TaskSpec
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.tasks import validate_task

SOURCE = "https://zenodo.org/records/15203754"
METADATA = ("https://github.com/fhalab/SSMuLA/blob/"
            "4926b8e614d426ca567159988834482c23da13a3/SSMuLA/landscape_global.py")
# Reviewed original-score inputs in data.zip from the above pinned Zenodo release.
# No scale2max inputs, global extrema, active thresholds or imputation are used.
LANDSCAPES = {
    "GB1": {
        "positions": [39, 40, 41, 54], "parent_sites": "VDGV", "length": 56,
        "sequence_column": "Variants", "value_column": "Fitness",
        "csv_sha256": "64767168e77a1d6c12e6211ed33cc2c45b764d866c3d93c29e5682e79686322b",
        "fasta_sha256": "f1acaa18f06a15a27d30cc57ebd2f2d40158d4a8d4e7bf6b60d742a178fc99ce",
        "unit": "reported_enrichment_relative_to_WT",
    },
    "ParD3": {
        "positions": [61, 64, 80], "parent_sites": "DKE", "length": 93,
        "sequence_column": "AAs", "value_column": "fitness",
        "csv_sha256": "3504b7fe86dfe1add9b2668d97267c37b7aabe84401a914458d440fee4068256",
        "fasta_sha256": "ccf1a28bafb59fda16e308d153e7b5e0c9559ab95055ac1222ab33b75e3b5257",
        "unit": "original_reported_fitness",
    },
}


def prepare(data_root: str | Path, landscape: str) -> dict:
    """Validate exact reviewed bytes, then write an open-design replay input.

    Refuse any changed source rather than silently certifying an unknown table.
    Refuse duplicate, invalid or missing values rather than aggregating/filtering.
    Existing prepared output is never overwritten.
    """
    spec = LANDSCAPES[landscape]
    root = Path(data_root).resolve(strict=True)
    directory = root / "data" / landscape
    source = directory / "fitness_landscape" / f"{landscape}.csv"
    fasta = directory / f"{landscape}.fasta"
    for path, expected in ((source, spec["csv_sha256"]), (fasta, spec["fasta_sha256"])):
        if file_sha256(path) != expected:
            raise ValueError(f"Pinned source SHA256 mismatch: {path.name}; review the source")
    lines = fasta.read_text().splitlines()
    if sum(line.startswith(">") for line in lines) != 1:
        raise ValueError("Expected exactly one parent FASTA record")
    reference = "".join(line.strip() for line in lines if not line.startswith(">"))
    positions = spec["positions"]
    if (len(reference) != spec["length"] or set(reference) - AMINO_ACIDS
            or "".join(reference[p - 1] for p in positions) != spec["parent_sites"]):
        raise ValueError("Reviewed parent sequence/positions do not match")
    rows, seen = [], set()
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sites = row[spec["sequence_column"]]
            if len(sites) != len(positions) or set(sites) - AMINO_ACIDS:
                raise ValueError("Noncanonical source sequence; no implicit filtering")
            if sites in seen:
                raise ValueError("Duplicate source sequence; no implicit aggregation")
            seen.add(sites)
            value = float(row[spec["value_column"]])
            if not math.isfinite(value):
                raise ValueError("Missing/nonfinite source score; no imputation")
            sequence = list(reference)
            for position, residue in zip(positions, sites):
                sequence[position - 1] = residue
            rows.append({"sequence": "".join(sequence), "value": value, "qc": "valid",
                         "source": "measured_replay",
                         "label_kind": "reported_experimental_assay_score"})
    if not rows or spec["parent_sites"] not in seen:
        raise ValueError("Missing original parent measurement")
    task = TaskSpec(name=f"{landscape} original-score measured replay",
        reference_sequence=reference, mutable_positions=positions, max_mutations=len(positions),
        candidates=[], candidate_access="open", feedback_source="measured_replay",
        metric="fitness", unit=spec["unit"], controls_per_batch=0,
        assay_protocol=f"ssmula-zenodo-15203754-{landscape}-original-score-v1")
    validate_task(task)
    out = root / "processed" / landscape
    out.mkdir(parents=True, exist_ok=False)
    measurements = out / "measurements.csv"
    with measurements.open("x", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    provenance = {"status": "ready_strict_measured_replay", "landscape": landscape,
        "source_release": SOURCE, "original_study": f"Original source table supplied in {SOURCE}",
        "metadata_source": METADATA, "source_sha256": spec["csv_sha256"],
        "parent_fasta_sha256": spec["fasta_sha256"],
        "parent": {"sequence": reference, "mutable_positions_1based": positions},
        "preprocessing": "Original finite reported scores, unchanged; no scaling, ranking, filtering or imputation",
        "rows": len(rows), "prepared_assets": {"measurements_csv_sha256": file_sha256(measurements)}}
    (out / "task.json").write_text(task.model_dump_json(indent=2) + "\n")
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return {"landscape": landscape, "directory": str(out), "rows": len(rows),
            "source_sha256": spec["csv_sha256"], "labels_printed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True,
                        help="Directory containing the official extracted data/ tree")
    parser.add_argument("--landscape", choices=list(LANDSCAPES), required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.data_root, args.landscape), indent=2))


if __name__ == "__main__":
    main()
