# SPDX-License-Identifier: MIT
"""Persistent, idempotent campaign controller shared by native and LangGraph runners."""
from __future__ import annotations

import uuid
from typing import Any

import numpy as np

from proteinrsi.agents import FeedbackAnalysis, MetaAgent, Team, skill_text
from proteinrsi.audit import redact, snapshot
from proteinrsi.contracts import (Batch, Candidate, GatePolicy, MetaPolicy, Observation, Patch,
    Sample, TaskKind, TaskSpec, TaskView, Workflow, canonical, digest)
from proteinrsi.improvement import ExperienceMemory, apply_patch, evaluate_trial
from proteinrsi.lab import export_batch
from proteinrsi.llm import LLMError, ProviderPaused
from proteinrsi.storage import BudgetExceeded, Conflict, Store
from proteinrsi.tasks import validate_task, validate_candidate


class Campaign:
    def __init__(self, store: Store, team: Team | None = None, meta_agent: MetaAgent | None = None):
        self.store = store
        self.team = team or Team(store)
        self.meta_agent = meta_agent or MetaAgent(self.team.llm, store)
        self.memory = ExperienceMemory(store)
        from proteinrsi.governance import MethodGovernance
        self.methods = MethodGovernance(self)

    @classmethod
    def initialize(cls, directory: str, task: TaskSpec, *, workflow: Workflow | None = None,
                   meta: MetaPolicy | None = None, gate: GatePolicy | None = None,
                   protein_config=None, local_tools=None, research_config=None, auto_meta_evaluation=True, governance_config=None) -> Campaign:
        validate_task(task)
        store = Store(directory)
        with store.lock(), store.transaction():
            if store.get("campaign", "state") is not None:
                raise Conflict("Campaign already exists; use resume/status, not init")
            workflow, meta, gate = workflow or Workflow(), meta or MetaPolicy(), gate or GatePolicy()
            from proteinrsi.prompting import snapshot_prompts
            snapshot_prompts(store)
            resources = task.budget.model_dump()
            if protein_config is not None:
                from proteinrsi.protein.esmc import ESMCConfig
                protein_config = ESMCConfig.model_validate(protein_config)
                resources["plm_inputs"] = protein_config.max_model_inputs
            store.configure_budget(resources)
            from proteinrsi.governance import GovernancePolicy
            governance = GovernancePolicy.model_validate(governance_config or {})
            store.put("configuration", "method_governance", governance.model_dump(), immutable=True)
            store.put("configuration", "auto_meta_evaluation", {"enabled": auto_meta_evaluation,
                "scope": "current_task_only"}, immutable=True)
            if protein_config is not None:
                store.put("configuration", "protein_model", protein_config.model_dump(), immutable=True)
            if local_tools is not None:
                from proteinrsi.localtools.config import LocalToolsConfig
                local_tools = LocalToolsConfig.model_validate(local_tools)
                store.put("configuration", "local_tools", local_tools.model_dump(mode="json"), immutable=True)
            if research_config is not None:
                from proteinrsi.research.contracts import ResearchConfig
                from proteinrsi.research.knowledge import snapshot_documents
                research_config = ResearchConfig.model_validate(research_config)
                store.put("configuration", "research", research_config.model_dump(), immutable=True)
                snapshot_documents(store)
            state = {"campaign_id": str(uuid.uuid4()), "task": task.model_dump(mode="json"),
                "workflow": workflow.model_dump(), "meta": meta.model_dump(), "gate": gate.model_dump(),
                "round_index": 0, "status": "ready", "observations": [], "history": [],
                "pending_batch": None, "pending_patch": None, "pending_meta": None,
                "last_patch_round": -100, "considered_round": -1, "planning_attempt": 0,
                "execution_semantics": "on-demand-v1", "initialization_done": False,
                "first_round_spent": 0}
            if task.initial_observation_policy == "provided_parent":
                initial = Observation(sample_id="provided-parent", batch_id="provided-initial-evidence",
                    sequence=task.reference_sequence, value=task.initial_parent_measurement.value,
                    metric=task.metric, unit=task.unit, source=task.feedback_source,
                    assay_protocol=task.assay_protocol, qc="valid").model_dump(mode="json")
                state["observations"] = [initial]
                state["initialization_done"] = True
                store.put("configuration", "provided_initial_evidence", {
                    "observations": [initial], "source_ref": task.initial_parent_measurement.source_ref,
                    "charged_queries": 0}, immutable=True)
                store.event("initial_evidence_provided", {"count": 1, "charged_queries": 0})
            store.put("campaign", "state", state)
            store.put("workflow_versions", workflow.version, workflow.model_dump(), immutable=True)
            store.put("meta_versions", meta.version, meta.model_dump(), immutable=True)
            campaign = cls(store)
            campaign.methods.bootstrap(state)
            store.event("campaign_initialized", {"campaign_id": state["campaign_id"],
                                                  "source": task.feedback_source})
        snapshot(store, "initialized", campaign.view())
        return campaign

    @property
    def state(self) -> dict[str, Any]:
        state = self.store.get("campaign", "state")
        if state is None:
            raise ValueError("No campaign; run init first")
        return state

    def view(self, state: dict | None = None, workflow: Workflow | None = None) -> TaskView:
        state = state or self.state
        task = TaskSpec.model_validate(state["task"])
        if (len(task.candidates) > task.proposal_pool_size and task.kind != TaskKind.RANKING
                and task.batch_fill_policy != "full_plate"):
            observed = {o["sequence"] for o in state["observations"]}
            eligible = [s for s in task.candidates if task.repeat_policy == "allow"
                        or (s not in observed and s != task.reference_sequence)]
            rng = np.random.default_rng(task.seed + state["round_index"])
            if len(eligible) > task.proposal_pool_size:
                chosen = rng.choice(len(eligible), size=task.proposal_pool_size, replace=False)
                eligible = [eligible[int(i)] for i in chosen]
            task = task.model_copy(update={"candidates": eligible})
        return TaskView(task=task, round_index=state["round_index"],
            observations=[Observation.model_validate(o) for o in state["observations"]],
            history=state["history"], remaining_wells=self.store.remaining("experimental_wells"),
            workflow=workflow or Workflow.model_validate(state["workflow"]),
            meta=MetaPolicy.model_validate(state["meta"]), experience=self.memory.retrieve(task.kind),
            artifacts=list(self.store.all("artifacts").values()),
            research_context={"workflow_validation_outcomes":
                list(self.store.all("workflow_validation_outcomes").values()),
                **({"method_history": self.methods.visible_history()} if self.methods.enabled else {})})

    def prepare(self) -> Batch | None:
        # Feedback commits before the improver runs. A stopped controller may have
        # persisted that feedback without its C/M decision; finish it before planning
        # the next batch. consider_improvement owns its own lock and rechecks state,
        # so call it outside this method's planning lock.
        state = self.state
        if (state.get("execution_semantics") == "on-demand-v1"
                and state["task"].get("execution_mode", "experimental") == "experimental"
                and state["status"] == "ready" and state["history"]
                and not state["pending_batch"] and not state["pending_patch"] and not state["pending_meta"]
                and state["considered_round"] != state["round_index"]):
            self.methods.assert_plannable(state)
            self.consider_improvement()
        with self.store.lock():
            state = self.state
            if state.get("execution_semantics") != "on-demand-v1":
                raise Conflict("Legacy campaign: keep the old executable or start a new v0.5 campaign; no silent semantic migration")
            if state["pending_batch"]:
                return Batch.model_validate(self.store.get("batches", state["pending_batch"]))
            if state["status"] == "complete":
                return None
            if state["status"] != "ready":
                raise Conflict("Campaign is not ready to plan")
            self.methods.assert_plannable(state)
            view = self.view(state)
            task = view.task
            if task.execution_mode == "computational":
                raise ValueError("Use the computational runner; no experimental batch may be submitted")
            remaining_round = task.batch_size - (state.get("first_round_spent", 0) if state["round_index"] == 0 else 0)
            n = min(remaining_round, view.remaining_wells)
            if state["round_index"] >= task.max_rounds or n <= task.controls_per_batch:
                state["status"] = "complete"
                self.store.put("campaign", "state", state)
                return None
            if task.batch_fill_policy == "full_plate" and n != task.batch_size:
                raise ValueError("Insufficient budget for a whole plate; no partial plate may be submitted")
            if task.initial_observation_policy == "parent_once" and not state.get("initialization_done"):
                batch_id = "b-initial-" + digest({"campaign": state["campaign_id"], "attempt": state["planning_attempt"]})[:16]
                batch = Batch(batch_id=batch_id, campaign_id=state["campaign_id"], round_index=0,
                    evidence_version=view.evidence_version, meta_version=view.meta.version, phase="initialization",
                    samples=[Sample(sample_id=batch_id+"-000", arm="control", workflow_version=view.workflow.version,
                        candidate=Candidate(sequence=task.reference_sequence, source="initial_parent"))])
                with self.store.transaction():
                    self.store.reserve("lab-"+batch_id, "experimental_wells", 1, batch.model_dump())
                    self.store.put("batches", batch_id, batch.model_dump(), immutable=True)
                    self.methods.bind_batch(state, batch)
                    state["pending_batch"], state["status"] = batch_id, "awaiting_approval"
                    self.store.put("campaign", "state", state)
                export_batch(batch, task, self.store.root/"batches"/batch_id)
                self.store.event("initial_parent_prepared", {"batch_id": batch_id})
                return batch
            # Scientific constraints and any explicitly supplied closed library; open design has no replay index.
            full_task = TaskSpec.model_validate(state["task"])
            snapshot(self.store, "round_input", view)
            slots = n - task.controls_per_batch
            chosen = []
            trial_patch = None
            challenger_workflow = None
            meta_trial = None
            excluded = {o.sequence for o in view.observations} | {task.reference_sequence}
            if (state["pending_meta"] and task.kind != TaskKind.AFFINITY
                    and self.store.get("configuration", "auto_meta_evaluation", {}).get("enabled")):
                from proteinrsi.online_meta import prepare_meta_trial
                planned = prepare_meta_trial(self, state, view, slots)
                if planned:
                    chosen, meta_trial = planned
                    trial_patch = Patch.model_validate(meta_trial["patch"])
            baseline = []
            if meta_trial is None:
                if task.batch_fill_policy == "full_plate":
                    from proteinrsi.plate import complete_candidates
                    baseline = complete_candidates(self, view, slots, excluded=excluded)
                else:
                    baseline = self.team.run(view)
                for candidate in baseline:
                    validate_candidate(full_task, candidate)
                measured = {o.sequence for o in view.observations}
                excluded = measured | {task.reference_sequence}
                if task.kind == TaskKind.AFFINITY or task.repeat_policy == "allow":
                    excluded = set()  # Explicit replicate predictions, never changed sequences.
                baseline = [c for c in baseline if c.sequence not in excluded]
                if state["pending_patch"] and task.kind != TaskKind.AFFINITY:
                    patch = Patch.model_validate(state["pending_patch"])
                    challenger_workflow = apply_patch(view.workflow, patch)
                    from proteinrsi.workflow_trial import prepare_workflow_trial
                    chosen, allocation = prepare_workflow_trial(self, state, view, patch,
                        challenger_workflow, baseline, slots, excluded)
                    if chosen:
                        trial_patch = patch
                if not trial_patch:
                    chosen = [(c, "baseline", view.workflow.version) for c in baseline[:slots]]
            if task.batch_fill_policy == "full_plate" and len(chosen) < slots:
                from proteinrsi.plate import complete_candidates
                occupied = excluded | {c.sequence for c, _, _ in chosen}
                extras = complete_candidates(self, view, slots - len(chosen), excluded=occupied,
                    initial=[c for c in baseline if c.sequence not in occupied])
                chosen.extend((c, "research" if trial_patch else "baseline", view.workflow.version) for c in extras)
            if task.batch_fill_policy == "full_plate" and len(chosen) != slots:
                raise ValueError("Full plate requires the exact number of unique research candidates")
            if not chosen:
                if full_task.candidate_access == "open" or set(full_task.candidates) - excluded:
                    self.store.event("no_valid_proposal", {"round": state["round_index"]})
                    raise ValueError("No new valid proposal returned; search-space exhaustion has not been established")
                state["status"] = "complete"
                self.store.event("candidate_space_exhausted", {"round": state["round_index"]})
                self.store.put("campaign", "state", state)
                return None
            for _ in range(task.controls_per_batch):
                chosen.append((Candidate(sequence=task.reference_sequence, source="control"),
                               "control", view.workflow.version))
            payload = {"campaign": state["campaign_id"], "round": view.round_index,
                       "evidence": view.evidence_version, "attempt": state.get("planning_attempt", 0), "choices": [(c.model_dump(), a, w) for c, a, w in chosen]}
            batch_id = "b-" + digest(payload)[:20]
            samples = [Sample(sample_id=f"{batch_id}-{i:03d}", candidate=c, arm=a,
                              workflow_version=w, replicate=i + 1 if a == "control" else 1)
                       for i, (c, a, w) in enumerate(chosen)]
            batch = Batch(batch_id=batch_id, campaign_id=state["campaign_id"], round_index=view.round_index,
                evidence_version=view.evidence_version, meta_version=view.meta.version, samples=samples,
                patch_id=trial_patch.patch_id if trial_patch else None)
            with self.store.transaction():
                self.store.reserve("lab-" + batch_id, "experimental_wells", len(samples), batch.model_dump())
                self.store.put("batches", batch_id, batch.model_dump(), immutable=True)
                if trial_patch:
                    self.store.put("trials", batch_id, meta_trial or {"target": "workflow",
                        "patch": trial_patch.model_dump(mode="json"),
                        "challenger": challenger_workflow.model_dump(), "gate": state["gate"], "allocation": allocation}, immutable=True)
                state["pending_batch"], state["status"] = batch_id, "awaiting_approval"
                if trial_patch:
                    self.methods.transition(state, trial_patch, "awaiting_approval", {"batch_id": batch_id})
                self.methods.bind_batch(state, batch)
                self.store.put("campaign", "state", state)
            export_batch(batch, task, self.store.root / "batches" / batch_id)
            self.store.event("batch_prepared", {"batch_id": batch_id, "wells": len(samples),
                "workflow": view.workflow.version, "meta": view.meta.version, "patch_id": batch.patch_id})
            snapshot(self.store, "awaiting_approval", self.view(state), batch_id=batch_id)
            return batch

    def approve(self, batch_id: str, *, operator: str) -> None:
        if not operator.strip():
            raise ValueError("Record an approving operator")
        with self.store.lock(), self.store.transaction():
            state = self.state
            if state["pending_batch"] != batch_id:
                raise Conflict("Batch is not current")
            if state["status"] == "awaiting_results":
                return
            if state["status"] != "awaiting_approval":
                raise Conflict("Batch is not awaiting approval")
            task = TaskSpec.model_validate(state["task"])
            if task.batch_fill_policy == "full_plate":
                batch = Batch.model_validate(self.store.get("batches", batch_id))
                if len(batch.samples) != task.batch_size:
                    raise ValueError("Cannot approve an incomplete plate")
                research = [x.candidate.sequence for x in batch.samples if x.arm != "control"]
                if len(research) != len(set(research)):
                    raise ValueError("Cannot fill a plate with duplicate research candidates")
            batch = Batch.model_validate(self.store.get("batches", batch_id))
            if batch.patch_id:
                patch = Patch.model_validate(self.store.get("trials", batch_id)["patch"])
                self.methods.transition(state, patch, "awaiting_results", {"batch_id": batch_id})
            self.store.settle("lab-" + batch_id)
            state["status"] = "awaiting_results"
            self.store.put("campaign", "state", state)
            self.store.event("batch_approved", {"batch_id": batch_id, "operator": operator})

    def cancel_prepared(self, batch_id: str, *, operator: str) -> None:
        if not operator.strip():
            raise ValueError("Record the cancelling operator")
        with self.store.lock(), self.store.transaction():
            state = self.state
            if state["pending_batch"] != batch_id or state["status"] != "awaiting_approval":
                raise Conflict("Only an unapproved batch can be cancelled/refunded")
            batch = Batch.model_validate(self.store.get("batches", batch_id))
            if batch.patch_id:
                patch = Patch.model_validate(self.store.get("trials", batch_id)["patch"])
                self.methods.transition(state, patch, "staged", {"cancelled_batch": batch_id})
            self.store.settle("lab-" + batch_id, release=True)
            state["pending_batch"], state["status"] = None, "ready"
            state["planning_attempt"] = state.get("planning_attempt", 0) + 1
            self.store.put("campaign", "state", state)
            self.store.event("batch_cancelled", {"batch_id": batch_id, "operator": operator})

    def ingest(self, observations: list[Observation]) -> dict:
        if not observations:
            raise ValueError("No measurements")
        with self.store.lock():
            state = self.state
            task = TaskSpec.model_validate(state["task"])
            batch_id = observations[0].batch_id
            saved = self.store.get("batches", batch_id)
            if saved is None:
                raise ValueError("Unknown batch")
            batch = Batch.model_validate(saved)
            expected = {s.sample_id: s.candidate.sequence for s in batch.samples}
            if (len(observations) != len(expected)
                    or {o.sample_id for o in observations} != set(expected)):
                raise ValueError("Import one final row per sample; partial results remain in the laboratory")
            for o in observations:
                if (o.batch_id != batch_id or o.sequence != expected[o.sample_id] or o.metric != task.metric
                    or o.unit != task.unit or o.source != task.feedback_source or o.assay_protocol != task.assay_protocol):
                    raise ValueError("Measurement identity, source, metric, unit or assay mismatch")
            normalized = sorted([o.model_dump(mode="json") for o in observations], key=lambda o: o["sample_id"])
            prior = self.store.get("measurements", batch_id)
            if prior is not None:
                if canonical(prior) != canonical(normalized):
                    raise Conflict("Previously imported measurements cannot be overwritten")
                if state["pending_batch"] != batch_id:
                    return state  # Complete idempotent replay, not a second round.
            if state["pending_batch"] != batch_id or state["status"] != "awaiting_results":
                raise Conflict("Only approved current batches accept results")
            self.store.put("measurements", batch_id, normalized, immutable=True)
            with self.store.transaction():
                by_id = {o.sample_id: o for o in observations}
                errors = [abs(s.candidate.predicted_value - by_id[s.sample_id].value) for s in batch.samples
                          if s.arm != "control" and s.candidate.predicted_value is not None
                          and by_id[s.sample_id].qc == "valid"]
                summary = {"round": state["round_index"], "batch_id": batch_id,
                    "qc_failure_fraction": sum(o.qc in {"failed", "inconclusive"} for o in observations) / max(1, sum(o.qc != "unavailable" for o in observations)),
                    "unavailable_queries": sum(o.qc == "unavailable" for o in observations),
                    "submitted_wells": len(batch.samples), "plate_capacity": task.batch_size,
                    "plate_utilization": len(batch.samples) / task.batch_size,
                    "prediction_mae": float(np.mean(errors)) if errors else None,
                    "workflow": Workflow.model_validate(state["workflow"]).version, "meta": batch.meta_version}
                state["observations"].extend(normalized)
                if batch.phase == "initialization":
                    state["initialization_done"] = True
                    state["first_round_spent"] = len(batch.samples)
                    state["pending_batch"] = None
                    state["status"] = "ready" if self.store.remaining("experimental_wells") else "complete"
                    self.store.put("campaign", "state", state)
                    self.store.event("initial_parent_revealed", {"batch_id": batch_id, "charged_queries": len(batch.samples),
                        "round_index_unchanged": 0})
                    return state
                if batch.patch_id:
                    trial = self.store.get("trials", batch_id)
                    patch = Patch.model_validate(trial["patch"])
                    result = evaluate_trial(batch, observations, GatePolicy.model_validate(trial["gate"]),
                                            direction=task.direction)
                    self.store.put("trial_results", batch_id, result.model_dump(), immutable=True)
                    self.memory.record(patch, result, campaign_id=state["campaign_id"],
                        observations=len(state["observations"]) - len(normalized), source=task.feedback_source)
                    target = trial.get("target", "workflow")
                    new = (MetaPolicy.model_validate(trial["challenger_meta"]) if target == "meta"
                           else Workflow.model_validate(trial["challenger"]))
                    self.methods.complete(state, patch, result, new, evaluation_ref="trial_results/"+batch_id)
                    summary["trial"] = {"target": target, **result.model_dump()}
                    if target == "meta":
                        evaluation_id = trial["evaluation_id"]
                        report = {**trial, "batch_id": batch_id, "result": result.model_dump(),
                            "charged_queries": sum(x.arm in {"baseline", "challenger"} for x in batch.samples),
                            "total_plate_queries": len(batch.samples), "promoted": result.decision == "accepted"}
                        self.store.put("meta_evaluations", evaluation_id, report, immutable=True)
                        attempt = self.store.get("meta_online_attempts", evaluation_id)
                        self.store.put("meta_online_attempts", evaluation_id, {**attempt, "state": "completed"})
                    self.store.event(target+"_trial_completed", {"batch_id": batch_id, **result.model_dump()})
                research_config = self.store.get("configuration", "research", {})
                if research_config.get("enabled"):
                    from proteinrsi.research.analysis import persist_analysis
                    # Lightweight QC reporting only; no automatic scientific model fitting.
                    analysis = persist_analysis(self.view(state), self.store)
                    # Trace references are scoped to the evidence used for this submitted batch.
                    trace_summaries = [{"run_id": r["run_id"], "workflow": r["workflow_version"],
                        "status": r["status"], "plan": r["plan"], "revisions": r["revisions"]}
                        for r in self.store.all("research_runs").values()
                        if r["evidence_version"] == batch.evidence_version and r["round"] == batch.round_index]
                    summary["research_analysis"] = {"artifact_ref": analysis["artifact_ref"],
                        "qc": analysis["qc"], "prediction_errors": {k: v for k, v in analysis["prediction_errors"].items() if k != "rows"}}
                    summary["research_traces"] = trace_summaries
                state["history"].append(summary)
                state["round_index"] += 1
                state["pending_batch"], state["status"] = None, "ready"
                if state["round_index"] >= task.max_rounds or self.store.remaining("experimental_wells") <= task.controls_per_batch:
                    state["status"] = "complete"
                self.store.put("campaign", "state", state)
                self.store.event("feedback_ingested", summary)
                snapshot(self.store, "feedback_committed", self.view(state))
                if state["status"] == "complete":
                    self.store.event("campaign_completed", {"rounds": state["round_index"]})
        # Measurements are committed even if subsequent LLM/metacognitive work fails.
        if state["status"] == "ready":
            self.consider_improvement()
        return self.state

    def consider_improvement(self) -> None:
        with self.store.lock():
            state = self.state
            if (state["status"] != "ready" or state["pending_patch"] or state["pending_meta"]
                    or state["pending_batch"] or not state["history"]):
                return
            if state["considered_round"] == state["round_index"]:
                return
            # Direct callers must obey the same frozen-engine check as prepare.
            self.methods.assert_plannable(state)
            view = self.view(state)
            try:
                self._checkpoint_feedback(state, view)
                # Governance pauses method improvement, not analysis of fresh
                # experimental evidence. Leave M unconsidered so an explicit
                # idle-boundary resume can use the saved C result.
                if self.methods.paused(state):
                    return
                view = self.view(state)
                snapshot(self.store, "improver_input", view)
                response = self.meta_agent.propose(view, state["last_patch_round"])
                if response.patch:
                    with self.store.transaction():
                        self._stage_patch(state, response.patch)
                        self.store.put("campaign", "state", state)
                self.store.event("meta_decision", {"round": view.round_index, "meta": view.meta.version,
                                                   "response": response.model_dump(mode="json")})
            except ProviderPaused:
                raise  # Preserve the unconsidered round and exact request for an authorized retry.
            except (BudgetExceeded, LLMError, ValueError, PermissionError) as exc:
                self.methods.rejected_proposal(state, exc)
                self.store.event("meta_change_blocked", {"round": view.round_index,
                                                         "error_type": type(exc).__name__})
            state["considered_round"] = state["round_index"]
            self.store.put("campaign", "state", state)

    def _checkpoint_feedback(self, state: dict, view: TaskView) -> None:
        """Persist actual C success before M; never recreate historical feedback.

        Inputs are frozen separately so a provider pause (or a crash after the
        provider receipt but before this commit) reuses the exact C request/cache.
        These controller-only records are not worker-writable method proposals.
        """
        identity = {"campaign_id": state["campaign_id"], "round": view.round_index,
                    "evidence_version": view.evidence_version}
        key = "feedback-" + digest(identity)
        saved = self.store.get("feedback_inputs", key)
        result = self.store.get("feedback_results", key)
        previous = state["history"][-1].get("analyst_feedback")
        if result is not None:
            if (saved is None or result["input_digest"] != digest(saved)
                    or result["identity"] != identity or result["feedback"] != previous):
                raise Conflict("Feedback checkpoint differs from its input or committed history")
            return
        if previous is not None:
            # Older states may already contain a real successful C response.
            # Reuse it without inventing a checkpoint or rerunning that evidence.
            if saved is not None:
                raise Conflict("Incomplete feedback checkpoint already has committed history")
            FeedbackAnalysis.model_validate(previous)
            return
        from proteinrsi.prompting import prompt_version
        llm = self.team.llm
        provenance = {"workflow": view.workflow.version, "meta": view.meta.version,
            "method_snapshots": state.get("method_governance", {}).get("active", {}),
            "prompts": prompt_version(self.store), "backend": "llm" if llm else "deterministic",
            "model": getattr(llm, "model", None), "url": getattr(llm, "base_url", None),
            "max_output_tokens": getattr(llm, "max_tokens", None),
            "client": getattr(llm, "cache_settings", {})}
        # Keep exact semantic identity even if misconfigured model/URL/settings
        # contain a credential. Never persist that secret in display provenance,
        # nor fingerprint ordinary Authorization credentials (rotation is safe).
        provenance_digest = digest(provenance)
        if saved is None:
            saved = {"schema_version": 1, "identity": identity,
                     "provenance": redact(provenance, (getattr(llm, "api_key", ""),)),
                     "provenance_digest": provenance_digest,
                     "view": view.model_dump(mode="json")}
            self.store.put("feedback_inputs", key, saved, immutable=True)
        elif saved["identity"] != identity or saved["provenance_digest"] != provenance_digest:
            raise Conflict("Unfinished feedback must use its original method and provider")
        feedback = self.team.analyst.feedback(TaskView.model_validate(saved["view"]))
        result = {"identity": identity, "input_digest": digest(saved),
                  "feedback": feedback.model_dump(mode="json")}
        with self.store.transaction():
            self.store.put("feedback_results", key, result, immutable=True)
            state["history"][-1]["analyst_feedback"] = result["feedback"]
            self.store.put("campaign", "state", state)
            self.store.event("analyst_feedback_completed", {**identity,
                "feedback_input_ref": key, "feedback_ref": key})

    def _stage_patch(self, state: dict, patch: Patch) -> None:
        task = TaskSpec.model_validate(state["task"])
        if patch.task_kind != task.kind:
            raise ValueError("Patch scope does not match task")
        current = Workflow.model_validate(state["workflow"]) if patch.target == "workflow" else MetaPolicy.model_validate(state["meta"])
        candidate = apply_patch(current, patch)
        if patch.target == "workflow":
            self.team.bind_tools(self.view(state, candidate))
            self.team.tools.catalog(task, candidate.tool_names)
            skill_text(candidate.skill_names)
        if self.store.get("patches", patch.patch_id):
            return  # Do not repeatedly test the same patch on the same evidence/claim.
        self.methods.stage(state, patch, candidate)
        self.store.put("patch_contexts", patch.patch_id, {"previous_patch_round": state["last_patch_round"],
            "evidence_version": self.view(state).evidence_version}, immutable=True)
        self.store.event("patch_staged", {"patch": patch.model_dump(mode="json")})
        state["pending_patch" if patch.target == "workflow" else "pending_meta"] = patch.model_dump(mode="json")
        state["last_patch_round"] = state["round_index"]
        self.store.put("patches", patch.patch_id, patch.model_dump(mode="json"), immutable=True)

    def stage_patch(self, patch: Patch) -> None:
        """Operator/API entrypoint; still validates scope, base version, fields, and trial requirements."""
        with self.store.lock(), self.store.transaction():
            state = self.state
            if state["status"] != "ready" or state["pending_patch"] or state["pending_meta"]:
                raise Conflict("Patches may only be staged at an idle round boundary")
            self._stage_patch(state, patch)
            self.store.put("campaign", "state", state)

    def report(self) -> dict:
        state = self.state
        task = TaskSpec.model_validate(state["task"])
        values = [o["value"] for o in state["observations"] if o["qc"] == "valid"]
        best = (max(values) if task.direction == "maximize" else min(values)) if values else None
        from proteinrsi.reporting import study_details
        return {**study_details(self), "campaign_id": state["campaign_id"], "status": state["status"],
                "completed_rounds": state["round_index"],
                "research_config": self.store.get("configuration", "research"),
                "protein_model": self.store.get("configuration", "protein_model"),
                "protein_snapshot": self.store.get("protein_backend", "snapshot"), "evidence_source": task.feedback_source,
                "workflow_version": Workflow.model_validate(state["workflow"]).version,
                "meta_version": MetaPolicy.model_validate(state["meta"]).version,
                "best_measured_value": best, "metric": task.metric, "unit": task.unit,
                "budget": self.store.usage(), "pending_batch": state["pending_batch"],
                "pending_workflow_patch": state["pending_patch"], "pending_meta_patch": state["pending_meta"],
                "workflow_trials": self.store.all("trial_results"),
                "meta_evaluations": self.store.all("meta_evaluations"),
                "method_governance": self.methods.report(),
                "execution_mode": task.execution_mode,
                "computational_iterations": self.store.all("computational_iterations"),
                "final_computational_candidates": (state["history"][-1].get("candidates", [])
                    if task.execution_mode == "computational" and state["history"] else [])}
