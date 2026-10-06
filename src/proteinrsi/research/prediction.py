# SPDX-License-Identifier: MIT
"""Explicit, context-bound task prediction. Never invoked automatically in LLM mode."""
import math
import re

import numpy as np
from proteinrsi.contracts import TaskKind, Candidate, digest
from proteinrsi.tasks import predict_from_observed, validate_candidate
from proteinrsi.tools import ToolSpec

PREDICT_TOOL = "research_fit_predict"

def fit_predict(view, sequences, features, model=None):
    from .analysis import _validate
    from proteinrsi.dataflow.resources import scope_for
    _validate(view)
    for seq in sequences:
        validate_candidate(view.task, Candidate(sequence=seq),
                           enforce_universe=view.task.candidate_access == "pool")
    unique = {o.sequence for o in view.observations if o.qc == "valid"}
    rows = [{"sequence": s, "predicted_value": None, "uncertainty": None} for s in sequences]
    identity = None
    if len(unique) >= 2:
        if features == "mutation":
            prediction, _ = predict_from_observed(view.task, sequences, view.observations, view.workflow)
        elif features == "esmc":
            if model is None:
                raise ValueError("ESMC is not configured; no hidden fallback to another model")
            # Explicit request authorizes feature extraction, not separate prior scoring.
            from proteinrsi.tasks import _features
            groups = {}
            for o in view.observations:
                if o.qc == "valid":
                    groups.setdefault(o.sequence, []).append(float(o.value))
            observed = list(groups)
            embeddings = model.embed(observed + sequences)
            extra = _features(observed + sequences, view.task.reference_sequence,
                              view.task.mutable_positions, view.workflow.strategy == "pairwise")
            features_x = np.column_stack([embeddings, extra])
            x, q = features_x[:len(observed)], features_x[len(observed):]
            center, scale = x.mean(0), x.std(0)
            scale[scale < 1e-6] = 1.0
            x, q = (x-center)/scale, (q-center)/scale
            x, q = x/np.sqrt(x.shape[1]), q/np.sqrt(q.shape[1])
            y = np.asarray([np.mean(groups[s]) for s in observed])
            alpha = np.linalg.solve(x @ x.T + view.workflow.ridge_alpha*np.eye(len(x)), y-y.mean())
            prediction = q @ x.T @ alpha + y.mean()
            identity = model.identity
        else:
            raise ValueError("Unknown feature source")
        if not np.isfinite(prediction).all():
            raise ValueError("Nonfinite task predictions")
        for row, value in zip(rows, prediction):
            row["predicted_value"] = float(value)
    return {"predictions": rows, "features": features, "model": identity,
        "training_variants": len(unique), "metric": view.task.metric, "unit": view.task.unit,
        "scope": scope_for(view),
        "evidence_version": view.evidence_version, "workflow": view.workflow.version,
        "status": "predicted" if len(unique) >= 2 else "insufficient_observations",
        "warning": "Uncalibrated revealed-label Ridge estimates, not measured fitness or calibrated Kd."}

