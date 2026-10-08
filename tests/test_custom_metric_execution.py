"""Synthetic custom-metric contracts and mandatory supported-sandbox acceptance.

The plumbing fixture below returns explicitly supplied JSON; it never evaluates
generated source. Only TestSupportedMetricSandbox executes generated programs,
and it uses the production disposable Landlock/seccomp worker. CI sets
PROTEINRSI_REQUIRE_METRIC_SANDBOX=1 so missing isolation fails rather than skips.
Nothing in this module calls a provider or runs a scientific experiment.
"""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from proteinrsi.contracts import EvaluationMetricProgram, digest
from proteinrsi.dataflow.schema import ContractError
from proteinrsi import evaluation_metrics as metrics
from proteinrsi.llm import ProviderPaused
from proteinrsi.llm_evaluation import (MAX_RESPONSE_REPAIRS, adjudicate_evaluation,
                                     ensure_evaluation_plan)
from proteinrsi.replay import sandbox
from proteinrsi.storage import Conflict, Store


METRIC_NAME = "task.margin-per-site.v7"
SANDBOX_STATE = sandbox.probe()
REQUIRE_SANDBOX = os.environ.get("PROTEINRSI_REQUIRE_METRIC_SANDBOX") == "1"


def output_fixture(baseline=2.0, challenger=3.0, *, name=METRIC_NAME):
    """Declared expected JSON, not an implementation of generated metric code."""
    return {"diagnostics": {"observations": [
        {"who": {"id": subject}, "metric": {"key": name},
         "estimate": {"value": value, "sigma": None}}
        for subject, value in (("challenger", challenger), ("baseline", baseline))]}}


