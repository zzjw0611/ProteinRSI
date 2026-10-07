# SPDX-License-Identifier: MIT
"""Bounded patches, prespecified experimental comparison, and scoped method memory."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from proteinrsi.contracts import (Batch, GatePolicy, GateResult, MetaPolicy, Observation,
                                  Patch, TaskKind, Workflow, digest)
from proteinrsi.storage import Store
from proteinrsi.metrics import metric_names, summarize_metrics


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
    if policy.criterion != "mean_bootstrap":
        raise ValueError("Multi-metric gates require complete metric vectors, not scalar mean scores")
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


def compare_metric_vectors(baseline: dict, challenger: dict, policy: GatePolicy, *,
                           n_baseline: int, n_challenger: int,
                           diagnostics: dict | None = None,
                           incomplete_reason: str | None = None) -> GateResult:
    """Frozen observed Pareto rule. No bootstrap, p-value, or max significance claim.

    Input metrics are direction-adjusted. For offline M these are equally weighted
    group means of case-level metric vectors, never extrema across unrelated cases.
    """
    if policy.criterion != "observed_pareto_v1":
        raise ValueError("Metric-vector comparison requires observed_pareto_v1")
    names = metric_names(policy.top_ns)
    if set(baseline) != set(names) or set(challenger) != set(names):
        raise ValueError("Metric vectors must exactly match the prespecified policy")
    if any(value is not None and not np.isfinite(value)
           for values in (baseline, challenger) for value in values.values()):
        raise ValueError("Non-finite evaluation data")
    comparisons = {}
    for name in names:
        old, new = baseline[name], challenger[name]
        delta = float(new - old) if old is not None and new is not None else None
        comparisons[name] = {"baseline": old, "challenger": new, "delta": delta,
            "absolute_tolerance": policy.absolute_tolerances[name],
            "improvement_margin": policy.improvement_margins[name],
            "improved": delta is not None and delta > policy.improvement_margins[name],
            "worsened": delta is not None and delta < -policy.absolute_tolerances[name]}
    details = {"criterion": policy.criterion, "policy": policy.model_dump(),
               "metric_orientation": "direction_adjusted_higher_is_better",
               "metrics": comparisons, "diagnostics": diagnostics or {},
               "evidence": "descriptive_observed_panel; no statistical significance or generalization claim"}
    decision = "inconclusive"
    if incomplete_reason:
        outcome, reason = "missing_coverage", incomplete_reason
    elif n_baseline < policy.min_per_arm or n_challenger < policy.min_per_arm:
        outcome, reason = "insufficient_units", "Too few independent experimental units/groups"
    elif any(row["delta"] is None for row in comparisons.values()):
        outcome, reason = "missing_metrics", "Complete prespecified top-N coverage is required in both arms/cases"
    else:
        improved = any(row["improved"] for row in comparisons.values())
        worsened = any(row["worsened"] for row in comparisons.values())
        positive = any(row["delta"] > 0 for row in comparisons.values())
        if improved and not worsened:
            decision, outcome = "accepted", "dominates"
            reason = "Observed Pareto improvement: no metric worse beyond tolerance and at least one improves beyond margin"
        elif worsened and positive:
            outcome, reason = "tradeoff", "Mixed observed gains and losses; no automatic promotion"
        elif worsened:
            decision, outcome = "rejected", "dominated"
            reason = "No observed metric improves and at least one worsens beyond tolerance"
        else:
            outcome, reason = "tie_or_below_margin", "No prespecified improvement beyond margin"
    details["outcome"] = outcome
    return GateResult(decision=decision, reason=reason, n_baseline=n_baseline,
                      n_challenger=n_challenger, details=details)


def evaluate_trial(batch: Batch, observations: list[Observation], policy: GatePolicy,
                   *, direction: str, reference_sequence: str | None = None) -> GateResult:
    if policy.criterion == "llm_adjudicated_v1":
        raise ValueError("LLM-adjudicated trials require a frozen E plan and actual E verdict")
    by_id = {o.sample_id: o for o in observations}
    if set(by_id) != {s.sample_id for s in batch.samples} or len(by_id) != len(observations):
        raise ValueError("Evaluation requires one final observation per scheduled sample")
    if any(by_id[s.sample_id].sequence != s.candidate.sequence for s in batch.samples):
        raise ValueError("Observation sequence differs from scheduled sample")
    groups: dict[str, dict[str, list[float]]] = {"baseline": defaultdict(list), "challenger": defaultdict(list)}
    counts = {"baseline": 0, "challenger": 0}
    failures = {"baseline": 0, "challenger": 0}
    rows = {"baseline": [], "challenger": []}
    parent_excluded = {"baseline": 0, "challenger": 0}
    for sample in batch.samples:
        if sample.arm not in {"baseline", "challenger"}:
            continue
        if policy.criterion == "observed_pareto_v1" and reference_sequence and sample.candidate.sequence == reference_sequence:
            parent_excluded[sample.arm] += 1
            continue
        counts[sample.arm] += 1
        o = by_id[sample.sample_id]
        rows[sample.arm].append(o.model_dump())
        if o.qc == "valid":
            groups[sample.arm][o.sequence].append(float(o.value))
        else:
            failures[sample.arm] += 1
    if counts["baseline"] != counts["challenger"] or not counts["challenger"]:
        raise ValueError("Unequal planned arm budgets")
    if direction not in {"maximize", "minimize"}:
        raise ValueError("Unknown metric direction")
    missing = any(row["qc"] == "unavailable" for arm in rows.values() for row in arm)
    excessive_qc = any(failures[a] / counts[a] > policy.max_qc_failure_fraction for a in counts)
    reason = ("Historical coverage missing; no promotion from a selectively observed subset" if missing else
              "QC failure limit exceeded; do not impute missing values" if excessive_qc else None)
    if policy.criterion == "mean_bootstrap" and reason:
        return GateResult(decision="inconclusive", reason=reason,
            n_baseline=len(groups["baseline"]), n_challenger=len(groups["challenger"]))
    if groups["baseline"].keys() & groups["challenger"].keys():
        raise ValueError("Overlapping variants are not independent evidence in this comparison")
    if policy.criterion == "observed_pareto_v1":
        summaries = {arm: summarize_metrics(values, direction=direction,
                        top_ns=policy.top_ns, submitted=counts[arm]) for arm, values in rows.items()}
        return compare_metric_vectors(summaries["baseline"]["signed_metrics"],
            summaries["challenger"]["signed_metrics"], policy,
            n_baseline=len(groups["baseline"]), n_challenger=len(groups["challenger"]),
            diagnostics={"arms": summaries, "parent_excluded": parent_excluded,
                "controls_and_research_excluded": True, "unit": "unique_sequence",
                "technical_repeats": "mean_within_sequence"}, incomplete_reason=reason)
    if reason:
        return GateResult(decision="inconclusive", reason=reason,
            n_baseline=len(groups["baseline"]), n_challenger=len(groups["challenger"]))
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
