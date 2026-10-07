# SPDX-License-Identifier: MIT
"""LLM-authored evaluation plans and LLM verdicts with durable provenance.

This controller validates identity, JSON shape, budget-compatible requests and
evidence references. It deliberately contains no scientific acceptance function.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError

from proteinrsi.contracts import EvaluationPlan, EvaluationVerdict, GateResult, digest
from proteinrsi.llm import ProviderPaused
from proteinrsi.prompting import compose, snapshot_prompts
from proteinrsi.storage import Conflict

CRITERION = "llm_adjudicated_v1"
MAX_RESPONSE_REPAIRS = 2  # Transport/schema repair budget, never a scientific gate.
# Keep the exact RPC exception name recognized by the existing guarded workers.
EvaluationPaused = ProviderPaused
EVALUATION_NAMESPACES = frozenset({"evaluation_requests", "evaluation_plans",
    "evaluation_bindings", "evaluation_evidence", "evaluation_verdicts", "evaluation_trial_inputs"})


@contextmanager
def _evaluation_lock(store):
    """Serialize evaluation calls without holding SQLite transactions over I/O."""
    import fcntl
    with (store.root / ".evaluation.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _check_public(value: Any) -> None:
    """Fail closed on evaluator-only fields; callers must supply public TaskViews.

    This is defense in depth, not an authorization to pass arbitrary case files.
    Dataset paths and unrevealed labels never enter either evaluation request.
    """
    private = {"labels", "hidden_labels", "private_labels", "raw_labels", "label_path",
               "labels_path", "labels_file", "dataset_path", "raw_label_path",
               "heldout_scores", "unrevealed_scores", "future_scores"}
    if isinstance(value, dict):
        if private & {str(key).lower() for key in value}:
            raise ValueError("Evaluation requests cannot contain label sources or unrevealed scores")
        for child in value.values():
            _check_public(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_public(child)


def _backend(team) -> dict:
    llm = getattr(team, "llm", None)
    if llm is None or not callable(getattr(llm, "complete", None)):
        raise EvaluationPaused("llm_adjudicated_v1 requires an actual configured LLM; no fallback verdict")
    return {"model": getattr(llm, "model", None), "base_url": getattr(llm, "base_url", None),
            "cache_settings": getattr(llm, "cache_settings", {})}


def _request(store, team, role: str, prompt: str, context: dict, schema: dict) -> tuple[str, dict]:
    _check_public(context)
    request = {"role": role, "instructions": compose(store, prompt), "context": context,
               "schema": schema, "backend": _backend(team)}
    key = digest(request)
    return "evaluation_requests/" + key, request


def _complete(store, team, request_ref: str, request: dict) -> Any:
    key = request_ref.split("/", 1)[1]
    store.put("evaluation_requests", key, request, immutable=True)
    response_key = key + "/response"
    cached = store.get("evaluation_requests", response_key)
    if cached is not None:
        return cached["response"]
    try:
        raw = team.llm.complete(request["role"], request["instructions"],
                                request["context"], request["schema"])
    except Exception as exc:
        store.event("evaluation_request_paused", {"request_ref": request_ref,
            "evaluation_request_ref": key,
            "role": request["role"], "error_type": type(exc).__name__})
        if isinstance(exc, ProviderPaused):
            raise
        raise EvaluationPaused("Evaluation LLM call paused; resume the same request after "
                               f"provider reconciliation ({type(exc).__name__})") from exc
    # Store the returned value before parsing. A malformed response is evidence of
    # a paused evaluation, never permission to draw another convenient verdict.
    try:
        store.put("evaluation_requests", response_key, {"response": raw}, immutable=True)
    except (TypeError, ValueError):
        store.put("evaluation_requests", response_key,
                  {"response": None, "invalid_response_type": type(raw).__name__}, immutable=True)
        return None
    return raw


def _pause_invalid(store, phase: str, request_ref: str, exc: Exception):
    store.event("evaluation_response_invalid", {"phase": phase, "request_ref": request_ref,
                                              "evaluation_request_ref": request_ref.split("/", 1)[1],
                                              "error_type": type(exc).__name__})
    raise EvaluationPaused(f"Malformed {phase} response; no decision was made. "
                           f"Reconcile the frozen request {request_ref}") from exc


def _validated_response(store, team, request_ref: str, request: dict, model, check=None):
    """Repair format/provenance errors while freezing all scientific inputs.

    Completed invalid responses and every repair request remain immutable. Resume
    replays their cached validation and reaches the same pending repair request.
    A valid verdict of any kind is never resampled or passed to this repair loop.
    """
    current_ref, current_request = request_ref, request
    for attempt in range(MAX_RESPONSE_REPAIRS + 1):
        raw = _complete(store, team, current_ref, current_request)
        try:
            response = model.model_validate(raw)
            if check is not None:
                check(response)
            return response, current_ref
        except (ValidationError, ValueError, TypeError) as exc:
            if attempt == MAX_RESPONSE_REPAIRS:
                _pause_invalid(store, request["role"], current_ref, exc)
            # Pydantic's formatted message embeds repr(input), whose dictionary
            # ordering can change on durable JSON reload. Stable structured
            # errors preserve the exact repair identity across process restarts.
            error = (exc.errors(include_url=False, include_context=False, include_input=False)
                     if isinstance(exc, ValidationError) else
                     [{"type": type(exc).__name__, "message": str(exc)}])
            repair = {"attempt": attempt + 1, "original_request_ref": request_ref,
                      "previous_response": raw, "validation_errors": error,
                      "instruction": "Repair only the schema/provenance error. Use the unchanged "
                      "scientific context, frozen plan and evidence. Do not invent references."}
            current_request = {**request, "context": {**request["context"], "response_repair": repair}}
            current_ref = "evaluation_requests/" + digest(current_request)
            store.event("evaluation_response_repair", {"role": request["role"],
                        "attempt": attempt + 1, "original_request_ref": request_ref,
                        "request_ref": current_ref,
                        "evaluation_request_ref": current_ref.split("/", 1)[1]})


def load_evaluation_plan(store, plan_ref: str) -> dict:
    """Load a content-addressed plan, checking its identity and schema."""
    if not isinstance(plan_ref, str) or not plan_ref.startswith("evaluation_plans/"):
        raise Conflict("Invalid evaluation plan reference")
    key = plan_ref.split("/", 1)[1]
    record = store.get("evaluation_plans", key)
    if not isinstance(record, dict) or record.get("plan_ref") != plan_ref:
        raise Conflict("Missing evaluation plan")
    body = {name: value for name, value in record.items() if name != "plan_ref"}
    if digest(body) != key:
        raise Conflict("Evaluation plan content does not match its frozen identity")
    EvaluationPlan.model_validate(record["plan"])
    return record


def ensure_evaluation_plan(store, team, *, evaluation_id: str, target: str,
                           context: dict, max_top_n: int) -> dict:
    """Ask E-plan before future outcomes, or recover the exact frozen plan.

    ``max_top_n`` discloses the number of potentially measurable unique units
    permitted by allocated resources. It is not a scientific limit on the LLM's
    chosen N: an incomplete top-N is represented as null in the factual report.
    The caller must not put the future validation outcomes in ``context``.
    """
    if not isinstance(evaluation_id, str) or not evaluation_id.strip() or target not in {"workflow", "meta"}:
        raise ValueError("Evaluation requires an explicit identity and workflow/meta target")
    if type(max_top_n) is not int or max_top_n < 1:
        raise ValueError("An evaluation needs capacity for at least one submitted unit per arm")
    snapshot_prompts(store)
    request_context = {"evaluation_id": evaluation_id, "target": target,
        "scientific_context": context, "available_unique_arm_capacity": max_top_n,
        "available_summaries": ["maximum", "best_in_task_direction", "LLM_chosen_top_N_means", "average"],
        "future_validation_outcomes_present": False}
    request_ref, request = _request(store, team, "E-plan", "evaluation_plan",
                                   request_context, EvaluationPlan.model_json_schema())
    with _evaluation_lock(store):
        binding = store.get("evaluation_bindings", evaluation_id)
        expected = {"evaluation_id": evaluation_id, "target": target,
                    "planning_request_ref": request_ref}
        if binding is not None:
            if any(binding.get(name) != value for name, value in expected.items()):
                raise Conflict("Evaluation identity cannot acquire a different plan, context, or LLM backend")
            if binding.get("plan_ref"):
                return load_evaluation_plan(store, binding["plan_ref"])
        else:
            # Bind the first request before LLM I/O, including a provider pause.
            binding = {**expected, "plan_ref": None}
            store.put("evaluation_bindings", evaluation_id, binding)
        plan, completed_ref = _validated_response(store, team, request_ref, request, EvaluationPlan)
        body = {"criterion": CRITERION, "evaluation_id": evaluation_id, "target": target,
                "request_ref": completed_ref, "initial_request_ref": request_ref,
                "plan": plan.model_dump(mode="json")}
        key = digest(body)
        record = {**body, "plan_ref": "evaluation_plans/" + key}
        with store.transaction():
            store.put("evaluation_plans", key, record, immutable=True)
            store.put("evaluation_bindings", evaluation_id, {**binding, "plan_ref": record["plan_ref"]})
            store.event("evaluation_plan_frozen", {"evaluation_id": evaluation_id,
                        "target": target, "plan_ref": record["plan_ref"], "request_ref": completed_ref,
                        "evaluation_plan_ref": key,
                        "evaluation_request_ref": completed_ref.split("/", 1)[1]})
        return record


def adjudicate_evaluation(store, team, *, evaluation_id: str, plan_ref: str,
                          evidence: dict[str, dict], n_baseline: int,
                          n_challenger: int) -> GateResult:
    """Persist trusted facts, ask E-verdict, validate provenance, honor its decision.

    Missing/QC/denominator facts belong in ``evidence`` even when metrics are null.
    There is no numerical veto, score comparison, Pareto rule, or fallback here.
    All evidence keys must identify this evaluation; references outside that exact
    set cannot be cited. The trusted controller owns construction of that set.
    """
    plan = load_evaluation_plan(store, plan_ref)
    binding = store.get("evaluation_bindings", evaluation_id)
    if (plan["evaluation_id"] != evaluation_id or not binding
            or binding.get("plan_ref") != plan_ref):
        raise Conflict("Verdict must refer to this evaluation's pre-outcome plan")
    planning_request = store.get("evaluation_requests", plan["request_ref"].split("/", 1)[1])
    if not planning_request or planning_request.get("backend") != _backend(team):
        raise Conflict("Evaluation backend identity cannot change after the plan was frozen")
    if (not isinstance(evidence, dict) or not evidence
            or any(not isinstance(ref, str) or not ref.strip() or not isinstance(facts, dict)
                   for ref, facts in evidence.items())):
        raise ValueError("Evaluation requires reference-keyed trusted evidence reports")
    if any(type(n) is not int or n < 0 for n in (n_baseline, n_challenger)):
        raise ValueError("Evidence denominators must be nonnegative integer counts")
    _check_public(evidence)
    evidence_body = {"evaluation_id": evaluation_id, "plan_ref": plan_ref,
                     "evidence": evidence, "n_baseline": n_baseline, "n_challenger": n_challenger}
    evidence_key = digest(evidence_body)
    evidence_ref = "evaluation_evidence/" + evidence_key
    request_context = {"evaluation_id": evaluation_id, "evaluation_plan": plan,
                       "evidence_ref": evidence_ref, **evidence_body,
                       "available_evidence_refs": sorted(evidence)}
    request_ref, request = _request(store, team, "E-verdict", "evaluation_verdict",
                                   request_context, EvaluationVerdict.model_json_schema())
    with _evaluation_lock(store):
        binding = store.get("evaluation_bindings", evaluation_id)
        if binding.get("verdict_request_ref") not in (None, request_ref):
            raise Conflict("Cannot change a frozen verdict request or its current-trial evidence")
        if binding.get("verdict_ref"):
            saved = store.get("evaluation_verdicts", binding["verdict_ref"].split("/", 1)[1])
            if saved is None or saved.get("initial_request_ref", saved.get("request_ref")) != request_ref:
                raise Conflict("Missing or mismatched frozen evaluation verdict")
            saved_body = {key: value for key, value in saved.items() if key not in {"result", "verdict_ref"}}
            if "evaluation_verdicts/" + digest(saved_body) != binding["verdict_ref"]:
                raise Conflict("Evaluation verdict content does not match its frozen identity")
            result = GateResult.model_validate(saved["result"])
            verdict = EvaluationVerdict.model_validate(saved["verdict"])
            if (result.decision != verdict.decision or result.reason != verdict.reason
                    or verdict.plan_ref != plan_ref
                    or not set(verdict.supporting_evidence_refs) <= set(evidence)
                    or result.n_baseline != n_baseline or result.n_challenger != n_challenger
                    or not result.details or result.details.get("evidence") != evidence
                    or result.details.get("verdict") != verdict.model_dump(mode="json")):
                raise Conflict("Frozen GateResult disagrees with the LLM verdict and evidence")
            return result
        with store.transaction():
            store.put("evaluation_evidence", evidence_key,
                      {**evidence_body, "evidence_ref": evidence_ref}, immutable=True)
            store.put("evaluation_bindings", evaluation_id,
                      {**binding, "verdict_request_ref": request_ref, "evidence_ref": evidence_ref})
        def valid_references(verdict):
            if verdict.plan_ref != plan_ref:
                raise ValueError("Verdict cites a different evaluation plan")
            if not set(verdict.supporting_evidence_refs) <= set(evidence):
                raise ValueError("Verdict cites evidence outside this evaluation")
        verdict, completed_ref = _validated_response(store, team, request_ref, request,
                                                     EvaluationVerdict, valid_references)
        verdict_body = {"evaluation_id": evaluation_id, "plan_ref": plan_ref,
                        "evidence_ref": evidence_ref, "request_ref": completed_ref,
                        "initial_request_ref": request_ref,
                        "verdict": verdict.model_dump(mode="json")}
        verdict_key = digest(verdict_body)
        verdict_ref = "evaluation_verdicts/" + verdict_key
        result = GateResult(decision=verdict.decision, reason=verdict.reason,
            n_baseline=n_baseline, n_challenger=n_challenger,
            details={"criterion": CRITERION, "evaluation_plan_ref": plan_ref,
                     "evaluation_verdict_ref": verdict_ref, "evaluation_evidence_ref": evidence_ref,
                     "plan": plan["plan"], "verdict": verdict.model_dump(mode="json"),
                     "evidence": evidence, "outcome": verdict.tradeoff_label or verdict.decision})
        record = {**verdict_body, "verdict_ref": verdict_ref, "result": result.model_dump(mode="json")}
        # The complete plan, facts and validated result are durable before the
        # caller can adopt a workflow/meta successor in its own transaction.
        with store.transaction():
            store.put("evaluation_verdicts", verdict_key, record, immutable=True)
            store.put("evaluation_bindings", evaluation_id, {**binding,
                      "verdict_request_ref": request_ref, "evidence_ref": evidence_ref,
                      "verdict_ref": verdict_ref})
            store.event("evaluation_verdict_recorded", {"evaluation_id": evaluation_id,
                        "plan_ref": plan_ref, "verdict_ref": verdict_ref,
                        "decision": verdict.decision, "request_ref": completed_ref,
                        "evaluation_plan_ref": plan_ref.split("/", 1)[1],
                        "evaluation_verdict_ref": verdict_key,
                        "evaluation_evidence_ref": evidence_key,
                        "evaluation_request_ref": completed_ref.split("/", 1)[1]})
        return result