def program_fixture(*, code_prefix="", name=METRIC_NAME):
    """Two artificial pre-outcome cases with a task-chosen, nested result shape."""
    code = code_prefix + f"""
assert context == {{}}
assert artifacts == {{}}
rows = []
for subject in ['challenger', 'baseline']:
    values = inputs['subjects'][subject]['values']
    value = sum(values) / len(values) / inputs['normalizer'] if values else None
    rows.append({{'who': {{'id': subject}}, 'metric': {{'key': {name!r}}},
                 'estimate': {{'value': value, 'sigma': None}}}})
result = {{'diagnostics': {{'observations': rows}}}}
"""
    return EvaluationMetricProgram(
        version="synthetic-contract-v1", code=code,
        definitions=[{"name": name, "description": "Synthetic normalized mean for protocol testing",
                      "unit": "fixture_units/site", "direction": "maximize"}],
        input_schema={"type": "object", "properties": {
            "subjects": {"type": "object", "properties": {
                subject: {"type": "object", "properties": {
                    "values": {"type": "array", "items": {"type": "number"}}},
                    "required": ["values"], "additionalProperties": False}
                for subject in ("baseline", "challenger")},
                "required": ["baseline", "challenger"], "additionalProperties": False},
            "normalizer": {"type": "number", "exclusiveMinimum": 0}},
            "required": ["subjects", "normalizer"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {
            "diagnostics": {"type": "object", "properties": {
                "observations": {"type": "array", "items": {"type": "object"}}},
                "required": ["observations"], "additionalProperties": False}},
            "required": ["diagnostics"], "additionalProperties": False},
        input_bindings={"subjects": "/arms", "normalizer": "/task/normalizer"},
        output_rows_pointer="/diagnostics/observations", subject_pointer="/who/id",
        name_pointer="/metric/key", value_pointer="/estimate/value",
        uncertainty_pointer="/estimate/sigma",
        tests=[{"name": "synthetic-two-arms", "inputs": {
                    "subjects": {"baseline": {"values": [2, 6]},
                                 "challenger": {"values": [4, 8]}}, "normalizer": 2},
                "expected_output": output_fixture(name=name)},
               {"name": "synthetic-missing-and-negative", "inputs": {
                    "subjects": {"baseline": {"values": []},
                                 "challenger": {"values": [-2, 0]}}, "normalizer": 1},
                "expected_output": output_fixture(None, -1.0, name=name)}])


def envelope_fixture(plan):
    return {"protocol": metrics.PROTOCOL, "evaluation_id": plan["evaluation_id"],
            "target": plan["target"], "task": {"normalizer": 2}, "top_ns": [2],
            "subject_refs": ["baseline", "challenger"],
            "arms": {"baseline": {"values": [10, 14]},
                     "challenger": {"values": [20, 22]}}, "cases": [],
            "denominators": {subject: {"submitted": 2, "unique_valid": 2}
                             for subject in ("baseline", "challenger")}}


class ScriptedMetricLLM:
    """A protocol-only response fixture. There is no network or model API."""
    model, base_url, cache_settings = "synthetic-metric-fixture", "https://fixture.invalid", {}

    def __init__(self, program):
        self.program, self.calls = program, []

    def complete(self, role, instructions, context, schema):
        self.calls.append((role, deepcopy(context)))
        if role == "E-plan":
            return {"metric_program": self.program.model_dump(mode="json"), "top_ns": [2],
                    "criteria": ["Compare the task-defined normalized synthetic metric"],
                    "rationale": "Protocol fixture only, not scientific evidence",
                    "tradeoff_handling": "Describe the observed tradeoffs",
                    "missing_evidence_handling": "Keep missing values explicitly null"}
        assert role == "E-verdict"
        ref = next(ref for ref in context["available_evidence_refs"]
                   if ref.startswith(metrics.RESULT_NS + "/"))
        return {"plan_ref": context["plan_ref"], "decision": "inconclusive",
                "reason": "Synthetic fixtures establish mechanics only",
                "supporting_evidence_refs": [ref]}


def freeze_fixture(tmp_path, program=None):
    store = Store(tmp_path / "synthetic-campaign")
    program = program or program_fixture()
    team = SimpleNamespace(llm=ScriptedMetricLLM(program))
    plan = ensure_evaluation_plan(store, team, evaluation_id="synthetic-trial-1",
        target="workflow", context={"objective": "Synthetic task-specific evaluation"}, max_top_n=2)
    return store, team, plan


@pytest.fixture
def plumbing_executor(monkeypatch):
    """Explicit JSON transport mock; never exec/eval/compile the supplied code."""
    replies, calls = {}, []

    def execute_fixture(store, view, arguments, *, pure=False):
        assert pure is True and view is None
        assert set(arguments) == {"code", "inputs"}
        calls.append(deepcopy(arguments))
        output = replies[digest(arguments["inputs"])]
        if callable(output):
            return output(arguments)
        return {"status": "ok", "execution_backend": sandbox.generated_profile(),
                "code_sha256": digest(arguments["code"]), "output": deepcopy(output)}

    def register(program, envelope=None, output=None):
        for fixture in program.tests:
            replies[digest(fixture.inputs)] = deepcopy(fixture.expected_output)
        if envelope is not None:
            inputs = {"subjects": deepcopy(envelope["arms"]),
                      "normalizer": envelope["task"]["normalizer"]}
            replies[digest(inputs)] = deepcopy(output or output_fixture(6, 10.5,
                                                    name=program.definitions[0].name))

    monkeypatch.setattr(metrics, "execute_code", execute_fixture)
    monkeypatch.setattr(sandbox, "probe", lambda: {"available": True, "fixture_only": True})
    return SimpleNamespace(calls=calls, replies=replies, register=register)


def plumbing_trial(tmp_path, plumbing_executor, program=None):
    program = program or program_fixture()
    plumbing_executor.register(program)
    store, team, plan = freeze_fixture(tmp_path, program)
    envelope = envelope_fixture(plan)
    plumbing_executor.register(program, envelope)
    return store, team, plan, envelope


def rewrite_addressed(store, namespace, record):
    """Attacker can rewrite JSON and its hash; provenance must still reject it."""
    body = {key: value for key, value in record.items() if key not in {"ref", "result_ref"}}
    ref = namespace + "/" + digest(body)
    replacement = {**body, "ref": ref}
    if namespace == metrics.RESULT_NS:
        replacement["result_ref"] = ref
    store.put(namespace, ref.split("/", 1)[1], replacement)
    return ref


def outcome_attempts(store):
    return {key: value for key, value in store.all(metrics.ATTEMPT_NS).items()
            if not key.startswith("validation-")}


@pytest.mark.parametrize("name", [METRIC_NAME, "novelTaskMetric42", "binder.local-confidence"])
def test_plumbing_arbitrary_names_bindings_shapes_and_durable_replay(tmp_path, plumbing_executor, name):
    program = program_fixture(name=name)
    store, team, plan, envelope = plumbing_trial(tmp_path, plumbing_executor, program)
    assert len(plumbing_executor.calls) == 4  # Exactly two runs of each synthetic test.
    validation = metrics.validate_metric_program(store, program)
    assert validation["fixture_only"] is True
    assert [entry["runs"] for entry in validation["tests"]] == [2, 2]
    assert all(entry["synthetic_fixture"] for entry in validation["tests"])
    assert validation["code_sha256"] == digest(program.code)
    assert validation["input_schema_sha256"] == digest(program.input_schema)
    assert validation["output_schema_sha256"] == digest(program.output_schema)
    assert team.llm.calls[0][1]["future_validation_outcomes_present"] is False
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 6
    assert plumbing_executor.calls[-1]["inputs"] == {
        "subjects": envelope["arms"], "normalizer": 2}
    assert result["measurement_authority"] is False
    assert result["deterministic_runs"] == 2
    rows = result["metric_table"]["rows"]
    assert [(row["subject_ref"], row["name"], row["value"]) for row in rows] == [
        ("baseline", name, 6), ("challenger", name, 10.5)]
    assert all(row["unit"] == "fixture_units/site" and row["evidence"] == "computed"
               and row["method"] == "evaluation_program:" + digest(program) for row in rows)
    assert result["denominators"] == envelope["denominators"]
    restarted = Store(store.root)
    assert metrics.execute_evaluation_metrics(restarted, plan, deepcopy(envelope)) == result
    assert metrics.load_evaluation_metric_result(restarted, result["result_ref"]) == result
    assert len(plumbing_executor.calls) == 6
    assert len(store.all(metrics.RESULT_NS)) == len(store.all(metrics.INPUT_NS)) == 1
    assert not store.all("evaluation_verdicts")  # A computation cannot manufacture a verdict.


def test_plumbing_custom_result_is_available_to_llm_verdict(tmp_path, plumbing_executor):
    store, team, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    verdict = adjudicate_evaluation(store, team, evaluation_id=plan["evaluation_id"],
        plan_ref=plan["plan_ref"], evidence={result["result_ref"]: result},
        n_baseline=2, n_challenger=2)
    assert verdict.decision == "inconclusive"
    assert result["result_ref"] in team.llm.calls[-1][1]["available_evidence_refs"]
    saved = next(iter(store.all("evaluation_verdicts").values()))
    assert saved["verdict"]["supporting_evidence_refs"] == [result["result_ref"]]


def test_verdict_cannot_ignore_the_persisted_custom_metric_result(tmp_path, plumbing_executor):
    store, team, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    other_ref = "workflow_trials/synthetic-trial-1/descriptive-facts"
    calls = []

    def omit_metric(role, instructions, context, schema):
        assert role == "E-verdict"
        calls.append(deepcopy(context))
        return {"plan_ref": plan["plan_ref"], "decision": "accepted",
                "reason": "Protocol-only malformed citation fixture",
                "supporting_evidence_refs": [other_ref]}

    team.llm.complete = omit_metric
    evidence = {result["result_ref"]: result, other_ref: {"synthetic": True}}
    for _ in range(2):
        with pytest.raises(ProviderPaused, match="no decision was made"):
            adjudicate_evaluation(Store(store.root), team, evaluation_id=plan["evaluation_id"],
                plan_ref=plan["plan_ref"], evidence=evidence, n_baseline=2, n_challenger=2)
    assert len(calls) == MAX_RESPONSE_REPAIRS + 1
    assert not store.all("evaluation_verdicts")
    assert len(plumbing_executor.calls) == 6


@pytest.mark.parametrize("fault", ["missing", "altered", "extra"])
def test_verdict_rejects_missing_or_replaced_custom_evidence(tmp_path, plumbing_executor, fault):
    store, team, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    evidence = {result["result_ref"]: deepcopy(result)}
    if fault == "missing":
        evidence = {"workflow_trials/synthetic-trial-1/facts": {"synthetic": True}}
    elif fault == "altered":
        evidence[result["result_ref"]]["output"] = {"replacement": True}
    else:
        evidence[metrics.RESULT_NS + "/" + "0" * 64] = deepcopy(result)
    with pytest.raises(Conflict):
        adjudicate_evaluation(store, team, evaluation_id=plan["evaluation_id"],
            plan_ref=plan["plan_ref"], evidence=evidence, n_baseline=2, n_challenger=2)
    assert len(team.llm.calls) == 1 and not store.all("evaluation_verdicts")


@pytest.mark.parametrize("extra", [{"network": True}, {"permissions": ["filesystem"]},
                                   {"artifact_refs": ["artifact:invented.json"]},
                                   {"credential_name": "OPENAI_API_KEY"}])
def test_metric_contract_cannot_request_capabilities(extra):
    raw = program_fixture().model_dump(mode="json")
    with pytest.raises(ValidationError):
        EvaluationMetricProgram.model_validate({**raw, **extra})


@pytest.mark.parametrize("binding", ["/labels", "/hidden_labels/secret", "/context", "/files",
                                     "", "/arms/~2invalid", "file:///etc/passwd"])
def test_metric_input_bindings_cannot_address_private_or_nonjson_capabilities(binding):
    raw = program_fixture().model_dump(mode="json")
    raw["input_bindings"] = {"subjects": binding}
    with pytest.raises(ValidationError):
        EvaluationMetricProgram.model_validate(raw)


@pytest.mark.parametrize("schema", [
    {"type": "array"}, {"type": "not-a-type"},
    {"type": "object", "$ref": "https://fixture.invalid/private-schema"},
    {"type": "object", "patternProperties": {".*": {}}},
    {"type": "object", "$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
])
def test_invalid_schemas_fail_before_generated_execution(tmp_path, plumbing_executor, schema):
    program = program_fixture().model_copy(update={"input_schema": schema})
    with pytest.raises(ContractError):
        metrics.validate_metric_program(Store(tmp_path), program)
    assert not plumbing_executor.calls


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown_metric", "unknown_subject",
                                     "boolean", "string", "negative_uncertainty", "nonfinite",
                                     "infinite_uncertainty", "wrong_shape"])
