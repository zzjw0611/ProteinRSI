# SPDX-License-Identifier: MIT
"""Bounded challenger execution; ordinary research is not owned by the challenger."""
from proteinrsi.contracts import GatePolicy
from proteinrsi.tasks import validate_candidate
from proteinrsi.trial_allocation import allocate_trial


def prepare_workflow_trial(campaign, state, view, patch, workflow, baseline, slots, excluded):
    gate = GatePolicy.model_validate(state["gate"])
    if slots // 2 < gate.min_per_arm:
        detail = {"reason": "Insufficient equal-arm plate capacity", "round": view.round_index}
        campaign.store.event("workflow_validation_deferred", {"patch_id": patch.patch_id, **detail})
        if campaign.methods.enabled:
            campaign.methods.defer(state, patch, detail["reason"])
        return [], {}
    campaign.methods.begin(state, patch)
    challenge_view = campaign.view(state, workflow)
    challenge_view.research_context = {**challenge_view.research_context,
        "validation_request": {"maximum_candidates": slots // 2,
            "minimum_candidates": gate.min_per_arm,
            "instruction": "Generate a bounded validation panel; the controller fills the remaining plate separately."}}
    try:
        challenger = campaign.team.run(challenge_view)
        # Use the full immutable task, not a preview catalogue.
        from proteinrsi.contracts import TaskSpec
        task = TaskSpec.model_validate(state["task"])
        for candidate in challenger:
            validate_candidate(task, candidate)
        chosen, allocation = allocate_trial(
            {"baseline": baseline, "challenger": challenger},
            {"baseline": view.workflow.version, "challenger": workflow.version},
            slots, gate.min_per_arm, excluded, view.task.seed + view.round_index)
    except Exception as exc:
        if not campaign.methods.failure(state, patch, exc):
            raise
        campaign.store.event("workflow_validation_failed", {
            "patch_id": patch.patch_id, "round": view.round_index, "error_type": type(exc).__name__})
        return [], {}
    campaign.store.event("workflow_validation_allocation", {"patch_id": patch.patch_id,
        "round": view.round_index, **allocation})
    if not chosen:
        detail = {"decision": "inconclusive", "round": view.round_index, **allocation}
        campaign.store.put("workflow_validation_outcomes", patch.patch_id, detail)
        campaign.methods.finish(state, patch, "inconclusive", detail=detail)
    return chosen, allocation
