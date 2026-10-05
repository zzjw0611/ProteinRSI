# SPDX-License-Identifier: MIT
"""Owner-facing reporting from revealed evidence and actual audit events only."""
from collections import Counter
from proteinrsi.contracts import TaskSpec
from proteinrsi.prompting import prompt_version

def mutation_list(reference, sequence):
    if not reference:
        return None
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
    initial = store.get("configuration", "provided_initial_evidence", {}).get("observations", [])
    initial_ids = {o["sample_id"] for o in initial}
    queried = [o for o in state["observations"] if o["sample_id"] not in initial_ids]
    timeline, cumulative, seen = [], list(initial), set()
    cumulative_queries = 0
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
        cumulative_queries += len(observations)
        known = [o for o in cumulative if o["qc"] == "valid"]
        batch = store.get("batches", bid)
        top = better(known, key=lambda o:o["value"]) if known else None
        timeline.append({"round": batch["round_index"]+1, "phase": batch.get("phase", "research"),
            "batch_id": bid, "batch_queries": len(observations), "plate_capacity": task.batch_size,
            "plate_utilization": len(observations)/task.batch_size, "cumulative_campaign_queries": cumulative_queries,
            "unique_variants": len({o["sequence"] for o in cumulative}),
            "best_measured_value": top["value"] if top else None,
            "best_sequence": top["sequence"] if top else None})
    calls = [e["payload"] for e in store.events() if e["kind"] == "tool_completed"]
    calls += [e["payload"]["payload"] for e in store.events()
              if e["kind"] == "validation_event" and e["payload"]["kind"] == "tool_completed"]
    jobs = [*store.all("tool_jobs").values(), *store.all("validation_tool_jobs").values()]
    tokens = Counter()
    records = list(store.all("llm_diagnostics").values())
    for prefix in ("", "validation_"):
        attempts = store.all(prefix + "llm_attempts")
        records.extend(attempts.values())
        for key, record in store.all(prefix + "llm").items():
            if f"{key}/attempt-{record.get('attempt', 1)}" not in attempts:
                records.append(record)
    for record in records:
        for k,v in record.get("usage", {}).items():
            if type(v) is int:
                tokens[k] += v
    n = len(queried)
    return {"objective": task.objective_description or f"{task.direction} measured {task.metric}",
        "best_variant": best_variant, "round_progress": timeline,
        "unique_measured_variants": len({o["sequence"] for o in valid}),
        "returned_observations": n,
        "unavailable_queries": sum(o["qc"] == "unavailable" for o in queried),
        "valid_query_measurements": sum(o["qc"] == "valid" for o in queried), "provided_initial_observations": len(initial),
        "initial_evidence": store.get("configuration", "provided_initial_evidence"),
        "repeat_queries": n-len({o["sequence"] for o in queried}),
        "initial_observation_policy": task.initial_observation_policy,
        "candidate_access": task.candidate_access, "batch_fill_policy": task.batch_fill_policy,
        "capacity": campaign.view().capacity,
        "workflow_validation_outcomes": store.all("workflow_validation_outcomes"),
        "tool_summary": {"completed_by_name": dict(Counter(c["tool"] for c in calls)),
            "failed_jobs": sum(j.get("state")=="failed" for j in jobs),
            "generated_code_failures": sum(j.get("spec", {}).get("name") == "research_python"
                and j.get("result", {}).get("status") == "failed" for j in jobs),
            "requests": [{k:e["payload"].get(k) for k in ("tool","purpose","context","cache_hit")}
                         for e in store.events() if e["kind"] == "tool_requested"]},
        "provider_reported_tokens": dict(tokens), "monetary_cost": None,
        "monetary_cost_note": "Token counts are not prices; no invented currency total.",
        "prompt_bundle_version": prompt_version(store),
        "execution_semantics": state.get("execution_semantics", "legacy"),
        "measurement_note": ("Computational results are unvalidated proxies, not laboratory observations."
            if task.execution_mode == "computational" else
            "Historical replay values are not new wet-lab measurements; repeated lookups are not independent repeats.")}