def test_output_normalization_rejects_invalid_rows(change):
    output = output_fixture()
    rows = output["diagnostics"]["observations"]
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif change == "unknown_metric":
        rows[0]["metric"]["key"] = "controller_invented_metric"
    elif change == "unknown_subject":
        rows[0]["who"]["id"] = "a-different-trial"
    elif change == "wrong_shape":
        output = {"rows": rows}
    else:
        key, value = {"boolean": ("value", True), "string": ("value", "2"),
                      "negative_uncertainty": ("sigma", -1), "nonfinite": ("value", float("nan")),
                      "infinite_uncertainty": ("sigma", float("inf"))}[change]
        rows[0]["estimate"][key] = value
    with pytest.raises((ValueError, ContractError, ValidationError)):
        metrics.normalize_metric_table(program_fixture(), output)


def test_explicit_missing_value_is_not_zero():
    table = metrics.normalize_metric_table(program_fixture(), output_fixture(None, -1))
    assert table["rows"][0]["subject_ref"] == "baseline"
    assert table["rows"][0]["value"] is None
    assert table["rows"][1]["value"] == -1


def test_program_output_cannot_claim_measurement_authority_or_change_units():
    program = program_fixture()
    output = output_fixture()
    for row in output["diagnostics"]["observations"]:
        row.update(evidence="measured", method="wetlab", unit="invented-unit",
                   measurement_authority=True)
    table = metrics.normalize_metric_table(program, output)
    for row in table["rows"]:
        assert row["evidence"] == "computed" and row["unit"] == program.definitions[0].unit
        assert row["method"] == "evaluation_program:" + digest(program)
        assert "measurement_authority" not in row


