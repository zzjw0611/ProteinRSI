# SPDX-License-Identifier: MIT
"""Task-neutral, reference-bound serial DAG execution.

Dependencies and resource contracts are checked independently of scientific efficacy.
This executor cannot approve experiments, alter a ledger, or grant new capabilities.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from proteinrsi.contracts import Model, digest
from .resources import NAMESPACE, ResourceStore
from .schema import ContractError, SchemaRegistry, pointer


class Binding(Model):
    source: str = Field(min_length=1, max_length=120)
    # input:name or step:name.port; not a file path or an arbitrary database key.
    schema_ref: str
    pointer: str = Field(default="", max_length=1000)
    delivery: Literal["value", "ref"] = "value"


class OutputView(Model):
    schema_ref: str
    pointer: str = ""


class Step(Model):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    operation: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=3000)
    arguments: dict[str, Any] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    outputs: dict[str, OutputView] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def no_hidden_overrides(self):
        if set(self.arguments) & set(self.bindings):
            raise ValueError("A parameter cannot be both a literal and a resource binding")
        if "result" in self.outputs:
            raise ValueError("The operation owns its result schema; add named output views instead")
        for name in (*self.bindings, *self.outputs):
            if not name or len(name) > 120:
                raise ValueError("Invalid port name")
        return self


class AgentOperation(Model):
    name: str = Field(pattern=r"^agent:[a-z][a-z0-9_-]{0,47}$")
    role: Literal["A", "B", "C"]
    instructions: str = Field(min_length=1, max_length=12000)
    input_schema: dict
    output_schema_ref: str


class Protocol(Model):
    protocol_version: Literal["resource-protocol-v1"] = "resource-protocol-v1"
    hypothesis: str = Field(min_length=1, max_length=4000)
    schemas: dict[str, dict] = Field(default_factory=dict, max_length=16)
    agent_operations: list[AgentOperation] = Field(default_factory=list, max_length=12)
    steps: list[Step] = Field(min_length=1, max_length=24)
    final_outputs: dict[str, Binding] = Field(min_length=1, max_length=16)

    @property
    def version(self):
        return "protocol:" + digest(self.model_dump(mode="json"))


@dataclass(frozen=True)
class Operation:
    name: str
    input_schema: dict
    output_schema_ref: str
    invoke: Callable[[dict, str], Any]
    implementation: str
    # Constraints on root resources, in addition to ordinary tool argument validation.
    resource_inputs: dict[str, str] | None = None
    agent_reply_contract: dict | None = None


class OperationRegistry:
    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas
        self._operations: dict[str, Operation] = {}

    def register(self, operation: Operation):
        if operation.name in self._operations:
            raise ContractError("Duplicate operation name")
        input_ref = "operation.input/" + digest({"name": operation.name,
                                                "schema": operation.input_schema})
        self.schemas.register(input_ref, operation.input_schema)
        self.schemas.schema(operation.output_schema_ref)
        self._operations[operation.name] = Operation(
            operation.name, deepcopy(operation.input_schema), operation.output_schema_ref,
            operation.invoke, operation.implementation, deepcopy(operation.resource_inputs),
            deepcopy(operation.agent_reply_contract))

    def get(self, name):
        if name not in self._operations:
            raise ContractError(f"Unavailable operation: {name}")
        return self._operations[name]

    def input_ref(self, operation):
        return "operation.input/" + digest({"name": operation.name, "schema": operation.input_schema})

    def catalog(self):
        return [{"name": op.name, "input_schema": deepcopy(op.input_schema),
                 "output_schema_ref": op.output_schema_ref,
                 "output_schema": self.schemas.schema(op.output_schema_ref),
                 "resource_inputs": deepcopy(op.resource_inputs or {}), "implementation": op.implementation,
                 "agent_reply_contract": deepcopy(op.agent_reply_contract)}
                for op in self._operations.values()]


class ProtocolExecutor:
    def __init__(self, resources: ResourceStore, operations: OperationRegistry, *, max_steps=24):
        self.resources, self.operations = resources, operations
        self.store, self.schemas = resources.store, resources.registry
        self.max_steps = max_steps

    def preflight(self, protocol: Protocol, inputs: dict[str, str]) -> dict:
        if len(protocol.steps) > self.max_steps:
            raise ContractError("Protocol exceeds the operator's step limit")
        types = {"input:" + name: self.resources.get(ref)["schema_ref"] for name, ref in inputs.items()}
        seen = set()
        for step in protocol.steps:
            if (step.step_id in seen or not set(step.depends_on) <= seen
                    or len(step.depends_on) != len(set(step.depends_on))):
                raise ContractError("Dependencies must be unique, ordered and already declared")
            operation = self.operations.get(step.operation)
            supplied = set(step.arguments) | set(step.bindings)
            required = set(operation.input_schema.get("required", []))
            if required - supplied:
                raise ContractError(f"Step {step.step_id} lacks required inputs: {sorted(required - supplied)}")
            properties = operation.input_schema.get("properties", {})
            if operation.input_schema.get("additionalProperties") is False and supplied - properties.keys():
                raise ContractError(f"Step {step.step_id} contains unknown inputs")
            for name in operation.resource_inputs or {}:
                if name not in step.bindings:
                    raise ContractError("Typed resources must be connected by bindings, not guessed IDs")
            for name, binding in step.bindings.items():
                self._check_binding(binding, types)
                nominal = (operation.resource_inputs or {}).get(name)
                if nominal and (binding.pointer or binding.delivery != "ref"
                                or not self.schemas.compatible(binding.schema_ref, nominal)):
                    raise ContractError("Typed resource input requires an explicit compatible adapter")
            types[f"step:{step.step_id}.result"] = operation.output_schema_ref
            for name, output in step.outputs.items():
                self.schemas.schema(output.schema_ref)
                if not output.pointer.startswith("/") and output.pointer:
                    raise ContractError("Output selector must be a JSON pointer")
                types[f"step:{step.step_id}.{name}"] = output.schema_ref
            seen.add(step.step_id)
        for binding in protocol.final_outputs.values():
            self._check_binding(binding, types)
            if binding.pointer:
                raise ContractError("Final outputs must be whole validated resources; add an output view")
        return {"protocol": protocol.version, "steps": len(seen), "status": "compatible",
                "scope": "nominal references, required fields and DAG; values/semantics checked at runtime"}

    def _check_binding(self, binding: Binding, types):
        if binding.source not in types:
            raise ContractError("Binding refers to a missing or forward output", path=binding.source)
        if not self.schemas.compatible(types[binding.source], binding.schema_ref):
            raise ContractError("Binding schema/version mismatch", path=binding.source)
        if binding.pointer and not binding.pointer.startswith("/"):
            raise ContractError("Binding selector must be a JSON pointer")

    def _read_binding(self, binding, refs):
        if binding.source not in refs:
            raise ContractError("No completed resource for this binding", path=binding.source)
        data = self.resources.get(refs[binding.source], schema_ref=binding.schema_ref)["data"]
        if binding.delivery == "ref":
            if binding.pointer:
                raise ContractError("Reference delivery cannot select a partial resource")
            return refs[binding.source]
        return pointer(data, binding.pointer)

    def execute(self, protocol: Protocol, inputs: dict[str, str], *, validate_final=None,
                after_step=None, configure=None, max_revisions=0, on_error=None, max_repairs=0) -> dict:
        check = self.preflight(protocol, inputs)
        refs = {"input:" + name: ref for name, ref in inputs.items()}
        run_key = "protocol-run:" + digest({"scope": self.resources.scope,
                                           "protocol": protocol.version, "inputs": inputs})
        journal = self.store.get(NAMESPACE, run_key, {"protocol": protocol.model_dump(mode="json"),
            "scope": self.resources.scope, "completed": [], "status": "running", "preflight": check,
            "revisions": [], "reviewed": []})
        journal["status"] = "running"
        self.store.put(NAMESPACE, run_key, journal)
        try:
            if journal.get("active_protocol"):
                protocol = Protocol.model_validate(journal["active_protocol"])
                if configure:
                    configure(protocol)
                self.preflight(protocol, inputs)
            # Operation-specific immutable receipts, independent of the whole protocol hash,
            # allow a repaired downstream step to reuse successful upstream computations.
            index = 0
            while index < len(protocol.steps):
                step = protocol.steps[index]
                op = self.operations.get(step.operation)
                step_key = None
                try:
                    args = deepcopy(step.arguments)
                    args.update({name: self._read_binding(binding, refs) for name, binding in step.bindings.items()})
                    self.schemas.validate(self.operations.input_ref(op), args)
                    parents = list(dict.fromkeys(refs[b.source] for b in step.bindings.values()))
                    step_key = "protocol-step:" + digest({"scope": self.resources.scope,
                        "operation": op.name, "implementation": op.implementation, "arguments": args,
                        "parents": parents, "output_schema": self.schemas.fingerprint(op.output_schema_ref)})
                    receipt = self.store.get(NAMESPACE, step_key)
                    self.store.event("research_step_started", {"run_id": run_key, "step_id": step.step_id,
                        "operation": op.name, "question": step.question, "cache_hit": bool(receipt),
                        "protocol": protocol.version})
                    if receipt is not None and receipt["status"] == "started":
                        raise ContractError("Prior execution has uncertain completion; reconcile it before retrying", code="uncertain_completion")
                    if receipt is None or receipt["status"] == "paused_provider":
                        self.store.put(NAMESPACE, step_key, {"status": "started"})
                        raw = op.invoke(args, step_key)
                        self.schemas.validate(op.output_schema_ref, raw, output=True)
                        result = self.resources.put(op.output_schema_ref, raw,
                            producer=op.name + "@" + op.implementation, parents=parents)
                        receipt = {"status": "done", "resource_id": result["resource_id"]}
                        self.store.put(NAMESPACE, step_key, receipt)
                    elif receipt["status"] != "done":
                        raise ContractError("Prior failed operation retained; change the failing step or reconcile")
                    raw_record = self.resources.get(receipt["resource_id"], schema_ref=op.output_schema_ref)
                    refs[f"step:{step.step_id}.result"] = receipt["resource_id"]
                    # A bad output-view mapping never invalidates the successful raw tool receipt.
                    for name, view in step.outputs.items():
                        data = pointer(raw_record["data"], view.pointer)
                        resource = self.resources.put(view.schema_ref, data,
                            producer="validated-output-view", parents=[receipt["resource_id"]])
                        refs[f"step:{step.step_id}.{name}"] = resource["resource_id"]
                except Exception as exc:
                    current = self.store.get(NAMESPACE, step_key) if step_key else None
                    if current and current.get("status") == "started" and not (
                            isinstance(exc, ContractError) and exc.code == "uncertain_completion"):
                        from proteinrsi.llm import ProviderPaused
                        self.store.put(NAMESPACE, step_key, {
                            "status": "paused_provider" if isinstance(exc, ProviderPaused) else "failed",
                            "error_type": type(exc).__name__})
                    journal.update(status="blocked", failed_step=step.step_id, error_type=type(exc).__name__)
                    self.store.put(NAMESPACE, run_key, journal)
                    if (on_error and isinstance(exc, ContractError) and exc.code != "uncertain_completion"
                            and journal.get("repair_attempts", 0) < max_repairs):
                        journal["repair_attempts"] = journal.get("repair_attempts", 0) + 1
                        self.store.put(NAMESPACE, run_key, journal)
                        revised = on_error(protocol, index, dict(refs), run_key, exc,
                                           journal["repair_attempts"])
                        if revised is not None and revised.version != protocol.version:
                            if ([s.model_dump(mode="json") for s in protocol.steps[:index]] !=
                                    [s.model_dump(mode="json") for s in revised.steps[:index]]):
                                raise ContractError("A repair cannot rewrite completed protocol steps")
                            if configure:
                                configure(revised)
                            self.preflight(revised, inputs)
                            journal.setdefault("repairs", []).append({"step": step.step_id,
                                "from": protocol.version, "to": revised.version, "error": exc.detail()})
                            protocol = revised
                            journal.update(active_protocol=protocol.model_dump(mode="json"), status="running")
                            self.store.put(NAMESPACE, run_key, journal)
                            self.store.event("research_plan_revised", {"run_id": run_key,
                                "reason": "execution_contract_repair", "attempt": journal["repair_attempts"],
                                "error": exc.detail(), "protocol": protocol.version})
                            # Successful receipts remain cached; discard stale suffix bindings only.
                            prefix = {"step:" + x.step_id + "." for x in protocol.steps[:index]}
                            refs = {k: v for k, v in refs.items()
                                    if k.startswith("input:") or any(k.startswith(p) for p in prefix)}
                            continue
                    raise
                if step.step_id not in journal["completed"]:
                    journal["completed"].append(step.step_id)
                journal["refs"] = dict(refs)
                self.store.put(NAMESPACE, run_key, journal)
                self.store.event("research_step_completed", {"run_id": run_key, "step_id": step.step_id,
                    "output_ref": receipt["resource_id"], "protocol": protocol.version})
                if (after_step and step.step_id not in journal["reviewed"]
                        and index + 1 < len(protocol.steps)
                        and len(journal["revisions"]) < max_revisions):
                    revised = after_step(protocol, index + 1, dict(refs), run_key)
                    if revised is not None and revised.version != protocol.version:
                        old_prefix = [s.model_dump(mode="json") for s in protocol.steps[:index + 1]]
                        new_prefix = [s.model_dump(mode="json") for s in revised.steps[:index + 1]]
                        if old_prefix != new_prefix:
                            raise ContractError("A revision cannot rewrite completed protocol steps")
                        if configure:
                            configure(revised)
                        self.preflight(revised, inputs)
                        journal["revisions"].append({"after": step.step_id, "from": protocol.version,
                                                      "to": revised.version})
                        protocol = revised
                        journal["active_protocol"] = protocol.model_dump(mode="json")
                    journal["reviewed"].append(step.step_id)
                    self.store.put(NAMESPACE, run_key, journal)
                index += 1
            outputs = {name: refs[b.source] for name, b in protocol.final_outputs.items()}
            if validate_final is not None:
                validate_final(outputs)
            journal.update(status="complete", final_outputs=outputs)
            self.store.put(NAMESPACE, run_key, journal)
            return {"protocol_version": protocol.version, "run_ref": run_key,
                    "outputs": {name: self.resources.describe(ref) for name, ref in outputs.items()},
                    "measurement_authority": False}
        except Exception as exc:
            journal.update(status="blocked", error_type=type(exc).__name__)
            if isinstance(exc, ContractError):
                journal["validation_error"] = exc.detail()
            self.store.put(NAMESPACE, run_key, journal)
            raise



def register_tool_operations(operations: OperationRegistry, gateway, task, allowed: list[str]):
    """Tool manifests are the only source of native argument/result contracts."""
    from proteinrsi.tools import ToolCall
    for spec in gateway.catalog(task, allowed):
        if spec["data_egress"] and not gateway.allow_egress:
            continue
        fingerprint = digest(spec)
        schema_ref = "tool.result/" + spec["name"] + "/" + fingerprint
        operations.schemas.register(schema_ref, spec["output_schema"])

        def invoke(arguments, key, spec=deepcopy(spec)):
            return gateway.call(ToolCall(name=spec["name"], arguments=arguments,
                purpose="Explicit protocol operation"), task, allowed=allowed, context_key=key)

        operations.register(Operation(name="tool:" + spec["name"], input_schema=spec["input_schema"],
            output_schema_ref=schema_ref, invoke=invoke, implementation=fingerprint))


def register_agent_operations(protocol, operations, llm, resources, context, *, max_repairs=2):
    """Protocol-local agent schemas may vary; no model-generated imports or privileges."""
    for name, schema in protocol.schemas.items():
        operations.schemas.register(name, schema, custom=True)
    for definition in protocol.agent_operations:
        previous = operations._operations.get(definition.name)
        if previous is not None:
            if previous.implementation != digest(definition.model_dump(mode="json")):
                raise ContractError("An existing operation definition is immutable; use a new name/version")
            continue
        # Custom input schemas are also checked before use, not just output schemas.
        input_ref = "custom.input/" + digest(definition.input_schema)
        operations.schemas.register(input_ref, definition.input_schema, custom=True)
        output_schema = operations.schemas.schema(definition.output_schema_ref)

        def invoke(arguments, key, definition=definition, output_schema=output_schema):
            if llm is None:
                raise ContractError("A protocol agent operation requires a configured LLM")
            record_key = key + ":format"
            record = resources.store.get(NAMESPACE, record_key, {"attempts": 0, "errors": []})
            while record["attempts"] <= max_repairs:
                attempt = record["attempts"]
                from proteinrsi.prompting import compose
                raw = llm.complete(definition.role,
                    compose(resources.store, "protocol_step", definition.instructions),
                    {"view": context, "inputs": arguments, "format_attempt": attempt,
                     "validation_errors": record["errors"]}, output_schema)
                record["attempts"] += 1
                resources.store.put(NAMESPACE, record_key, record)
                try:
                    operations.schemas.validate(definition.output_schema_ref, raw, output=True)
                    return raw
                except ContractError as exc:
                    record["errors"] = [exc.detail()]
                    resources.store.put(NAMESPACE, record_key, record)
            raise ContractError("Protocol agent exhausted its format repair limit")

        operations.register(Operation(name=definition.name, input_schema=definition.input_schema,
            output_schema_ref=definition.output_schema_ref, invoke=invoke,
            implementation=digest(definition.model_dump(mode="json"))))


class ProtocolReview(Model):
    rationale: str = Field(min_length=1, max_length=3000)
    replacement: Protocol | None = None
