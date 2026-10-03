#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Normalize user-licensed measurement tables without guessing sites or aggregating repeats."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from proteinrsi.contracts import TaskSpec
from proteinrsi.tasks import validate_task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--sequence-column", required=True)
    parser.add_argument("--value-column", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--positions", type=int, nargs="+", required=True)
    parser.add_argument("--site-sequences", action="store_true")
    parser.add_argument("--controls", type=int, default=1)
    parser.add_argument("--metric", default="fitness")
    parser.add_argument("--unit", default="a.u.")
    parser.add_argument("--assay-protocol", default="operator-normalized-v1")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rows, seen = [], set()
    with Path(args.csv).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            sequence = row[args.sequence_column].strip()
            if args.site_sequences:
                if len(sequence) != len(args.positions):
                    raise ValueError("Site sequence length does not match explicit position count")
                full = list(args.reference)
                for position, residue in zip(args.positions, sequence):
                    if position < 1 or position > len(full):
                        raise ValueError("Position outside reference")
                    full[position - 1] = residue
                sequence = "".join(full)
            if sequence in seen:
                raise ValueError("Duplicate variant: resolve replicate/QC aggregation explicitly before import")
            seen.add(sequence)
            value = row[args.value_column].strip()
            if not value:
                raise ValueError("Missing measurement: use a reviewed QC-aware preprocessing protocol")
            numeric = float(value)
            import math
            if not math.isfinite(numeric):
                raise ValueError("Non-finite measurement")
            rows.append({"sequence": sequence, "value": numeric, "qc": "valid"})
    if not rows:
        raise ValueError("No measurements")
    if args.controls and args.reference not in seen:
        raise ValueError("No measured parent; supply a real parent measurement or explicitly use --controls 0")
    task = TaskSpec(name="User-provided measured landscape", reference_sequence=args.reference,
        mutable_positions=args.positions, max_mutations=len(args.positions), candidates=[r["sequence"] for r in rows],
        metric=args.metric, unit=args.unit, feedback_source="measured_replay",
        assay_protocol=args.assay_protocol, controls_per_batch=args.controls)
    validate_task(task)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    with (out / "measurements.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sequence", "value", "qc"])
        writer.writeheader()
        writer.writerows(rows)
    (out / "task.json").write_text(json.dumps(task.model_dump(mode="json"), indent=2) + "\n")
    (out / "provenance.json").write_text(json.dumps({
        "source_sha256": hashlib.sha256(Path(args.csv).read_bytes()).hexdigest(),
        "sequence_column": args.sequence_column, "value_column": args.value_column,
        "positions_1based": args.positions, "site_sequences": args.site_sequences,
        "license": "USER_MUST_VERIFY_ORIGINAL_DATA_LICENSE", "rows": len(rows)
    }, indent=2) + "\n")
    print(out)


if __name__ == "__main__":
    main()