def test_two_distinct_preoutcome_fixtures_are_required():
    raw = program_fixture().model_dump(mode="json")
    raw["tests"] = raw["tests"][:1]
    with pytest.raises(ValidationError):
        EvaluationMetricProgram.model_validate(raw)
    raw["tests"] *= 2
    with pytest.raises(ValidationError, match="fixture names"):
        EvaluationMetricProgram.model_validate(raw)


@pytest.mark.parametrize("fault", ["mismatch", "nondeterministic", "wrong_backend", "wrong_code_hash"])
def test_preoutcome_fixture_validation_rejects_bad_execution(tmp_path, plumbing_executor, fault):
    program = program_fixture()
    plumbing_executor.register(program)
    count = 0

    def faulty(arguments):
        nonlocal count
        count += 1
        output = output_fixture(99 if fault == "mismatch" or (
            fault == "nondeterministic" and count == 2) else 2)
        return {"status": "ok", "output": output,
                "execution_backend": "inprocess" if fault == "wrong_backend" else sandbox.generated_profile(),
                "code_sha256": "0" * 64 if fault == "wrong_code_hash" else digest(program.code)}

    plumbing_executor.replies[digest(program.tests[0].inputs)] = faulty
    store = Store(tmp_path)
    with pytest.raises(ContractError):
        metrics.validate_metric_program(store, program)
    assert not store.all(metrics.VALIDATION_NS)
    calls = len(plumbing_executor.calls)
    with pytest.raises(ContractError):
        metrics.validate_metric_program(Store(store.root), program)
    assert len(plumbing_executor.calls) == calls  # Completed failures cannot be resampled.


@pytest.mark.parametrize("field", ["code_sha256", "input_schema_sha256", "output_schema_sha256",
                                   "validation_sha256", "tests"])
def test_frozen_validation_receipt_tamper_is_rejected(tmp_path, plumbing_executor, field):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    key = plan["metric_validation_ref"].split("/", 1)[1]
    record = store.get(metrics.VALIDATION_NS, key)
    record[field] = [] if field == "tests" else "0" * 64
    store.put(metrics.VALIDATION_NS, key, record)
    with pytest.raises(Conflict):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 4


@pytest.mark.parametrize("field", ["code", "input_schema", "output_schema", "input_bindings"])
def test_frozen_program_or_schema_tamper_is_rejected(tmp_path, plumbing_executor, field):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    changed = deepcopy(plan)
    raw = changed["plan"]["metric_program"]
    raw[field] = raw[field] + "\n# post-outcome edit" if field == "code" else {}
    store.put("evaluation_plans", plan["plan_ref"].split("/", 1)[1], changed)
    with pytest.raises(Conflict):
        metrics.execute_evaluation_metrics(store, changed, envelope)
    assert len(plumbing_executor.calls) == 4


def test_rehashed_plan_cannot_replace_its_validated_source(tmp_path, plumbing_executor):
    store, _, plan, _ = plumbing_trial(tmp_path, plumbing_executor)
    changed = deepcopy(plan)
    raw = changed["plan"]["metric_program"]
    raw["code"] += "\n# unvalidated revision"
    changed["metric_program_sha256"] = digest(EvaluationMetricProgram.model_validate(raw))
    body = {key: value for key, value in changed.items() if key != "plan_ref"}
    changed["plan_ref"] = "evaluation_plans/" + digest(body)
    store.put("evaluation_plans", changed["plan_ref"].split("/", 1)[1], changed)
    with pytest.raises(Conflict):
        metrics.execute_evaluation_metrics(store, changed, envelope_fixture(changed))
    assert len(plumbing_executor.calls) == 4


