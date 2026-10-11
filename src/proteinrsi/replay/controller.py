# SPDX-License-Identifier: MIT
"""Budgeted historical replay. Labels live in the controller, research runs in a fresh worker."""
from pathlib import Path
from proteinrsi.contracts import Observation, Patch, TaskSpec
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
    while True:
        # Finish Task feedback before any optional method work. Independent-mode
        # replay yields at a proposal boundary rather than spending the remaining
        # study budget while an unvalidated method silently waits forever.
        campaign.analyze_feedback()
        independent = campaign.store.get("configuration", "research", {}).get("workflow_validation") == "independent"
        if independent:
            campaign._optional_improvement()
            state = campaign.state
            if state["pending_patch"] and not state["pending_batch"] and state["status"] == "ready":
                if validation_cases is None:
                    campaign.store.event("harness_validation_required", {
                        "patch_id": Patch.model_validate(state["pending_patch"]).patch_id,
                        "round": state["round_index"], "task_analysis_completed": True,
                        "reason": "Supply controller-only validation cases or explicitly continue with step"})
                    break
                from proteinrsi.harness_evaluation import evaluate_workflow
                evaluate_workflow(campaign, validation_cases, promote=True)
        batch = campaign.prepare()
        if batch is None:
            break
        # Explicit closed libraries must be fully covered. Open design receives missing-record feedback.
        if task.candidate_access != "open" and any(s.candidate.sequence not in oracle._labels for s in batch.samples):
            raise UnknownMeasurement("Batch contains an unavailable sequence; not approved, no phenotype returned")
        campaign.approve(batch.batch_id, operator="explicit-guarded-replay" if guarded else "explicit-inprocess-replay")
        saved = campaign.store.get("measurements", batch.batch_id)
        # E may pause after paid observations are durable but before adoption.
        # Resume that exact evidence rather than querying the oracle again.
        observations = ([Observation.model_validate(row) for row in saved]
                        if saved is not None else oracle.measure(batch))
        missing = [o.sample_id for o in observations if o.qc == "unavailable"]
        if missing and saved is None:
            campaign.store.event("replay_unavailable", {"batch_id": batch.batch_id,
                "sample_ids": missing, "charged_queries": len(missing),
                "reason": "No historical measurement; not a failed assay or low fitness"})
        campaign.ingest(observations)
    return campaign.report()
