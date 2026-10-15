# SPDX-License-Identifier: MIT
"""Shared, durable action execution and local repair, independent of scientific planning."""
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import Field

from proteinrsi.contracts import Model, digest
from proteinrsi.dataflow.resources import NAMESPACE
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.tools import ToolCall


class ExecutionFailure(ContractError):
    def __init__(self, tool, detail, *, recoverable=False):
        self.tool, self.failure, self.recoverable = tool, detail, recoverable
        super().__init__(detail.get("message", "Tool execution failed"),
                         path=detail.get("path", ""), code=detail["kind"])

    def detail(self):
        return {**super().detail(), "tool": self.tool, "failure": self.failure,
                "recovery": "repair_current_call" if self.recoverable else "inspect_runtime"}


def require_success(tool, result):
    """Interpret execution status, not scientific checks or low fitness."""
    if tool != "research_python" or result.get("status") != "failed":
        return result
    error = result.get("error_type", "UnknownError")
    recoverable = error in {"SyntaxError", "IndentationError", "NameError", "UnboundLocalError",
        "KeyError", "IndexError", "TypeError", "ValueError", "AttributeError", "AssertionError",
        "ImportError", "ModuleNotFoundError", "ZeroDivisionError"}
    raise ExecutionFailure(tool, {"kind": "program_execution_failed", "error_type": error,
        "message": str(result.get("error", error))[:2000], "stdout": result.get("stdout", "")[:4000],
        "source_file": result.get("source_file"), "code_sha256": result.get("code_sha256")},
        recoverable=recoverable)


def input_shape(value, depth=0):
    """Repair sees fields and small examples, not another copy of the whole dataset."""
    if isinstance(value, dict):
        return {key: input_shape(child, depth + 1) for key, child in list(value.items())[:40]}
    if isinstance(value, list):
        return {"type": "array", "count": len(value),
                "examples": [input_shape(child, depth + 1) for child in value[:2]] if depth < 4 else []}
    if isinstance(value, str):
        return value if len(value) <= 200 else {"type": "string", "length": len(value)}
    return value


class CodeRepair(Model):
    code: str = Field(min_length=1, max_length=30000)
    reason: str = Field(min_length=1, max_length=2000)


def execute_tool(gateway, call, task, *, allowed, context_key, llm=None, view=None,
                 resources=None, max_repairs=2):
    """Changed arguments get new attempts; success/unknown completion are never blindly rerun."""
    from proteinrsi.permissions import require_tools
    grant = require_tools(gateway.store, [call.name])
    if call.name not in allowed or call.name not in gateway._tools:
        raise PermissionError("Tool is not authorized for this step")
    spec = gateway._tools[call.name][0]
    identity = {"call": call.model_dump(mode="json"), "context": context_key,
                "implementation": spec.model_dump(mode="json"),
                "authorization": grant.version if grant else None}
    key = "execution-call:" + digest(identity)
    journal = gateway.store.get(NAMESPACE, key, {"attempts": [], "current": call.model_dump(mode="json")})
    if journal.get("status") == "done":
        return deepcopy(journal["result"])
    while True:
        current = ToolCall.model_validate(journal["current"])
        try:
            result = gateway.call(current, task, allowed=allowed, context_key=context_key)
            require_success(current.name, result)
        except ExecutionFailure as failure:
            # Argument errors and program errors are known returns. Permission and
            # transport failures deliberately do not enter this repair path.
            fingerprint = digest(current)
            if not journal["attempts"] or journal["attempts"][-1]["call_sha256"] != fingerprint:
                journal["attempts"].append({"call_sha256": fingerprint,
                    "call": current.model_dump(mode="json"), "error": failure.detail()})
                gateway.store.event("execution_attempt_failed", {"call_ref": key,
                    "tool": current.name, "attempt": len(journal["attempts"]), "error": failure.detail()})
            journal.update(status="failed", error=failure.detail())
            gateway.store.put(NAMESPACE, key, journal)
            if not llm or not failure.recoverable or len(journal["attempts"]) > max_repairs:
                raise failure
            repair_key = key + "/repair/" + str(len(journal["attempts"]))
            saved = gateway.store.get(NAMESPACE, repair_key)
            code_only = current.name == "research_python" and failure.code == "program_execution_failed"
            instructions = ("Repair this failed execution attempt only. Preserve the scientific purpose, "
                "task constraints, injected inputs and all successful upstream work. Do not rewrite the "
                "protocol, manufacture data, query an experiment or request new authority. "
                "Python has context, inputs, artifacts and write_artifact; set result to a JSON object.")
            args = current.arguments
            # Match the actual sandbox projection, including exclusion of M's
            # private definitions from Task execution and its repair prompts.
            repair_view = (view.for_task() if view is not None
                           and spec.execution_contract.get("context_scope") == "task" else view)
            context = {"tool": current.name, "purpose": current.purpose, "error": failure.detail(),
                "arguments": {k: deepcopy(v) for k, v in args.items() if k not in {"inputs"}},
                "input_shape": input_shape(args.get("inputs", {})),
                "input_schema": spec.input_schema,
                "context_shape": input_shape(repair_view.model_dump(mode="json")) if repair_view is not None else {}}
            if spec.execution_contract:
                context["execution_contract"] = deepcopy(spec.execution_contract)
                instructions += " Follow execution_contract and context_shape for actual Python inputs."
            if resources and args.get("resource_refs"):
                context["resource_shapes"] = {name: input_shape(resources.get(ref)["data"])
                    for name, ref in args["resource_refs"].items()}
            if code_only:
                schema = CodeRepair.model_json_schema()
            else:
                schema = {"type": "object", "properties": {"arguments": spec.input_schema,
                    "reason": {"type": "string"}}, "required": ["arguments", "reason"],
                    "additionalProperties": False}
                context["arguments"] = deepcopy(args)
            if saved is None:
                from proteinrsi.dataflow.responses import validated_response
                def validate(raw):
                    if code_only:
                        return CodeRepair.model_validate(raw).model_dump()
                    from jsonschema import Draft202012Validator
                    if next(Draft202012Validator(schema).iter_errors(raw), None) is not None:
                        raise ContractError("Invalid repaired tool arguments", code="tool_arguments")
                    return raw
                try:
                    saved = validated_response(gateway.store, llm, key=repair_key, role="B-repair",
                        instructions=instructions, context=context, schema=schema, validate=validate,
                        max_repairs=max_repairs)
                except Exception as exc:
                    # A repair runtime/provider failure must not replace the original
                    # program error or be counted as another generated-code attempt.
                    gateway.store.event("execution_repair_failed", {"call_ref": key,
                        "tool": current.name, "program_error": failure.detail(),
                        "repair_error_type": type(exc).__name__})
                    raise
                gateway.store.put(NAMESPACE, repair_key, saved, immutable=True)
            repaired_args = ({**args, "code": saved["code"]} if code_only else saved["arguments"])
            updated = current.model_copy(update={"arguments": repaired_args})
            if digest(updated) in {item["call_sha256"] for item in journal["attempts"]}:
                raise ExecutionFailure(current.name, {"kind": "repair_no_progress",
                    "message": "Repair repeated a previously failed call"})
            journal.update(current=updated.model_dump(mode="json"), status="repairing")
            gateway.store.put(NAMESPACE, key, journal)
            gateway.store.event("execution_call_repaired", {"call_ref": key,
                "attempt": len(journal["attempts"]), "tool": current.name, "reason": saved["reason"],
                "from": fingerprint, "to": digest(updated), "upstream_rerun": False})
        else:
            journal.update(status="done", result=result)
            gateway.store.put(NAMESPACE, key, journal)
            return result


