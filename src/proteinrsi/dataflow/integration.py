# SPDX-License-Identifier: MIT
"""An opt-in bridge to the existing A/B/C, ToolGateway and campaign controller.

Only the outer task adapter converts final resources to legacy experimental candidates.
The generic protocol itself can return structures, metrics, rankings or predictions.
"""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy

from pydantic import ValidationError

from proteinrsi.contracts import Candidate, digest, sequence_hash
from proteinrsi.prompting import compose
from .design import _candidate_payload, proposal_contract
from .protocol import (Operation, OperationRegistry, Protocol, ProtocolExecutor, ProtocolReview,
                       register_agent_operations, register_tool_operations)
from .resources import (ResourceStore, SEQUENCES, RANKING, NAMESPACE,
                        scope_for, standard_registry)
from .schema import ContractError
from .tasks import default_profiles


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


def build_operations(team, view, resources):
    from proteinrsi.agents import Plan
    from proteinrsi.research.analysis import register_analysis_tools
    gateway = team.tools.fork()
    core = register_analysis_tools(gateway, view)
    allowed = list(dict.fromkeys([*view.workflow.tool_names, *core]))
    operations = OperationRegistry(resources.registry)
    register_tool_operations(operations, gateway, view.task, allowed)
    ref_schema = {"type": "string", "pattern": r"^resource:[0-9a-f]{64}$"}

    def propose(args, key):
        tool_results = [args["evidence"]] if args.get("evidence") else []
        if args.get("reuse"):
            tool_results.append({"candidates": [c.model_dump(mode="json")
                                                 for c in resources.candidates(args["reuse"])]})
        response = team.designer.propose(view, Plan(rationale=args["question"]), tool_results,
                                        gateway.catalog(view.task, allowed))
        # Explicit tool requests are executed exactly as in the existing design loop.
        candidates = list(response.candidates)
        for turn in range(view.workflow.design_tool_rounds):
            if not response.tool_calls:
                break
            if turn == view.workflow.design_tool_rounds - 1:
                raise ContractError("Designer did not finish within its tool-turn limit")
            for call in response.tool_calls:
                result = gateway.call(call, view.task, allowed=allowed, context_key=key)
                tool_results.append(result)
                for raw in _candidate_payload(result) or []:
                    candidates.append(Candidate.model_validate(raw))
            response = team.designer.propose(view, Plan(rationale=args["question"]), tool_results,
                                             gateway.catalog(view.task, allowed))
            candidates.extend(response.candidates)
        descriptor = resources.sequences(candidates, producer="agent:B")
        return resources.get(descriptor["resource_id"])["data"]

    operations.register(Operation("agent:propose", object_schema({
        "question": {"type": "string"}, "reuse": ref_schema, "evidence": {"type": "object"}}, ["question"]),
        SEQUENCES, propose, "typed-designer-v2", agent_reply_contract=proposal_contract(view)))

    def rank(args, key):
        candidates = resources.candidates(args["candidates"])
        evidence = [args["evidence"]] if args.get("evidence") else []
        ranked = team.analyst.rank(view, candidates, evidence)
        annotated = resources.sequences(ranked, producer="agent:C:ranked")
        return {"candidate_set_ref": annotated["resource_id"],
                "ordered_ids": ["seq:" + sequence_hash(c.sequence) for c in ranked],
                "summary": "Analyst ranking; no implicit model execution"}

    operations.register(Operation("agent:rank", object_schema({"candidates": ref_schema,
        "evidence": {"type": "object"}}, ["candidates"]), RANKING, rank, "typed-ranking-v2",
        resource_inputs={"candidates": SEQUENCES}))

    def select(args, key):
        ranking = resources.get(args["ranking"], schema_ref=RANKING)["data"]
        candidates = resources.ranked_candidates(args["ranking"])
        selected = team.principal.finalize(view, candidates)
        return {"candidate_set_ref": ranking["candidate_set_ref"],
                "ordered_ids": ["seq:" + sequence_hash(c.sequence) for c in selected],
                "summary": "Principal priorities; no experiment submission"}

    operations.register(Operation("agent:select", object_schema({"ranking": ref_schema}, ["ranking"]),
        RANKING, select, "typed-selection-v1", resource_inputs={"ranking": RANKING}))

    def normalize(args, key):
        candidates = _candidate_payload(args["result"])
        if candidates is None:
            raise ContractError("Native result contains no successful candidates; no format guessing")
        descriptor = resources.sequences(candidates, producer="adapter:normalize_candidates")
        return resources.get(descriptor["resource_id"])["data"]

    operations.register(Operation("adapter:normalize_candidates", object_schema({
        "result": {"type": "object"}}, ["result"]), SEQUENCES, normalize, "normalize-candidates-v1"))

    def ordered(args, key):
        descriptor = resources.sequences(resources.ranked_candidates(args["ranking"]),
                                        producer="adapter:ranked_sequences")
        return resources.get(descriptor["resource_id"])["data"]

    operations.register(Operation("adapter:ranked_sequences", object_schema({"ranking": ref_schema}, ["ranking"]),
        SEQUENCES, ordered, "ranked-sequences-v2", resource_inputs={"ranking": RANKING}))

    def accept_checked(args, key):
        candidates = resources.candidates(args["candidates"])
        check = resources.get(args["check_result"])
        if not check["producer"].startswith("tool:"):
            raise ContractError("Panel checks require an executed tool result, not an agent assertion")
        result = check["data"]
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ContractError("Panel validation code/tool did not succeed")
        output = result.get("output", result)
        if not isinstance(output, dict):
            raise ContractError("Panel check output must be an object")
        identities = ["seq:" + sequence_hash(c.sequence) for c in candidates]
        checked = output.get("candidate_ids", [])
        if (not isinstance(checked, list) or any(not isinstance(x, str) for x in checked)
                or len(checked) != len(identities) or set(checked) != set(identities)):
            raise ContractError("Panel checks belong to a different candidate set")
        checks = output.get("checks", [])
        if (not isinstance(checks, list) or not checks
                or any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
                       or c.get("passed") is not True for c in checks)):
            raise ContractError("Panel contains failed or malformed scientific checks")
        if not set(args["required_checks"]) <= {c["name"] for c in checks}:
            raise ContractError("Panel result omitted a required check")
        return resources.get(args["candidates"], schema_ref=SEQUENCES)["data"]

    operations.register(Operation("adapter:accept_checked_candidates", object_schema({
        "candidates": ref_schema, "check_result": ref_schema,
        "required_checks": {"type": "array", "items": {"type": "string", "minLength": 1},
                            "minItems": 1, "uniqueItems": True}},
        ["candidates", "check_result", "required_checks"]), SEQUENCES, accept_checked,
        "checked-panel-v1", resource_inputs={"candidates": SEQUENCES}, agent_reply_contract={
            "purpose": "Accept candidates only after actual code/tool checks of this exact set.",
            "check_output": {"candidate_ids": "All sequence-derived IDs checked by the executed code",
                "checks": [{"name": "A planner-defined check name", "passed": True}]},
            "usage": "Bind a tool result by reference. Define task-specific grouping/pairing checks in optional Python; return this adapter result as final candidates."}))
    sequence_args = resources.registry.register("protein.sequence_arguments/v1", object_schema({
        "sequences": {"type": "array", "items": {"type": "string"}}}, ["sequences"]))
    operations.register(Operation("adapter:sequence_arguments", object_schema({"candidates": ref_schema}, ["candidates"]),
        sequence_args, lambda args, key: {"sequences": [c.sequence for c in resources.candidates(args["candidates"])]},
        "sequence-arguments-v1", resource_inputs={"candidates": SEQUENCES}))
    return operations


