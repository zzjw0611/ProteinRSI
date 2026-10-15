# SPDX-License-Identifier: MIT
"""Checkpoint scheduling for frozen W/M replay comparisons.

The controller constructs same-snapshot comparisons from already disclosed
inputs. These are not mislabeled as independent proteins or confirmation data.
"""
from concurrent.futures import ThreadPoolExecutor

from proteinrsi.contracts import EvaluationPolicy, GateResult, Patch, TaskSpec, digest
from proteinrsi.evaluation import MetaCase
from proteinrsi.evaluation_fork import ModelRevisionUnavailable
from proteinrsi.storage import BudgetExceeded, Conflict


class EvaluationScheduler:
    """Local background execution with durable attempts, not a second task loop.

    Only independently funded main-study comparisons run in the background.
    Nested W comparisons remain checkpoints of their fixed-M arm. Publication
    happens in the owning controller, never in the background thread.
    """
    def __init__(self, campaign, oracle, *, cases=None, team_factory=None):
        self.campaign, self.oracle = campaign, oracle
        self.cases, self.team_factory = cases, team_factory
        config = campaign.store.get("configuration", "research", {})
        settings = EvaluationPolicy.model_validate(config.get("auto_evaluation") or {})
        self.enabled = (config.get("evaluation_execution", "blocking") == "background"
            and settings.budget_scope == "independent" and not campaign.methods.is_evaluation)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-validation")
        self.futures = {}
        self.submitted_patches = set()

    def submit(self, run):
        from proteinrsi.loop_evaluation import execute_evaluation
        store, eid = self.campaign.store, run.draft["evaluation_id"]
        if eid in self.futures:
            return {"evaluation_id": eid, "status": "running"}
        job = {"evaluation_id": eid, "patch_id": run.patch.patch_id,
               "target": run.patch.target, "status": "running"}
        store.put("evaluation_jobs", eid, job)
        store.event("evaluation_job_started", job)

        def execute():
            from proteinrsi.recovery import is_provider_paused
            try:
                report = execute_evaluation(run, team_factory=self.team_factory)
            except Exception as exc:
                import traceback
                from proteinrsi.audit import redact
                abandoned = store.get("harness_attempts", eid, {}).get("state") == "abandoned"
                paused = is_provider_paused(exc)
                result = {**job, "status": "cancelled" if abandoned else
                          "paused_provider" if paused else "blocked",
                          "pause_reason": getattr(exc, "reason", "provider_paused") if paused else None,
                          "error_type": type(exc).__name__,
                          "error": redact(str(exc), (getattr(self.campaign.team.llm, "api_key", ""),)),
                          "traceback": redact(traceback.format_exc(),
                              (getattr(self.campaign.team.llm, "api_key", ""),))}
                store.put("evaluation_jobs", eid, result)
                store.event("evaluation_job_stopped", result)
                raise
            result = {**job, "status": "completed", "report_ref": "harness_evaluations/" + eid}
            store.put("evaluation_jobs", eid, result)
            store.event("evaluation_job_completed", result)
            return report

        self.futures[eid] = self.pool.submit(execute)
        self.submitted_patches.add(run.patch.patch_id)
        return {"evaluation_id": eid, "status": "running"}

    def poll(self, *, publish=True):
        from proteinrsi.recovery import is_provider_paused
        for eid, future in list(self.futures.items()):
            if future.done():
                try:
                    future.result()
                except Exception as exc:
                    abandoned = self.campaign.store.get("evaluation_jobs", eid, {}).get("status") == "cancelled"
                    if not abandoned and not is_provider_paused(exc):
                        raise
        for eid, job in self.campaign.store.all("evaluation_jobs").items():
            if job["status"] == "blocked":
                raise Conflict("Background validation is blocked; reconcile " + eid)
            if publish and job["status"] == "completed":
                publish_evaluation(self.campaign, eid)

    def checkpoint(self, current):
        self.poll()
        from proteinrsi.trajectory import export_text
        export_text(current.store.root, current.store.root / "trajectory.txt")
        current._optional_improvement()
        pending = current.state["pending_patch"] or current.state["pending_meta"]
        if pending and Patch.model_validate(pending).patch_id in self.submitted_patches:
            return
        result = schedule_evaluation(current, self.oracle, cases=self.cases, team_factory=self.team_factory,
                                     submit=self.submit if self.enabled else None)
        config = current.store.get("configuration", "research", {})
        if (config.get("context_policy") == "state-v1" and not self.enabled
                and result is None and (current.state["pending_patch"] or current.state["pending_meta"])):
            return "method_awaits_authorized_validation"

    def close(self, *, publish=False):
        # The CLI owns these workers. At the end, drain their work and retain
        # exact paused attempts; do not leave unsupervised threads or lose receipts.
        self.pool.shutdown(wait=True)
        self.poll(publish=publish)


