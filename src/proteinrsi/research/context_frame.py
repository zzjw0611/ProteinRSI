# SPDX-License-Identifier: MIT
"""Stage inputs over revealed evidence; scientific choices remain with the agent."""
from copy import deepcopy
import math

from proteinrsi.contracts import digest


def observation_rows(value, task):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict) or value.get("format") != "revealed-observations-v1":
        return []
    rows = [{**value["shared_fields"], **dict(zip(value["columns"], row))}
            for row in value["rows"]]
    positions = value.get("sequence_encoding", {}).get("positions_1based")
    if positions:
        for row in rows:
            sequence = list(task["reference_sequence"])
            for position, residue in zip(positions, row["sequence"]):
                sequence[position - 1] = residue
            row["sequence"] = "".join(sequence)
    return rows


def state_frame(builder, context, role):
    """Project a stage over current state; history is always explicitly referenced.

    Runtime resources and sandbox inputs keep their complete TaskView. Only the
    provider presentation changes. Only the explicit stage ``view`` is a TaskView;
    tool results, schemas and repair metadata are never inferred to be task state.
    """
    def ref(value, path):
        return builder.reference(value, path)

    def project_view(view, path):
        task = view.get("task", {})
        rows = observation_rows(view.get("observations", []), task)
        history = view.get("history", [])
        research = view.get("research_context", {})
        state = research.get("research_state")
        if state is None:
            valid = [row for row in rows if row.get("qc") == "valid"
                     and isinstance(row.get("value"), (int, float)) and math.isfinite(row["value"])]
            best = (min if task.get("direction") == "minimize" else max)(
                valid, key=lambda row: row["value"]) if valid else None
            state = {"format": "research-state-v1", "facts": {"round": view.get("round_index"),
                "observations": len(rows), "evidence_version": digest(rows), "best_measured": best},
                "working_memory": {}, "artifact_refs": []}
        batch = rows[-1].get("batch_id") if rows else None
        latest = [row for row in rows if row.get("batch_id") == batch] if batch is not None else []
        projected = {key: project(value, path + "." + key) for key, value in view.items()
                     if key not in {"observations", "history", "experience", "artifacts", "research_context"}}
        projected["observations"] = {**ref(view.get("observations", []), path + ".observations"),
            "coverage": {"total_rows": len(rows), "evidence_version": state["facts"]["evidence_version"]}}
        projected["history"] = ref(history, path + ".history")
        if role != "M" and isinstance(view.get("workflow"), dict):
            projected["workflow"] = {key: deepcopy(value) for key, value in view["workflow"].items()
                if not key.endswith("_prompt") and key != "programs"}
            projected["workflow"]["definition"] = ref(view["workflow"], path + ".workflow")
        projected["current_measurements"] = builder.pack(latest, path + ".current_measurements", threshold=20000)
        if history and role != "C-feedback":
            feedback = history[-1].get("analyst_feedback")
            if feedback:
                projected["latest_feedback"] = builder.pack(feedback, path + ".latest_feedback", threshold=6000)
        projected["experience"] = ref(view.get("experience", []), path + ".experience")
        projected["artifacts"] = ref(view.get("artifacts", []), path + ".artifacts")
        projected["research_context"] = {key: project(value, path + ".research_context." + key)
            for key, value in research.items() if key != "research_state"
            and (role == "M" or key != "execution_summary")}
        projected["research_context"]["research_state"] = deepcopy(state)
        return projected

    def project(value, path):
        if path.endswith((".history", ".method_history", ".workflow_validation_outcomes",
                          ".recent_execution", ".repair_history", ".excluded_sequences")) and isinstance(value, list):
            return ref(value, path)
        if path.endswith(".schemas") and isinstance(value, dict):
            return {"names": sorted(value), "definitions": ref(value, path)}
        if path.endswith(".operations") and isinstance(value, list):
            return [builder.operation_contract(row, f"{path}[{i}]") for i, row in enumerate(value)]
        if path.endswith(".recent_method_outcomes") and isinstance(value, list):
            return [{**{key: deepcopy(row[key]) for key in ("patch_id", "target", "round", "status", "scope") if key in row},
                     "changed_fields": sorted(row.get("changes", {})),
                     "hypothesis": builder.pack(row.get("hypothesis", ""), f"{path}[{i}].hypothesis", threshold=2000),
                     "verdict": builder.pack({key: row.get("outcome", {}).get("result", {}).get(key)
                        for key in ("decision", "reason")}, f"{path}[{i}].verdict", threshold=3000),
                     "details": ref(row, f"{path}[{i}]")} for i, row in enumerate(value[-3:])]
        if isinstance(value, dict):
            return {key: project(child, path + "." + key) for key, child in value.items()}
        if isinstance(value, list):
            if len(value) > 50:
                return ref(value, path)
            return builder.pack([project(child, f"{path}[{i}]") for i, child in enumerate(value)], path, threshold=20000)
        return builder.pack(value, path, threshold=20000)

    frame = {key: project_view(value, "$.view") if key == "view" and isinstance(value, dict)
             else project(value, "$." + key) for key, value in context.items()}
    # Facts and tool schemas are controller projections. The role decides what
    # experiments or optional analyses to perform; no mutation strategy is baked in.
    frame["stage"] = {"role": role, "history_access": "scoped_refs_or_revealed_query",
        "memory_authority": "agent_hypotheses; controller_facts_are_immutable"}
    return frame