def register_prediction_tool(gateway, view, model=None):
    # Parent broker may supply this contextual binding to a restricted worker.
    if getattr(gateway, "remote_context_tools", False):
        return
    gateway._tools.pop(PREDICT_TOOL, None)
    props = {"sequences": {"type": "array", "minItems": 1, "maxItems": 384,
                          "uniqueItems": True, "items": {"type": "string", "pattern": "^[ACDEFGHIKLMNPQRSTVWY]+$"}},
             "features": {"enum": ["mutation", "esmc"] if model is not None else ["mutation"]}}
    from proteinrsi.dataflow.resources import scope_for
    spec = ToolSpec(name=PREDICT_TOOL, capability="property.predict", description="Fit a task-specific Ridge head on revealed measurements and predict supplied sequences ONLY on explicit request.",
        limitations="Needs at least two distinct valid observed variants. No calibrated uncertainty or generic affinity claim.",
        when_to_use=["You need a numeric task estimate based on current measured data."],
        when_not_to_use=["No sufficient measured data.", "You only need a sequence prior."],
        cost_hint="One tool call; mutation features use CPU only; esmc explicitly consumes uncached embedding inputs.",
        output_semantics="Uncalibrated estimates in task units; null on insufficient data; no measurements created.",
        implementation_version="explicit-ridge-v2:" + scope_for(view),
        task_kinds=[TaskKind.VARIANT, TaskKind.RANKING],
        input_schema={"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
        output_schema={"type": "object", "required": ["artifact_ref", "predictions", "evidence_kind", "evidence_version", "workflow", "scope", "metric", "unit", "status"],
            "properties": {"artifact_ref": {"type": "string", "pattern": "^task_predictions/[0-9a-f]{64}$"},
                "predictions": {"type": "array", "items": {"type": "object", "required": ["sequence", "predicted_value", "uncertainty"],
                "properties": {"sequence": {"type": "string"}, "predicted_value": {"type": ["number", "null"]}, "uncertainty": {"type": "null"}}}},
                "scope": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "evidence_version": {"type": "string"}, "workflow": {"type": "string"},
                "metric": {"type": "string"}, "unit": {"type": "string"},
                "evidence_kind": {"const": "proxy"}, "status": {"enum": ["predicted", "insufficient_observations"]}}})
    def execute(arguments):
        result = {**fit_predict(view, model=model, **arguments), "evidence_kind": "proxy"}
        key = digest(result)
        gateway.store.put("task_predictions", key, result, immutable=True)
        return {**result, "artifact_ref": "task_predictions/" + key}
    gateway.register(spec, execute)

def prediction_row(store, ref, sequence, *, scope=None, evidence_version=None,
                   workflow=None, metric=None, unit=None):
    """Read a content-addressed, trusted tool artifact; never trust a supplied number.

    Live resources require the exact task/round/evidence/workflow scope. Retrospective
    diagnostics instead check the frozen batch's evidence/workflow and task units.
    The worker may read task_predictions, but cannot write that protected namespace.
    """
    if not isinstance(ref, str) or not re.fullmatch(r"task_predictions/[0-9a-f]{64}", ref):
        raise ValueError("Invalid task prediction reference")
    data = store.get("task_predictions", ref.split("/", 1)[1])
    if not isinstance(data, dict) or "task_predictions/" + digest(data) != ref:
        raise ValueError("Unknown or modified task prediction artifact")
    expected = {"scope": scope, "evidence_version": evidence_version, "workflow": workflow,
                "metric": metric, "unit": unit}
    if (any(value is not None and data.get(key) != value for key, value in expected.items())
            or data.get("evidence_kind") != "proxy"
            or data.get("status") not in {"predicted", "insufficient_observations"}):
        raise ValueError("Prediction provenance mismatch")
    rows = [r for r in data.get("predictions", [])
            if isinstance(r, dict) and r.get("sequence") == sequence]
    if len(rows) != 1:
        raise ValueError("Prediction sequence mismatch")
    row = rows[0]
    value = row.get("predicted_value")
    if ("predicted_value" not in row or row.get("uncertainty") is not None
            or (value is not None and (isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value)))
            or ((value is None) != (data["status"] == "insufficient_observations"))):
        raise ValueError("Invalid task prediction value")
    return data, row


def attach_predictions(by_seq, refs, view, tool_results, store):
    from proteinrsi.dataflow.resources import scope_for
    # Only current execution's explicitly requested artifacts may annotate predictions.
    allowed = {r.get("artifact_ref"): r for r in tool_results if isinstance(r, dict)
               and isinstance(r.get("artifact_ref"), str)}
    for seq, ref in refs.items():
        if seq not in by_seq or ref not in allowed:
            raise ValueError("Prediction reference is not an explicitly obtained task prediction")
        data, row = prediction_row(store, ref, seq, scope=scope_for(view),
            evidence_version=view.evidence_version, workflow=view.workflow.version,
            metric=view.task.metric, unit=view.task.unit)
        if allowed[ref] != {**data, "artifact_ref": ref}:
            raise ValueError("Prediction result differs from its trusted tool artifact")
        value = row["predicted_value"]
        by_seq[seq] = by_seq[seq].model_copy(update={"predicted_value": value,
            "prediction_ref": ref, "uncertainty": None,
            "evidence_kind": "proxy" if value is not None else "none"})
