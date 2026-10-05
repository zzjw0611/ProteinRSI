# SPDX-License-Identifier: MIT
"""An allowlisted scientific tool gateway. MCP payloads remain untrusted data."""
from __future__ import annotations

from typing import Any, Callable

from jsonschema import Draft202012Validator
from pydantic import Field

from proteinrsi.contracts import Model, TaskKind, TaskSpec, digest
from proteinrsi.storage import Store


class ToolCall(Model):
    name: str
    arguments: dict[str, Any]
    purpose: str = Field(default="", max_length=2000)


class ToolSpec(Model):
    name: str
    capability: str
    description: str = ""
    limitations: str = ""
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    cost_hint: str = "No empirical runtime estimate available; use deployment-specific measurement."
    examples: list[dict[str, Any]] = Field(default_factory=list)
    output_semantics: str = ""
    validation_level: str = "adapter_contract_only"
    result_schema_version: str = "1"
    implementation_version: str
    task_kinds: list[TaskKind]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    protected_inputs: dict[str, str] = Field(default_factory=dict)
    license_id: str = "NOASSERTION"
    data_egress: bool = False


class ToolGateway:
    def __init__(self, store: Store, *, allow_egress: bool = False):
        self.store = store
        self.allow_egress = allow_egress
        self._tools: dict[str, tuple[ToolSpec, Callable[[dict], dict]]] = {}

    def fork(self) -> ToolGateway:
        """Copy bindings for a round-local overlay; shared store retains budget/cache rules."""
        gateway = ToolGateway(self.store, allow_egress=self.allow_egress)
        gateway._tools = dict(self._tools)
        return gateway

    def register(self, spec: ToolSpec, executor: Callable[[dict], dict]) -> None:
        if spec.name in self._tools:
            raise ValueError("Duplicate tool binding")
        Draft202012Validator.check_schema(spec.input_schema)
        Draft202012Validator.check_schema(spec.output_schema)
        self._tools[spec.name] = (spec, executor)

    def catalog(self, task: TaskSpec, allowed: list[str]) -> list[dict]:
        missing = set(allowed) - self._tools.keys()
        if missing:
            raise ValueError(f"Unconfigured tools: {sorted(missing)}")
        return [spec.model_dump(mode="json") for name, (spec, _) in self._tools.items()
                if name in allowed and task.kind in spec.task_kinds]

    def call(self, call: ToolCall, task: TaskSpec, *, allowed: list[str], context_key: str) -> dict:
        if call.name not in allowed or call.name not in self._tools:
            raise PermissionError("Tool is not allowlisted for this workflow")
        spec, executor = self._tools[call.name]
        if task.kind not in spec.task_kinds:
            raise ValueError("Tool does not support this task")
        if spec.data_egress and not self.allow_egress:
            raise PermissionError("External sequence/data transfer requires explicit operator opt-in")
        Draft202012Validator(spec.input_schema).validate(call.arguments)
        task_values = task.model_dump()
        for argument, field in spec.protected_inputs.items():
            if field not in task_values or call.arguments.get(argument) != task_values[field]:
                raise ValueError(f"Protected input {argument} does not match task.{field}")
        payload = {"spec": spec.model_dump(mode="json"), "call": call.model_dump(), "context": context_key}
        key = "tool-" + digest(payload)
        previous = self.store.get("tool_jobs", key)
        self.store.event("tool_requested", {"tool": call.name, "purpose": call.purpose,
            "context": context_key, "key": key, "arguments": call.arguments,
            "cache_hit": bool(previous and previous.get("state")=="done")})
        if previous:
            if previous["state"] != "done":
                raise RuntimeError("Uncertain/failed prior tool job; reconcile rather than resubmit blindly")
            return previous["result"]
        self.store.reserve(key, "tool_calls", 1, payload)
        self.store.settle(key)
        record = {"state": "started", "spec": spec.model_dump(mode="json"),
                  "call": call.model_dump(), "context": context_key}
        self.store.put("tool_jobs", key, record)
        try:
            result = executor(call.arguments)
            if not isinstance(result, dict):
                raise ValueError("Tool result must be an object")
            Draft202012Validator(spec.output_schema).validate(result)
            # JSON serialization also rejects NaN/Infinity before persistence.
            self.store.put("tool_jobs", key, {**record, "state": "done", "result": result})
        except Exception as exc:
            self.store.put("tool_jobs", key, {**record, "state": "failed", "error_type": type(exc).__name__})
            self.store.event("tool_failed", {"key": key, "tool": call.name, "error_type": type(exc).__name__})
            raise
        self.store.event("tool_completed", {"key": key, "tool": call.name,
                                            "version": spec.implementation_version})
        return result
