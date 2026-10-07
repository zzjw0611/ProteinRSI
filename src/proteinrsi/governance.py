# SPDX-License-Identifier: MIT
"""Controller-owned method snapshots, candidate lifecycles and pointer-only rollback.

This module has no model, shell, laboratory or refund capability. A snapshot is
an executable/configuration record, not evidence of scientific generalization.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import metadata
from pathlib import Path
import platform
from typing import Literal

from jsonschema import ValidationError as SchemaError
from pydantic import Field

from proteinrsi.audit import redact
from proteinrsi.contracts import GatePolicy, MetaPolicy, Model, Patch, TaskSpec, Workflow, digest
from proteinrsi.storage import BudgetExceeded, Conflict

Target = Literal["workflow", "meta"]
TYPES = {"workflow": Workflow, "meta": MetaPolicy}
PENDING = {"workflow": "pending_patch", "meta": "pending_meta"}
TERMINAL = {"accepted", "rejected", "inconclusive", "failed", "cancelled"}
TRANSITIONS = {
    "staged": {"validating", "awaiting_approval", "failed", "blocked", "inconclusive", "cancelled"},
    "validating": {"staged", "awaiting_approval", "accepted", "rejected", "inconclusive", "failed", "blocked", "cancelled"},
    "awaiting_approval": {"staged", "awaiting_results", "cancelled"},
    "awaiting_results": {"accepted", "rejected", "inconclusive"},
    "blocked": {"cancelled"},
}
CONFIG_KEYS = ("prompt_bundle", "research", "know_how", "protein_model", "local_tools")
CALL_NAMESPACES = ("llm", "tool_jobs", "validation_llm", "validation_tool_jobs")


class GovernancePolicy(Model):
    """Administrative settings: not part of either evolvable W/M schema."""
    max_consecutive_failures: int = Field(default=3, ge=1, le=100)
    max_changed_fields: int = Field(default=3, ge=1, le=20)
    max_deferred_rounds: int = Field(default=2, ge=1, le=20)


@lru_cache(maxsize=1)
def _packaged_source() -> dict:
    # Only distributed package source, never campaign data, environment variables,
    # credentials, model weights, or arbitrary operator-supplied paths.
    root = Path(__file__).resolve().parent
    sources = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
               for p in sorted(root.rglob("*"))
               if p.is_file() and not p.is_symlink() and p.suffix in {".py", ".md", ".json"}}
    versions = {}
    for name in ("pydantic", "numpy", "httpx", "jsonschema", "gepa", "langgraph",
                 "langgraph-checkpoint-sqlite", "mcp", "torch", "transformers",
                 "huggingface-hub", "safetensors"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"files": sources, "python": platform.python_version(), "dependencies": versions}


class MethodGovernance:
    def __init__(self, campaign):
        self.campaign, self.store = campaign, campaign.store

    @property
    def enabled(self) -> bool:
        # Old campaigns are not silently migrated or given invented historical snapshots.
        return self.store.get("configuration", "method_governance") is not None

    @property
    def policy(self) -> GovernancePolicy:
        return GovernancePolicy.model_validate(self.store.get("configuration", "method_governance"))

    def _snapshot(self, target: Target, definition) -> str:
        source = _packaged_source()
        source_ref = digest(source)
        self.store.put("method_assets", source_ref, source, immutable=True)
        configuration = {name: self.store.get("configuration", name) for name in CONFIG_KEYS}
        snapshot = {"schema_version": 1, "target": target, "version": definition.version,
            "definition": definition.model_dump(mode="json"), "source_ref": source_ref,
            "configuration": configuration,
            "contracts": {"method": type(definition).model_json_schema(),
                          "task": TaskSpec.model_json_schema(), "gate": GatePolicy.model_json_schema()},
            "limits": "External engines/weights are declared, not copied. Per-run generated code and protocols remain execution artifacts."}
        acceptance_policy = self.store.get("configuration", "acceptance_policy")
        if acceptance_policy is not None:
            snapshot["acceptance_policy"] = acceptance_policy
        ref = "method-" + digest(snapshot)
        self.store.put("method_snapshots", ref, snapshot, immutable=True)
        return ref

    def bootstrap(self, state: dict) -> None:
        if not self.enabled:
            return
        state["method_governance"] = {"active": {}, "consecutive_failures": 0,
            "paused": False, "pause_reason": None}
        for target, cls in TYPES.items():
            definition = cls.model_validate(state[target])
            ref = self._snapshot(target, definition)
            state["method_governance"]["active"][target] = ref
            self.store.put("method_activations", target+"/"+definition.version,
                {"snapshot_ref": ref, "origin": "initial"}, immutable=True)
        self.store.put("campaign", "state", state)

    def assert_idle(self, state: dict) -> None:
        if (state["status"] != "ready" or state["pending_batch"]
                or state["pending_patch"] or state["pending_meta"]):
            raise Conflict("Method changes require an idle round boundary with no pending candidate")

    def assert_plannable(self, state: dict) -> None:
        if not self.enabled:
            return
        for target, ref in state.get("method_governance", {}).get("active", {}).items():
            snapshot = self._validated_snapshot(target, ref)
            if TYPES[target].model_validate(state[target]).model_dump(mode="json") != snapshot["definition"]:
                raise Conflict("Active method differs from its snapshot; use an audited version change")
        for key in PENDING.values():
            if state[key]:
                patch = Patch.model_validate(state[key])
                record = self.store.get("method_candidate_states", patch.patch_id, {})
                offline_running = any(attempt.get("state") in {"started", "paused_provider", "measurements_committed"}
                    and attempt.get("patch_id") == patch.patch_id
                    for attempt in self.store.all("meta_attempts").values())
                if record.get("status") == "blocked" or offline_running:
                    raise Conflict("Candidate completion is uncertain; inspect and explicitly abandon before continuing")

    def paused(self, state: dict) -> bool:
        return bool(state.get("method_governance", {}).get("paused"))

    def stage(self, state: dict, patch: Patch, candidate) -> None:
        if not self.enabled:
            return
        if self.paused(state):
            raise Conflict("Method improvement is paused; an operator must resume it")
        if len(patch.changes) > self.policy.max_changed_fields:
            raise ValueError("Patch changes too many fields; test a smaller method change")
        self.assert_plannable(state)
        current = TYPES[patch.target].model_validate(state[patch.target])
        base_ref = self._snapshot(patch.target, current)
        candidate_ref = self._snapshot(patch.target, candidate)
        record = {"patch": patch.model_dump(mode="json"), "parent_version": current.version,
            "base_snapshot_ref": base_ref, "candidate_snapshot_ref": candidate_ref,
            "evidence_version": self.campaign.view(state).evidence_version,
            "round": state["round_index"], "campaign_id": state["campaign_id"],
            "validation_plan": {"stages": ["contract_preflight", "bounded_candidate_execution", "experimental_comparison"],
                "gate": state["gate"], "metric": ("direction_adjusted_best_topN_avg"
                    if state["gate"].get("criterion") in {"observed_pareto_v1", "llm_adjudicated_v1"} else "mean_signed_outcome"),
                "task_metric": state["task"]["metric"], "direction": state["task"]["direction"],
                "budget_scope": "shared_campaign", "transfer_validated": False}}
        self.store.put("method_candidates", patch.patch_id, record, immutable=True)
        self.transition(state, patch, "staged", {"candidate_snapshot_ref": candidate_ref})

    def transition(self, state: dict, patch: Patch, status: str, detail: dict | None = None) -> None:
        if not self.enabled:
            return
        previous = self.store.get("method_candidate_states", patch.patch_id)
        old = previous["status"] if previous else None
        if old == status:
            return  # Idempotent resume; never recount the same failure.
        if (old is None and status != "staged") or (old is not None and status not in TRANSITIONS.get(old, set())):
            raise Conflict(f"Invalid candidate transition: {old} -> {status}")
        with self.store.transaction():
            event = {"patch_id": patch.patch_id, "target": patch.target, "from": old, "to": status,
                "round": state["round_index"], "detail": detail or {},
                "sequence": 1 if previous is None else previous["sequence"] + 1}
            key = digest(event)
            self.store.put("method_transitions", key, event, immutable=True)
            self.store.put("method_candidate_states", patch.patch_id,
                {"status": status, "sequence": event["sequence"], "transition_ref": key})
            self.store.event("method_candidate_transition", event)

    def begin(self, state: dict, patch: Patch) -> None:
        if not self.enabled:
            return
        with self.store.transaction():
            if self.store.get("method_call_boundaries", patch.patch_id) is None:
                boundary = {ns: {key: digest(call) for key, call in self.store.all(ns).items()}
                            for ns in CALL_NAMESPACES}
                self.store.put("method_call_boundaries", patch.patch_id, boundary, immutable=True)
            self.transition(state, patch, "validating")

    def defer(self, state: dict, patch: Patch, reason: str) -> None:
        """Allow a bounded number of distinct-round deferrals, not an immortal queue."""
        if not self.enabled:
            return
        with self.store.transaction():
            key = f"{patch.patch_id}/{state['round_index']}"
            if self.store.get("method_deferrals", key) is None:
                self.store.put("method_deferrals", key,
                    {"patch_id": patch.patch_id, "round": state["round_index"], "reason": reason}, immutable=True)
            count = sum(item["patch_id"] == patch.patch_id for item in self.store.all("method_deferrals").values())
            if count >= self.policy.max_deferred_rounds and state["gate"].get("criterion") != "llm_adjudicated_v1":
                self.finish(state, patch, "inconclusive", detail={"reason": reason, "deferred_rounds": count})

    def _count_failure(self, state: dict, reason: str) -> None:
        control = state["method_governance"]
        control["consecutive_failures"] += 1
        if control["consecutive_failures"] >= self.policy.max_consecutive_failures:
            control["paused"], control["pause_reason"] = True, reason
            self.store.event("method_improvement_paused", {"reason": reason,
                "consecutive_failures": control["consecutive_failures"]})

    def finish(self, state: dict, patch: Patch, status: str, *, detail: dict | None = None) -> None:
        """Close a candidate without refunding, deleting facts or publishing a version."""
        with self.store.transaction():
            if self.enabled:
                previous = self.store.get("method_candidate_states", patch.patch_id, {})
                self.transition(state, patch, status, detail)
                if previous.get("status") not in TERMINAL:
                    if status == "accepted":
                        state["method_governance"]["consecutive_failures"] = 0
                    elif status != "cancelled":
                        self._count_failure(state, status)
            state[PENDING[patch.target]] = None
            self.store.put("campaign", "state", state)

    def complete(self, state: dict, patch: Patch, result, candidate, *, evaluation_ref: str) -> None:
        """Trusted gate entrypoint, called only with a freshly computed gate result."""
        if state["gate"].get("criterion") == "llm_adjudicated_v1":
            # Structural provenance only: no numeric acceptance rule is applied.
            # Even an internal caller must present the actual durable E verdict.
            from proteinrsi.llm_evaluation import load_evaluation_plan
            detail = result.details or {}
            plan_ref, verdict_ref = detail.get("evaluation_plan_ref"), detail.get("evaluation_verdict_ref")
            if not isinstance(verdict_ref, str) or not verdict_ref.startswith("evaluation_verdicts/"):
                raise Conflict("Adoption requires the recorded E verdict")
            plan = load_evaluation_plan(self.store, plan_ref)
            verdict = self.store.get("evaluation_verdicts", verdict_ref.split("/", 1)[1])
            inputs = self.store.get("evaluation_trial_inputs", plan["evaluation_id"], {})
            if (not verdict or verdict.get("result") != result.model_dump(mode="json")
                    or verdict.get("plan_ref") != plan_ref or plan["target"] != patch.target
                    or inputs.get("context", {}).get("patch") != patch.model_dump(mode="json")):
                raise Conflict("Evaluation verdict is not bound to this candidate and frozen plan")
        with self.store.transaction():
            if result.decision == "accepted":
                if self.enabled:
                    record = self.store.get("method_candidates", patch.patch_id)
                    snapshot = self._validated_snapshot(patch.target, record["candidate_snapshot_ref"])
                    if (snapshot["definition"] != candidate.model_dump(mode="json")
                            or TYPES[patch.target].model_validate(state[patch.target]).version != patch.base_version):
                        raise Conflict("Evaluated candidate or parent differs from the staged version")
                    self._switch(state, patch.target, record["candidate_snapshot_ref"],
                        reason=result.reason, operator="trusted-evaluator", action="adopt",
                        evaluation_ref=evaluation_ref)
                else:
                    state[patch.target] = candidate.model_dump()
                self.store.put(patch.target+"_versions", candidate.version, candidate.model_dump(), immutable=True)
            self.finish(state, patch, result.decision,
                        detail={"evaluation_ref": evaluation_ref, "result": result.model_dump()})

    def _validated_snapshot(self, target: Target, ref: str) -> dict:
        record = self.store.get("method_snapshots", ref)
        if not record or "method-" + digest(record) != ref or record["target"] != target:
            raise Conflict("Unknown or modified method snapshot")
        assets = self.store.get("method_assets", record["source_ref"])
        if not assets or digest(assets) != record["source_ref"]:
            raise Conflict("Missing or modified method source bundle")
        if record["source_ref"] != digest(_packaged_source()):
            raise Conflict("Executable/dependencies differ; use the original environment, not silent migration")
        if record.get("acceptance_policy") != self.store.get("configuration", "acceptance_policy"):
            raise Conflict("Acceptance policy differs from the frozen method snapshot")
        configuration = {name: self.store.get("configuration", name) for name in CONFIG_KEYS}
        if record["configuration"] != configuration:
            raise Conflict("Method configuration differs from the frozen snapshot")
        return record

    def _switch(self, state: dict, target: Target, ref: str, *, reason: str, operator: str,
                action: str, evaluation_ref: str | None = None) -> None:
        record = self._validated_snapshot(target, ref)
        definition = TYPES[target].model_validate(record["definition"])
        previous = TYPES[target].model_validate(state[target]).version
        if previous == definition.version:
            raise Conflict("The requested method is already active")
        state[target] = definition.model_dump()
        state["method_governance"]["active"][target] = ref
        event = {"target": target, "from_version": previous, "to_version": definition.version,
            "snapshot_ref": ref, "round": state["round_index"], "action": action,
            "operator": operator, "reason": reason, "evaluation_ref": evaluation_ref,
            "sequence": len(self.store.all("method_switches")) + 1,
            "evidence_version": self.campaign.view(state).evidence_version,
            "budget": self.store.usage()}
        self.store.put("method_switches", digest(event), event, immutable=True)
        activation_key = target+"/"+definition.version
        if self.store.get("method_activations", activation_key) is None:
            self.store.put("method_activations", activation_key,
                {"snapshot_ref": ref, "origin": action, "evaluation_ref": evaluation_ref}, immutable=True)
        self.store.event("method_version_switched", event)

    def failure(self, state: dict, patch: Patch, exc: Exception) -> bool:
        """Return True only when failure is definite enough to continue the baseline.

        Unknown exceptions and interrupted/uncertain external calls stay blocked.
        The caller re-raises them; no new request or reservation is manufactured.
        """
        if not self.enabled:
            return False
        from proteinrsi.recovery import is_provider_paused
        if is_provider_paused(exc):
            return False  # A bounded provider pause is neither rejection nor uncertain execution.
        from proteinrsi.llm import LLMError
        from proteinrsi.replay.broker import WorkerExecutionError
        definite = isinstance(exc, (ValueError, PermissionError, BudgetExceeded, SchemaError))
        if isinstance(exc, WorkerExecutionError):
            definite = exc.error_type in {"ValueError", "ValidationError", "ContractError", "PermissionError", "BudgetExceeded"}
        # A completed but invalid provider response is a definite failed proposal.
        if isinstance(exc, LLMError):
            definite = True
        uncertain_types = {"ReadTimeout", "WriteTimeout", "ReadError", "WriteError",
            "RemoteProtocolError", "TimeoutError", "TimeoutExpired", "ConnectionError"}
        boundary = self.store.get("method_call_boundaries", patch.patch_id, {})
        for namespace in CALL_NAMESPACES:
            for key, call in self.store.all(namespace).items():
                if boundary.get(namespace, {}).get(key) == digest(call):
                    continue
                if call.get("state") in {"started", "running", "uncertain"}:
                    definite = False
                if call.get("state") == "failed" and call.get("error_type") in uncertain_types:
                    definite = False
        llm = getattr(self.campaign.team, "llm", None)
        detail = {"error_type": type(exc).__name__,
            "reason": redact(str(exc), (getattr(llm, "api_key", ""),))[:500]}
        if definite:
            self.finish(state, patch, "failed", detail=detail)
        else:
            with self.store.transaction():
                self.transition(state, patch, "blocked", detail)
                self.store.put("campaign", "state", state)
        return definite

    def rollback(self, target: Target, version: str, *, operator: str, reason: str) -> None:
        self._operator(operator, reason)
        if target not in TYPES:
            raise ValueError("Unknown method target")
        with self.store.lock(), self.store.transaction():
            state = self.campaign.state
            self._require_enabled()
            self.assert_idle(state)
            activation = self.store.get("method_activations", target+"/"+version)
            if activation is None:
                raise Conflict("Rollback is restricted to previously active versions in this campaign")
            self._switch(state, target, activation["snapshot_ref"], operator=operator,
                         reason=reason, action="rollback")
            # Do not immediately undo an operator's rollback with another automatic patch.
            state["last_patch_round"] = state["round_index"]
            state["considered_round"] = state["round_index"]
            self.store.put("campaign", "state", state)

    def abandon(self, patch_id: str, *, operator: str, reason: str,
                acknowledge_uncertain: bool = False) -> None:
        self._operator(operator, reason)
        with self.store.lock(), self.store.transaction():
            self._require_enabled()
            state = self.campaign.state
            if state["status"] != "ready" or state["pending_batch"]:
                raise Conflict("Submitted/prepared batches must keep their original method attribution")
            pending = next((Patch.model_validate(state[k]) for k in PENDING.values() if state[k]), None)
            if pending is None or pending.patch_id != patch_id:
                raise Conflict("Candidate is not pending")
            status = self.store.get("method_candidate_states", patch_id)["status"]
            if status in {"blocked", "validating"} and not acknowledge_uncertain:
                raise Conflict("Confirm that external work has been reconciled/stopped; no automatic cancellation")
            self.finish(state, pending, "cancelled", detail={"operator": operator, "reason": reason,
                "acknowledge_uncertain": acknowledge_uncertain})
            state["considered_round"] = state["round_index"]
            self.store.put("campaign", "state", state)

    def rejected_proposal(self, state: dict, exc: Exception) -> None:
        if not self.enabled:
            return
        with self.store.transaction():
            record = {"round": state["round_index"], "meta": MetaPolicy.model_validate(state["meta"]).version,
                "evidence_version": self.campaign.view(state).evidence_version, "error_type": type(exc).__name__}
            key = digest(record)
            if self.store.get("method_proposal_failures", key) is None:
                self.store.put("method_proposal_failures", key, record, immutable=True)
                self._count_failure(state, "invalid_or_failed_proposal")
                self.store.put("campaign", "state", state)

    def resume(self, *, operator: str, reason: str) -> None:
        self._operator(operator, reason)
        with self.store.lock(), self.store.transaction():
            self._require_enabled()
            state = self.campaign.state
            self.assert_idle(state)
            control = state["method_governance"]
            control.update(paused=False, pause_reason=None, consecutive_failures=0)
            self.store.put("campaign", "state", state)
            self.store.event("method_improvement_resumed", {"operator": operator, "reason": reason})
            # Administrative MetaPolicy.enabled remains unchanged.

    def bind_batch(self, state: dict, batch) -> None:
        """Freeze actual per-arm provenance, including unadopted Meta descendants."""
        if not self.enabled:
            return
        workflows = {Workflow.model_validate(state["workflow"]).version: state["workflow"]}
        metas = {MetaPolicy.model_validate(state["meta"]).version: state["meta"]}
        trial = self.store.get("trials", batch.batch_id, {})
        if trial.get("target") == "meta":
            for definition in trial["descendants"].values():
                workflows[Workflow.model_validate(definition).version] = definition
            for name in ("baseline_meta", "challenger_meta"):
                definition = trial[name]
                metas[MetaPolicy.model_validate(definition).version] = definition
        elif trial.get("challenger"):
            definition = trial["challenger"]
            workflows[Workflow.model_validate(definition).version] = definition
        llm = getattr(self.campaign.team, "llm", None)
        record = {"batch_id": batch.batch_id, "evidence_version": batch.evidence_version,
            **({"evaluation_plan_ref": trial["evaluation_plan_ref"]}
               if trial.get("evaluation_plan_ref") else {}),
            "workflow_snapshots": {version: self._snapshot("workflow", Workflow.model_validate(w))
                for version, w in workflows.items()},
            "meta_snapshots": {version: self._snapshot("meta", MetaPolicy.model_validate(m))
                for version, m in metas.items()},
            "samples": {sample.sample_id: sample.workflow_version for sample in batch.samples},
            "research_run_refs": [key for key, run in self.store.all("research_runs").items()
                if run.get("evidence_version") == batch.evidence_version and run.get("round") == batch.round_index],
            "protein_snapshot": self.store.get("protein_backend", "snapshot"),
            "llm": {"model": getattr(llm, "model", None), "base_url": getattr(llm, "base_url", None),
                    "cache_settings": getattr(llm, "cache_settings", {})}}
        self.store.put("batch_method_bindings", batch.batch_id, record, immutable=True)

    def visible_history(self, limit: int = 12) -> list[dict]:
        """Bounded, task-local outcomes; exclude live candidates from both trial arms."""
        if not self.enabled:
            return []
        records = self.store.all("method_candidates")
        statuses = self.store.all("method_candidate_states")
        history = []
        for patch_id, candidate in sorted(records.items(), key=lambda item: (item[1]["round"], item[0])):
            state = statuses.get(patch_id, {})
            if state.get("status") not in TERMINAL:
                continue
            transition = self.store.get("method_transitions", state["transition_ref"])
            history.append({"patch_id": patch_id, "target": candidate["patch"]["target"],
                "round": candidate["round"], "changes": candidate["patch"]["changes"],
                "hypothesis": candidate["patch"]["hypothesis"], "status": state["status"],
                "outcome": transition["detail"], "scope": "current_campaign_only", "transfer_validated": False})
        return history[-limit:]

    def report(self) -> dict:
        return {"enabled": self.enabled,
            "control": self.campaign.state.get("method_governance"),
            "candidates": self.store.all("method_candidates"),
            "evaluation_plans": self.store.all("evaluation_plans"),
            "evaluation_bindings": self.store.all("evaluation_bindings"),
            "evaluation_verdicts": self.store.all("evaluation_verdicts"),
            "candidate_states": self.store.all("method_candidate_states"),
            "switches": sorted(self.store.all("method_switches").values(), key=lambda x: x["sequence"])}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise Conflict("This campaign predates method snapshots; start a new study, do not invent history")

    @staticmethod
    def _operator(operator: str, reason: str) -> None:
        if not operator.strip() or not reason.strip():
            raise ValueError("A nonempty operator and reason are required")
