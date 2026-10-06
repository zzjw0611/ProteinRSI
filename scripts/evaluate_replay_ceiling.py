#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Terminal-only oracle ceiling audit; never an input to a research agent."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path

from proteinrsi.localtools.artifacts import file_sha256


def evaluate(campaign: str | Path, dataset: str | Path) -> dict:
    database = Path(campaign).resolve(strict=True) / "state.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        def get(namespace, key):
            row = connection.execute("SELECT value FROM kv WHERE namespace=? AND key=?",
                                     (namespace, key)).fetchone()
            return json.loads(row[0]) if row else None
        state = get("campaign", "state")
        if not state or state["status"] != "complete" or state.get("pending_batch"):
            raise ValueError("Oracle ceiling audit requires a terminal completed campaign")
        task = state["task"]
        if task["feedback_source"] != "measured_replay":
            raise ValueError("Only a historical measured replay may use this evaluation")
        pin = get("configuration", "replay_dataset")
    # Deliberately do not open or inspect the landscape before the terminal gate.
    path = Path(dataset).resolve(strict=True)
    if not pin or file_sha256(path) != pin["sha256"]:
        raise ValueError("Dataset differs from the completed campaign's pinned source")
    labels = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            sequence = row["sequence"]
            if sequence in labels:
                raise ValueError("Duplicate source identity")
            qc = row.get("qc") or "valid"
            value = float(row["value"]) if row.get("value", "").strip() else None
            if (qc == "valid" and (value is None or not math.isfinite(value))
                    or qc != "valid" and value is not None):
                raise ValueError("Invalid source measurement")
            labels[sequence] = (value, qc)
    known = [o for o in state["observations"] if o["qc"] == "valid"]
    if not known:
        raise ValueError("Completed campaign has no valid revealed observation")
    for observed in state["observations"]:
        if labels.get(observed["sequence"], (None, "unavailable")) != (observed["value"], observed["qc"]):
            raise ValueError("Saved feedback differs from the pinned historical source")
    values = [value for value, qc in labels.values() if qc == "valid"]
    if not values:
        raise ValueError("Source has no valid values")
    maximize = task["direction"] == "maximize"
    best = max if maximize else min
    revealed_best, optimum = best(o["value"] for o in known), best(values)
    parent = [o["value"] for o in known if o["sequence"] == task["reference_sequence"]]
    parent_value = parent[0] if parent else None
    better = (lambda a, b: a > b) if maximize else (lambda a, b: a < b)
    return {"audit_phase": "posthoc_after_terminal_no_agent_feedback", "campaign_id": state["campaign_id"],
        "completed_rounds": state["round_index"], "dataset_sha256": pin["sha256"],
        "metric": task["metric"], "unit": task["unit"], "direction": task["direction"],
        "valid_landscape_records": len(values), "revealed_best": revealed_best,
        "landscape_optimum_value": optimum,
        "remaining_score_gap": optimum - revealed_best if maximize else revealed_best - optimum,
        "records_strictly_better_than_revealed_best": sum(better(v, revealed_best) for v in values),
        "provided_or_queried_parent_value": parent_value,
        "records_strictly_better_than_parent": (sum(better(v, parent_value) for v in values)
                                                  if parent_value is not None else None),
        "unqueried_sequence_identities_exported": False,
        "interpretation": "Evaluation-only access after the study stopped. These labels were not available to the proposer and do not count as discovered variants or justify earlier stopping. The optimum is only over supplied historical records, not all possible biology."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = evaluate(args.campaign, args.dataset)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"Terminal-only ceiling audit saved to {args.out}; do not feed it to a research agent")


if __name__ == "__main__":
    main()
