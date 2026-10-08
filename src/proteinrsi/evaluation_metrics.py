# SPDX-License-Identifier: MIT
"""Frozen task-specific metrics over immutable, capability-free evaluation JSON.

Only an explicitly selected disposable isolation worker executes generated code.
The controller validates contracts and provenance, never scientific preference.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from importlib.metadata import version, distributions
import platform
import re
import sys

from proteinrsi.contracts import EvaluationMetricProgram, canonical, digest
from proteinrsi.dataflow.resources import Metric, MetricTable
from proteinrsi.dataflow.schema import ContractError, SchemaRegistry, pointer
from proteinrsi.llm import ProviderPaused
from proteinrsi.research.code import execute_code
from proteinrsi.storage import Conflict

PROTOCOL = "evaluation_inputs/v1"
# Public legacy receipt constant; active executions use sandbox.generated_profile().
PROFILE = "landlock_seccomp_generated_v1"
BWRAP_PROFILE = "bwrap_seccomp_generated_v1"
_BWRAP_IDENTITY_FIELDS = {
    "sandbox_backend", "bwrap_path", "bwrap_sha256", "bwrap_version", "bwrap_policy_sha256"}
RESULT_NS = "evaluation_metric_results"
INPUT_NS = "evaluation_metric_inputs"
VALIDATION_NS = "evaluation_metric_validations"
ATTEMPT_NS = "evaluation_metric_attempts"
MAX_INPUT_BYTES = 2 * 1024**2
MAX_CRASH_ATTEMPTS = 2
SUBJECTS = ["baseline", "challenger"]


class MetricExecutionPaused(ProviderPaused):
    """A computation failure is a pause, never a numerical rejection or fallback."""


def runtime_identity() -> dict:
    """Pin computation implementation, interpreter and isolation contract."""
    from proteinrsi.research import code, code_worker
    from proteinrsi.replay import sandbox
    from proteinrsi.dataflow import schema, resources
    from proteinrsi import contracts
    source_root = Path(__file__).resolve().parent
    package_sources = {str(path.relative_to(source_root)): digest(path.read_text())
                       for path in sorted(source_root.rglob("*"))
                       if path.is_file() and path.suffix in {".py", ".json", ".md"}}
    dependency_versions = sorted([distribution.metadata["Name"], distribution.version]
                                 for distribution in distributions() if distribution.metadata["Name"])
    profile = sandbox.generated_profile()
    runtime = {"profile": profile, "python": sys.version.split()[0],
            "package_sources_sha256": digest(package_sources),
            "installed_dependency_versions": dependency_versions,
            "python_implementation": platform.python_implementation(),
            "machine": platform.machine(), "numpy": version("numpy"),
            "controller_sha256": digest(Path(__file__).read_text()),
            "executor_sha256": digest(Path(code.__file__).read_text()),
            "worker_sha256": digest(Path(code_worker.__file__).read_text()),
            "sandbox_sha256": digest(Path(sandbox.__file__).read_text()),
            "schema_validator_sha256": digest(Path(schema.__file__).read_text()),
            "jsonschema": version("jsonschema"), "pydantic": version("pydantic"),
            "metric_contracts_sha256": digest(Path(contracts.__file__).read_text()),
            "metric_table_sha256": digest(Path(resources.__file__).read_text()),
            "limits": {"cpu_seconds": 10, "wall_seconds": 20, "memory_bytes": 2 * 1024**3,
                       "output_bytes": 512000},
            "network": False, "credentials": False, "artifacts": False,
            "filesystem": "runtime_readonly; no campaign, labels, or user paths"}
    # Keep historical Landlock identities unchanged apart from source hashes.
    # Only the explicitly selected alternate backend adds executable/policy pins.
    if profile == BWRAP_PROFILE:
        runtime.update(sandbox.backend_identity())
    return runtime


def _recognized_isolation_identity(runtime):
    """Check historical receipts without accessing today's backend or binaries."""
    if not isinstance(runtime, dict):
        return False
    backend_fields = {key for key in runtime
                      if key == "sandbox_backend" or key.startswith("bwrap_")}
    if runtime.get("profile") == PROFILE:
        return not backend_fields
    if (runtime.get("profile") != BWRAP_PROFILE
            or backend_fields != _BWRAP_IDENTITY_FIELDS
            or runtime.get("sandbox_backend") != "bwrap"):
        return False
    path, version_text = runtime.get("bwrap_path"), runtime.get("bwrap_version")
    return (isinstance(path, str) and "\0" not in path and Path(path).is_absolute()
            and isinstance(version_text, str) and bool(version_text.strip())
            and len(version_text) <= 200
            and all(isinstance(runtime.get(field), str)
                    and re.fullmatch(r"[0-9a-f]{64}", runtime[field]) is not None
                    for field in ("bwrap_sha256", "bwrap_policy_sha256")))


