# SPDX-License-Identifier: MIT
"""Bounded patches, prespecified experimental comparison, and scoped method memory."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from proteinrsi.contracts import (Batch, GatePolicy, GateResult, MetaPolicy, Observation,
                                  Patch, TaskKind, Workflow, digest)
from proteinrsi.storage import Store


def apply_patch(current: Workflow | MetaPolicy, patch: Patch) -> Workflow | MetaPolicy:
    expected_target = "workflow" if isinstance(current, Workflow) else "meta"
    if patch.target != expected_target or patch.base_version != current.version:
        raise ValueError("Patch target/base version mismatch")
    # No arbitrary Python execution, dynamic imports or mutation of the trusted core.
    allowed = set(type(current).model_fields)
    if isinstance(current, MetaPolicy):
        allowed -= {"enabled"}  # Administrative on/off setting cannot self-enable.
    if not patch.changes or set(patch.changes) - allowed:
        raise PermissionError("Patch contains empty, protected or unknown fields")
    data = current.model_dump()
    data.update(patch.changes)
    candidate = type(current).model_validate(data)
    if candidate.version == current.version:
        raise ValueError("No-op patch")
    return candidate


def _bootstrap_delta(old: np.ndarray, new: np.ndarray, policy: GatePolicy,
                     *, paired: bool, seed: int) -> tuple[float, tuple[float, float]]:
    rng = np.random.default_rng(seed)
    if paired:
        if len(old) != len(new):
            raise ValueError("Paired comparisons require aligned cases")
        differences = new - old
        boot = np.mean(rng.choice(differences, size=(policy.bootstrap_samples, len(old))), axis=1)
    else:
        boot = (rng.choice(new, size=(policy.bootstrap_samples, len(new))).mean(axis=1)
                - rng.choice(old, size=(policy.bootstrap_samples, len(old))).mean(axis=1))
    q = (1 - policy.confidence) / 2
    interval = tuple(float(x) for x in np.quantile(boot, [q, 1 - q]))
    return float(new.mean() - old.mean()), interval


def compare_scores(old: list[float], new: list[float], policy: GatePolicy,
                   *, paired: bool = False, seed: int = 0) -> GateResult:
    n_old, n_new = len(old), len(new)
    if n_old < policy.min_per_arm or n_new < policy.min_per_arm:
        return GateResult(decision="inconclusive", reason="Too few independent experimental units/cases",
                          n_baseline=n_old, n_challenger=n_new)
    arrays = np.asarray(old, dtype=float), np.asarray(new, dtype=float)
    if not all(np.isfinite(a).all() for a in arrays):
        raise ValueError("Non-finite evaluation data")
    effect, interval = _bootstrap_delta(*arrays, policy, paired=paired, seed=seed)
    if interval[0] > policy.min_effect:
        decision = "accepted"
    elif interval[1] < -policy.min_effect:
        decision = "rejected"
    else:
        decision = "inconclusive"
    return GateResult(decision=decision, effect=effect, interval=interval,
        reason="Single prespecified exploratory bootstrap comparison; not a generalization guarantee",
        n_baseline=n_old, n_challenger=n_new)


def evaluate_trial(batch: Batch, observations: list[Observation], policy: GatePolicy,
                   *, direction: str) -> GateResult:
    by_id = {o.sample_id: o for o in observations}
    if set(by_id) != {s.sample_id for s in batch.samples} or len(by_id) != len(observations):
        raise ValueError("Evaluation requires one final observation per scheduled sample")
    groups: dict[str, dict[str, list[float]]] = {"baseline": defaultdict(list), "challenger": defaultdict(list)}
    counts = {"baseline": 0, "challenger": 0}
    failures = {"baseline": 0, "challenger": 0}
    for sample in batch.samples:
        if sample.arm not in {"baseline", "challenger"}:
            continue
        counts[sample.arm] += 1
        o = by_id[sample.sample_id]
        if o.qc == "valid":
            groups[sample.arm][o.sequence].append(float(o.value))
        else:
            failures[sample.arm] += 1
    if counts["baseline"] != counts["challenger"] or not counts["challenger"]:
        raise ValueError("Unequal planned arm budgets")
    if any(by_id[s.sample_id].qc == "unavailable" for s in batch.samples if s.arm in {"baseline", "challenger"}):
        return GateResult(decision="inconclusive", reason="Historical coverage missing; no promotion from a selectively observed subset",
            n_baseline=len(groups["baseline"]), n_challenger=len(groups["challenger"]))
    if any(failures[a] / counts[a] > policy.max_qc_failure_fraction for a in counts):
        return GateResult(decision="inconclusive", reason="QC failure limit exceeded; do not impute missing values",
            n_baseline=len(groups["baseline"]), n_challenger=len(groups["challenger"]))
    if groups["baseline"].keys() & groups["challenger"].keys():
        raise ValueError("Overlapping variants are not independent evidence in this comparison")
    sign = 1 if direction == "maximize" else -1
    # Technical replicates are aggregated; they do not inflate the sample size.
    values = {arm: [sign * float(np.mean(v)) for v in seqs.values()] for arm, seqs in groups.items()}
    return compare_scores(values["baseline"], values["challenger"], policy,
                          seed=int(digest(batch.batch_id)[:8], 16))


class ExperienceMemory:
    def __init__(self, store: Store):
        self.store = store

    def record(self, patch: Patch, result: GateResult, *, campaign_id: str,
               observations: int, source: str) -> None:
        record = {"patch": patch.model_dump(mode="json"), "result": result.model_dump(),
                  "scope": {"task_kind": patch.task_kind.value, "campaign_id": campaign_id,
                            "observations_at_trial": observations, "evidence_source": source},
                  "status": "local_support" if result.decision == "accepted" else result.decision,
                  "transfer_validated": False}
        self.store.put("experience", patch.patch_id, record, immutable=True)

    def retrieve(self, kind: TaskKind, *, include_unvalidated: bool = False) -> list[dict[str, Any]]:
        return [record for record in self.store.all("experience").values()
                if record["scope"]["task_kind"] == kind.value
                and (record["status"] == "local_support" or include_unvalidated)]