@pytest.mark.parametrize("field", ["output", "metric_table", "program_sha256", "input_ref",
                                   "output_sha256", "metric_table_sha256", "denominators"])
def test_result_hash_detects_unaddressed_tamper(tmp_path, plumbing_executor, field):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    result[field] = {"tampered": True}
    store.put(metrics.RESULT_NS, result["result_ref"].split("/", 1)[1], result)
    with pytest.raises(Conflict):
        metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    assert len(plumbing_executor.calls) == 6


@pytest.mark.parametrize("field", ["code_sha256", "runtime", "definitions", "denominators"])
def test_result_provenance_rejects_rehashed_forgery(tmp_path, plumbing_executor, field):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    result[field] = [] if field == "definitions" else {"forged": True}
    forged_ref = rewrite_addressed(store, metrics.RESULT_NS, result)
    with pytest.raises(Conflict):
        metrics.load_evaluation_metric_result(store, forged_ref)


@pytest.mark.parametrize("field", ["inputs", "envelope", "input_schema_sha256"])
def test_input_provenance_rejects_rehashed_forgery(tmp_path, plumbing_executor, field):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    inputs = store.get(metrics.INPUT_NS, result["input_ref"].split("/", 1)[1])
    if field == "inputs":
        inputs["inputs"]["normalizer"] = 99
    elif field == "envelope":
        inputs["envelope"]["evaluation_id"] = "other-trial"
    else:
        inputs[field] = "0" * 64
    result["input_ref"] = rewrite_addressed(store, metrics.INPUT_NS, inputs)
    forged_ref = rewrite_addressed(store, metrics.RESULT_NS, result)
    with pytest.raises(Conflict):
        metrics.load_evaluation_metric_result(store, forged_ref)


@pytest.mark.parametrize("fault", ["consistent_output", "consistent_inputs"])
def test_rehashed_artifacts_cannot_replace_the_completed_attempt(tmp_path, plumbing_executor, fault):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    if fault == "consistent_output":
        result["output"] = output_fixture(6000, 10000)
        result["output_sha256"] = digest(result["output"])
        result["metric_table"] = metrics.normalize_metric_table(program_fixture(), result["output"])
        result["metric_table_sha256"] = digest(result["metric_table"])
    else:
        inputs = store.get(metrics.INPUT_NS, result["input_ref"].split("/", 1)[1])
        inputs["inputs"]["normalizer"] = inputs["envelope"]["task"]["normalizer"] = 99
        result["input_ref"] = rewrite_addressed(store, metrics.INPUT_NS, inputs)
    forged_ref = rewrite_addressed(store, metrics.RESULT_NS, result)
    with pytest.raises(Conflict):
        metrics.load_evaluation_metric_result(store, forged_ref)


