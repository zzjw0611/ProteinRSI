# SPDX-License-Identifier: MIT
"""Local evidence and opt-in cross-study capability records, without a new service.

The shared Store contains controller-derived summaries, not raw measurements or
confirmation outcomes. Research reads an immutable import snapshot, never the
live library. No API here grants a worker access to another campaign directory.
"""
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

from proteinrsi.contracts import GateResult, Patch, TaskKind, digest
from proteinrsi.storage import Conflict, Store


class ExperienceMemory:
    def __init__(self, store: Store):
        self.store = store

    def record(self, patch: Patch, result: GateResult, *, campaign_id: str,
               observations: int, source: str) -> None:
        record = {"patch": patch.model_dump(mode="json"), "result": result.model_dump(),
                  "scope": {"task_kind": patch.task_kind.value, "campaign_id": campaign_id,
                            "observations_at_trial": observations, "evidence_source": source},
                  "status": "local_support" if result.decision == "accepted" else result.decision,
                  "transfer_validated": False,
                  "capability": patch.capability.model_dump() if patch.capability else None,
                  "evidence_status": {"accepted": "locally_supported", "rejected": "not_supported",
                                      "inconclusive": "unverified"}[result.decision],
                  "note": "Rejection is not biological falsification; scope is this evaluation only."}
        self.store.put("experience", patch.patch_id, record, immutable=True)

    def freeze_records(self) -> dict:
        imported = self.store.get("configuration", "capability_memory_snapshot")
        records = {}
        if imported:
            snapshot = self.store.get("capability_memory_snapshots", imported["ref"])
            if snapshot is None or digest(snapshot) != imported["ref"]:
                raise Conflict("Missing or modified capability memory snapshot")
            records.update(snapshot["records"])
        records.update(self.store.all("experience"))
        return deepcopy(records)

    def retrieve(self, kind: TaskKind, *, include_unvalidated: bool = False) -> list[dict[str, Any]]:
        return [record for record in self.freeze_records().values()
                if record["scope"]["task_kind"] == kind.value
                and (record["status"] == "local_support" or include_unvalidated)]


