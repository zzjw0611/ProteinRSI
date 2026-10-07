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
from proteinrsi.reporting_metrics import arm_metrics, charged_metrics, metric_display_names, sample_metrics


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


def summarize(campaign: str | Path, *, top_ns=None, query_bin_size: int = 100) -> dict:
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
        from proteinrsi.reporting_metrics import evaluation_report_configuration
        evaluation_context = evaluation_report_configuration(records, state, top_ns)
        top_ns = tuple(evaluation_context["top_ns"])
        direction, reference = task.get("direction", "maximize"), task.get("reference_sequence", "")
        all_batches, all_measurements = records.all("batches"), records.all("measurements")
        initial = records.get("configuration", "provided_initial_evidence", {}).get("observations", [])
        ledger = [dict(zip(("key", "resource", "amount", "fingerprint", "state"), row))
                  for row in connection.execute(
                      "SELECT key,resource,amount,fingerprint,state FROM charges ORDER BY rowid")]
        accounting = charged_metrics(all_batches, all_measurements, ledger, initial=initial,
            reference=reference, direction=direction, top_ns=top_ns, query_bin_size=query_bin_size)
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
            batch = all_batches[history["batch_id"]]
            measured = all_measurements.get(history["batch_id"], [])
            observed = {o["sample_id"]: o for o in measured}
            counts = {"submitted": len(batch["samples"]), "returned": len(measured),
                "valid": sum(o["qc"] == "valid" for o in measured),
                "unavailable": sum(o["qc"] == "unavailable" for o in measured),
                "other_nonvalid": sum(o["qc"] not in {"valid", "unavailable"} for o in measured),
                "nonvalid": sum(o["qc"] != "valid" for o in measured),
                "not_returned": len(batch["samples"]) - len(measured),
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
                "round_metrics": sample_metrics(batch["samples"], measured, reference=reference,
                    direction=direction, top_ns=top_ns),
                "arm_metrics": arm_metrics(batch["samples"], measured, reference=reference,
                    direction=direction, top_ns=top_ns),
                "accumulated_campaign": accounting["batch_endpoints"].get(batch["batch_id"]),
                "trial": history.get("trial"), "pipelines": pipelines,
                "tool_executions_since_previous_prepared_batch": tools_by_batch.get(batch["batch_id"], []),
                "prediction_denominator_note": "MAE uses valid returned non-control samples with a numeric pre-assay prediction. Null and unavailable samples are not zeros; n=0 does not imply a tool was never called."})
        charges = {}
        for charge in ledger:
            resource, amount, status = charge["resource"], charge["amount"], charge["state"]
            charges.setdefault(resource, {}).setdefault(status, 0)
            charges[resource][status] += amount
        return {"campaign_id": state["campaign_id"], "status": state["status"],
            **evaluation_context,
            "completed_rounds": state["round_index"], "metric": task["metric"], "unit": task["unit"],
            "direction": direction, "top_ns": list(top_ns),
            "metric_display_names": metric_display_names(direction, top_ns),
            "metric_definition": "best is the direction-best (max for maximize, min for minimize); "
                "topNmean and avg use unique-sequence means of valid observations, in original units. "
                "TopN requires N unique valid sequences; insufficient values remain null with effective N. "
                "Parent is excluded from round/arm/window outcomes and included in campaign metrics. "
                "Campaign denominators count charged queries only; evidence_denominators also include "
                "uncharged initial evidence. Missing outcomes are never zeros.",
            "gate_policy_as_recorded": state.get("gate"),
            "charges_by_state": charges, "rounds": rows,
            **{key: value for key, value in accounting.items() if key != "batch_endpoints"},
            "source_note": "Read-only descriptive audit of this campaign only; no source landscape "
                "or comparator labels read, no backfill, and no reevaluation of saved gate decisions."}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--top-n", nargs="+", type=int,
                        help="Descriptive override only; default uses saved LLM choices, then presentation defaults5/10")
    parser.add_argument("--query-bin-size", type=int, default=100)
    args = parser.parse_args()
    result = summarize(args.campaign, top_ns=args.top_n, query_bin_size=args.query_bin_size)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"Read-only per-round audit saved to {args.out}")


if __name__ == "__main__":
    main()