@dataclass
class AgentAction:
    """Adapters normalize their scientific reply contracts to this execution contract."""
    result: dict = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


def run_agent_step(store, key, decide, invoke, *, max_turns=None):
    """One durable tools/results loop for installed and protocol-local agent steps."""
    run_key = "agent-execution:" + key
    journal = store.get(NAMESPACE, run_key, {"turn": 0, "results": [], "decisions": []})
    if journal.get("status") == "done":
        return journal
    while True:
        turn = journal["turn"]
        turn_key = run_key + "/turn/" + str(turn)
        raw = store.get(NAMESPACE, turn_key)
        if raw is None:
            action = decide(journal["results"], turn)
            raw = {"result": action.result,
                   "tool_calls": [call.model_dump(mode="json") for call in action.tool_calls]}
            store.put(NAMESPACE, turn_key, raw, immutable=True)
        if not raw["tool_calls"]:
            journal.update(status="done", result=raw["result"])
            store.put(NAMESPACE, run_key, journal)
            return journal
        if max_turns is not None and turn >= max_turns - 1:
            raise ContractError("Agent did not finish within its legacy tool-turn limit")
        calls = [ToolCall.model_validate(call) for call in raw["tool_calls"]]
        # Cache-equivalent tools cannot reveal new data. Preserve results and ask
        # for a new decision instead of replaying an identical action forever.
        def signature(call):
            return digest({"name": call["name"], "arguments": call["arguments"]})
        previous = {signature(call) for decision in journal["decisions"] for call in decision["tool_calls"]}
        repeated = all(signature(call.model_dump(mode="json")) in previous for call in calls)
        if repeated and journal.get("no_progress"):
            raise ContractError("Agent repeated completed actions after corrective feedback",
                                code="action_no_progress")
        if repeated:
            journal["results"].append({"status": "already_completed", "message":
                "These exact actions have completed. Use their existing results, choose a new action "
                "or finish; an empty history lookup means unknown fitness, not an execution failure."})
        else:
            for i, call in enumerate(calls):
                receipt_key = turn_key + "/tool/" + str(i)
                result = store.get(NAMESPACE, receipt_key)
                if result is None:
                    result = invoke(call, receipt_key)
                    store.put(NAMESPACE, receipt_key, result, immutable=True)
                journal["results"].append(result)
        journal["no_progress"] = repeated
        journal["decisions"].append(raw)
        journal["turn"] += 1
        store.put(NAMESPACE, run_key, journal)
