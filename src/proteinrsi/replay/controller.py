# SPDX-License-Identifier: MIT
"""Budgeted historical replay. Labels live in the controller, research runs in a fresh worker."""
from pathlib import Path
from proteinrsi.contracts import TaskSpec
from proteinrsi.lab import CSVOracle, UnknownMeasurement
from proteinrsi.localtools.artifacts import file_sha256
from .broker import GuardedTeam, GuardedMetaAgent, reader_roots
from .sandbox import probe, SandboxUnavailable


def run_replay(campaign, dataset, *, guarded=True):
    task = TaskSpec.model_validate(campaign.state["task"])
    if task.feedback_source not in ("measured_replay", "synthetic"):
        raise ValueError("Historical data cannot impersonate new wet-lab results")
    path = Path(dataset).resolve(strict=True)
    if guarded:
        security = probe()
        if not security["available"]:
            raise SandboxUnavailable(security["reason"])
        # Never permit label placement inside the worker's dependency/code allowlist.
        if any(path.is_relative_to(Path(root).resolve()) for root in reader_roots()):
            raise ValueError("Move labels outside the worker dependency/source allowlist")
    oracle = CSVOracle(path, task)  # controller-only; worker is spawned by exec, not given this object
    if not task.candidates:
        raise ValueError("Replay requires a label-free eligible catalogue in task.candidates")
    needed = set(task.candidates)
    if task.controls_per_batch or task.initial_observation_policy == "parent_once":
        needed.add(task.reference_sequence)
    if any(seq not in oracle._labels for seq in needed):
        raise UnknownMeasurement("Eligible catalogue includes unavailable measurements; repair preparation before spending budget")
    campaign.store.put("configuration", "replay_dataset", {"sha256": file_sha256(path),
        "feedback_source": task.feedback_source}, immutable=True)
    campaign.store.event("replay_started", {"execution": "guarded" if guarded else "inprocess_explicit",
        "source": task.feedback_source, "labels_sent_to_worker": False,
        "security_note": "inprocess is NOT an OS-isolated benchmark" if not guarded else "Landlock + seccomp + capability RPC"})
    if guarded:
        old = campaign.team
        campaign.team = GuardedTeam(campaign.store, old.llm, old.tools, old.protein_model)
        campaign.meta_agent = GuardedMetaAgent(campaign.team)
    while (batch := campaign.prepare()) is not None:
        # Check complete availability BEFORE approval/settlement; no partial charge or fabricated result.
        if any(s.candidate.sequence not in oracle._labels for s in batch.samples):
            raise UnknownMeasurement("Batch contains an unavailable sequence; not approved, no phenotype returned")
        campaign.approve(batch.batch_id, operator="explicit-guarded-replay" if guarded else "explicit-inprocess-replay")
        campaign.ingest(oracle.measure(batch))
    return campaign.report()
