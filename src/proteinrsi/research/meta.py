# SPDX-License-Identifier: MIT
"""Agent-selected analysis before a W/M proposal; no prescribed diagnostic sequence."""
from pydantic import model_validator

from proteinrsi.agents import MetaResponse
from proteinrsi.contracts import Workflow, digest
from proteinrsi.improvement import apply_patch
from proteinrsi.prompting import compose
from proteinrsi.tools import ToolCall, ToolGateway
from .analysis import ANALYSIS_TOOLS, register_analysis_tools
from .code import CODE_TOOL, register_code_tool
from .methods import run_method_programs


class MetaStep(MetaResponse):
    tool_call: ToolCall | None = None

    @model_validator(mode="after")
    def separate_action_from_proposal(self):
        if self.tool_call is not None and self.patch is not None:
            raise ValueError("Return a tool call or a final proposal, not both")
        return self


def analysis_gateway(agent, view):
    gateway = agent.tools.fork() if agent.tools is not None else ToolGateway(agent.store)
    if not getattr(gateway, "remote_context_tools", False):
        for name in ANALYSIS_TOOLS:
            gateway._tools.pop(name, None)
    core = register_analysis_tools(gateway, view)
    register_code_tool(gateway, view)
    configured = [name for name in view.workflow.tool_names if name in gateway._tools]
    allowed = list(dict.fromkeys([*core, *configured,
                                 *([CODE_TOOL] if CODE_TOOL in gateway._tools else [])]))
    catalog = gateway.catalog(view.task, allowed)
    catalog = [item for item in catalog if not item["data_egress"] or gateway.allow_egress]
    return gateway, catalog


def propose(agent, view, last_patch_round):
    """Resume exactly the same paid analysis; a new policy gets a new invocation identity."""
    store, llm = agent.store, agent.llm
    if store is None:
        raise ValueError("Autonomous Meta requires a persistent store")
    config = store.get("configuration", "research", {})
    limit = config.get("meta_tool_rounds", 0)
    gateway, catalog = analysis_gateway(agent, view)
    allowed = [item["name"] for item in catalog]
    instructions = compose(store, "meta", view.meta.prompt)
    original = {"view": view.model_dump(mode="json"), "last_patch_round": last_patch_round,
        "workflow_schema": Workflow.model_json_schema(),
        "meta_schema": type(view.meta).model_json_schema(), "available_tools": catalog}
    key = "meta-analysis:" + digest({"context": original, "instructions": instructions,
        "tool_rounds": limit, "model": getattr(llm, "model", None),
        "url": getattr(llm, "base_url", None), "client": getattr(llm, "cache_settings", {})})
    final_key = key + "/final"
    cached = store.get("research_step_outputs", final_key)
    if cached is not None:
        return MetaResponse.model_validate(cached["response"])
    initial = store.get("research_step_outputs", key)
    if initial is None:
        initial = {**original, "compute_usage": store.usage(), "meta": view.meta.version,
                   "workflow": view.workflow.version, "round": view.round_index}
        store.put("research_step_outputs", key, initial, immutable=True)
        store.event("meta_analysis_started", {"output_id": key, "meta": view.meta.version,
            "workflow": view.workflow.version, "round": view.round_index,
            "evidence_version": view.evidence_version})
    programs = run_method_programs(store, gateway, view, view.meta, owner="M")
    results = []
    for index in range(limit + 1):
        step_key = key + "/step/" + str(index)
        context = {**initial, "program_results": programs, "analysis_results": results,
                   "tool_rounds_remaining": limit - index}
        decision_key = step_key + "/decision"
        raw = store.get("research_step_outputs", decision_key)
        if raw is None:
            schema = MetaStep.model_json_schema() if index < limit else MetaResponse.model_json_schema()
            raw = llm.complete("M", instructions, context, schema)
            # Validate before persisting an executable request.
            parsed = MetaStep.model_validate(raw) if index < limit else MetaResponse.model_validate(raw)
            raw = parsed.model_dump(mode="json")
            store.put("research_step_outputs", decision_key, raw, immutable=True)
        decision = MetaStep.model_validate(raw)
        if decision.tool_call is None:
            response = MetaResponse.model_validate(decision.model_dump(exclude={"tool_call"}))
            proposed_version = None
            if response.patch is not None:
                patch = response.patch
                if patch.task_kind != view.task.kind:
                    raise ValueError("Meta patch task scope differs")
                parent = view.workflow if patch.target == "workflow" else view.meta
                proposed_version = apply_patch(parent, patch).version
            record = {"response": response.model_dump(mode="json"), "meta": view.meta.version,
                "workflow": view.workflow.version, "round": view.round_index,
                "evidence_version": view.evidence_version, "proposed_version": proposed_version,
                "tool_rounds_used": index, "adopted": False}
            store.put("research_step_outputs", final_key, record, immutable=True)
            store.event("meta_analysis_completed", {"output_id": final_key,
                **{k: v for k, v in record.items() if k != "response"},
                "patch_id": response.patch.patch_id if response.patch else None,
                "target": response.patch.target if response.patch else None})
            return response
        if index == limit:
            raise ValueError("Meta exceeded the operator tool-round limit")
        receipt = store.get("research_step_outputs", step_key)
        if receipt is None:
            output = gateway.call(decision.tool_call, view.task, allowed=allowed, context_key=step_key)
            receipt = {"tool_call": decision.tool_call.model_dump(mode="json"), "result": output,
                       "meta": view.meta.version, "authority": "computational_evidence"}
            store.put("research_step_outputs", step_key, receipt, immutable=True)
            store.event("meta_analysis_tool_completed", {"output_id": step_key,
                "meta": view.meta.version, "tool": decision.tool_call.name,
                "status": output.get("status", "done")})
        results.append({"output_id": step_key, **receipt})
    raise AssertionError("unreachable")