def test_input_snapshot_cannot_change_on_resume(tmp_path, plumbing_executor):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    metrics.execute_evaluation_metrics(store, plan, envelope)
    envelope["arms"]["challenger"]["values"] = [999]
    with pytest.raises(Conflict, match="frozen metric inputs"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 6


@pytest.mark.parametrize("field,value", [("protocol", "wrong/v1"), ("evaluation_id", "other"),
    ("target", "meta"), ("top_ns", [1]), ("subject_refs", ["baseline"]), ("unexpected", True)])
def test_input_scope_changes_fail_before_execution(tmp_path, plumbing_executor, field, value):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    envelope[field] = value
    with pytest.raises(Conflict, match="frozen evaluation scope"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 4
    assert not store.all(metrics.INPUT_NS)


@pytest.mark.parametrize("value", [0, "not-a-number", float("nan"), float("inf")])
def test_invalid_bound_inputs_never_reach_the_executor(tmp_path, plumbing_executor, value):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    envelope["task"]["normalizer"] = value
    with pytest.raises((metrics.MetricExecutionPaused, ValueError)):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 4
    assert not store.all(metrics.RESULT_NS)


@pytest.mark.parametrize("fault", ["worker", "schema", "nonfinite", "nondeterministic"])
def test_execution_failure_pauses_once_without_fallback_or_repair(tmp_path, plumbing_executor, fault):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    key = digest({"subjects": envelope["arms"], "normalizer": 2})
    count = 0

    def failure(arguments):
        nonlocal count
        count += 1
        if fault == "worker":
            return {"status": "failed", "error_type": "Timeout"}
        output = ({"wrong": "shape"} if fault == "schema" else
                  output_fixture(float("nan") if fault == "nonfinite" else count, 10.5))
        return {"status": "ok", "output": output, "execution_backend": sandbox.generated_profile(),
                "code_sha256": digest(arguments["code"])}

    plumbing_executor.replies[key] = failure
    with pytest.raises(metrics.MetricExecutionPaused, match="no verdict or replacement"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    calls = len(plumbing_executor.calls)
    with pytest.raises(metrics.MetricExecutionPaused, match="paused after failure"):
        metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    assert len(plumbing_executor.calls) == calls
    assert not store.all(metrics.RESULT_NS) and not store.all("evaluation_verdicts")
    assert next(iter(outcome_attempts(store).values()))["state"] == "failed"
    assert len(store.all(metrics.INPUT_NS)) == 1


def test_unsupported_isolation_fails_closed_before_executor(tmp_path, plumbing_executor, monkeypatch):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    monkeypatch.setattr(sandbox, "probe", lambda: {"available": False, "reason": "fixture unsupported"})
    with pytest.raises(metrics.MetricExecutionPaused, match="no unsafe fallback"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 4
    assert not store.all(metrics.RESULT_NS)
    attempt = next(iter(outcome_attempts(store).values()))
    assert attempt["state"] == "pending" and attempt["runs"] == 0


def test_production_executor_never_launches_on_unsupported_host(tmp_path, monkeypatch):
    from proteinrsi.research import code

    def forbidden_launch(*args, **kwargs):
        pytest.fail("Unsupported isolation must never launch generated code")

    monkeypatch.setattr(sandbox, "probe", lambda: {"available": False})
    monkeypatch.setattr(code.subprocess, "Popen", forbidden_launch)
    store = Store(tmp_path)
    with pytest.raises(sandbox.SandboxUnavailable):
        code.execute_code(store, None, {"code": "result = {}", "inputs": {}}, pure=True)
    assert not store.all("code_programs")


@pytest.mark.parametrize("interruptions", [1, metrics.MAX_CRASH_ATTEMPTS])
def test_interrupted_execution_retries_only_identical_inputs_with_a_bound(
        tmp_path, plumbing_executor, interruptions):
    class SyntheticWorkerInterruption(BaseException):
        pass

    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    input_key = digest({"subjects": envelope["arms"], "normalizer": 2})

    def interrupt(arguments):
        raise SyntheticWorkerInterruption()

    plumbing_executor.replies[input_key] = interrupt
    for _ in range(interruptions):
        with pytest.raises(SyntheticWorkerInterruption):
            metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
    calls = len(plumbing_executor.calls)
    changed = deepcopy(envelope)
    changed["task"]["normalizer"] = 3
    with pytest.raises(Conflict, match="frozen metric inputs"):
        metrics.execute_evaluation_metrics(Store(store.root), plan, changed)
    plumbing_executor.replies[input_key] = output_fixture(6, 10.5)
    if interruptions == metrics.MAX_CRASH_ATTEMPTS:
        with pytest.raises(metrics.MetricExecutionPaused):
            metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
        assert not store.all(metrics.RESULT_NS) and len(plumbing_executor.calls) == calls
    else:
        result = metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
        assert result["output"] == output_fixture(6, 10.5)
        assert len(plumbing_executor.calls) == calls + 2


def test_changed_runtime_pauses_but_completed_result_replays(tmp_path, plumbing_executor, monkeypatch):
    store, _, plan, envelope = plumbing_trial(tmp_path, plumbing_executor)
    original = metrics.runtime_identity()
    monkeypatch.setattr(metrics, "runtime_identity", lambda: {**original, "python": "changed"})
    with pytest.raises(metrics.MetricExecutionPaused, match="runtime changed"):
        metrics.execute_evaluation_metrics(store, plan, envelope)
    assert len(plumbing_executor.calls) == 4
    monkeypatch.setattr(metrics, "runtime_identity", lambda: original)
    result = metrics.execute_evaluation_metrics(store, plan, envelope)
    monkeypatch.setattr(metrics, "runtime_identity", lambda: {**original, "python": "changed"})
    assert metrics.execute_evaluation_metrics(store, plan, envelope) == result
    assert len(plumbing_executor.calls) == 6


@pytest.mark.skipif(not SANDBOX_STATE["available"] and not REQUIRE_SANDBOX,
                    reason="Real custom-metric acceptance needs the selected Linux filesystem backend and seccomp")
class TestSupportedMetricSandbox:
    """No execute_code/probe mocks: these are real production-worker acceptance tests."""

    @pytest.fixture(autouse=True)
    def require_real_sandbox(self):
        assert sandbox.probe()["available"], (
            "PROTEINRSI_REQUIRE_METRIC_SANDBOX=1 requires the real selected filesystem backend and seccomp; "
            "the supported-sandbox acceptance job must not silently skip: " + str(sandbox.probe()))

    def test_supported_sandbox_program_fixtures_execution_and_replay(self, tmp_path):
        store, team, plan = freeze_fixture(tmp_path)
        envelope = envelope_fixture(plan)
        result = metrics.execute_evaluation_metrics(store, plan, envelope)
        assert result["output"] == output_fixture(6, 10.5)
        assert result["runtime"]["profile"] == sandbox.generated_profile()
        assert result["deterministic_runs"] == 2
        assert [row["value"] for row in result["metric_table"]["rows"]] == [6, 10.5]
        generated_events = [e for e in store.events() if e["kind"] == "generated_code_completed"]
        assert len(generated_events) == 6
        assert metrics.execute_evaluation_metrics(Store(store.root), plan, envelope) == result
        assert len([e for e in store.events() if e["kind"] == "generated_code_completed"]) == 6
        assert len(team.llm.calls) == 1

    def test_supported_sandbox_denies_network_reads_writes_processes_and_credentials(
            self, tmp_path, monkeypatch):
        hidden = tmp_path / "private-label-fixture.csv"
        hidden.write_text("synthetic-canary-only")
        alias = tmp_path / "indirect-private-fixture.csv"
        alias.symlink_to(hidden)
        target = tmp_path / "forbidden-output"
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-credential-canary")
        monkeypatch.setenv("PROTEINRSI_API_KEY", "synthetic-credential-canary")
        store_path = tmp_path / "synthetic-campaign" / "state.sqlite3"
        prefix = f"""
import os, socket, resource, ctypes
assert ctypes.CDLL(None).prctl(1, 0, 0, 0, 0) == -1  # Cannot disable parent-death binding.
assert os.environ.get('OPENAI_API_KEY') is None
assert os.environ.get('PROTEINRSI_API_KEY') is None
def denied(operation):
    try:
        operation()
    except OSError as exc:
        assert exc.errno in (1, 2, 13, 30)
        return
    raise AssertionError('Forbidden operation was not denied')
for forbidden in [{str(hidden)!r}, {str(alias)!r}, {str(store_path)!r}, '/etc/passwd', '/proc/self/environ']:
    denied(lambda: open(forbidden).read())
denied(lambda: socket.socket())
denied(lambda: socket.socketpair())
denied(lambda: open({str(target)!r}, 'w'))
denied(lambda: open(os.path.join(os.getcwd(), 'output.txt'), 'w'))
denied(lambda: os.open({str(hidden)!r}, os.O_RDONLY | os.O_TRUNC))
denied(lambda: os.fork())
try:
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
except (PermissionError, ValueError):
    pass  # CPython maps setrlimit EPERM to ValueError on some supported versions.
else:
    raise AssertionError('Changing resource limits was not denied')
"""
        store, _, plan = freeze_fixture(tmp_path, program_fixture(code_prefix=prefix))
        result = metrics.execute_evaluation_metrics(store, plan, envelope_fixture(plan))
        assert result["output"] == output_fixture(6, 10.5)
        assert hidden.read_text() == "synthetic-canary-only"
        assert not target.exists()
        assert not store.all("artifacts")

    @pytest.mark.parametrize("attack", [
        "result = {'artifacts': []}",
        "result = {'not_json_finite': float('nan')}",
        "print('X' * 17000)\nresult = {}",
        "import os, json\nos.write(1, json.dumps({'status':'ok','output':{'padding':'X'*600000}}).encode())\nos._exit(0)",
    ])
    def test_supported_sandbox_rejects_impure_or_invalid_outputs(self, tmp_path, attack):
        program = program_fixture().model_copy(update={"code": attack})
        store = Store(tmp_path)
        with pytest.raises((ValueError, ContractError)):
            metrics.validate_metric_program(store, program)
        assert not store.all(metrics.VALIDATION_NS) and not store.all("artifacts")

    @pytest.mark.parametrize("attack", ["while True:\n        pass", "import time\n    time.sleep(30)",
                                        "bytearray(3 * 1024 ** 3)"])
    def test_supported_sandbox_resource_limits_pause_without_fallback(self, tmp_path, attack):
        prefix = "if inputs['normalizer'] == 99:\n    " + attack + "\n"
        store, _, plan = freeze_fixture(tmp_path, program_fixture(code_prefix=prefix))
        envelope = envelope_fixture(plan)
        envelope["task"]["normalizer"] = 99
        with pytest.raises(metrics.MetricExecutionPaused):
            metrics.execute_evaluation_metrics(store, plan, envelope)
        assert not store.all(metrics.RESULT_NS) and not store.all("evaluation_verdicts")
        events = len(store.events())
        with pytest.raises(metrics.MetricExecutionPaused, match="paused after failure"):
            metrics.execute_evaluation_metrics(Store(store.root), plan, envelope)
        assert len(store.events()) == events

    def test_supported_sandbox_stored_source_tamper_pauses(self, tmp_path):
        program = program_fixture()
        store, _, plan = freeze_fixture(tmp_path, program)
        source = store.root / "programs" / (digest(program.code) + ".py")
        source.write_text("result = {'forged': True}")
        with pytest.raises(metrics.MetricExecutionPaused):
            metrics.execute_evaluation_metrics(store, plan, envelope_fixture(plan))
        assert not store.all(metrics.RESULT_NS)

    def test_supported_sandbox_private_envelope_never_reaches_generated_code(self, tmp_path):
        store, _, plan = freeze_fixture(tmp_path)
        envelope = envelope_fixture(plan)
        envelope["task"]["hidden_labels"] = {"synthetic-secret": 999}
        events = len(store.events())
        with pytest.raises(ValueError, match="label sources"):
            metrics.execute_evaluation_metrics(store, plan, envelope)
        assert len(store.events()) == events
        assert not store.all(metrics.INPUT_NS)


    @pytest.mark.parametrize("mode", ["workflow", "meta", "offline_meta"])
    def test_supported_sandbox_campaign_verdict_and_report_share_artifact(
            self, campaign, monkeypatch, mode):
        from proteinrsi.evaluation import evaluate_meta
        from proteinrsi.reporting_metrics import evaluation_report_configuration
        from test_custom_metric_integration import custom_plan
        from test_llm_evaluation_integration import make_campaign, measurements
        from test_rsi import ScriptedOffspringTeam, make_meta_cases
        target = "meta" if mode in {"meta", "offline_meta"} else "workflow"
        campaign, transport, _, _, _ = make_campaign(campaign, monkeypatch,
                                                      target=target, plan=custom_plan())
        if mode == "offline_meta":
            report = evaluate_meta(campaign, make_meta_cases(campaign), promote=True,
                                   team_factory=ScriptedOffspringTeam)
            result = report["metric_facts"]
        else:
            if mode == "meta":
                monkeypatch.setattr("proteinrsi.online_meta.make_validation_team",
                                    lambda campaign, store: ScriptedOffspringTeam(store))
            batch = campaign.prepare()
            campaign.approve(batch.batch_id, operator="synthetic-sandbox-acceptance")
            campaign.ingest(measurements(campaign, batch, [0, 0, 10], [5, 5, 5]))
            result = next(iter(campaign.store.all(metrics.RESULT_NS).values()))
        assert transport.contexts["verdict"][-1]["evidence"][result["result_ref"]] == result
        output = evaluation_report_configuration(campaign.store, campaign.state)
        assert output["llm_evaluation_metric_results"][result["result_ref"]] == result
        assert result["metric_table_sha256"] == digest(result["metric_table"])
        assert result["runtime"]["profile"] == sandbox.generated_profile()
        assert len([event for event in campaign.store.events()
                    if event["kind"] == "generated_code_completed"]) == 6
        assert campaign.store.usage()["llm_calls"]["committed"] == 2


    def test_supported_sandbox_worker_dies_with_controller(self, tmp_path):
        import json
        import os
        from pathlib import Path
        import signal
        import subprocess
        import sys
        import time
        source = r"""
import json, os, sys
from proteinrsi.research import code
from proteinrsi.storage import Store
original = code.subprocess.Popen
def observed(*args, **kwargs):
    child = original(*args, **kwargs)
    if args and 'proteinrsi.research.code_worker' in args[0]:
        print(json.dumps({'pid': child.pid, 'work': kwargs['cwd']}), flush=True)
    return child
code.subprocess.Popen = observed
code.execute_code(Store(sys.argv[1]), None, {'code': "import os, time\nos.write(1, b'ACTIVE\\n')\ntime.sleep(120)\nresult = {}", 'inputs': {}}, pure=True)
"""
        controller = subprocess.Popen([sys.executable, '-c', source, str(tmp_path / 'controller')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        worker_pid = None
        try:
            import select
            assert select.select([controller.stdout], [], [], 10)[0]
            line = controller.stdout.readline()
            assert line, "Controller exited before worker start: " + controller.stderr.read()
            observed = json.loads(line)
            worker_pid = observed['pid']
            output = Path(observed['work']) / 'stdout'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if output.exists() and b'ACTIVE' in output.read_bytes():
                    break
                time.sleep(0.05)
            else:
                pytest.fail('Real worker never reached isolated generated code')
            controller.kill()
            controller.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                stat = Path('/proc') / str(worker_pid) / 'stat'
                if not stat.exists() or stat.read_text().split()[2] == 'Z':
                    break
                time.sleep(0.05)
            else:
                pytest.fail('Generated worker survived controller death')
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=5)
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


    def test_supported_sandbox_direct_stdout_cannot_bypass_response_limit(self, tmp_path):
        from proteinrsi.research.code import execute_code
        code = "import os,json\nos.write(1,json.dumps({'status':'ok','output':{'padding':'X'*600000}}).encode())\nos._exit(0)"
        result = execute_code(Store(tmp_path), None, {'code': code, 'inputs': {}}, pure=True)
        assert result['status'] == 'failed'
        assert result['error_type'] == 'InvalidWorkerOutput'
