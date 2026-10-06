# SPDX-License-Identifier: MIT
"""Descriptive diagnostics on revealed measurements, never an experimental oracle."""
from __future__ import annotations

from collections import defaultdict
import math
import numpy as np

from proteinrsi.contracts import TaskView, digest
from proteinrsi.tools import ToolSpec

ANALYSIS_TOOLS = ["research_evidence_summary", "research_prediction_errors", "research_combination_effects"]


def _validate(view: TaskView) -> None:
    ids = set()
    for o in view.observations:
        if o.sample_id in ids:
            raise ValueError("Duplicate observed sample identity")
        ids.add(o.sample_id)
        if (o.metric, o.unit, o.assay_protocol, o.source) != (
                view.task.metric, view.task.unit, view.task.assay_protocol, view.task.feedback_source):
            raise ValueError("Cannot pool incompatible measurements")


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "mean": float(np.mean(values)) if values else None,
            "sample_sd": float(np.std(values, ddof=1)) if len(values) > 1 else None}


def evidence_summary(view: TaskView) -> dict:
    _validate(view)
    batches = defaultdict(list)
    for o in view.observations:
        batches[o.batch_id].append(o)
    report = []
    for batch_id, obs in sorted(batches.items()):
        valid = [o for o in obs if o.qc == "valid"]
        # With no Sample metadata, WT is the only identifiable reference, not all controls.
        wt = [float(o.value) for o in valid if o.sequence == view.task.reference_sequence]
        report.append({"batch_id": batch_id, "n": len(obs), "n_valid": len(valid),
            "n_failed": sum(o.qc == "failed" for o in obs),
            "n_unavailable": sum(o.qc == "unavailable" for o in obs),
            "n_inconclusive": sum(o.qc == "inconclusive" for o in obs),
            "unique_valid_sequences": len({o.sequence for o in valid}), "wt_measurements": _stats(wt)})
    return {"evidence_version": view.evidence_version, "source": view.task.feedback_source,
        "metric": view.task.metric, "unit": view.task.unit, "assay_protocol": view.task.assay_protocol,
        "n_observations": len(view.observations),
        "n_unique_valid_sequences": len({o.sequence for o in view.observations if o.qc == "valid"}),
        "batches": report, "interpretation": "Descriptive QC only; no automatic correction, exclusion or causal batch-effect claim."}


def _average_ranks(values):
    values = np.asarray(values, dtype=float)
    out = np.empty(len(values), dtype=float)
    order = np.argsort(values, kind="stable")
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[order[j]] == values[order[i]]:
            j += 1
        out[order[i:j]] = (i + j - 1) / 2 + 1
        i = j
    return out


def prediction_errors(view: TaskView, store) -> dict:
    """Match only predictions frozen in submitted batches, not newly refitted predictions."""
    _validate(view)
    observations = {(o.batch_id, o.sample_id): o for o in view.observations if o.qc == "valid"}
    rows = []
    for batch_id in sorted({o.batch_id for o in view.observations}):
        batch = store.get("batches", batch_id)
        if not batch:
            continue
        for sample in batch["samples"]:
            o = observations.get((batch_id, sample["sample_id"]))
            candidate = sample["candidate"]
            prediction = candidate.get("predicted_value")
            # Uncalibrated proxy scores must never be evaluated as phenotype predictions.
            if o is None or sample["arm"] == "control":
                continue
            ref = candidate.get("prediction_ref")
            if ref is not None:
                from .prediction import prediction_row
                _, row = prediction_row(store, ref, o.sequence,
                    evidence_version=batch["evidence_version"], workflow=sample["workflow_version"],
                    metric=view.task.metric, unit=view.task.unit)
                if (prediction != row["predicted_value"] or candidate.get("uncertainty") is not None
                        or candidate.get("evidence_kind") != ("proxy" if prediction is not None else "none")):
                    raise ValueError("Frozen prediction differs from its trusted tool artifact")
            if prediction is None:
                continue
            if candidate["sequence"] != o.sequence or not math.isfinite(prediction):
                raise ValueError("Saved prediction identity/value mismatch")
            rows.append({"sample_id": o.sample_id, "batch_id": batch_id, "sequence": o.sequence,
                         "prediction": prediction, "measured": o.value,
                         "residual": o.value - prediction})
    residual = [r["residual"] for r in rows]
    correlation = None
    if len(rows) >= 3:
        x, y = _average_ranks([r["prediction"] for r in rows]), _average_ranks([r["measured"] for r in rows])
        if x.std() > 0 and y.std() > 0:
            correlation = float(np.corrcoef(x, y)[0, 1])
    by_depth = defaultdict(list)
    for row in rows:
        depth = sum(a != b for a, b in zip(view.task.reference_sequence, row["sequence"]))
        by_depth[str(depth)].append(abs(row["residual"]))
    return {"evidence_version": view.evidence_version, "n": len(rows), "rows": rows,
        "mae": float(np.mean(np.abs(residual))) if rows else None,
        "bias_measured_minus_predicted": float(np.mean(residual)) if rows else None,
        "spearman": correlation, "absolute_error_by_mutation_depth": {k: _stats(v) for k, v in by_depth.items()},
        "interpretation": "Frozen pre-measurement task-head predictions only; descriptive, replicates may be dependent. No generalization claim."}


