# SPDX-License-Identifier: MIT
"""Checkpoint scheduling for the first-stage fixed-H replay protocol.

The controller constructs same-snapshot comparisons from already disclosed
inputs. These are not mislabeled as independent proteins or confirmation data.
"""
from proteinrsi.contracts import EvaluationPolicy, Patch, TaskSpec, digest
from proteinrsi.evaluation import MetaCase
from proteinrsi.storage import BudgetExceeded


def schedule_evaluation(campaign, oracle=None, *, cases=None, team_factory=None):
    state, store = campaign.state, campaign.store
    if state["pending_batch"] or state["status"] != "ready" or not state["pending_patch"]:
        return None
    config = store.get("configuration", "research", {})
    settings = EvaluationPolicy.model_validate(config.get("auto_evaluation") or {})
    if not settings.enabled:
        return None
    patch = Patch.model_validate(state["pending_patch"])

    def defer(reason):
        key = digest({"patch": patch.patch_id, "round": state["round_index"], "reason": reason})
        if store.get("evaluation_deferrals", key) is None:
            record = {"patch_id": patch.patch_id, "round": state["round_index"],
                      "reason": reason, "status": "pending_validation", "active_version_unchanged": True}
            store.put("evaluation_deferrals", key, record, immutable=True)
            store.event("harness_validation_deferred", record)
        return None

    if cases is not None:
        from proteinrsi.harness_evaluation import evaluate_workflow
        try:
            return evaluate_workflow(campaign, cases, promote=True, team_factory=team_factory)
        except BudgetExceeded:
            if any(a.get("patch_id") == patch.patch_id and a.get("state") in {"started", "paused_provider", "blocked"}
                   for a in store.all("harness_attempts").values()):
                raise
            return defer("Insufficient paired-validation budget; retaining original Harness")
    if oracle is None:
        return defer("No authorized replay feedback adapter; real-lab validation requires authorized feedback")
    frozen = store.get("evaluation_auto_inputs", patch.patch_id)
    if frozen is None:
        task = TaskSpec.model_validate(state["task"])
        data = task.model_dump(mode="json")
        q = settings.query_budget_per_arm or task.batch_size * settings.rounds
        data.update(max_rounds=settings.rounds,
                    budget={"experimental_wells": q, "llm_calls": settings.llm_calls_per_arm,
                            "tool_calls": settings.tool_calls_per_arm})
        # Validation is a continuation of a frozen observed snapshot, never a new warm-start purchase.
        data.update(initial_observation_policy="none", initial_parent_measurement=None)
        TaskSpec.model_validate(data)
        frozen = {"task": data, "initial": state["observations"], "history": state["history"],
            "round_index": state["round_index"], "first_round_spent": state.get("first_round_spent", 0), "query_budget": q,
            "case_id": "snapshot-" + patch.patch_id, "group_id": "current-scientific-start",
            "source": store.get("configuration", "replay_dataset"), "scope": "same_snapshot"}
        store.put("evaluation_auto_inputs", patch.patch_id, frozen, immutable=True)
    # Complete mapping remains solely in controller memory and is never persisted in a TaskView.
    labels = {seq: value for seq, (value, qc) in oracle._labels.items() if qc == "valid"}
    case = MetaCase(case_id=frozen["case_id"], group_id=frozen["group_id"], split="validation",
        task=frozen["task"], initial=frozen["initial"], labels=labels, query_budget=frozen["query_budget"])
    from proteinrsi.loop_evaluation import evaluate_workflow_loop
    try:
        return evaluate_workflow_loop(campaign, [case], promote=True, team_factory=team_factory,
            scope="same_snapshot", start_overrides={case.case_id: {
                "history": frozen["history"], "round_index": frozen["round_index"],
                "first_round_spent": frozen["first_round_spent"], "initialization_done": True}})
    except BudgetExceeded:
        # Only a pre-start shortage may safely continue the parent. Started or
        # uncertain external work must be reconciled rather than ignored.
        if any(a.get("patch_id") == patch.patch_id and a.get("state") in {"started", "paused_provider", "blocked"}
               for a in store.all("harness_attempts").values()):
            raise
        return defer("Insufficient paired-validation budget; retaining original Harness")
