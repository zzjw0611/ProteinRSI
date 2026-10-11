# SPDX-License-Identifier: MIT
"""Budgeted historical replay. Labels live in the controller, research runs in a fresh worker."""
from pathlib import Path
from proteinrsi.contracts import Patch, TaskSpec
from proteinrsi.lab import CSVOracle, UnknownMeasurement
from proteinrsi.localtools.artifacts import file_sha256
from .broker import GuardedTeam, GuardedMetaAgent, reader_roots
from .sandbox import probe, SandboxUnavailable


def run_replay(campaign, dataset, *, guarded=True, validation_cases=None):
    task = TaskSpec.model_validate(campaign.state["task"])
    if task.feedback_source not in ("measured_replay", "synthetic"):
        raise ValueError("Historical data cannot impersonate new wet-lab results")
    intent = campaign.store.get("configuration", "goal_intent", {})
    if (intent and task.kind.value == "variant_design" and not intent.get("candidates")
            and task.candidates):
        raise ValueError("Legacy replay injected a candidate catalogue into open design; preserve its audit and start a new corrected campaign")
    path = Path(dataset).resolve(strict=True)
    if guarded:
        security = probe()
        if not security["available"]:
            raise SandboxUnavailable(security["reason"])
        # Never permit label placement inside the worker's dependency/code allowlist.
        if any(path.is_relative_to(Path(root).resolve()) for root in reader_roots()):
            raise ValueError("Move labels outside the worker dependency/source allowlist")
    oracle = CSVOracle(path, task)  # controller-only; worker is spawned by exec, not given this object
    if task.candidate_access != "open" and not task.candidates:
        raise ValueError("Replay requires a label-free eligible catalogue in task.candidates")
    needed = set(task.candidates)
    if task.controls_per_batch or task.initial_observation_policy != "none":
        needed.add(task.reference_sequence)
    if any(seq not in oracle._labels for seq in needed):
        raise UnknownMeasurement("Eligible catalogue includes unavailable measurements; repair preparation before spending budget")
    if task.initial_observation_policy == "provided_parent":
        value, qc = oracle._labels[task.reference_sequence]
        if qc != "valid" or value != task.initial_parent_measurement.value:
            raise ValueError("Provided parent measurement differs from the replay dataset")
    campaign.store.put("configuration", "replay_dataset", {"sha256": file_sha256(path),
        "feedback_source": task.feedback_source}, immutable=True)
    campaign.store.event("replay_started", {"execution": "guarded" if guarded else "inprocess_explicit",
        "source": task.feedback_source, "labels_sent_to_worker": False,
        "security_note": "inprocess is NOT an OS-isolated benchmark" if not guarded else "Landlock + seccomp + capability RPC"})
    if guarded:
        old = campaign.team
        campaign.team = GuardedTeam.from_team(old)
        campaign.meta_agent = GuardedMetaAgent(campaign.team)
    from proteinrsi.task_loop import run_task_loop

    class ReplayFeedback:
        def validate(self, batch):
            if task.candidate_access != "open" and any(s.candidate.sequence not in oracle._labels for s in batch.samples):
                raise UnknownMeasurement("Batch contains an unavailable sequence; no phenotype returned")

        def measure(self, batch):
            observations = oracle.measure(batch)
            missing = [o.sample_id for o in observations if o.qc == "unavailable"]
            if missing:
                campaign.store.event("replay_unavailable", {"batch_id": batch.batch_id,
                    "sample_ids": missing, "charged_queries": len(missing),
                    "reason": "No historical measurement; not a failed assay or low fitness"})
            return observations

    def checkpoint(current):
        config = current.store.get("configuration", "research", {})
        if config.get("workflow_validation") != "independent":
            return None
        current._optional_improvement()
        state = current.state
        if not state["pending_patch"] or state["status"] != "ready":
            return None
        if config.get("evaluation_protocol") == "task-loop-v1":
            from proteinrsi.evaluation_scheduler import schedule_evaluation
            schedule_evaluation(current, oracle, cases=validation_cases)
            return None
        # Historical decision-case protocol remains explicit and unchanged.
        if validation_cases is None:
            current.store.event("harness_validation_required", {
                "patch_id": Patch.model_validate(state["pending_patch"]).patch_id,
                "round": state["round_index"], "task_analysis_completed": True,
                "reason": "Supply controller-only validation cases or explicitly continue with step"})
            return "validation_cases_required"
        from proteinrsi.harness_evaluation import evaluate_workflow
        evaluate_workflow(current, validation_cases, promote=True)
        return None

    result = run_task_loop(campaign, ReplayFeedback(),
        operator="explicit-guarded-replay" if guarded else "explicit-inprocess-replay",
        on_checkpoint=checkpoint,
        raise_on_pause=campaign.store.get("configuration", "research", {}).get("evaluation_protocol") != "task-loop-v1")
    return {**campaign.report(), "loop_result": result.model_dump(mode="json")}