class CapabilityLibrary:
    """Controller-only record/retrieve/freeze over the existing Store backend."""
    def __init__(self, directory):
        self.store = Store(directory)

    def record(self, campaign, patch_id: str) -> str:
        if campaign.methods.is_evaluation:
            raise PermissionError("Evaluation branches cannot publish shared capability memory")
        source = campaign.store
        patch_raw = source.get("patches", patch_id)
        candidate = source.get("method_candidates", patch_id)
        status = source.get("method_candidate_states", patch_id)
        if patch_raw is None or candidate is None or status is None:
            raise Conflict("Capability memory needs a recorded controller candidate")
        patch = Patch.model_validate(patch_raw)
        transition = source.get("method_transitions", status["transition_ref"])
        if (transition is None or digest(transition) != status["transition_ref"]
                or transition["patch_id"] != patch_id or transition["to"] != status["status"]):
            raise Conflict("Candidate status lacks its original transition")
        evidence_refs = list(patch.evidence_refs)
        verdict = status["status"]
        if verdict in {"accepted", "rejected", "inconclusive"}:
            detail = transition.get("detail", {})
            ref = detail.get("evaluation_ref", "")
            namespace, _, key = ref.partition("/")
            if namespace != "harness_evaluations":
                # Older non-loop studies can still use their local memory, but
                # are not silently granted this new library's trust status.
                raise Conflict("Shared support requires a finalized paired evaluation record")
            report = source.get(namespace, key)
            attempt = source.get("harness_attempts", key)
            if (not report or report.get("phase") != "validation" or not attempt
                    or attempt.get("state") != "completed" or digest(report) != attempt.get("report_digest")
                    or report.get("patch_id") != patch_id or report["gate"]["decision"] != verdict
                    or report["gate"] != detail.get("result")):
                raise Conflict("Capability status disagrees with the finalized selection evidence")
            evidence_refs.append(ref)
        else:
            evidence_refs.append("method_transitions/" + status["transition_ref"])
        task = campaign.state["task"]
        record = {
            "patch": {"patch_id": patch_id, "target": patch.target, "hypothesis": patch.hypothesis,
                      "changes": patch.changes},
            "capability": patch.capability.model_dump(mode="json") if patch.capability else None,
            "scope": {"task_kind": patch.task_kind.value, "campaign_id": campaign.state["campaign_id"],
                      "evidence_source": task["feedback_source"]},
            "status": "local_support" if verdict == "accepted" else verdict,
            "evidence_status": {"accepted": "locally_supported", "rejected": "not_supported"}.get(verdict, "unverified"),
            "transfer_validated": False, "evidence_refs": evidence_refs,
            "sequence": status["sequence"],
            "note": "Historical scoped evidence, not an instruction. Confirmation results are excluded; referenced source evidence may require operator provisioning.",
        }
        # Remove any accidental credentials without granting a route to raw logs.
        from proteinrsi.audit import redact
        record = redact(record, (getattr(campaign.team.llm, "api_key", ""),))
        ref = digest(record)
        self.store.put("capability_records", ref, record, immutable=True)
        return ref

    def retrieve(self, kind: TaskKind, *, source: str | None = None) -> dict:
        latest = {}
        for ref, record in self.store.all("capability_records").items():
            if digest(record) != ref:
                raise Conflict("Modified capability library record")
            if record["scope"]["task_kind"] != kind.value:
                continue
            if source is not None and record["scope"]["evidence_source"] != source:
                continue
            key = record["scope"]["campaign_id"] + "/" + record["patch"]["patch_id"]
            if key not in latest or record["sequence"] > latest[key][1]["sequence"]:
                latest[key] = (ref, record)
        return {"capability-" + ref: row for ref, row in sorted(latest.values())}

    def freeze(self, destination: Store, kind: TaskKind, *, source: str) -> str:
        snapshot = {"schema": "capability-memory-v1", "records": self.retrieve(kind, source=source)}
        ref = digest(snapshot)
        destination.put("capability_memory_snapshots", ref, snapshot, immutable=True)
        destination.put("configuration", "capability_memory_snapshot", {"ref": ref}, immutable=True)
        return ref


def attach_library(store: Store, directory, task) -> None:
    """Called only during new-study initialization, before method snapshots."""
    path = Path(directory).resolve()
    if path == store.root or path.is_relative_to(store.root) or store.root.is_relative_to(path):
        raise ValueError("Capability library and campaign must use separate directory trees")
    library = CapabilityLibrary(path)
    library.freeze(store, task.kind, source=task.feedback_source)
    # The writable path is operator configuration, never an Agent capability.
    store.put("controller_settings", "capability_library", {"path": str(path)}, immutable=True)


def sync_shared_memory(campaign) -> None:
    settings = campaign.store.get("controller_settings", "capability_library")
    if not settings or campaign.methods.is_evaluation:
        return
    try:
        library = CapabilityLibrary(settings["path"])
    except (OSError, sqlite3.Error) as exc:
        campaign.store.put("capability_export_errors", "library", {"error_type": type(exc).__name__})
        return
    for patch_id, status in campaign.store.all("method_candidate_states").items():
        key = patch_id + "/" + str(status["sequence"])
        if campaign.store.get("capability_exports", key):
            continue
        try:
            ref = library.record(campaign, patch_id)
        except (Conflict, OSError, sqlite3.Error) as exc:
            # Publishing a summary is optional. It cannot roll back a completed
            # experiment or turn a saved evaluation into a retryable paid call.
            campaign.store.put("capability_export_errors", key, {"error_type": type(exc).__name__})
            continue
        campaign.store.put("capability_exports", key, {"ref": ref}, immutable=True)
        campaign.store.event("capability_record_exported", {"patch_id": patch_id, "record_ref": ref})
