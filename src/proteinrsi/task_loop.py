# SPDX-License-Identifier: MIT
"""Thin, resumable Task driver shared by research and method validation.

It owns no scientific strategy and cannot publish a method. A feedback adapter
is controller-only; only revealed observations enter the campaign's TaskView.
"""
from typing import Literal, Protocol

from proteinrsi.contracts import Batch, Model, Observation, digest
from proteinrsi.recovery import is_provider_paused
from proteinrsi.storage import BudgetExceeded


class TaskLoopResult(Model):
    status: Literal["completed", "waiting_feedback", "waiting_approval", "paused",
                    "budget_exhausted", "waiting_validation"]
    reason: str
    completed_rounds: int
    pending_batch: str | None = None
    analysis_pending: bool = False


class FeedbackAdapter(Protocol):
    def validate(self, batch: Batch) -> None: ...
    def measure(self, batch: Batch) -> list[Observation] | None: ...


def run_task_loop(campaign, feedback: FeedbackAdapter | None = None, *,
                  operator: str = "task-loop", on_checkpoint=None, raise_on_pause: bool = False) -> TaskLoopResult:
    """Advance modular Campaign transitions; stop at a durable external boundary.

    No adapter means prepare-only (a real laboratory can import results later).
    An optional main-study scheduler may validate a method at a safe boundary.
    Fixed-W children omit it; fixed-M children use it for branch-local W trials.
    Governance, not this driver, determines method-change authority.
    Known provider pauses are resumable; unknown side effects/integrity errors
    deliberately propagate rather than being treated as an ordinary stop.
    """
    def stopped(status, reason):
        state = campaign.state
        result = TaskLoopResult(status=status, reason=reason,
            completed_rounds=state["round_index"], pending_batch=state["pending_batch"],
            analysis_pending=bool(state["history"] and not state["history"][-1].get("analyst_feedback")))
        record = result.model_dump(mode="json")
        campaign.store.put("task_loop_status", "latest", record)
        key = digest({"result": record, "workflow": state["workflow"],
                      "budget": campaign.store.usage()})
        if campaign.store.get("task_loop_results", key) is None:
            campaign.store.put("task_loop_results", key, record, immutable=True)
            campaign.store.event("task_loop_stopped", {**record, "result_ref": key})
        return result

    try:
        while True:
            campaign.analyze_feedback()
            state = campaign.state
            if state["status"] == "complete":
                return stopped("completed", state.get("stop_reason", "task_boundary"))
            if not state["pending_batch"]:
                if on_checkpoint is not None:
                    pause = on_checkpoint(campaign)
                    if pause:
                        return stopped("waiting_validation", str(pause))
                state = campaign.state
                task = state["task"]
                remaining = campaign.store.remaining("experimental_wells")
                if task.get("batch_fill_policy") == "full_plate" and remaining < task["batch_size"]:
                    return stopped("budget_exhausted", "insufficient_whole_plate_budget")
            batch = campaign.prepare()
            if batch is None:
                return stopped("completed", campaign.state.get("stop_reason", "task_boundary"))
            if feedback is None:
                status = "waiting_feedback" if campaign.state["status"] == "awaiting_results" else "waiting_approval"
                return stopped(status, "external_feedback_required")
            feedback.validate(batch)
            campaign.approve(batch.batch_id, operator=operator)
            saved = campaign.store.get("measurements", batch.batch_id)
            observations = ([Observation.model_validate(o) for o in saved]
                            if saved is not None else feedback.measure(batch))
            if observations is None:
                return stopped("waiting_feedback", "external_feedback_pending")
            campaign.ingest(observations)
    except BudgetExceeded as exc:
        return stopped("budget_exhausted", str(exc))
    except Exception as exc:
        if not is_provider_paused(exc):
            raise
        result = stopped("paused", "provider_paused")
        if raise_on_pause:
            raise
        return result
