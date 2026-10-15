# SPDX-License-Identifier: MIT
"""Current research state: controller facts and replaceable agent working memory.

The immutable receipts remain the authority. This projection never grants access
to an oracle or replaces the full revealed TaskView available to local analysis.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from typing import Annotated

from pydantic import Field

from proteinrsi.contracts import Model, digest, sequence_hash


MemoryText = Annotated[str, Field(max_length=1000)]


class WorkingMemory(Model):
    focus: str = Field(default="", max_length=2000)
    hypotheses: list[MemoryText] = Field(default_factory=list, max_length=8)
    open_questions: list[MemoryText] = Field(default_factory=list, max_length=8)
    evidence_refs: list[MemoryText] = Field(default_factory=list, max_length=24)


def enabled(store):
    return store.get("configuration", "research", {}).get("context_policy") == "state-v1"


def current_state(store, state, *, memory=None):
    """Recompute facts; an agent can only supply WorkingMemory, never facts."""
    active = store.get("research_state", "active", {})
    previous = store.get("research_states", active.get("ref", ""), {})
    observations, task = state["observations"], state["task"]
    valid = [row for row in observations if row.get("qc") == "valid"
             and isinstance(row.get("value"), (float, int)) and math.isfinite(row["value"])]
    best = (min if task.get("direction") == "minimize" else max)(
        valid, key=lambda row: row["value"]) if valid else None
    latest_batch = observations[-1].get("batch_id") if observations else None
    latest = [row for row in observations if row.get("batch_id") == latest_batch]
    facts = {"round": state["round_index"], "max_rounds": task["max_rounds"],
        "remaining_queries": store.remaining("experimental_wells"),
        "query_usage": store.usage().get("experimental_wells", {}),
        "evidence_version": digest(observations), "observations": len(observations),
        "qc_counts": dict(Counter(row.get("qc") for row in observations)),
        "best_measured": deepcopy(best), "latest_batch_id": latest_batch,
        "latest_batch_count": len(latest), "latest_qc_counts": dict(Counter(row.get("qc") for row in latest)),
        "metric": task["metric"], "unit": task["unit"], "direction": task.get("direction", "maximize")}
    from proteinrsi.localtools.artifacts import ArtifactStore
    artifacts = [{key: row[key] for key in ("ref", "kind", "sha256", "origin") if key in row}
                 for row in ArtifactStore(store).catalog(task_only=True)[-12:]]
    return {"format": "research-state-v1", "facts": facts,
        "working_memory": WorkingMemory.model_validate(
            memory if memory is not None else previous.get("working_memory", {})).model_dump(mode="json"),
        "artifact_refs": artifacts, "previous_state_ref": active.get("ref")}


def checkpoint_state(store, state, *, memory=None, source_ref):
    """Called inside the feedback transaction, once for the committed receipt."""
    value = {**current_state(store, state, memory=memory), "source_ref": source_ref}
    ref = digest(value)
    store.put("research_states", ref, value, immutable=True)
    store.put("research_state", "active", {"ref": ref})
    store.event("research_state_updated", {"round": state["round_index"],
        "research_state_ref": ref, "previous_state_ref": value["previous_state_ref"],
        "source_ref": source_ref, "best_measured": value["facts"]["best_measured"],
        "working_memory": value["working_memory"]})
    return ref


def execution_summary(store):
    """Bounded, branch-local summary of actual events, not model self-reports.

    Forwarded child events are excluded. The same implementation supplies the
    Improver and the Evaluator; all raw receipts remain available for inspection.
    """
    counts, failures, starts, duration = Counter(), [], {}, 0.0
    context_bytes, tokens, improver_behavior = [], [], []
    kinds = ("llm_started", "llm_completed", "llm_failed", "tool_completed", "tool_failed",
             "context_prepared", "context_read_completed", "research_plan_revised", "research_blocked",
             "candidate_validation_feedback", "evaluation_response_repair", "generated_code_completed",
             "meta_analysis_completed")
    with store.connect() as con:
        rows = con.execute("SELECT id,timestamp,kind,payload FROM events WHERE kind IN (" +
                           ",".join("?" for _ in kinds) + ") ORDER BY id", kinds)
        for event_id, timestamp, kind, raw in rows:
            payload = json.loads(raw)
            counts[kind] += 1
            if kind == "meta_analysis_completed":
                improver_behavior.append({key: payload.get(key) for key in
                    ("round", "meta", "target", "patch_id", "tool_rounds_used")})
            key = payload.get("attempt_key", payload.get("key"))
            if kind == "llm_started":
                starts[key] = timestamp
            elif kind in {"llm_completed", "llm_failed"}:
                started = starts.pop(key, None)
                if started is not None:
                    duration += timestamp - started
            if kind.endswith("failed") or kind == "research_blocked" or payload.get("status") == "failed":
                failures.append({"event_id": event_id, "kind": kind,
                    **{key: payload[key] for key in ("role", "tool", "error_type", "error", "step_id") if key in payload}})
            if kind == "context_prepared":
                context_bytes.append(payload["context_bytes"])
                if payload.get("estimated_input_tokens") is not None:
                    tokens.append(payload["estimated_input_tokens"])
    # Tool wrappers may succeed while the executed Python returns a failure.
    tool_failures, checked_ids, check_names = 0, set(), set()
    for receipt in store.all("tool_jobs").values():
        result = receipt.get("result") or {}
        if not isinstance(result, dict):
            continue
        if result.get("status") == "failed":
            tool_failures += 1
        output = result.get("output", result)
        if isinstance(output, dict) and isinstance(output.get("checks"), list):
            checks = output["checks"]
            if checks and all(isinstance(row, dict) and row.get("passed") is True for row in checks):
                checked_ids.update(output.get("candidate_ids", []))
                check_names.update(row["name"] for row in checks if isinstance(row.get("name"), str))
    invalid_responses = sum(receipt.get("status") == "invalid"
                            for receipt in store.all("research_step_outputs").values())
    submitted_ids = {"seq:" + sequence_hash(sample["candidate"]["sequence"])
                     for batch in store.all("batches").values() for sample in batch["samples"]}
    return {"authority": "controller_execution_receipts", "scope": "this_branch_only",
        "event_counts": dict(counts), "failures": failures[-8:], "tool_result_failures": tool_failures,
        "invalid_response_count": invalid_responses,
        "repair_count": counts["research_plan_revised"] + counts["candidate_validation_feedback"] + counts["evaluation_response_repair"],
        "llm_elapsed_seconds": duration, "context_reads": counts["context_read_completed"],
        "context_bytes_max": max(context_bytes, default=0), "context_bytes_total": sum(context_bytes),
        "estimated_input_tokens_total": sum(tokens),
        "improver_behavior": improver_behavior[-3:],
        "candidate_checks": {"unique_checked_candidates": len(checked_ids), "check_names": sorted(check_names),
            "unique_submitted_candidates": len(submitted_ids),
            "submitted_with_executed_checks": len(submitted_ids & checked_ids),
            "scope": "executed checker reports, matched by sequence-derived identity; not biological proof"},
        "receipt_namespaces": ["llm_attempts", "tool_jobs", "research_step_outputs", "context_reads"]}
