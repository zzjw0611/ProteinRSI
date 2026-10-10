# SPDX-License-Identifier: MIT
"""Portable method programs. Source is versioned data, never trusted controller code."""
from proteinrsi.contracts import TaskView, Workflow, MetaPolicy, digest
from proteinrsi.tools import ToolCall
from .code import CODE_TOOL


def run_method_programs(store, gateway, view: TaskView, method: Workflow | MetaPolicy, *, owner: str):
    """Run the programs explicitly retained by W/M, on revealed inputs, with paid receipts."""
    if not method.programs:
        return []
    if not store.get("configuration", "research", {}).get("enable_generated_code", False):
        raise PermissionError("Method programs require operator-enabled generated code")
    context_key = "method-programs:" + digest({"owner": owner, "version": method.version,
                                               "view": view.model_dump(mode="json")})
    outputs = []
    for program in method.programs:
        output_id = context_key + "/" + program.version
        saved = store.get("research_step_outputs", output_id)
        if saved is None:
            result = gateway.call(ToolCall(name=CODE_TOOL, purpose=program.purpose,
                arguments={"code": program.code, "inputs": program.inputs}), view.task,
                allowed=[CODE_TOOL], context_key=output_id)
            saved = {"owner": owner, "method_version": method.version,
                "program": program.name, "program_version": program.version,
                "workflow": view.workflow.version, "meta": view.meta.version,
                "round": view.round_index, "evidence_version": view.evidence_version,
                "result": result, "authority": "computed_unvalidated"}
            store.put("research_step_outputs", output_id, saved, immutable=True)
            store.event("method_program_executed", {"output_id": output_id,
                **{k: v for k, v in saved.items() if k != "result"},
                "status": result.get("status", "unknown")})
        outputs.append({"output_id": output_id, **saved})
    return outputs