def _registry(program):
    registry = SchemaRegistry()
    registry.register("custom.evaluation.input/v1", program.input_schema, custom=True)
    registry.register("custom.evaluation.output/v1", program.output_schema, custom=True)
    if program.input_schema.get("type") != "object" or program.output_schema.get("type") != "object":
        raise ContractError("Metric input and output schemas must describe JSON objects")
    return registry


def _bounded(value):
    if len(canonical(value).encode()) > MAX_INPUT_BYTES:
        raise ContractError("Metric input/contract exceeds 2MB")


def normalize_metric_table(program, output) -> dict:
    """One stable evidence type regardless of the task's generated output schema."""
    _registry(program).validate("custom.evaluation.output/v1", output, output=True)
    raw_rows = pointer(output, program.output_rows_pointer)
    if not isinstance(raw_rows, list) or len(raw_rows) > 64:
        raise ContractError("Metric output must contain a bounded list of rows")
    definitions = {item.name: item for item in program.definitions}
    expected = {(subject, name) for subject in SUBJECTS for name in definitions}
    seen, rows = set(), []
    for raw in raw_rows:
        subject, name = pointer(raw, program.subject_pointer), pointer(raw, program.name_pointer)
        if not isinstance(subject, str) or not isinstance(name, str) or (subject, name) not in expected:
            raise ContractError("Metric row uses an undeclared subject or metric name")
        if (subject, name) in seen:
            raise ContractError("Metric output duplicates a subject/name pair")
        seen.add((subject, name))
        value = pointer(raw, program.value_pointer)
        uncertainty = pointer(raw, program.uncertainty_pointer) if program.uncertainty_pointer else None
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ContractError("Metric values must be finite numbers or explicit null")
        if uncertainty is not None and (isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float))):
            raise ContractError("Metric uncertainty must be finite numeric or explicit null")
        rows.append(Metric(subject_ref=subject, name=name, value=value,
            unit=definitions[name].unit, method="evaluation_program:" + digest(program),
            evidence="computed", uncertainty=uncertainty))
    if seen != expected:
        raise ContractError("Metric output must include every declared subject/name pair, using null for missing evidence")
    return MetricTable(rows=sorted(rows, key=lambda row: (row.subject_ref, row.name))).model_dump(mode="json")


def _run(store, program, inputs):
    from proteinrsi.replay.sandbox import generated_profile
    profile = generated_profile()
    _bounded(inputs)
    _registry(program).validate("custom.evaluation.input/v1", inputs)
    # No view, artifact refs, broker, arbitrary paths or credentials are forwarded.
    result = execute_code(store, None, {"code": program.code, "inputs": deepcopy(inputs)}, pure=True)
    if result.get("status") != "ok":
        # Avoid returning observed inputs/output or exception text to a repair LLM.
        raise ContractError("Isolated metric execution failed: " + str(result.get("error_type", "WorkerFailure")))
    if result.get("execution_backend") != profile or result.get("code_sha256") != digest(program.code):
        raise ContractError("Metric result lacks the required isolated execution provenance")
    output = result.get("output")
    from proteinrsi.llm_evaluation import _check_public
    _check_public(output)
    _bounded(output)
    return output, normalize_metric_table(program, output)


