# SPDX-License-Identifier: MIT
"""Task-specific validation, sequence edits, and a small observed-data-only baseline."""
from __future__ import annotations

from itertools import combinations
from typing import Iterable

import numpy as np

from proteinrsi.contracts import AMINO_ACIDS, Candidate, Observation, TaskKind, TaskSpec, Workflow


def apply_mutations(reference: str, edits: Iterable[dict]) -> str:
    result = list(reference)
    seen: set[int] = set()
    for edit in edits:
        if set(edit) != {"position", "from", "to"}:
            raise ValueError("Each edit needs position, from, to")
        position = edit["position"]
        if type(position) is not int or not 1 <= position <= len(reference) or position in seen:
            raise ValueError("Invalid or duplicate 1-based position")
        if reference[position - 1] != edit["from"] or edit["to"] not in AMINO_ACIDS:
            raise ValueError("Reference residue mismatch or noncanonical residue")
        if edit["from"] == edit["to"]:
            raise ValueError("No-op mutation")
        seen.add(position)
        result[position - 1] = edit["to"]
    return "".join(result)


def validate_candidate(task: TaskSpec, candidate: Candidate, *, enforce_universe: bool = True) -> None:
    seq = candidate.sequence
    if task.kind in (TaskKind.VARIANT, TaskKind.RANKING):
        if len(seq) != len(task.reference_sequence):
            raise ValueError("Mutation tasks do not allow insertions/deletions")
        changes = {i + 1 for i, (a, b) in enumerate(zip(task.reference_sequence, seq)) if a != b}
        if changes - set(task.mutable_positions) or len(changes) > task.max_mutations:
            raise ValueError("Fixed residues or mutation-count constraints violated")
    elif task.kind == TaskKind.AFFINITY and seq != task.reference_sequence:
        raise ValueError("Affinity inputs are immutable")
    elif task.kind == TaskKind.BINDER:
        # v0.1 implements fixed-length scaffold redesign, not arbitrary topology design.
        if len(seq) != len(task.reference_sequence):
            raise ValueError("This binder adapter requires a fixed-length starting scaffold")
        changes = {i + 1 for i, (a, b) in enumerate(zip(task.reference_sequence, seq)) if a != b}
        if changes - set(task.mutable_positions) or len(changes) > task.max_mutations:
            raise ValueError("Binder scaffold constraints violated")
    if enforce_universe and task.candidates and seq not in task.candidates and seq != task.reference_sequence:
        raise ValueError("Candidate is outside the explicitly allowed library")


def validate_task(task: TaskSpec) -> None:
    for sequence in task.candidates:
        validate_candidate(task, Candidate(sequence=sequence), enforce_universe=False)


def _features(sequences: list[str], reference: str, positions: list[int], pairwise: bool) -> np.ndarray:
    # A fixed feature map is independent of held-out phenotype labels.
    keys = [(p - 1, aa) for p in positions for aa in sorted(AMINO_ACIDS) if aa != reference[p - 1]]
    x = np.array([[float(seq[p] == aa) for p, aa in keys] for seq in sequences], dtype=float)
    if not keys:
        return np.zeros((len(sequences), 1))
    if pairwise:
        # Only cross-position interactions; use sparse-active columns to cap memory.
        pairs = [(i, j) for i, j in combinations(range(len(keys)), 2) if keys[i][0] != keys[j][0]]
        if len(pairs) > 15000:
            raise ValueError("Too many pairwise features; use an external fitted predictor")
        x = np.column_stack([x] + [x[:, i] * x[:, j] for i, j in pairs])
    return x


def predict_from_observed(
    task: TaskSpec, sequences: list[str], observations: list[Observation], workflow: Workflow,
) -> tuple[np.ndarray, np.ndarray]:
    """Ridge baseline. Novelty is a heuristic, NOT calibrated uncertainty/affinity."""
    if not sequences:
        return np.array([]), np.array([])
    valid = [o for o in observations if o.qc == "valid" and o.value is not None]
    if not valid:
        return np.zeros(len(sequences)), np.ones(len(sequences))
    groups: dict[str, list[float]] = {}
    for o in valid:
        groups.setdefault(o.sequence, []).append(float(o.value))
    observed = list(groups)
    y = np.array([np.mean(groups[s]) for s in observed])
    all_x = _features(observed + sequences, task.reference_sequence, task.mutable_positions,
                      workflow.strategy == "pairwise")
    x, query = all_x[:len(observed)], all_x[len(observed):]
    center, y_center = x.mean(axis=0), float(y.mean())
    x, query = x - center, query - center
    # Dual form: matrix size is the number of measured variants, not all pairwise features.
    alpha = np.linalg.solve(x @ x.T + np.eye(len(x)) * workflow.ridge_alpha, y - y_center)
    prediction = query @ x.T @ alpha + y_center
    novelty = np.array([min(sum(a != b for a, b in zip(s, o)) for o in observed)
                        / max(1, len(task.mutable_positions)) for s in sequences])
    return prediction, novelty


def rank_candidates(task: TaskSpec, sequences: list[str], observations: list[Observation],
                    workflow: Workflow, seed: int) -> list[Candidate]:
    prediction, novelty = predict_from_observed(task, sequences, observations, workflow)
    rng = np.random.default_rng(seed)
    direction = 1 if task.direction == "maximize" else -1
    scale = max(float(np.std(prediction)), 0.1)
    if workflow.strategy == "diverse":
        scores = novelty + rng.uniform(0, 0.01, len(sequences))
    else:
        scores = direction * prediction + workflow.exploration * scale * novelty
        scores += rng.uniform(0, 1e-8, len(sequences))
    order = np.argsort(-scores, kind="stable")
    return [Candidate(sequence=sequences[i], source="observed_only_ridge",
                      predicted_value=float(prediction[i]), evidence_kind="proxy",
                      rationale="Ridge prediction from revealed labels; novelty is uncalibrated.")
            for i in order]
