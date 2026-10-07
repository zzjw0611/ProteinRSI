# SPDX-License-Identifier: MIT
"""Descriptive metrics from saved samples only; never an oracle or promotion gate.

Charged-query windows use immutable sample order within ledger reservation order.
They are retrospective accounting windows, not simulated early assay disclosures.
"""
from __future__ import annotations

from proteinrsi.contracts import digest
from proteinrsi.metrics import summarize_metrics


def metric_display_names(direction: str, top_ns=(5, 10)) -> dict[str, str]:
    return {"best": "max" if direction == "maximize" else "min",
            **{f"top{n}mean": f"top{n}mean" for n in top_ns}, "avg": "avg"}


def align_observations(samples: list[dict], observations: list[dict]) -> list[dict | None]:
    """Validate saved identities before aligning missing results to charged slots."""
    expected = {s["sample_id"]: s["candidate"]["sequence"] for s in samples}
    if len(expected) != len(samples):
        raise ValueError("Duplicate frozen sample identity")
    found = {}
    for observation in observations:
        identity = observation["sample_id"]
        if identity in found or identity not in expected:
            raise ValueError("Duplicate or unknown returned sample identity")
        if observation["sequence"] != expected[identity]:
            raise ValueError("Frozen sample identity differs from measurement")
        found[identity] = observation
    return [found.get(s["sample_id"]) for s in samples]


def sample_metrics(samples: list[dict], observations: list[dict], *, reference: str = "",
                   direction: str = "maximize", top_ns=(5, 10)) -> dict:
    """Round/arm/window outcomes exclude the parent, with excluded costs visible."""
    aligned = align_observations(samples, observations)
    all_stats = summarize_metrics(observations, direction=direction, top_ns=top_ns,
                                  submitted=len(samples))
    eligible = [(s, o) for s, o in zip(samples, aligned)
                if not reference or s["candidate"]["sequence"] != reference]
    stats = summarize_metrics([o for _, o in eligible if o is not None],
                              direction=direction, top_ns=top_ns, submitted=len(eligible))
    return {**stats, "metric_display_names": metric_display_names(direction, top_ns),
            "submitted_denominators": all_stats["denominators"],
            "parent": {"included": False, "excluded_submissions": len(samples) - len(eligible),
                       "excluded_returned": len(observations) - sum(o is not None for _, o in eligible)}}


def arm_metrics(samples: list[dict], observations: list[dict], *, reference: str = "",
                direction: str = "maximize", top_ns=(5, 10)) -> dict:
    align_observations(samples, observations)
    result = {}
    for arm in sorted({s["arm"] for s in samples}):
        selected = [s for s in samples if s["arm"] == arm]
        ids = {s["sample_id"] for s in selected}
        result[arm] = sample_metrics(selected, [o for o in observations if o["sample_id"] in ids],
                                    reference=reference, direction=direction, top_ns=top_ns)
    return result


def campaign_metrics(observations: list[dict], *, submitted: int, initial: list[dict],
                     reference: str = "", direction: str = "maximize", top_ns=(5, 10)) -> dict:
    """Metrics include known initial parent evidence once, outside query denominators."""
    evidence = [*initial, *observations]
    stats = summarize_metrics(evidence, direction=direction, top_ns=top_ns,
                              submitted=submitted + len(initial))
    query = summarize_metrics(observations, direction=direction, top_ns=top_ns,
                              submitted=submitted)
    parents = [o for o in evidence if o["sequence"] == reference and o["qc"] == "valid"]
    return {**stats, "metric_display_names": metric_display_names(direction, top_ns),
            "denominators": query["denominators"],
            "evidence_denominators": stats["denominators"],
            "parent": {"included": True, "valid_observations": len(parents),
                       "unique_valid_sequences": int(bool(parents)),
                       "uncharged_initial_observations": len(initial),
                       "uncharged_valid_parent_observations": sum(
                           o["sequence"] == reference and o["qc"] == "valid" for o in initial)}}


def charged_metrics(batches: dict[str, dict], measurements: dict[str, list[dict]],
                    charges: list[dict], *, initial: list[dict], reference: str = "",
                    direction: str = "maximize", top_ns=(5, 10), query_bin_size: int = 100) -> dict:
    """Reconstruct every committed well, or explicitly withhold comparable curves.

    Some historical offline evaluators did not retain sponsored branch outcomes.
    Their real costs remain in denominators; no scores or identities are invented.
    """
    if type(query_bin_size) is not int or query_bin_size < 1:
        raise ValueError("Query bin size must be a positive integer")
    committed = [c for c in charges
                 if c["resource"] == "experimental_wells" and c["state"] == "committed"]
    by_fingerprint = {digest(batch): bid for bid, batch in batches.items()}
    total, reconstructed, observations, slots, seen, endpoints = 0, 0, [], [], set(), {}
    unmatched = []
    for charge in committed:
        amount = charge["amount"]
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid committed experimental query count")
        total += amount
        key = charge["key"]
        bid = (key if key in batches else key[4:] if key.startswith("lab-") and key[4:] in batches
               else by_fingerprint.get(charge.get("fingerprint")))
        if bid is None or bid in seen or len(batches[bid]["samples"]) != amount:
            unmatched.append({"charge_key": key, "charged_queries": amount})
            continue
        seen.add(bid)
        batch = batches[bid]
        returned = measurements.get(bid, [])
        aligned = align_observations(batch["samples"], returned)
        slots.extend(zip(batch["samples"], aligned))
        observations.extend(o for o in aligned if o is not None)
        reconstructed += amount
        endpoints[bid] = {"cumulative_charged_queries": total,
                          "unreconstructed_charged_queries": total - reconstructed,
                          "campaign_metrics": campaign_metrics(observations, submitted=total,
                              initial=initial, reference=reference, direction=direction, top_ns=top_ns)}
    curve = []
    if not unmatched:
        for start in range(0, total, query_bin_size):
            end = min(start + query_bin_size, total)
            window = slots[start:end]
            curve.append({"query_start": start + 1, "query_end": end,
                "charged_queries": end - start, "complete_bin": end - start == query_bin_size,
                "window_metrics": sample_metrics([s for s, _ in window],
                    [o for _, o in window if o is not None], reference=reference,
                    direction=direction, top_ns=top_ns),
                "campaign_metrics": campaign_metrics([o for _, o in slots[:end] if o is not None],
                    submitted=end, initial=initial, reference=reference,
                    direction=direction, top_ns=top_ns)})
    return {"charged_queries": total, "reconstructed_charged_queries": reconstructed,
            "unreconstructed_charged_queries": total - reconstructed,
            "unreconstructed_charges": unmatched, "query_bin_size": query_bin_size,
            "equal_query_curve_status": "complete" if not unmatched else "withheld_unreconstructed_charges",
            "equal_query_curve": curve, "batch_endpoints": endpoints,
            "initial_metrics": campaign_metrics([], submitted=0, initial=initial,
                reference=reference, direction=direction, top_ns=top_ns),
            "campaign_metrics": campaign_metrics(observations, submitted=total, initial=initial,
                reference=reference, direction=direction, top_ns=top_ns),
            "query_order_note": "Committed ledger reservation order, then frozen sample order. "
                "Retrospective query windows, not early disclosure within an assay batch. "
                "Unavailable, failed, control and validation wells retain their charged slots; "
                "a final short window is explicitly marked. Reserved/released wells are excluded."}
