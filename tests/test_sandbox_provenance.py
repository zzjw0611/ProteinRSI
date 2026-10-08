# SPDX-License-Identifier: MIT
"""UNIT-ONLY sandbox provenance tests, never isolation or scientific evidence.

The worker, availability probe and executable identity are explicitly mocked.
Generated source is never executed, no provider is called, and every returned
value is fixed protocol-test JSON. Real confinement acceptance is tested elsewhere.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from proteinrsi import evaluation_metrics as metrics
from proteinrsi.contracts import EvaluationMetricProgram, digest
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.replay import sandbox
from proteinrsi.storage import Conflict, Store


BWRAP_IDENTITY = {
    "sandbox_backend": "bwrap",
    "bwrap_path": "/synthetic-unit-only/bin/bwrap",
    "bwrap_sha256": "a" * 64,
    "bwrap_version": "bubblewrap unit-fixture",
    "bwrap_policy_sha256": "b" * 64,
}
OUTPUT = {"rows": [{"subject_ref": subject, "name": "unit_value", "value": None}
                   for subject in ("baseline", "challenger")]}


def unit_program():
    return EvaluationMetricProgram(
        version="unit-provenance-v1",
        code="result = {'rows': [{'subject_ref': s, 'name': 'unit_value', 'value': None} "
             "for s in ['baseline', 'challenger']]}",
        definitions=[{"name": "unit_value", "description": "Unit-only empty protocol value",
                      "unit": "unit-fixture", "direction": "descriptive"}],
        input_bindings={"case": "/task/case"},
        input_schema={"type": "object", "properties": {"case": {"type": "integer"}},
                      "required": ["case"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"rows": {"type": "array"}},
                       "required": ["rows"], "additionalProperties": False},
        tests=[{"name": "unit-case-" + str(index), "inputs": {"case": index},
                "expected_output": deepcopy(OUTPUT)} for index in (0, 1)])


@pytest.fixture
def unit_worker(monkeypatch):
    """Fixed transport JSON with mutable fake backend pins; never eval or exec."""
    monkeypatch.delenv("PROTEINRSI_SANDBOX_BACKEND", raising=False)
    identity = deepcopy(BWRAP_IDENTITY)
    calls = []
    monkeypatch.setattr(sandbox, "backend_identity", lambda: deepcopy(identity))
    monkeypatch.setattr(sandbox, "probe", lambda: {"available": True, "unit_only": True})

    def runtime():
        # Stable unit runtime avoids filesystem/version changes during these tests.
        profile = sandbox.generated_profile()
        return {"profile": profile, "unit_only": True,
                **(deepcopy(identity) if profile == metrics.BWRAP_PROFILE else {})}

    def execute(store, view, arguments, *, pure=False):
        assert pure is True and view is None
        assert set(arguments) == {"code", "inputs"}
        calls.append(deepcopy(arguments))
        return {"status": "ok", "execution_backend": sandbox.generated_profile(),
                "code_sha256": digest(arguments["code"]), "output": deepcopy(OUTPUT)}

    monkeypatch.setattr(metrics, "runtime_identity", runtime)
    monkeypatch.setattr(metrics, "execute_code", execute)
    return SimpleNamespace(identity=identity, calls=calls)


def unit_trial(tmp_path):
    """Build a synthetic frozen plan directly; no LLM or measured trial is used."""
    store = Store(tmp_path / "unit-provenance")
    program = unit_program()
    validation = metrics.validate_metric_program(store, program)
    body = {
        "evaluation_id": "unit-only-trial", "target": "workflow",
        "metric_validation_ref": validation["validation_ref"],
        "metric_program_sha256": digest(program),
        "plan": {"metric_program": program.model_dump(mode="json"), "top_ns": [1],
                 "criteria": ["Unit-only protocol check"], "rationale": "Unit fixture",
                 "tradeoff_handling": "Unit fixture", "missing_evidence_handling": "Null"},
    }
    plan = {**body, "plan_ref": "evaluation_plans/" + digest(body)}
    store.put("evaluation_plans", plan["plan_ref"].split("/", 1)[1], plan, immutable=True)
    envelope = {"protocol": metrics.PROTOCOL, "evaluation_id": plan["evaluation_id"],
                "target": "workflow", "task": {"case": 2}, "top_ns": [1],
                "subject_refs": ["baseline", "challenger"], "arms": {}, "cases": [],
                "denominators": {}}
    return store, program, validation, plan, envelope


def rehash_validation(record, runtime):
    """Allow rehashing in adversarial unit cases so the schema check is exercised."""
    body = {key: deepcopy(value) for key, value in record.items()
            if key not in {"validation_ref", "validation_sha256"}}
    body["runtime"] = deepcopy(runtime)
    key = digest({"program_sha256": body["program_sha256"], "runtime": runtime})
    return {**body, "validation_ref": metrics.VALIDATION_NS + "/" + key,
            "validation_sha256": digest(body)}


def forbidden_current_backend(*args, **kwargs):
    pytest.fail("Historical receipt validation must not inspect the current backend")


def test_unit_default_runtime_keeps_legacy_profile_without_backend_pins(monkeypatch):
    monkeypatch.delenv("PROTEINRSI_SANDBOX_BACKEND", raising=False)
    monkeypatch.setattr(sandbox, "backend_identity", forbidden_current_backend)
    runtime = metrics.runtime_identity()
    assert metrics.PROFILE == "landlock_seccomp_generated_v1"
    assert runtime["profile"] == metrics.PROFILE
    assert not {key for key in runtime if key == "sandbox_backend" or key.startswith("bwrap_")}
    assert metrics._recognized_isolation_identity(runtime)


def test_unit_bwrap_runtime_adds_binary_version_and_policy_pins(monkeypatch):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap")
    monkeypatch.setattr(sandbox, "backend_identity", lambda: deepcopy(BWRAP_IDENTITY))
    runtime = metrics.runtime_identity()
    assert runtime["profile"] == metrics.BWRAP_PROFILE
    assert {key: runtime[key] for key in BWRAP_IDENTITY} == BWRAP_IDENTITY
    assert metrics._recognized_isolation_identity(runtime)
    assert "sandbox_sha256" in runtime and "package_sources_sha256" in runtime


@pytest.mark.parametrize("selected", ["landlock", "bwrap"])
@pytest.mark.parametrize("reported", [metrics.PROFILE, metrics.BWRAP_PROFILE, "inprocess"])
def test_unit_run_requires_current_selected_profile(monkeypatch, unit_worker, selected, reported):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", selected)
    program = unit_program()
    monkeypatch.setattr(metrics, "execute_code", lambda *args, **kwargs: {
        "status": "ok", "execution_backend": reported,
        "code_sha256": digest(program.code), "output": deepcopy(OUTPUT)})
    if reported == sandbox.generated_profile():
        assert metrics._run(None, program, {"case": 0})[0] == OUTPUT
    else:
        with pytest.raises(ContractError, match="execution provenance"):
            metrics._run(None, program, {"case": 0})


@pytest.mark.parametrize("backend", ["landlock", "bwrap"])
def test_unit_historical_validation_does_not_need_current_backend(
        tmp_path, monkeypatch, unit_worker, backend):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", backend)
    store, program, validation, plan, _ = unit_trial(tmp_path)
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "not-installed-or-selected")
    monkeypatch.setattr(metrics, "runtime_identity", forbidden_current_backend)
    monkeypatch.setattr(sandbox, "backend_identity", forbidden_current_backend)
    monkeypatch.setattr(sandbox, "probe", forbidden_current_backend)
    metrics._check_validation(validation, program)
    assert metrics.verify_plan_program(store, plan)[1] == validation
    assert len(unit_worker.calls) == 4


@pytest.mark.parametrize("runtime", [
    None, [], {}, {"profile": None}, {"profile": "landlock"},
    {"profile": "landlock_seccomp_generated_v2"},
    {"profile": "bwrap_seccomp_generated_v2", **BWRAP_IDENTITY},
    {"profile": metrics.PROFILE, **BWRAP_IDENTITY},
    {"profile": metrics.BWRAP_PROFILE},
])
def test_unit_rehashed_unknown_or_mismatched_profile_is_rejected(tmp_path, unit_worker, runtime):
    _, program, validation, _, _ = unit_trial(tmp_path)
    with pytest.raises(Conflict, match="validation was altered"):
        metrics._check_validation(rehash_validation(validation, runtime), program)


@pytest.mark.parametrize("field", list(BWRAP_IDENTITY))
def test_unit_rehashed_bwrap_receipt_requires_every_backend_pin(tmp_path, unit_worker, field):
    _, program, validation, _, _ = unit_trial(tmp_path)
    runtime = {"profile": metrics.BWRAP_PROFILE, **BWRAP_IDENTITY}
    del runtime[field]
    with pytest.raises(Conflict, match="validation was altered"):
        metrics._check_validation(rehash_validation(validation, runtime), program)


@pytest.mark.parametrize("field,value", [
    ("sandbox_backend", "landlock"), ("bwrap_path", "relative/bwrap"),
    ("bwrap_path", None), ("bwrap_path", "/bad\0path"),
    ("bwrap_sha256", "A" * 64), ("bwrap_sha256", "g" * 64),
    ("bwrap_sha256", "a" * 63), ("bwrap_policy_sha256", False),
    ("bwrap_policy_sha256", "b" * 65), ("bwrap_version", ""),
    ("bwrap_version", " \n"), ("bwrap_version", "v" * 201),
    ("bwrap_version", 1), ("bwrap_unrecognized", "extra"),
])
def test_unit_rehashed_bwrap_receipt_rejects_malformed_identity(
        tmp_path, unit_worker, field, value):
    _, program, validation, _, _ = unit_trial(tmp_path)
    runtime = {"profile": metrics.BWRAP_PROFILE, **BWRAP_IDENTITY, field: value}
    with pytest.raises(Conflict, match="validation was altered"):
        metrics._check_validation(rehash_validation(validation, runtime), program)


@pytest.mark.parametrize("initial,changed", [("landlock", "bwrap"), ("bwrap", "landlock")])
def test_unit_backend_switch_pauses_unfinished_trial_without_revalidation(
        tmp_path, monkeypatch, unit_worker, initial, changed):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", initial)
    store, _, validation, plan, envelope = unit_trial(tmp_path)
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", changed)
    for _ in range(2):
        with pytest.raises(metrics.MetricExecutionPaused, match="runtime changed"):
            metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    assert len(unit_worker.calls) == 4
    assert store.all(metrics.VALIDATION_NS) == {validation["validation_ref"].split("/", 1)[1]: validation}
    assert not store.all(metrics.RESULT_NS)
    inputs = next(iter(store.all(metrics.INPUT_NS).values()))
    assert inputs["envelope"] == envelope
    attempt = [value for key, value in store.all(metrics.ATTEMPT_NS).items()
               if not key.startswith("validation-")][0]
    assert attempt["state"] == "pending" and attempt["runs"] == 0


@pytest.mark.parametrize("field,value", [
    ("bwrap_path", "/different-unit-only/bin/bwrap"),
    ("bwrap_sha256", "c" * 64), ("bwrap_version", "bubblewrap different-unit-fixture"),
    ("bwrap_policy_sha256", "d" * 64),
])
def test_unit_backend_pin_change_pauses_frozen_execution(
        tmp_path, monkeypatch, unit_worker, field, value):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap")
    store, _, validation, plan, envelope = unit_trial(tmp_path)
    unit_worker.identity[field] = value
    with pytest.raises(metrics.MetricExecutionPaused, match="runtime changed"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(unit_worker.calls) == 4
    assert metrics.verify_plan_program(store, plan)[1] == validation
    assert not store.all(metrics.RESULT_NS)


def test_unit_unavailable_frozen_backend_pauses_before_worker(tmp_path, monkeypatch, unit_worker):
    store, _, _, plan, envelope = unit_trial(tmp_path)

    def unavailable():
        raise sandbox.SandboxUnavailable("Unit fixture: executable removed")

    monkeypatch.setattr(metrics, "runtime_identity", unavailable)
    with pytest.raises(metrics.MetricExecutionPaused, match="runtime is unavailable"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(unit_worker.calls) == 4 and not store.all(metrics.RESULT_NS)


@pytest.mark.parametrize("backend", ["landlock", "bwrap"])
def test_unit_completed_result_replays_with_original_identity_without_backend_access(
        tmp_path, monkeypatch, unit_worker, backend):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", backend)
    store, _, validation, plan, envelope = unit_trial(tmp_path)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(unit_worker.calls) == 6
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap" if backend == "landlock" else "landlock")
    monkeypatch.setattr(metrics, "runtime_identity", forbidden_current_backend)
    monkeypatch.setattr(metrics, "execute_code", forbidden_current_backend)
    monkeypatch.setattr(sandbox, "backend_identity", forbidden_current_backend)
    monkeypatch.setattr(sandbox, "probe", forbidden_current_backend)
    restarted = Store(store.root)
    assert metrics.load_evaluation_metric_result(restarted, result["result_ref"]) == result
    assert metrics.execute_evaluation_metrics(restarted, plan, envelope) == result
    assert result["runtime"] == validation["runtime"]
    assert store.all(metrics.VALIDATION_NS) == {validation["validation_ref"].split("/", 1)[1]: validation}


@pytest.mark.parametrize("initial,changed", [("landlock", "bwrap"), ("bwrap", "landlock")])
def test_unit_interrupted_trial_requires_original_backend_on_resume(
        tmp_path, monkeypatch, unit_worker, initial, changed):
    class UnitInterruption(BaseException):
        pass

    def interrupted_worker(*args, **kwargs):
        raise UnitInterruption()

    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", initial)
    store, _, validation, plan, envelope = unit_trial(tmp_path)
    with monkeypatch.context() as interrupt:
        interrupt.setattr(metrics, "execute_code", interrupted_worker)
        with pytest.raises(UnitInterruption):
            metrics.execute_evaluation_metrics(store, plan, envelope)
    key = digest({"evaluation_id": plan["evaluation_id"], "plan_ref": plan["plan_ref"]})
    interrupted = store.get(metrics.ATTEMPT_NS, key)
    assert interrupted["state"] == "running" and interrupted["runs"] == 1
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", changed)
    with pytest.raises(metrics.MetricExecutionPaused, match="runtime changed"):
        metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    assert store.get(metrics.ATTEMPT_NS, key) == interrupted
    assert len(unit_worker.calls) == 4 and not store.all(metrics.RESULT_NS)
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", initial)
    result = metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    assert result["runtime"] == validation["runtime"] and len(unit_worker.calls) == 6
    assert len(store.all(metrics.VALIDATION_NS)) == 1
