# SPDX-License-Identifier: MIT
"""Descriptive measured-outcome metrics, with explicit coverage and unique units.

These are observed panel summaries, not population estimates or significance tests.
Callers choose the cohort (for example, excluding a supplied parent and controls).
"""
from collections import defaultdict
from math import isfinite
from statistics import mean


def metric_names(top_ns=(5, 10)) -> list[str]:
    return ["best", *(f"top{n}mean" for n in top_ns), "avg"]


def summarize_metrics(observations: list[dict], *, direction: str = "maximize",
                      top_ns=(5, 10), submitted: int | None = None) -> dict:
    if direction not in {"maximize", "minimize"}:
        raise ValueError("Unknown metric direction")
    if not top_ns or any(type(n) is not int or n < 1 for n in top_ns) or len(set(top_ns)) != len(top_ns):
        raise ValueError("top_ns requires distinct positive integers")
    submitted = len(observations) if submitted is None else submitted
    if type(submitted) is not int or submitted < len(observations):
        raise ValueError("Submitted count cannot be smaller than returned observations")
    groups = defaultdict(list)
    counts = {"submitted": submitted, "returned": len(observations), "valid": 0,
              "unavailable": 0, "other_nonvalid": 0,
              "not_returned": submitted - len(observations)}
    for row in observations:
        qc = row.get("qc", "valid")
        if qc == "valid":
            value = row.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError("Valid metrics require finite measured values")
            if not row.get("sequence"):
                raise ValueError("Unique-sequence metrics require sequence identity")
            groups[row["sequence"]].append(float(value))
            counts["valid"] += 1
        elif row.get("value") is not None:
            raise ValueError("Non-valid measurements cannot contain numeric outcomes")
        elif qc == "unavailable":
            counts["unavailable"] += 1
        elif qc in {"failed", "inconclusive"}:
            counts["other_nonvalid"] += 1
        else:
            raise ValueError("Unknown observation QC")
    # Sort within groups as well as across them to make aggregation independent of input order.
    values = sorted((mean(sorted(v)) for v in groups.values()), reverse=direction == "maximize")
    n = len(values)
    counts.update(unique_valid=n, technical_repeats=counts["valid"] - n,
                  nonvalid=counts["unavailable"] + counts["other_nonvalid"])
    metrics = {"best": values[0] if n else None}
    top_n = {}
    for size in top_ns:
        name = f"top{size}mean"
        metrics[name] = mean(values[:size]) if n >= size else None
        top_n[name] = {"required": size, "effective": min(size, n), "complete": n >= size}
    metrics["avg"] = mean(values) if n else None
    sign = 1 if direction == "maximize" else -1
    return {"metrics": metrics,
            "signed_metrics": {name: sign * value if value is not None else None
                               for name, value in metrics.items()},
            "denominators": counts, "top_n": top_n, "direction": direction,
            "aggregation": "unique_sequence_mean"}
