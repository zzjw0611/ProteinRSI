#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only per-round audit from purchased observations and frozen batch records."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

from proteinrsi.research.prediction import prediction_row


class ReadOnlyRecords:
    def __init__(self, connection):
        self.connection = connection

    def get(self, namespace, key, default=None):
        row = self.connection.execute("SELECT value FROM kv WHERE namespace=? AND key=?",
                                      (namespace, key)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, namespace):
        return {key: json.loads(value) for key, value in self.connection.execute(
            "SELECT key,value FROM kv WHERE namespace=?", (namespace,))}


def summarize(campaign: str | Path) -> dict:
    path = Path(campaign).resolve(strict=True) / "state.sqlite3"
    if not path.is_file():
        raise ValueError("Campaign state does not exist")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")  # One consistent read snapshot while a study may be running.
        records = ReadOnlyRecords(connection)
        state = records.get("campaign", "state")
        if state is None:
            raise ValueError("No initialized campaign")
        task = state["task"]
        runs = [*records.all("research_runs").values(),
                *records.all("validation_research_runs").values()]
        tools_by_batch, pending_tools = {}, []
        for kind, raw in connection.execute("SELECT kind,payload FROM events ORDER BY id"):
            payload, branch = json.loads(raw), None
            if kind == "validation_event":
                branch, kind, payload = payload["branch"], payload["kind"], payload["payload"]
            if kind in {"tool_completed", "tool_failed"}:
                pending_tools.append({"tool": payload["tool"], "state": kind,
                                      "branch": branch, "job_key": payload.get("key")})
            elif kind == "batch_prepared":
                tools_by_batch[payload["batch_id"]] = pending_tools
                pending_tools = []
        rows = []
        for history in state["history"]:
            batch = records.get("batches", history["batch_id"])
            measured = records.get("measurements", history["batch_id"], [])
            observed = {o["sample_id"]: o for o in measured}
            counts = {"submitted": len(batch["samples"]), "returned": len(measured),
                "valid": sum(o["qc"] == "valid" for o in measured),
                "unavailable": sum(o["qc"] == "unavailable" for o in measured),
                "other_nonvalid": sum(o["qc"] not in {"valid", "unavailable"} for o in measured),
                "frozen_numeric_predictions": 0, "verified_prediction_refs": 0,
                "valid_with_prediction": 0, "valid_without_prediction": 0,
                "unavailable_with_prediction": 0}
            errors = []
            for sample in batch["samples"]:
                candidate = sample["candidate"]
                value, ref = candidate.get("predicted_value"), candidate.get("prediction_ref")
                has_prediction = type(value) in (int, float) and math.isfinite(value)
                if value is not None and not has_prediction:
                    raise ValueError("Invalid frozen numeric prediction")
                if ref is not None:
                    _, original = prediction_row(records, ref, candidate["sequence"],
                        evidence_version=batch["evidence_version"], workflow=sample["workflow_version"],
                        metric=task["metric"], unit=task["unit"])
                    if (value != original["predicted_value"] or candidate.get("uncertainty") is not None
                            or candidate.get("evidence_kind") != ("proxy" if value is not None else "none")):
                        raise ValueError("Frozen batch prediction differs from protected artifact")
                    counts["verified_prediction_refs"] += 1
                counts["frozen_numeric_predictions"] += int(has_prediction)
                result = observed.get(sample["sample_id"])
                if result is None:
                    continue
                if result["sequence"] != candidate["sequence"]:
                    raise ValueError("Frozen sample identity differs from measurement")
                if result["qc"] == "valid":
                    counts["valid_with_prediction" if has_prediction else "valid_without_prediction"] += 1
                    if has_prediction and sample["arm"] != "control":
                        errors.append(abs(result["value"] - value))
                elif result["qc"] == "unavailable" and has_prediction:
                    counts["unavailable_with_prediction"] += 1
            pipelines = []
            for run in runs:
                if (run.get("evidence_version") != batch["evidence_version"]
                        or run.get("round") != batch["round_index"]):
                    continue
                plan = run.get("plan", {})
                completed = run.get("completed", [])
                steps = plan.get("steps", [])
                completed_ids = {v if isinstance(v, str) else v.get("step_id") for v in completed}
                pipelines.append({"run_id": run.get("run_id"), "branch": run.get("branch"),
                    "workflow": run.get("workflow_version"), "status": run.get("status"),
                    "planned_operations": [s["operation"] for s in steps],
                    "completed_operations": [s["operation"] for s in steps if s["step_id"] in completed_ids],
                    "revision_count": len(run.get("revisions", [])),
                    "repair_count": len(run.get("repairs", []))})
            rows.append({"round": batch["round_index"] + 1, "batch_id": batch["batch_id"],
                **counts, "frozen_valid_prediction_error_n": len(errors),
                "frozen_valid_prediction_mae": sum(errors) / len(errors) if errors else None,
                "trial": history.get("trial"), "pipelines": pipelines,
                "tool_executions_since_previous_prepared_batch": tools_by_batch.get(batch["batch_id"], []),
                "prediction_denominator_note": "MAE uses valid returned non-control samples with a numeric pre-assay prediction. Null and unavailable samples are not zeros; n=0 does not imply a tool was never called."})
        charges = {}
        for resource, amount, status in connection.execute("SELECT resource,amount,state FROM charges"):
            charges.setdefault(resource, {}).setdefault(status, 0)
            charges[resource][status] += amount
        return {"campaign_id": state["campaign_id"], "status": state["status"],
            "completed_rounds": state["round_index"], "metric": task["metric"], "unit": task["unit"],
            "charges_by_state": charges, "rounds": rows,
            "source_note": "Read-only audit of this campaign only; no source landscape or comparator labels read."}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = summarize(args.campaign)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"Read-only per-round audit saved to {args.out}")


if __name__ == "__main__":
    main()