def run_campaign_protocol(team, view, config, *, protocol: Protocol | None = None):
    """Called from ResearchRunner for an explicit protocol_mode='typed' task snapshot."""
    from proteinrsi.research.resources import ResourceSelector
    profiles = default_profiles()
    contract = profiles.contract(view)
    resources = ResourceStore(team.store, standard_registry(), scope_for(view))
    context = view.model_dump(mode="json")
    context_ref = resources.put("context.task/v1", context, producer="task-view", is_input=True)["resource_id"]
    inputs = {"context": context_ref}
    if view.task.candidates:
        inputs["supplied_candidates"] = resources.sequences(
            [Candidate(sequence=s) for s in view.task.candidates], producer="supplied-task-input")["resource_id"]
    operations = build_operations(team, view, resources)
    catalogue = team.tools.catalog(view.task, view.workflow.tool_names)
    selected = ResourceSelector(team.store, config, team.llm).select(view, catalogue)
    plan_key = "protocol-plan:" + digest({"scope": resources.scope, "contract": asdict(contract),
        "operations": operations.catalog(), "config": config.model_dump(mode="json"),
        "prompts": team.store.get("configuration", "prompt_bundle"),
        "model": getattr(team.llm, "model", None), "url": getattr(team.llm, "base_url", None),
        "client": getattr(team.llm, "cache_settings", {})})
    plan_record = team.store.get(NAMESPACE, plan_key, {"attempts": 0, "errors": []})
    if protocol is None and plan_record.get("protocol"):
        protocol = Protocol.model_validate(plan_record["protocol"])
    if protocol is None:
        if team.llm is None:
            raise ContractError("Typed protocol planning requires an LLM or an explicit Protocol")
        while plan_record["attempts"] <= config.max_format_repairs:
            attempt = plan_record["attempts"]
            raw = team.llm.complete("A-plan", compose(team.store, "protocol_planner", view.workflow.principal_prompt) +
                "\nThis is a resource-protocol-v1 plan, NOT the legacy five-operation plan. "
                "Choose operations from the supplied registry; no scientific tool is mandatory. "
                "Connect input:name or step:step_id.result with schema_ref and pointer. "
                "For typed references use delivery=ref. Native tool result schemas cannot be changed. "
                "Use adapter:normalize_candidates for existing code/tool candidates rather than copying them. "
                "Use custom.* schemas and agent operations only when an existing contract is insufficient. "
                "Return final_outputs matching the task contract. Do not query experiments or grant permissions.",
                {"view": context, "task_contract": asdict(contract), "resources": selected,
                 "input_resources": {name: resources.describe(ref) for name, ref in inputs.items()},
                 "operations": operations.catalog(), "schemas": resources.registry.catalog(),
                 "format_attempt": attempt, "validation_errors": plan_record["errors"],
                 "max_steps": min(config.max_plan_steps, config.max_executed_steps)}, Protocol.model_json_schema())
            plan_record["attempts"] += 1
            team.store.put(NAMESPACE, plan_key, plan_record)
            try:
                candidate = Protocol.model_validate(raw)
                # Use a fresh registry per failed attempt; do not retain invalid definitions.
                trial_schemas = standard_registry()
                trial_resources = ResourceStore(team.store, trial_schemas, resources.scope)
                trial_ops = build_operations(team, view, trial_resources)
                register_agent_operations(candidate, trial_ops, team.llm, trial_resources, context,
                                          max_repairs=config.max_format_repairs)
                ProtocolExecutor(trial_resources, trial_ops, max_steps=config.max_plan_steps).preflight(candidate, inputs)
                for port, schema in contract.required_outputs.items():
                    binding = candidate.final_outputs.get(port)
                    if binding is None or binding.schema_ref != schema:
                        raise ContractError("Final output port/schema does not match the task contract")
                protocol = candidate
                plan_record["protocol"] = protocol.model_dump(mode="json")
                team.store.put(NAMESPACE, plan_key, plan_record)
                break
            except (ContractError, ValidationError, ValueError) as exc:
                plan_record["errors"] = [{"code": type(exc).__name__, "message": str(exc)[:1500]}]
                team.store.put(NAMESPACE, plan_key, plan_record)
        if protocol is None:
            raise ContractError("Protocol planning exhausted the format/preflight repair budget")
    register_agent_operations(protocol, operations, team.llm, resources, context,
                              max_repairs=config.max_format_repairs)
    for name, schema in contract.required_outputs.items():
        binding = protocol.final_outputs.get(name)
        if binding is None or binding.schema_ref != schema:
            raise ContractError("Protocol final output does not match the task contract")
    executor = ProtocolExecutor(resources, operations,
                                max_steps=min(config.max_plan_steps, config.max_executed_steps))
    def configure(revised):
        cloned_schemas = deepcopy(resources.registry)
        cloned_resources = ResourceStore(team.store, cloned_schemas, resources.scope)
        cloned_ops = OperationRegistry(cloned_schemas)
        cloned_ops._operations = dict(operations._operations)
        register_agent_operations(revised, cloned_ops, team.llm, cloned_resources, context,
                                  max_repairs=config.max_format_repairs)
        for port, schema in contract.required_outputs.items():
            binding = revised.final_outputs.get(port)
            if binding is None or binding.schema_ref != schema:
                raise ContractError("Revision changed the task's final output contract")
        ProtocolExecutor(cloned_resources, cloned_ops, max_steps=config.max_plan_steps).preflight(revised, inputs)
        resources.registry._schemas = cloned_schemas._schemas
        resources.registry._input_only = cloned_schemas._input_only
        operations._operations = cloned_ops._operations

    def review(current, completed, refs, run_key, failure=None, repair_attempt=0):
        if team.llm is None or (failure is None and not config.review_after_step):
            return None
        key = run_key + (":repair:" + str(repair_attempt) if failure else ":review:" + str(completed))
        saved = team.store.get(NAMESPACE, key, {"attempts": 0, "errors": []})
        if "decision" in saved:
            raw = saved["decision"]
            return Protocol.model_validate(raw) if raw else None
        while saved["attempts"] <= config.max_format_repairs:
            attempt = saved["attempts"]
            raw = team.llm.complete("A-review", compose(team.store, "protocol_step", view.workflow.principal_prompt) +
                "\nReview actual resource outputs. replacement=null keeps the protocol. "
                "Only replace unexecuted steps; completed steps, operation definitions, schemas and final task contract "
                "cannot change. New operations require new names. Never invent experiment results.",
                {"view": context, "protocol": current.model_dump(mode="json"), "completed_steps": completed,
                 "actual_resources": {name: resources.describe(ref) for name, ref in refs.items()},
                 "validation_errors": saved["errors"], "format_attempt": attempt,
                 "execution_error": failure.detail() if failure else None,
                 "operations": operations.catalog(), "schemas": resources.registry.catalog()}, ProtocolReview.model_json_schema())
            saved["attempts"] += 1
            team.store.put(NAMESPACE, key, saved)
            try:
                decision = ProtocolReview.model_validate(raw)
                revised = decision.replacement
                if revised:
                    if ([s.model_dump(mode="json") for s in current.steps[:completed]] !=
                            [s.model_dump(mode="json") for s in revised.steps[:completed]]):
                        raise ContractError("Cannot rewrite completed steps")
                    configure(revised)
                    executor.preflight(revised, inputs)
                saved["decision"] = revised.model_dump(mode="json") if revised else None
                team.store.put(NAMESPACE, key, saved)
                return revised
            except (ContractError, ValidationError, ValueError) as exc:
                saved["errors"] = [{"code": type(exc).__name__, "message": str(exc)[:1500]}]
                team.store.put(NAMESPACE, key, saved)
        raise ContractError("Protocol review exhausted its repair budget")

    team.store.event("research_plan_created", {"runner": "resource-protocol-v1",
        "round": view.round_index, "protocol": protocol.version, "plan": protocol.model_dump(mode="json")})
    try:
        result = executor.execute(protocol, inputs,
            validate_final=lambda outputs: profiles.accept(view, resources, outputs),
            after_step=review, configure=configure, max_revisions=config.max_revisions,
            on_error=review, max_repairs=config.max_format_repairs)
    except Exception as exc:
        team.store.event("research_blocked", {"runner": "resource-protocol-v1", "round": view.round_index,
            "protocol": protocol.version, "error_type": type(exc).__name__,
            "error": exc.detail() if isinstance(exc, ContractError) else {"code": type(exc).__name__}})
        raise
    execution = team.store.get(NAMESPACE, result["run_ref"])
    protocol = Protocol.model_validate(execution.get("active_protocol", execution["protocol"]))
    refs = {name: value["resource_id"] for name, value in result["outputs"].items()}
    candidates = profiles.accept(view, resources, refs)
    # Compatibility bridge only. All arbitrary final resources are kept, including
    # fixed-input predictions that correctly return no newly generated sequence.
    run_id = digest({"scope": resources.scope, "protocol": protocol.version})
    record = {"run_id": run_id, "runner": "resource-protocol-v1", "round": view.round_index,
        "evidence_version": view.evidence_version, "workflow_version": view.workflow.version,
        "meta_version": view.meta.version, "plan": protocol.model_dump(mode="json"),
        "resources": selected, "completed": execution["completed"],
        "revisions": execution["revisions"], "repairs": execution.get("repairs", []), "status": "complete",
        "task_contract": asdict(contract), "protocol_result": result,
        "state": {"candidates": [c.model_dump(mode="json") for c in candidates],
                  "ranked": [], "final": [c.model_dump(mode="json") for c in candidates], "tool_results": []}}
    team.store.put("research_runs", run_id, record)
    team.store.event("team_completed", {"round": view.round_index, "workflow": view.workflow.version,
        "meta": view.meta.version, "evidence": view.evidence_version, "trace_id": run_id,
        "backend": "typed_protocol", "candidate_count": len(candidates), "protocol_result": result})
    return candidates
