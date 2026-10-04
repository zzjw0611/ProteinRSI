# SPDX-License-Identifier: MIT
"""Persistent, idempotent campaign controller shared by native and LangGraph runners."""
from __future__ import annotations

import uuid
from typing import Any

import numpy as np

from proteinrsi.agents import MetaAgent, Team, skill_text
from proteinrsi.contracts import (Batch, Candidate, GatePolicy, MetaPolicy, Observation, Patch,
    Sample, TaskKind, TaskSpec, TaskView, Workflow, canonical, digest)
from proteinrsi.improvement import ExperienceMemory, apply_patch, evaluate_trial
from proteinrsi.lab import export_batch
from proteinrsi.llm import LLMError
from proteinrsi.storage import BudgetExceeded, Conflict, Store
from proteinrsi.tasks import validate_task


class Campaign:
    def __init__(self, store: Store, team: Team | None = None, meta_agent: MetaAgent | None = None):
        self.store = store
        self.team = team or Team(store)
        self.meta_agent = meta_agent or MetaAgent(self.team.llm)
        self.memory = ExperienceMemory(store)

    @classmethod
    def initialize(cls, directory: str, task: TaskSpec, *, workflow: Workflow | None = None,
                   meta: MetaPolicy | None = None, gate: GatePolicy | None = None,
                   protein_config=None, local_tools=None, research_config=None) -> Campaign:
        validate_task(task)
        store = Store(directory)
        with store.lock():
            if store.get("campaign", "state") is not None:
                raise Conflict("Campaign already exists; use resume/status, not init")
            workflow, meta, gate = workflow or Workflow(), meta or MetaPolicy(), gate or GatePolicy()
            resources = task.budget.model_dump()
            if protein_config is not None:
                from proteinrsi.protein.esmc import ESMCConfig
                protein_config = ESMCConfig.model_validate(protein_config)
                resources["plm_inputs"] = protein_config.max_model_inputs
            store.configure_budget(resources)
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
                "last_patch_round": -100, "considered_round": -1, "planning_attempt": 0}
            store.put("campaign", "state", state)
            store.put("workflow_versions", workflow.version, workflow.model_dump(), immutable=True)
            store.put("meta_versions", meta.version, meta.model_dump(), immutable=True)
            store.event("campaign_initialized", {"campaign_id": state["campaign_id"],
                                                  "source": task.feedback_source})
        return cls(store)

    @property
    def state(self) -> dict[str, Any]:
        state = self.store.get("campaign", "state")
        if state is None:
            raise ValueError("No campaign; run init first")
        return state

    def view(self, state: dict | None = None, workflow: Workflow | None = None) -> TaskView:
        state = state or self.state
        task = TaskSpec.model_validate(state["task"])
        if len(task.candidates) > task.proposal_pool_size:
            observed = {o["sequence"] for o in state["observations"]}
            eligible = [s for s in task.candidates if s not in observed and s != task.reference_sequence]
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
            artifacts=list(self.store.all("artifacts").values()))

    def prepare(self) -> Batch | None:
        with self.store.lock():
            state = self.state
            if state["pending_batch"]:
                return Batch.model_validate(self.store.get("batches", state["pending_batch"]))
            if state["status"] == "complete":
                return None
            if state["status"] != "ready":
                raise Conflict("Campaign is not ready to plan")
            view = self.view(state)
            task = view.task
            n = min(task.batch_size, view.remaining_wells)
            if state["round_index"] >= task.max_rounds or n <= task.controls_per_batch:
                state["status"] = "complete"
                self.store.put("campaign", "state", state)
                return None
            baseline = self.team.run(view)
            measured = {o.sequence for o in view.observations}
            excluded = measured | {task.reference_sequence}
            if task.kind == TaskKind.AFFINITY:
                excluded = set()  # Explicit replicate predictions, never changed sequences.
            baseline = [c for c in baseline if c.sequence not in excluded]
            slots = n - task.controls_per_batch
            chosen: list[tuple[Candidate, str, str]] = []
            trial_patch: Patch | None = None
            challenger_workflow = None
            if state["pending_patch"] and task.kind != TaskKind.AFFINITY:
                patch = Patch.model_validate(state["pending_patch"])
                challenger_workflow = apply_patch(view.workflow, patch)
                gate = GatePolicy.model_validate(state["gate"])
                per_arm = slots // 2
                if per_arm >= gate.min_per_arm:
                    challenger = self.team.run(self.view(state, challenger_workflow))
                    challenger = [c for c in challenger if c.sequence not in excluded]
                    # Equal budgets, shared evidence, disjoint submitted variants. Alternate
                    # which policy wins a collision by a prespecified round-based order.
                    arms = {"baseline": baseline, "challenger": challenger}
                    used = set(excluded)
                    for _ in range(per_arm):
                        order = ["baseline", "challenger"]
                        if (task.seed + view.round_index) % 2:
                            order.reverse()
                        for arm in order:
                            candidate = next((c for c in arms[arm] if c.sequence not in used), None)
                            if candidate is None:
                                break
                            used.add(candidate.sequence)
                            version = view.workflow.version if arm == "baseline" else challenger_workflow.version
                            chosen.append((candidate, arm, version))
                    if len(chosen) == 2 * per_arm:
                        trial_patch = patch
                    else:
                        chosen = []
            if not trial_patch:
                chosen = [(c, "baseline", view.workflow.version) for c in baseline[:slots]]
            if not chosen:
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
            self.store.reserve("lab-" + batch_id, "experimental_wells", len(samples), batch.model_dump())
            self.store.put("batches", batch_id, batch.model_dump(), immutable=True)
            if trial_patch:
                self.store.put("trials", batch_id, {"patch": trial_patch.model_dump(mode="json"),
                    "challenger": challenger_workflow.model_dump(), "gate": state["gate"]}, immutable=True)
            state["pending_batch"], state["status"] = batch_id, "awaiting_approval"
            self.store.put("campaign", "state", state)
            export_batch(batch, task, self.store.root / "batches" / batch_id)
            self.store.event("batch_prepared", {"batch_id": batch_id, "wells": len(samples),
                "workflow": view.workflow.version, "meta": view.meta.version, "patch_id": batch.patch_id})
            return batch

    def approve(self, batch_id: str, *, operator: str) -> None:
        if not operator.strip():
            raise ValueError("Record an approving operator")
        with self.store.lock():
            state = self.state
            if state["pending_batch"] != batch_id:
                raise Conflict("Batch is not current")
            if state["status"] == "awaiting_results":
                return
            if state["status"] != "awaiting_approval":
                raise Conflict("Batch is not awaiting approval")
            self.store.settle("lab-" + batch_id)
            state["status"] = "awaiting_results"
            self.store.put("campaign", "state", state)
            self.store.event("batch_approved", {"batch_id": batch_id, "operator": operator})

    def cancel_prepared(self, batch_id: str, *, operator: str) -> None:
        if not operator.strip():
            raise ValueError("Record the cancelling operator")
        with self.store.lock():
            state = self.state
            if state["pending_batch"] != batch_id or state["status"] != "awaiting_approval":
                raise Conflict("Only an unapproved batch can be cancelled/refunded")
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
            by_id = {o.sample_id: o for o in observations}
            errors = [abs(s.candidate.predicted_value - by_id[s.sample_id].value) for s in batch.samples
                      if s.arm != "control" and s.candidate.predicted_value is not None
                      and by_id[s.sample_id].qc == "valid"]
            summary = {"round": state["round_index"], "batch_id": batch_id,
                "qc_failure_fraction": sum(o.qc != "valid" for o in observations) / len(observations),
                "prediction_mae": float(np.mean(errors)) if errors else None,
                "workflow": Workflow.model_validate(state["workflow"]).version, "meta": batch.meta_version}
            state["observations"].extend(normalized)
            if batch.patch_id:
                trial = self.store.get("trials", batch_id)
                patch = Patch.model_validate(trial["patch"])
                result = evaluate_trial(batch, observations, GatePolicy.model_validate(trial["gate"]),
                                        direction=task.direction)
                self.store.put("trial_results", batch_id, result.model_dump(), immutable=True)
                self.memory.record(patch, result, campaign_id=state["campaign_id"],
                    observations=len(state["observations"]) - len(normalized), source=task.feedback_source)
                if result.decision == "accepted":
                    new = Workflow.model_validate(trial["challenger"])
                    self.store.put("workflow_versions", new.version, new.model_dump(), immutable=True)
                    state["workflow"] = new.model_dump()
                state["pending_patch"] = None
                summary["trial"] = result.model_dump()
                self.store.event("workflow_trial_completed", {"batch_id": batch_id, **result.model_dump()})
            research_config = self.store.get("configuration", "research", {})
            if research_config.get("enabled"):
                from proteinrsi.research.analysis import persist_analysis
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
        # Measurements are committed even if subsequent LLM/metacognitive work fails.
        if state["status"] == "ready":
            self.consider_improvement()
        return self.state

    def consider_improvement(self) -> None:
        with self.store.lock():
            state = self.state
            if state["status"] != "ready" or state["pending_patch"] or state["pending_meta"]:
                return
            if state["considered_round"] == state["round_index"]:
                return
            view = self.view(state)
            try:
                feedback = self.team.analyst.feedback(view)
                if state["history"]:
                    state["history"][-1]["analyst_feedback"] = feedback.model_dump()
                view = self.view(state)
                response = self.meta_agent.propose(view, state["last_patch_round"])
                if response.patch:
                    self._stage_patch(state, response.patch)
                self.store.event("meta_decision", {"round": view.round_index, "meta": view.meta.version,
                                                   "response": response.model_dump(mode="json")})
            except (BudgetExceeded, LLMError, ValueError, PermissionError) as exc:
                self.store.event("meta_change_blocked", {"round": view.round_index,
                                                         "error_type": type(exc).__name__})
            state["considered_round"] = state["round_index"]
            self.store.put("campaign", "state", state)

    def _stage_patch(self, state: dict, patch: Patch) -> None:
        task = TaskSpec.model_validate(state["task"])
        if patch.task_kind != task.kind:
            raise ValueError("Patch scope does not match task")
        current = Workflow.model_validate(state["workflow"]) if patch.target == "workflow" else MetaPolicy.model_validate(state["meta"])
        candidate = apply_patch(current, patch)
        if patch.target == "workflow":
            self.team.tools.catalog(task, candidate.tool_names)
            skill_text(candidate.skill_names)
        if self.store.get("patches", patch.patch_id):
            return  # Do not repeatedly test the same patch on the same evidence/claim.
        state["pending_patch" if patch.target == "workflow" else "pending_meta"] = patch.model_dump(mode="json")
        state["last_patch_round"] = state["round_index"]
        self.store.put("patches", patch.patch_id, patch.model_dump(mode="json"), immutable=True)

    def stage_patch(self, patch: Patch) -> None:
        """Operator/API entrypoint; still validates scope, base version, fields, and trial requirements."""
        with self.store.lock():
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
        return {"campaign_id": state["campaign_id"], "status": state["status"],
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
                "meta_evaluations": self.store.all("meta_evaluations")}