def validate_metric_program(store, program: EvaluationMetricProgram) -> dict:
    """Run pre-outcome synthetic fixtures twice; freeze only tested source/contracts."""
    _bounded(program.model_dump(mode="json"))
    registry = _registry(program)
    runtime = runtime_identity()
    key = digest({"program_sha256": digest(program), "runtime": runtime})
    prior = store.get(VALIDATION_NS, key)
    if prior is not None:
        if prior.get("validation_ref") != VALIDATION_NS + "/" + key:
            raise Conflict("Invalid frozen metric validation")
        _check_validation(prior, program)
        return prior
    attempt_key = "validation-" + key
    attempt = store.get(ATTEMPT_NS, attempt_key)
    if attempt and attempt.get("state") == "failed":
        raise ContractError(attempt["error"])
    if attempt and attempt.get("runs", 0) >= MAX_CRASH_ATTEMPTS:
        raise MetricExecutionPaused("Metric fixture validation interrupted repeatedly; reconcile the same frozen request")
    attempt = {"state": "running", "program_sha256": digest(program),
               "runtime": runtime, "runs": (attempt or {}).get("runs", 0) + 1,
               "fixture_only": True}
    store.put(ATTEMPT_NS, attempt_key, attempt)
    results = []
    try:
        for fixture in program.tests:
            registry.validate("custom.evaluation.input/v1", fixture.inputs)
            normalize_metric_table(program, fixture.expected_output)
            first, table = _run(store, program, fixture.inputs)
            second, second_table = _run(store, program, fixture.inputs)
            if canonical(first) != canonical(fixture.expected_output):
                raise ContractError("Metric fixture did not match its declared expected output: " + fixture.name)
            if canonical(first) != canonical(second) or table != second_table:
                raise ContractError("Metric fixture is nondeterministic: " + fixture.name)
            results.append({"name": fixture.name, "input_sha256": digest(fixture.inputs),
                            "output_sha256": digest(first), "metric_table_sha256": digest(table),
                            "runs": 2, "synthetic_fixture": True})
    except (ValueError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ContractError) else "Metric fixture contract failed"
        store.put(ATTEMPT_NS, attempt_key, {**attempt, "state": "failed", "error": message})
        raise ContractError(message) from exc
    except Exception:
        # Missing kernel primitives can be reconciled without changing the source.
        # Do not spend crash retries when the worker did not start.
        from proteinrsi.replay.sandbox import SandboxUnavailable
        if isinstance(sys.exception(), SandboxUnavailable):
            store.put(ATTEMPT_NS, attempt_key, {**attempt, "state": "pending", "runs": attempt["runs"] - 1})
        raise
    body = {"program_sha256": digest(program), "code_sha256": digest(program.code),
            "input_schema_sha256": digest(program.input_schema),
            "output_schema_sha256": digest(program.output_schema), "runtime": runtime,
            "tests": results, "fixture_only": True, "status": "validated"}
    # Key pins inputs/runtime; an additional hash protects all fixture receipts.
    record = {**body, "validation_ref": VALIDATION_NS + "/" + key, "validation_sha256": digest(body)}
    with store.transaction():
        store.put(VALIDATION_NS, key, record, immutable=True)
        store.put(ATTEMPT_NS, attempt_key, {**attempt, "state": "completed", "validation_ref": record["validation_ref"]})
    return record


def _check_validation(record, program):
    body = {k: v for k, v in record.items() if k not in {"validation_ref", "validation_sha256"}}
    expected = digest({"program_sha256": digest(program), "runtime": record.get("runtime")})
    expected_tests = [{"name": fixture.name, "input_sha256": digest(fixture.inputs),
                       "output_sha256": digest(fixture.expected_output),
                       "metric_table_sha256": digest(normalize_metric_table(program, fixture.expected_output)),
                       "runs": 2, "synthetic_fixture": True} for fixture in program.tests]
    if (record.get("validation_ref") != VALIDATION_NS + "/" + expected
            or record.get("validation_sha256") != digest(body)
            or record.get("program_sha256") != digest(program)
            or record.get("code_sha256") != digest(program.code)
            or record.get("input_schema_sha256") != digest(program.input_schema)
            or record.get("output_schema_sha256") != digest(program.output_schema)
            or record.get("tests") != expected_tests
            or not _recognized_isolation_identity(record.get("runtime"))
            or record.get("fixture_only") is not True or record.get("status") != "validated"):
        raise Conflict("Frozen metric program validation was altered")


def verify_plan_program(store, plan, *, executing=False):
    program = EvaluationMetricProgram.model_validate(plan["plan"]["metric_program"])
    ref = plan.get("metric_validation_ref")
    if not isinstance(ref, str) or not ref.startswith(VALIDATION_NS + "/"):
        raise Conflict("Custom metric plan has no pre-outcome validation")
    validation = store.get(VALIDATION_NS, ref.split("/", 1)[1])
    if not isinstance(validation, dict) or validation.get("validation_ref") != ref:
        raise Conflict("Missing metric program validation")
    _check_validation(validation, program)
    if plan.get("metric_program_sha256") != digest(program):
        raise Conflict("Plan metric program hash differs from its validated source")
    if executing:
        from proteinrsi.replay.sandbox import SandboxUnavailable
        try:
            current_runtime = runtime_identity()
        except SandboxUnavailable as exc:
            raise MetricExecutionPaused("Frozen metric runtime is unavailable; restore its original isolation backend, no unsafe fallback") from exc
        if validation["runtime"] != current_runtime:
            raise MetricExecutionPaused("Frozen metric runtime changed; use its original runtime, never silently upgrade a measured trial")
    return program, validation