def stage_frame(builder, context, *, inline=False):
    """Keep the stage contract visible, with complete originals behind scoped refs.

    Required step arguments/bindings are supplied by ProtocolExecutor. This layer
    presents them and does not infer a scientific workflow or select experiments.
    """
    frame = deepcopy(context) if inline else builder.pack(context)
    view = context.get("view", context)
    task = view.get("task", {})
    if not isinstance(task, dict):
        return frame
    # Large candidate catalogues and histories stay references. Small facts and
    # the operator's goal do not require additional model turns to retrieve.
    task_frame = {key: deepcopy(value) if inline else builder.pack(value, f"$.task.{key}", threshold=6000)
                  for key, value in task.items()}
    if "view" in context and isinstance(frame.get("view"), dict):
        frame["view"]["task"] = task_frame
    elif "task" in context:
        frame["task"] = task_frame
    for key in (() if inline else ("task_contract", "proposal_contract", "step", "arguments",
                "available_candidate_sets", "validation_errors", "repair_state", "improvement_brief")):
        if key in context:
            frame[key] = builder.pack(context[key], "$." + key, threshold=6000)
    if not inline and "query" in context:
        frame["query"] = builder.pack(context["query"], "$.query", threshold=6000)
    if not inline and isinstance(context.get("improvement_brief"), dict):
        frame["improvement_brief"] = {key: builder.pack(value,
            f"$.improvement_brief.{key}", threshold=6000)
            for key, value in context["improvement_brief"].items()}
    facts = {key: deepcopy(view[key]) for key in
             ("round_index", "remaining_wells", "remaining_queries", "evidence_version", "capacity") if key in view}
    rounds = task.get("max_rounds")
    capacity = view.get("capacity")
    if isinstance(capacity, dict) and "usable_queries_before_round_limit" in capacity:
        facts.update(remaining_rounds=capacity["remaining_rounds"],
                     plate_capacity=capacity["plate_wells"],
                     executable_task_queries=capacity["usable_queries_before_round_limit"])
    elif isinstance(rounds, int) and isinstance(view.get("round_index"), int):
        facts["remaining_rounds"] = max(0, rounds - view["round_index"])
        facts["plate_capacity"] = task.get("batch_size")
        remaining = view.get("remaining_wells", view.get("remaining_queries"))
        if isinstance(task.get("batch_size"), int) and isinstance(remaining, int):
            usable = min(remaining, facts["remaining_rounds"] * task["batch_size"])
            if task.get("batch_fill_policy") == "full_plate":
                usable -= usable % task["batch_size"]
            facts["executable_task_queries"] = usable
    frame["execution_facts"] = facts
    rows = observation_rows(view.get("observations", []), task)
    if rows:
        valid = [row for row in rows if row.get("qc") == "valid"
                 and isinstance(row.get("value"), (int, float))
                 and math.isfinite(row["value"])]
        best = (min if task.get("direction") == "minimize" else max)(
            valid, key=lambda row: row["value"]) if valid else None
        summary = {"authority": "computed_from_revealed_measurements",
            "evidence_version": view.get("evidence_version") or digest(rows),
            "coverage": {"total_rows": len(rows), "valid_value_rows": len(valid),
                         "observations_digest": digest(rows), "complete": True},
            "metric": task.get("metric"), "unit": task.get("unit"),
            "best_measured": deepcopy(best),
            "source": builder.reference(view["observations"], "$.view.observations")}
        frame["observation_summary"] = builder.pack(summary, "$.observation_summary", threshold=6000)
        batch = rows[-1].get("batch_id")
        if batch is not None:
            latest = [row for row in rows if row.get("batch_id") == batch]
            frame["current_measurements"] = builder.pack(latest, "$.current_measurements", threshold=12000)
    return frame
