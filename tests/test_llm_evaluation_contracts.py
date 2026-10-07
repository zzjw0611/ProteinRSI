"""Protocol tests: the LLM owns scientific criteria and the final decision."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from proteinrsi.contracts import GatePolicy, digest
from proteinrsi.llm import ProviderPaused
from proteinrsi.llm_evaluation import (MAX_RESPONSE_REPAIRS, adjudicate_evaluation,
                                     ensure_evaluation_plan, load_evaluation_plan)
from proteinrsi.storage import Conflict, Store


@pytest.fixture(autouse=True)
def historical_pending_evaluations(monkeypatch):
    """Keep historical plan/summary behavior as an explicit migration regression."""
    from legacy_evaluation_fixture import install_legacy_pending_requests
    install_legacy_pending_requests(monkeypatch)


PLAN = {"top_ns": [3, 7], "criteria": ["Judge biological usefulness under the task objective"],
        "rationale": "Separate observed panel performance from broader claims",
        "tradeoff_handling": "Evaluate gains and losses in their scientific context",
        "missing_evidence_handling": "Consider missingness without inventing measured values"}


class ScriptedLLM:
    """An explicitly scripted transport fixture, not scientific evidence."""
    model, base_url = "protocol-test", "https://fixture.invalid/v1"
    cache_settings = {}

    def __init__(self, decision="accepted", *, plan=None, replies=None):
        self.calls, self.decision = [], decision
        self.plan, self.replies = deepcopy(plan or PLAN), replies

    def complete(self, role, instructions, context, schema):
        self.calls.append((role, deepcopy(context)))
        if self.replies is not None:
            return self.replies(role, context, len(self.calls))
        if role == "E-plan":
            return deepcopy(self.plan)
        return {"plan_ref": context["plan_ref"], "decision": self.decision,
                "reason": "Decision under the frozen scientific criteria",
                "supporting_evidence_refs": [context["available_evidence_refs"][0]],
                "tradeoff_label": "descriptive tradeoff"}


def setup(tmp_path, llm=None):
    store = Store(tmp_path)
    team = SimpleNamespace(llm=llm or ScriptedLLM())
    plan = ensure_evaluation_plan(store, team, evaluation_id="trial-1", target="workflow",
                                  context={"objective": "task-local exploration"}, max_top_n=2)
    return store, team, plan


def evidence(old=10.0, new=1.0):
    return {"workflow_trials/trial-1/metrics": {
        "baseline": {"metrics": {"maximum": old, "best": old, "top3mean": None,
                                  "top7mean": None, "avg": old},
                     "denominators": {"submitted": 2, "unique_valid": 1}},
        "challenger": {"metrics": {"maximum": new, "best": new, "top3mean": None,
                                    "top7mean": None, "avg": new},
                       "denominators": {"submitted": 2, "unique_valid": 1}}}}


def adjudicate(store, team, plan, facts=None, **kwargs):
    return adjudicate_evaluation(store, team, evaluation_id="trial-1", plan_ref=plan["plan_ref"],
        evidence=facts or evidence(), n_baseline=kwargs.get("n_baseline", 1),
        n_challenger=kwargs.get("n_challenger", 1))


def test_new_policy_contains_no_numerical_acceptance_configuration():
    policy = GatePolicy(criterion="llm_adjudicated_v1")
    assert policy.model_dump(mode="json") == {"criterion": "llm_adjudicated_v1"}
    assert policy.required_per_arm == 1
    assert policy.top_ns == []
    assert GatePolicy.model_validate(policy.model_dump()).model_dump() == policy.model_dump()
    for key, value in {"top_ns": [5], "min_per_arm": 4, "min_effect": 0,
                       "max_qc_failure_fraction": 0.25, "confidence": 0.95,
                       "bootstrap_samples": 100, "absolute_tolerances": {},
                       "improvement_margins": {}}.items():
        with pytest.raises(ValidationError, match="no configured numeric"):
            GatePolicy(criterion="llm_adjudicated_v1", **{key: value})
    assert "criterion" not in GatePolicy().model_dump()
    assert GatePolicy(criterion="observed_pareto_v1").required_per_arm == 10


def test_llm_chooses_unfixed_ns_even_when_incomplete_and_plan_is_durable(tmp_path):
    store, team, plan = setup(tmp_path)
    assert plan["plan"]["top_ns"] == [3, 7]  # Capacity is only two, not a scientific veto.
    assert team.llm.calls[0][0] == "E-plan"
    assert team.llm.calls[0][1]["available_unique_arm_capacity"] == 2
    assert load_evaluation_plan(Store(tmp_path), plan["plan_ref"]) == plan
    key = digest({key: value for key, value in plan.items() if key != "plan_ref"})
    assert plan["plan_ref"] == "evaluation_plans/" + key
    again = ensure_evaluation_plan(store, team, evaluation_id="trial-1", target="workflow",
                                  context={"objective": "task-local exploration"}, max_top_n=2)
    assert again == plan and len(team.llm.calls) == 1
    with pytest.raises(Conflict, match="different plan"):
        ensure_evaluation_plan(store, team, evaluation_id="trial-1", target="workflow",
                               context={"objective": "post-hoc changed criteria"}, max_top_n=2)


@pytest.mark.parametrize("decision,old,new", [("accepted", 10, 1), ("rejected", 1, 10),
                                               ("inconclusive", 1, 10)])
def test_valid_llm_verdict_is_not_overridden_by_metric_direction(tmp_path, decision, old, new):
    store, team, plan = setup(tmp_path, ScriptedLLM(decision))
    result = adjudicate(store, team, plan, evidence(old, new))
    assert result.decision == decision
    assert result.effect is None and result.interval is None
    assert result.details["outcome"] == "descriptive tradeoff"
    assert len(team.llm.calls) == 2
    # Durability precedes adoption, and repeat calls do not resample a valid verdict.
    assert len(store.all("evaluation_verdicts")) == 1
    assert adjudicate(Store(tmp_path), team, plan, evidence(old, new)) == result
    assert len(team.llm.calls) == 2


def test_missing_metrics_and_zero_valid_denominators_do_not_create_a_veto(tmp_path):
    store, team, plan = setup(tmp_path)
    result = adjudicate(store, team, plan, evidence(None, None), n_baseline=0, n_challenger=0)
    assert result.decision == "accepted"
    assert result.n_baseline == result.n_challenger == 0
    assert result.details["evidence"] == evidence(None, None)


def test_bad_reference_is_repaired_without_changing_plan_or_evidence(tmp_path):
    def replies(role, context, count):
        if role == "E-plan":
            return PLAN
        return {"plan_ref": context["plan_ref"], "decision": "accepted", "reason": "Scientific judgment",
                "supporting_evidence_refs": ["other-trial/fabricated" if count == 2 else
                                             context["available_evidence_refs"][0]]}
    store, team, plan = setup(tmp_path, ScriptedLLM(replies=replies))
    result = adjudicate(store, team, plan)
    assert result.decision == "accepted" and len(team.llm.calls) == 3
    first, repaired = team.llm.calls[1][1], team.llm.calls[2][1]
    assert {k: v for k, v in repaired.items() if k != "response_repair"} == first
    assert repaired["response_repair"]["previous_response"]["supporting_evidence_refs"] == ["other-trial/fabricated"]
    assert len([key for key in store.all("evaluation_requests") if key.endswith("/response")]) == 3
    assert adjudicate(store, team, plan) == result and len(team.llm.calls) == 3


def test_exhausted_invalid_decision_pauses_and_does_not_resample_on_resume(tmp_path):
    def replies(role, context, count):
        return PLAN if role == "E-plan" else {"decision": "accepted"}
    store, team, plan = setup(tmp_path, ScriptedLLM(replies=replies))
    with pytest.raises(ProviderPaused, match="no decision was made"):
        adjudicate(store, team, plan)
    calls = len(team.llm.calls)
    assert calls == 1 + MAX_RESPONSE_REPAIRS + 1
    assert not store.all("evaluation_verdicts")
    with pytest.raises(ProviderPaused):
        adjudicate(store, team, plan)
    assert len(team.llm.calls) == calls
    with pytest.raises(Conflict, match="frozen verdict request"):
        adjudicate(store, team, plan, evidence(100, 1000))


def test_missing_llm_and_hidden_inputs_fail_closed(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ProviderPaused, match="actual configured LLM"):
        ensure_evaluation_plan(store, SimpleNamespace(llm=None), evaluation_id="trial", target="meta",
                               context={}, max_top_n=1)
    llm = ScriptedLLM()
    with pytest.raises(ValueError, match="label sources"):
        ensure_evaluation_plan(store, SimpleNamespace(llm=llm), evaluation_id="trial", target="meta",
                               context={"nested": {"labels": {"secret-sequence": 4}}}, max_top_n=1)
    assert not llm.calls


def test_postplan_backend_or_result_rewrite_is_rejected(tmp_path):
    store, team, plan = setup(tmp_path)
    team.llm.model = "changed-after-plan"
    with pytest.raises(Conflict, match="backend identity"):
        adjudicate(store, team, plan)
    team.llm.model = "protocol-test"
    adjudicate(store, team, plan)
    key, record = next(iter(store.all("evaluation_verdicts").items()))
    record["result"]["decision"] = "rejected"
    store.put("evaluation_verdicts", key, record)
    with pytest.raises(Conflict, match="disagrees"):
        adjudicate(store, team, plan)
