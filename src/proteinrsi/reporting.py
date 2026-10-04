# SPDX-License-Identifier: MIT
"""Owner-facing reporting from revealed evidence and actual audit events only."""
from collections import Counter
from proteinrsi.contracts import TaskSpec
from proteinrsi.prompting import prompt_version

def mutation_list(reference, sequence):
    return [f"{a}{i+1}{b}" for i,(a,b) in enumerate(zip(reference,sequence)) if a != b]

def study_details(campaign):
    store, state = campaign.store, campaign.state
    task = TaskSpec.model_validate(state["task"])
    valid = [o for o in state["observations"] if o["qc"] == "valid"]
    better = max if task.direction == "maximize" else min
    best = better(valid, key=lambda o:o["value"]) if valid else None
    best_variant = ({"sequence": best["sequence"], "mutations": mutation_list(task.reference_sequence,best["sequence"]),
        "value": best["value"], "sample_id": best["sample_id"], "batch_id": best["batch_id"],
        "source": best["source"]} if best else None)
    timeline, cumulative, seen = [], [], set()
    # Event order is actual feedback order; initial parent isn't an extra round.
    for event in store.events():
        if event["kind"] not in ("initial_parent_revealed", "feedback_ingested"):
            continue
        bid = event["payload"]["batch_id"]
        if bid in seen:
            continue
        seen.add(bid)
        observations = store.get("measurements", bid, [])
        cumulative += observations
        known = [o for o in cumulative if o["qc"] == "valid"]
        batch = store.get("batches", bid)
        top = better(known, key=lambda o:o["value"]) if known else None
        timeline.append({"round": batch["round_index"]+1, "phase": batch.get("phase", "research"),
            "batch_id": bid, "batch_queries": len(observations), "cumulative_campaign_queries": len(cumulative),
            "unique_variants": len({o["sequence"] for o in cumulative}),
            "best_measured_value": top["value"] if top else None,
            "best_sequence": top["sequence"] if top else None})
    calls = [e["payload"] for e in store.events() if e["kind"] == "tool_completed"]
    jobs = list(store.all("tool_jobs").values())
    tokens = Counter()
    for record in store.all("llm").values():
        for k,v in record.get("usage", {}).items():
            if type(v) is int:
                tokens[k] += v
    n = len(state["observations"])
    return {"objective": task.objective_description or f"{task.direction} measured {task.metric}",
        "best_variant": best_variant, "round_progress": timeline,
        "unique_measured_variants": len({o["sequence"] for o in valid}),
        "returned_observations": n, "repeat_queries": n-len({o["sequence"] for o in state["observations"]}),
        "initial_observation_policy": task.initial_observation_policy,
        "candidate_access": task.candidate_access,
        "tool_summary": {"completed_by_name": dict(Counter(c["tool"] for c in calls)),
            "failed_jobs": sum(j.get("state")=="failed" for j in jobs),
            "requests": [{k:e["payload"].get(k) for k in ("tool","purpose","context","cache_hit")}
                         for e in store.events() if e["kind"] == "tool_requested"]},
        "provider_reported_tokens": dict(tokens), "monetary_cost": None,
        "monetary_cost_note": "Token counts are not prices; no invented currency total.",
        "prompt_bundle_version": prompt_version(store),
        "execution_semantics": state.get("execution_semantics", "legacy"),
        "measurement_note": "Historical replay values are not new wet-lab measurements; repeated lookups are not independent repeats."}