def _addressed(store, namespace, ref):
    if not isinstance(ref, str) or not ref.startswith(namespace + "/"):
        raise Conflict("Invalid metric artifact reference")
    record = store.get(namespace, ref.split("/", 1)[1])
    if not isinstance(record, dict) or record.get("ref") != ref:
        raise Conflict("Missing metric artifact")
    body = {k: v for k, v in record.items() if k not in {"ref", "result_ref"}}
    if namespace + "/" + digest(body) != ref:
        raise Conflict("Metric artifact content differs from its identity")
    return record


def load_evaluation_metric_result(store, ref):
    from proteinrsi.llm_evaluation import load_evaluation_plan
    record = _addressed(store, RESULT_NS, ref)
    plan = load_evaluation_plan(store, record["plan_ref"])
    program, validation = verify_plan_program(store, plan)
    inputs = _addressed(store, INPUT_NS, record["input_ref"])
    attempt_key = digest({"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"]})
    attempt = store.get(ATTEMPT_NS, attempt_key)
    if (not attempt or attempt.get("state") != "completed"
            or attempt.get("result_ref") != ref or attempt.get("input_ref") != record["input_ref"]
            or attempt.get("evaluation_id") != plan["evaluation_id"]
            or attempt.get("plan_ref") != plan["plan_ref"]):
        raise Conflict("Metric result is not the committed result of this frozen execution")
    envelope = inputs["envelope"]
    _validate_envelope(envelope, plan)
    bound = {name: deepcopy(pointer(envelope, path)) for name, path in program.input_bindings.items()}
    _registry(program).validate("custom.evaluation.input/v1", bound)
    if (record["result_ref"] != ref or record["evaluation_id"] != plan["evaluation_id"]
            or inputs["plan_ref"] != record["plan_ref"]
            or inputs["evaluation_id"] != record["evaluation_id"]
            or inputs["input_schema_sha256"] != digest(program.input_schema)
            or inputs["inputs"] != bound
            or record["program_sha256"] != digest(program)
            or record["code_sha256"] != digest(program.code)
            or record["runtime"] != validation["runtime"]
            or record["definitions"] != [item.model_dump(mode="json") for item in program.definitions]
            or record["denominators"] != envelope["denominators"]
            or record["deterministic_runs"] != 2
            or record["measurement_authority"] is not False
            or record["evidence_kind"] != "computed_from_scoped_observations"
            or record["validation_ref"] != validation["validation_ref"]
            or digest(record["output"]) != record["output_sha256"]
            or normalize_metric_table(program, record["output"]) != record["metric_table"]
            or digest(record["metric_table"]) != record["metric_table_sha256"]):
        raise Conflict("Metric result provenance or MetricTable was altered")
    return deepcopy(record)


def _validate_envelope(envelope, plan):
    required = {"protocol", "evaluation_id", "target", "task", "top_ns", "subject_refs",
                "arms", "cases", "denominators"}
    if (set(envelope) != required or envelope["protocol"] != PROTOCOL
            or envelope["evaluation_id"] != plan["evaluation_id"]
            or envelope["target"] != plan["target"] or envelope["subject_refs"] != SUBJECTS
            or envelope["top_ns"] != plan["plan"]["top_ns"]):
        raise Conflict("Metric input envelope differs from its frozen evaluation scope")


