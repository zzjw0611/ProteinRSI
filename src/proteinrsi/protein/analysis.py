# SPDX-License-Identifier: MIT
"""ESMC priors and observed-label ridge heads, not calibrated functional predictions."""
from __future__ import annotations

import numpy as np

from proteinrsi.contracts import Candidate, TaskView, digest
from proteinrsi.tasks import _features


def rank_with_esmc(view: TaskView, candidates: list[Candidate], model) -> tuple[list[Candidate], dict]:
    task, workflow = view.task, view.workflow
    sequences = [candidate.sequence for candidate in candidates]
    prior = np.asarray(model.score_variants(task.reference_sequence, sequences))
    groups: dict[str, list[float]] = {}
    for observation in view.observations:
        if observation.qc != "valid" or observation.value is None:
            continue
        if (observation.metric, observation.unit, observation.assay_protocol, observation.source) != (
                task.metric, task.unit, task.assay_protocol, task.feedback_source):
            raise ValueError("Do not mix experimental protocols, units or sources in the fitted head")
        groups.setdefault(observation.sequence, []).append(float(observation.value))
    observed = list(groups)
    # The sequence prior has no assay direction; a more likely residue is not necessarily
    # a lower Kd, a higher activity, or a better protein for this task.
    prediction = None
    if len(observed) >= 2:
        features = model.embed(observed + sequences)
        # Retain evolvable additive/pairwise mutation features alongside the fixed backbone.
        extra = _features(observed + sequences, task.reference_sequence, task.mutable_positions,
                          workflow.strategy == "pairwise")
        features = np.column_stack([features, extra])
        x, query = features[:len(observed)], features[len(observed):]
        center, scale = x.mean(0), x.std(0)
        scale[scale < 1e-6] = 1.0
        x, query = (x - center) / scale, (query - center) / scale
        # Normalize overall feature dimension, not using any query-label statistics.
        x, query = x / np.sqrt(x.shape[1]), query / np.sqrt(query.shape[1])
        y = np.asarray([np.mean(groups[s]) for s in observed])
        mean = y.mean()
        alpha = np.linalg.solve(x @ x.T + workflow.ridge_alpha * np.eye(len(x)), y - mean)
        prediction = query @ x.T @ alpha + mean
        distance = np.sqrt(np.maximum(0, ((query[:, None] - x[None]) ** 2).sum(-1))).min(1)
        novelty = distance / max(float(distance.max()), 1e-8)
        score = prediction * (1 if task.direction == "maximize" else -1)
        score = score + workflow.exploration * max(float(prediction.std()), 0.1) * novelty
    else:
        score = prior.copy()
        novelty = np.asarray([sum(a != b for a, b in zip(s, task.reference_sequence))
                              / max(1, len(task.mutable_positions)) for s in sequences])
    if workflow.strategy == "diverse":
        score = novelty
    rng = np.random.default_rng(task.seed + view.round_index)
    order = np.argsort(-(score + rng.uniform(0, 1e-9, len(score))), kind="stable")
    ranked = []
    for i in order:
        note = (f"ESMC600M masked-marginal prior={prior[i]:.6g}; "
                + ("revealed-label ridge head (uncalibrated)." if prediction is not None
                   else "insufficient measured variants for a fitted head; prior only."))
        ranked.append(candidates[i].model_copy(update={
            "predicted_value": float(prediction[i]) if prediction is not None else None,
            "uncertainty": None, "evidence_kind": "proxy",
            "rationale": (candidates[i].rationale + "\n" + note).strip(),
        }))
    report = {"model": model.identity, "evidence_version": view.evidence_version,
              "workflow": workflow.version, "training_variants": len(observed),
              "prediction_kind": "observed_label_ridge" if prediction is not None else "sequence_prior_only",
              "score_method": "sum of WT-context single-mask log-odds; not joint epistasis",
              "scores": [{"sequence": s, "sequence_prior": float(prior[i]),
                          "task_prediction": float(prediction[i]) if prediction is not None else None}
                         for i, s in enumerate(sequences)]}
    model.store.put("plm_analysis", digest(report), report, immutable=True)
    return ranked, report