def publish_evaluation(campaign, eid):
    """Consume a recorded verdict once, at an idle task boundary."""
    from proteinrsi.governance import TYPES, PENDING
    from proteinrsi.harness_evaluation import _saved_report
    store = campaign.store
    with store.lock(), store.transaction():
        job = store.get("evaluation_jobs", eid)
        if not job or job["status"] != "completed":
            return
        state = campaign.state
        if state["pending_batch"] or state["status"] not in {"ready", "complete"}:
            return
        attempt = store.get("harness_attempts", eid)
        report = _saved_report(store, eid, attempt)
        if (not report.get("publication_deferred") or report["phase"] != "validation"
                or not attempt["promote_requested"]):
            raise Conflict("Only deferred selection verdicts can be published")
        patch = Patch.model_validate(store.get("patches", report["patch_id"]))
        cls, pending = TYPES[patch.target], PENDING[patch.target]
        matches = (state[pending] == patch.model_dump(mode="json")
                   and cls.model_validate(state[patch.target]).version == patch.base_version)
        publication = {"evaluation_id": eid, "patch_id": patch.patch_id,
            "round": state["round_index"], "status": "published" if matches else "superseded",
            "promoted": matches and report["gate"]["decision"] == "accepted"}
        if matches:
            record = store.get("method_candidates", patch.patch_id)
            candidate = cls.model_validate(campaign.methods._validated_snapshot(
                patch.target, record["candidate_snapshot_ref"])["definition"])
            result = GateResult.model_validate(report["gate"])
            campaign.memory.record(patch, result, campaign_id=state["campaign_id"],
                observations=len(state["observations"]), source=state["task"]["feedback_source"])
            campaign.methods.complete(state, patch, result, candidate,
                                      evaluation_ref="harness_evaluations/" + eid)
        store.put("evaluation_publications", eid, publication, immutable=True)
        store.put("evaluation_jobs", eid, {**job, "status": publication["status"]})
        store.event("evaluation_publication", publication)
    from proteinrsi.memory import sync_shared_memory
    sync_shared_memory(campaign)


