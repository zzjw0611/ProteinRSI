# SPDX-License-Identifier: MIT
"""Original artificial fixture. These numbers are NOT protein measurements."""
from __future__ import annotations

import csv
from itertools import product
from pathlib import Path
import json

import numpy as np

from proteinrsi.contracts import BudgetSpec, TaskSpec


def make_fixture(directory: str | Path, *, seed: int = 17, rounds: int = 5) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reference = "ACDEFGHIKLMNPQRSTVWY"
    positions = [2, 5, 9, 14]
    options = [[reference[p - 1]] + [a for a in "ACDE" if a != reference[p - 1]][:2] for p in positions]
    rng = np.random.default_rng(seed)
    weights = rng.normal(0, 0.4, (4, 3))
    interactions = rng.normal(0, 0.8, (3, 3))
    records = []
    for indices in product(range(3), repeat=4):
        sequence = list(reference)
        for i, (p, idx) in enumerate(zip(positions, indices)):
            sequence[p - 1] = options[i][idx]
        value = 5 + sum(weights[i, idx] for i, idx in enumerate(indices))
        value += interactions[indices[0], indices[1]] + 0.8 * (indices[2] == indices[3])
        records.append({"sequence": "".join(sequence), "value": float(value), "qc": "valid"})
    path = directory / "synthetic_labels.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sequence", "value", "qc"])
        writer.writeheader()
        writer.writerows(records)
    task = TaskSpec(name="Artificial combinatorial landscape; NOT a wet experiment",
        reference_sequence=reference, mutable_positions=positions, max_mutations=4,
        candidates=[r["sequence"] for r in records], metric="synthetic_fitness", unit="synthetic_a.u.",
        feedback_source="synthetic", assay_protocol="artificial-fixture-v1", batch_size=12,
        max_rounds=rounds, seed=seed, budget=BudgetSpec(experimental_wells=12 * rounds))
    task_path = directory / "task.json"
    task_path.write_text(json.dumps(task.model_dump(mode="json"), indent=2) + "\n")
    return task_path, path