def execute_evaluation_metrics(store, plan, envelope):
    """Execute once durably, return the identical verified artifact after restart.

    Repeated execution is permitted only after an interrupted pure computation,
    with a bounded crash retry. A completed code/contract failure remains paused;
    code, fixtures, criteria and inputs cannot be repaired after viewing outcomes.
    """
    from proteinrsi.llm_evaluation import _check_public, _evaluation_lock, load_evaluation_plan
    frozen = load_evaluation_plan(store, plan["plan_ref"])
    if frozen != plan:
        raise Conflict("Caller changed the frozen evaluation plan")
    program, validation = verify_plan_program(store, plan)
    _check_public(envelope)
    _bounded(envelope)
    _validate_envelope(envelope, plan)
    projection_error = None
    try:
        inputs = {name: deepcopy(pointer(envelope, path)) for name, path in program.input_bindings.items()}
    except ContractError as exc:
        # Preserve the exact measured envelope even when its dynamic projection
        # fails. This is a paused frozen computation, not a request to retune it.
        inputs = None
        projection_error = exc.code
    input_body = {"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"],
                  "envelope": envelope, "inputs": inputs, "input_schema_sha256": digest(program.input_schema)}
    if projection_error:
        input_body["projection_error"] = projection_error
    input_ref = INPUT_NS + "/" + digest(input_body)
    key = digest({"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"]})
    with _evaluation_lock(store):
        attempt = store.get(ATTEMPT_NS, key)
        if attempt and attempt["input_ref"] != input_ref:
            raise Conflict("An evaluation cannot replace its frozen metric inputs")
        if attempt and attempt.get("result_ref"):
            return load_evaluation_metric_result(store, attempt["result_ref"])
        if attempt and (attempt["state"] == "failed" or attempt.get("runs", 0) >= MAX_CRASH_ATTEMPTS):
            raise MetricExecutionPaused("Frozen metric execution is paused after failure; preserve evidence and start any revised plan in a new prospective evaluation")
        store.put(INPUT_NS, input_ref.split("/", 1)[1], {**input_body, "ref": input_ref}, immutable=True)
        if attempt is None:
            attempt = {"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"],
                       "input_ref": input_ref, "state": "pending", "runs": 0}
            store.put(ATTEMPT_NS, key, attempt)
        if projection_error:
            store.put(ATTEMPT_NS, key, {**attempt, "state": "failed", "error_type": "InputProjection"})
            raise MetricExecutionPaused("Frozen metric input binding failed; measurements and exact inputs are preserved without post-outcome repair")
        verify_plan_program(store, plan, executing=True)
        # Unsupported isolation is never a request to execute in the controller.
        from proteinrsi.replay.sandbox import probe
        if not probe()["available"]:
            raise MetricExecutionPaused("Custom evaluation metrics require the selected isolation backend and seccomp; no unsafe fallback")
        attempt = {**attempt, "state": "running", "runs": attempt["runs"] + 1}
        store.put(ATTEMPT_NS, key, attempt)
        try:
            output, table = _run(store, program, inputs)
            repeated, repeated_table = _run(store, program, inputs)
            if canonical(output) != canonical(repeated) or table != repeated_table:
                raise ContractError("Metric computation is nondeterministic on the frozen inputs")
        except Exception as exc:
            store.put(ATTEMPT_NS, key, {**attempt, "state": "failed", "error_type": type(exc).__name__})
            store.event("evaluation_metric_paused", {"evaluation_id": plan["evaluation_id"],
                        "input_ref": input_ref, "error_type": type(exc).__name__})
            raise MetricExecutionPaused("Frozen metric computation failed; no verdict or replacement metrics were produced (" + type(exc).__name__ + ")") from exc
        body = {"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"],
                "program_sha256": digest(program), "code_sha256": digest(program.code),
                "input_ref": input_ref, "validation_ref": validation["validation_ref"],
                "runtime": validation["runtime"], "output": output, "output_sha256": digest(output),
                "metric_table": table, "metric_table_sha256": digest(table),
                "definitions": [item.model_dump(mode="json") for item in program.definitions],
                "denominators": deepcopy(envelope["denominators"]),
                "deterministic_runs": 2, "measurement_authority": False,
                "evidence_kind": "computed_from_scoped_observations"}
        ref = RESULT_NS + "/" + digest(body)
        record = {**body, "ref": ref, "result_ref": ref}
        with store.transaction():
            store.put(RESULT_NS, ref.split("/", 1)[1], record, immutable=True)
            store.put(ATTEMPT_NS, key, {**attempt, "state": "completed", "result_ref": ref})
            store.event("evaluation_metrics_computed", {"evaluation_id": plan["evaluation_id"],
                        "evaluation_metric_result_ref": ref.split("/", 1)[1],
                        "metric_table_sha256": digest(table)})
        return load_evaluation_metric_result(store, ref)