def schedule_evaluation(campaign, oracle=None, *, cases=None, team_factory=None, submit=None):
    from proteinrsi.memory import sync_shared_memory
    sync_shared_memory(campaign)
    state, store = campaign.state, campaign.store
    if state["pending_batch"] or state["status"] != "ready" or not (state["pending_patch"] or state["pending_meta"]):
        return None
    config = store.get("configuration", "research", {})
    settings = EvaluationPolicy.model_validate(config.get("auto_evaluation") or {})
    patch = Patch.model_validate(state["pending_patch"] or state["pending_meta"])
    if config.get("method_policy", "validate") == "direct":
        with store.lock(), store.transaction():
            campaign.methods.adopt_unvalidated(campaign.state, patch)
        return {"patch_id": patch.patch_id, "status": "adopted_unvalidated", "validated": False}
    if not settings.enabled:
        return None
    if patch.target not in campaign.methods.allowed_targets:
        return None
    if patch.target == "meta" and config.get("evaluation_protocol") != "method-loop-v1":
        return None
    def dispatch(selected, **kwargs):
        from proteinrsi.loop_evaluation import evaluate_method, prepare_evaluation
        if submit is None:
            return evaluate_method(campaign, selected, **kwargs)
        run = prepare_evaluation(campaign, selected, **kwargs, defer_publication=True)
        return run if isinstance(run, dict) else submit(run)

    def defer(reason):
        key = digest({"patch": patch.patch_id, "round": state["round_index"], "reason": reason})
        if store.get("evaluation_deferrals", key) is None:
            record = {"patch_id": patch.patch_id, "round": state["round_index"],
                      "reason": reason, "status": "pending_validation", "active_version_unchanged": True}
            store.put("evaluation_deferrals", key, record, immutable=True)
            store.event("harness_validation_deferred", record)
        return None

    if cases is not None:
        try:
            return dispatch(cases, target=patch.target, promote=True, team_factory=team_factory)
        except (BudgetExceeded, ModelRevisionUnavailable) as exc:
            if any(a.get("patch_id") == patch.patch_id and a.get("state") in {"started", "paused_provider", "blocked"}
                   for a in store.all("harness_attempts").values()):
                raise
            return defer(str(exc) if isinstance(exc, ModelRevisionUnavailable) else
                         "Insufficient paired-validation budget; retaining original Harness")
    if oracle is None:
        return defer("No authorized replay feedback adapter; real-lab validation requires authorized feedback")
    frozen = store.get("evaluation_auto_inputs", patch.patch_id)
    if frozen is None:
        task = TaskSpec.model_validate(state["task"])
        data = task.model_dump(mode="json")
        rounds = settings.meta_rounds if patch.target == "meta" else settings.rounds
        batch_size = settings.batch_size or task.batch_size
        if patch.target == "meta":
            q = settings.meta_query_budget_per_arm or batch_size * (rounds + 2 * settings.rounds * (rounds - 1))
        else:
            q = settings.query_budget_per_arm or batch_size * rounds
        data.update(max_rounds=rounds, batch_size=batch_size,
                    budget={"experimental_wells": q,
                        "llm_calls": settings.meta_llm_calls_per_arm if patch.target == "meta" else settings.llm_calls_per_arm,
                        "tool_calls": settings.meta_tool_calls_per_arm if patch.target == "meta" else settings.tool_calls_per_arm})
        # Validation is a continuation of a frozen observed snapshot, never a new warm-start purchase.
        data.update(initial_observation_policy="none", initial_parent_measurement=None)
        TaskSpec.model_validate(data)
        frozen = {"task": data, "initial": state["observations"], "history": state["history"],
            "round_index": state["round_index"], "first_round_spent": 0, "query_budget": q,
            "case_id": "snapshot-" + patch.patch_id, "group_id": "current-scientific-start",
            "source": store.get("configuration", "replay_dataset"), "scope": "same_snapshot"}
        from proteinrsi.research.state import enabled, current_state
        if enabled(store):
            frozen["research_state"] = current_state(store, state)
        store.put("evaluation_auto_inputs", patch.patch_id, frozen, immutable=True)
    # Complete mapping remains solely in controller memory and is never persisted in a TaskView.
    labels = {seq: value for seq, (value, qc) in oracle._labels.items() if qc == "valid"}
    case = MetaCase(case_id=frozen["case_id"], group_id=frozen["group_id"], split="validation",
        task=frozen["task"], initial=frozen["initial"], labels=labels, query_budget=frozen["query_budget"])
    try:
        return dispatch([case], target=patch.target, promote=True, team_factory=team_factory,
            scope="same_snapshot", start_overrides={case.case_id: {
                "history": frozen["history"], "round_index": frozen["round_index"],
                "first_round_spent": frozen["first_round_spent"], "initialization_done": True,
                **({"research_state": frozen["research_state"]} if "research_state" in frozen else {})}})
    except (BudgetExceeded, ModelRevisionUnavailable) as exc:
        # Only a pre-start shortage may safely continue the parent. Started or
        # uncertain external work must be reconciled rather than ignored.
        if any(a.get("patch_id") == patch.patch_id and a.get("state") in {"started", "paused_provider", "blocked"}
               for a in store.all("harness_attempts").values()):
            raise
        return defer(str(exc) if isinstance(exc, ModelRevisionUnavailable) else
                     "Insufficient paired-validation budget; retaining original Harness")