def combination_effects(view: TaskView, *, scale: str, pooling: str = "within_batch") -> dict:
    _validate(view)
    if scale not in ("linear", "log") or pooling not in ("within_batch", "pooled"):
        raise ValueError("Explicit supported scale and pooling required")
    groups = defaultdict(lambda: defaultdict(list))
    reference = view.task.reference_sequence
    for o in view.observations:
        if o.qc == "valid":
            groups[o.batch_id if pooling == "within_batch" else "pooled"][o.sequence].append(float(o.value))
    rows, unavailable = [], []
    for group, raw in sorted(groups.items()):
        means = {s: float(np.mean(v)) for s, v in raw.items()}
        for seq in sorted(means):
            if len(seq) != len(reference):
                unavailable.append({"group": group, "sequence": seq, "reason": "length_mismatch"})
                continue
            changes = [i for i, (a, b) in enumerate(zip(reference, seq)) if a != b]
            if len(changes) < 2:
                continue
            singles = [reference[:i] + seq[i] + reference[i + 1:] for i in changes]
            missing = [s for s in [reference, *singles] if s not in means]
            if missing:
                unavailable.append({"group": group, "sequence": seq, "reason": "missing_measured_constituents",
                                    "missing_sequences": missing})
                continue
            relevant = [means[s] for s in [reference, *singles, seq]]
            if scale == "log" and any(v <= 0 for v in relevant):
                unavailable.append({"group": group, "sequence": seq, "reason": "nonpositive_value_for_log_scale"})
                continue
            transform = math.log if scale == "log" else float
            wt = transform(means[reference])
            expected = wt + sum(transform(means[s]) - wt for s in singles)
            rows.append({"group": group, "sequence": seq, "n_mutations": len(changes),
                         "observed_on_scale": transform(means[seq]), "null_on_scale": expected,
                         "deviation_on_scale": transform(means[seq]) - expected})
    return {"evidence_version": view.evidence_version, "scale": scale, "pooling": pooling,
        "comparisons": rows, "unavailable": unavailable,
        "interpretation": "Scale-dependent descriptive null-model deviation, NOT statistical significance. Pooled comparisons may confound batches; no missing labels inferred."}


def analyze_visible(view: TaskView, store) -> dict:
    return {"qc": evidence_summary(view), "prediction_errors": prediction_errors(view, store)}


def persist_analysis(view: TaskView, store) -> dict:
    report = analyze_visible(view, store)
    key = digest(report)
    store.put("research_analysis", key, report, immutable=True)
    return {"artifact_ref": "research_analysis/" + key, **report}


def register_analysis_tools(gateway, view: TaskView) -> list[str]:
    """Context-bound functions: an LLM cannot supply observations, SQL, paths or labels."""
    if getattr(gateway, "remote_context_tools", False):
        return list(ANALYSIS_TOOLS)
    from proteinrsi.contracts import TaskKind
    def register(name, properties, required, fn, description):
        spec = ToolSpec(name=name, capability="evidence.analyze", description=description,
            limitations="Revealed-data diagnostics only, no measurements created or modified.",
            implementation_version="research-analysis-v1:" + view.evidence_version,
            task_kinds=list(TaskKind), input_schema={"type": "object", "properties": properties,
                "required": required, "additionalProperties": False}, output_schema={"type": "object"})
        def execute(arguments):
            report = fn(arguments)
            key = digest(report)
            gateway.store.put("research_analysis", key, report, immutable=True)
            return {"artifact_ref": "research_analysis/" + key, "analysis": report, "evidence_kind": "derived_analysis"}
        gateway.register(spec, execute)
    register(ANALYSIS_TOOLS[0], {}, [], lambda _: evidence_summary(view),
             "Inspect revealed experimental QC, WT controls and batch summaries.")
    register(ANALYSIS_TOOLS[1], {}, [], lambda _: prediction_errors(view, gateway.store),
             "Compare frozen submitted predictions against now-revealed measurements.")
    register(ANALYSIS_TOOLS[2], {"scale": {"enum": ["linear", "log"]},
             "pooling": {"enum": ["within_batch", "pooled"]}}, ["scale"],
             lambda a: combination_effects(view, **a), "Inspect measured combinations versus measured constituent singles on an explicit scale.")
    return list(ANALYSIS_TOOLS)
