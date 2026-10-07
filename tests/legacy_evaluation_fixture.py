"""Build historical pending E requests to exercise migration without rewriting them.

The pre-custom-metric tests deliberately keep their old numeric facts. New metric
execution and verdict tests live in test_custom_metric_* and do not use this helper.
This seeds persisted legacy state, not a production switch or unsafe executor.
"""
from copy import deepcopy

from proteinrsi.contracts import digest


def install_legacy_pending_requests(monkeypatch):
    import proteinrsi.llm_evaluation as evaluation
    original_request = evaluation._request

    def seed(store, team, role, prompt, context, schema):
        current_ref, current = original_request(store, team, role, prompt, context, schema)
        if role != "E-plan":
            return current_ref, current
        evaluation_id = context["evaluation_id"]
        if store.get("evaluation_bindings", evaluation_id) is None:
            historical = deepcopy(current)
            historical["instructions"] = "Historical E-plan: predeclare criteria and top-N values before outcomes."
            historical["context"].pop("metric_execution_contract", None)
            historical["context"]["available_summaries"] = [
                "maximum", "best_in_task_direction", "LLM_chosen_top_N_means", "average"]
            historical["schema"]["properties"].pop("metric_program", None)
            historical["schema"]["required"] = [name for name in historical["schema"]["required"]
                                                   if name != "metric_program"]
            historical["schema"].pop("$defs", None)
            key = digest(historical)
            ref = "evaluation_requests/" + key
            store.put("evaluation_requests", key, historical, immutable=True)
            store.put("evaluation_bindings", evaluation_id, {"evaluation_id": evaluation_id,
                "target": context["target"], "planning_request_ref": ref, "plan_ref": None})
        return current_ref, current

    monkeypatch.setattr(evaluation, "_request", seed)
