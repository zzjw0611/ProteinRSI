# SPDX-License-Identifier: MIT
"""Paired W/M comparisons through the public Task Loop.

Frozen input manifests contain no future labels. Cases/feedback providers are
trusted controller inputs; child Campaigns never receive a full measurement map.
"""
from collections import defaultdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass

import numpy as np

from proteinrsi.contracts import (Candidate, EvaluationCaseSpec, EvaluationPolicy, EvaluationSpec,
    GatePolicy, GateResult, MetaPolicy, Observation, Patch, TaskSpec, Workflow, digest)
from proteinrsi.evaluation import _measurement_denominators, validate_saved_metric_evidence
from proteinrsi.evaluation_fork import (evaluation_budget_store, freeze_resources, freeze_start,
    fork_for_evaluation)
from proteinrsi.improvement import apply_patch, compare_scores
from proteinrsi.metrics import summarize_metrics
from proteinrsi.online_meta import make_validation_team
from proteinrsi.recovery import is_provider_paused
from proteinrsi.storage import AllocatedBudgetView, Conflict
from proteinrsi.task_loop import run_task_loop
from proteinrsi.tasks import validate_task, validate_candidate


class CaseFeedback:
    """Trusted fixture/replay lookup. It is never passed to an Agent or tool."""
    def __init__(self, case):
        self.case = case
        self._labels = {s: (v, "valid") for s, v in case.labels.items()}

    def validate(self, batch):
        # Missing historical measurements produce unavailable, never invented fitness.
        for sample in batch.samples:
            validate_candidate(self.case.task, sample.candidate)
            if sample.candidate.sequence not in self.case.labels and (
                    self.case.task.candidate_access != "open" or self.case.task.feedback_source != "measured_replay"):
                from proteinrsi.lab import UnknownMeasurement
                raise UnknownMeasurement("Requested variant has no authorized historical measurement")

    def measure(self, batch):
        t = self.case.task
        return [Observation(sample_id=s.sample_id, batch_id=batch.batch_id,
            sequence=s.candidate.sequence, value=self.case.labels.get(s.candidate.sequence),
            qc="valid" if s.candidate.sequence in self.case.labels else "unavailable",
            metric=t.metric, unit=t.unit, source=t.feedback_source, assay_protocol=t.assay_protocol)
            for s in batch.samples]


def _validate(campaign, cases, confirmation, scope):
    from proteinrsi.harness_evaluation import _episode_identity
    if not cases or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Nonempty unique validation cases required")
    task = TaskSpec.model_validate(campaign.state["task"])
    groups = {}
    for case in cases:
        t = case.task
        validate_task(t)
        if t.execution_mode != "experimental" or t.feedback_source not in {"synthetic", "measured_replay"}:
            raise ValueError("This automated comparison requires authorized replay feedback, not simulated wetlab")
        if (t.kind, t.metric, t.unit, t.direction, t.assay_protocol) != (task.kind, task.metric, task.unit, task.direction, task.assay_protocol):
            raise ValueError("Case scientific contract differs from the study")
        if case.split != ("test" if confirmation else "validation"):
            raise ValueError("Version selection and final confirmation must use different splits")
        if case.query_budget < 1 or case.query_budget > t.budget.experimental_wells:
            raise ValueError("Invalid new-query budget")
        if t.batch_fill_policy == "full_plate" and case.query_budget % t.batch_size:
            raise ValueError("Validation requires whole-plate budgets")
        if t.candidate_access != "open" and set(t.candidates) - case.labels.keys():
            raise ValueError("Closed-case candidate catalogue is not covered by measurements")
        if any(not np.isfinite(v) for v in case.labels.values()):
            raise ValueError("Nonfinite historical measurement")
        for o in case.initial:
            validate_candidate(t, Candidate(sequence=o.sequence))
            if (o.metric, o.unit, o.assay_protocol, o.source) != (t.metric, t.unit, t.assay_protocol, t.feedback_source):
                raise ValueError("Initial evidence source/units mismatch")
            if o.qc == "valid" and (o.sequence not in case.labels or o.value != case.labels[o.sequence]):
                raise ValueError("Initial evidence differs from authorized measurements")
        if scope == "declared_cases" and case.history:
            raise ValueError("Independent cases cannot import undeclared task history")
        seen = {o["sequence"] for o in campaign.state["observations"]}
        if confirmation:
            # A renamed starting case cannot make selection measurements unseen.
            for report in campaign.store.all("harness_evaluations").values():
                if report.get("phase") != "validation" or report.get("protocol") not in {"task-loop-v1", "method-loop-v1"}:
                    continue
                prior_spec = campaign.store.get("evaluation_specs", report["spec_ref"])
                starts = {c["case_id"]: campaign.store.get("evaluation_starts", c["start_ref"])
                          for c in prior_spec["cases"]}
                for pair in report["traces"]:
                    for trace in pair.values():
                        prior_task = starts[trace["case_id"]]["task"]
                        if (prior_task["reference_sequence"], prior_task["assay_protocol"]) == (t.reference_sequence, t.assay_protocol):
                            seen.update(row["sequence"] for row in trace["observed_rows"])
                            seen.update(trace.get("validation_observed_sequences", []))
        initial = {o.sequence for o in case.initial}
        if scope == "declared_cases" and t.reference_sequence == task.reference_sequence and (set(case.labels) & seen) - initial:
            raise ValueError("Already revealed measurements must be declared as initial evidence")
        episode = _episode_identity(case)
        if episode in groups and groups[episode] != case.group_id:
            raise ValueError("Repeated starting evidence must share an evaluation group")
        groups[episode] = case.group_id
        prior = campaign.store.get("harness_case_bindings", episode)
        phase = "confirmation" if confirmation else "validation"
        if prior and prior["phase"] != phase:
            raise ValueError("Selection cases cannot be reused for confirmation")
    return task


def _revealed_sequences(value):
    """Read only operator-mirrored measurement receipts, including nested wrappers."""
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict) and "sample_id" in row and "qc" in row:
                yield row["sequence"]
    elif isinstance(value, dict) and "value" in value:
        yield from _revealed_sequences(value["value"])


def _trace(child, case, result, workflow, meta, top_ns, start_round):
    from proteinrsi.research.state import execution_summary
    binding = child.store.get("configuration", "evaluation_branch")
    # Only new observations, not free copied evidence; controls remain in costs.
    rows, selected, submitted = [], [], 0
    for batch_id, raw in child.store.all("batches").items():
        measured = child.store.get("measurements", batch_id)
        if measured is None:
            continue
        by_id = {r["sample_id"]: r for r in measured}
        submitted += len(raw["samples"])
        for sample in raw["samples"]:
            if sample["arm"] == "control":
                continue
            row = by_id[sample["sample_id"]]
            if row["sequence"] == case.task.reference_sequence:
                continue
            rows.append(row)
            selected.append(row["sequence"])
    summary = summarize_metrics(rows, direction=case.task.direction, top_ns=top_ns,
                                submitted=len(rows))
    return {"case_id": case.case_id, "group_id": case.group_id, "meta": meta.version,
        "descendant": Workflow.model_validate(child.state["workflow"]).version, "selected": selected, "observed_rows": rows,
        "submitted": len(rows), "total_submitted_wells": submitted,
        "query_cap": case.query_budget, "metric_summary": summary, "parent_excluded": True,
        "usage": child.store.usage(), "loop_result": result.model_dump(mode="json"),
        "completed_new_rounds": child.state["round_index"] - start_round,
        "execution_summary": execution_summary(child.store),
        "history": child.state["history"][start_round:], "branch": binding,
        "task_reports": child.store.all("task_reports"),
        "validation_observed_sequences": sorted({seq for record in
            child.store.all("validation_measurements").values() for seq in _revealed_sequences(record)}),
        "method_switches": list(child.store.all("method_switches").values()),
        "meta_decisions": [e["payload"] for e in child.store.events() if e["kind"] == "meta_decision"],
        "nested_evaluations": [{k: r.get(k) for k in
            ("evaluation_id", "target", "gate", "promoted", "spec_ref")}
            for r in child.store.all("harness_evaluations").values()],
        "replay_experiment_cost": {"amount": 0, "basis": "historical_replay_no_new_wetlab"}
            if case.task.feedback_source == "measured_replay" else None}


def _envelope(plan, task, cases, traces):
    aligned, counts, groups = [], [], set()
    for case, pair in zip(cases, traces):
        arms = {arm: pair[arm]["observed_rows"] for arm in pair}
        aligned.append({"case_id": case.case_id, "group_id": case.group_id, "arms": arms,
                        "execution": {arm: pair[arm]["execution_summary"] for arm in pair}})
        counts.append({"case_id": case.case_id, "group_id": case.group_id,
            **{a: _measurement_denominators(rows, len(rows)) for a, rows in arms.items()}})
        groups.add(case.group_id)
    return {"protocol": "evaluation_inputs/v1", "evaluation_id": plan["evaluation_id"],
        "target": plan["target"], "task": task.model_dump(mode="json"), "top_ns": plan["plan"]["top_ns"],
        "subject_refs": ["baseline", "challenger"], "arms": {"baseline": [], "challenger": []},
        "cases": aligned, "denominators": {"n_cases": len(cases), "n_groups": len(groups),
            "unit": "independent_group", "cases": counts, "parent_excluded": True}}


def _numeric_result(traces, policy):
    # Computational truncation is a recorded incomplete trial, not a positive capability claim.
    if any(t["loop_result"]["status"] != "completed" or t["metric_summary"]["denominators"]["unavailable"]
           for pair in traces for t in pair.values()):
        return GateResult(decision="inconclusive", reason="Incomplete branch or unavailable measurements",
                          n_baseline=len(traces), n_challenger=len(traces))
    if policy.criterion == "observed_pareto_v1":
        from proteinrsi.evaluation import compare_grouped_metrics
        # Unequal early stops are not silently dropped or normalized after outcomes.
        if any(p["baseline"]["submitted"] != p["challenger"]["submitted"] for p in traces):
            return GateResult(decision="inconclusive", reason="Unequal submitted observations",
                              n_baseline=len(traces), n_challenger=len(traces))
        return compare_grouped_metrics(traces, policy)
    grouped = defaultdict(list)
    for pair in traces:
        grouped[pair["baseline"]["group_id"]].append(
            [pair[a]["metric_summary"]["signed_metrics"]["avg"] for a in ("baseline", "challenger")])
    if any(v is None for rows in grouped.values() for row in rows for v in row):
        return GateResult(decision="inconclusive", reason="No comparable measured outcomes",
                          n_baseline=len(grouped), n_challenger=len(grouped))
    means = [np.mean(rows, axis=0) for rows in grouped.values()]
    return compare_scores([float(x[0]) for x in means], [float(x[1]) for x in means], policy, paired=True)


@dataclass(frozen=True)
class EvaluationRun:
    campaign: object
    cases: list
    patch: Patch
    baseline: Workflow | MetaPolicy
    challenger: Workflow | MetaPolicy
    spec: EvaluationSpec
    draft: dict
    attempt: dict


def _run_arm(run, spec, case, case_spec, arm, plan, team_factory):
    campaign, store = run.campaign, run.campaign.store
    method = run.baseline if arm == "baseline" else run.challenger
    workflow = method if spec.target == "workflow" else Workflow.model_validate(run.draft["workflow"])
    meta = MetaPolicy.model_validate(run.draft["meta"]) if spec.target == "workflow" else method
    branch = fork_for_evaluation(campaign, spec, case_spec, workflow, meta, arm=arm,
        team_factory=team_factory or (lambda s: make_validation_team(campaign, s)))
    cached = branch.store.get("loop_arm_results", "result")
    if cached is not None:
        return cached
    branch.store.event("evaluation_arm_started", {"arm": arm, "spec_ref": spec.spec_ref})
    feedback = CaseFeedback(case)

    def checkpoint(current):
        if store.get("harness_attempts", run.draft["evaluation_id"])["state"] == "abandoned":
            return "evaluation_abandoned"
        from proteinrsi.evaluation_scheduler import schedule_evaluation
        if spec.target == "meta":
            current._optional_improvement()
            schedule_evaluation(current, feedback, team_factory=team_factory)

    result = run_task_loop(branch, feedback, operator="authorized-method-evaluation",
        on_checkpoint=checkpoint, raise_on_pause=True)
    if result.status in {"paused", "waiting_feedback", "waiting_approval", "waiting_validation"}:
        from proteinrsi.llm import ProviderPaused
        raise ProviderPaused(f"Validation branch paused ({result.reason}); resume the same attempt",
                             reason=result.reason)
    if any(a.get("state") in {"started", "paused_provider", "blocked"}
           for a in branch.store.all("harness_attempts").values()):
        raise Conflict("Nested evaluation is unfinished; reconcile before closing the M arm")
    start = store.get("evaluation_starts", case_spec.start_ref)
    cached = _trace(branch, case, result, workflow, meta,
        plan["plan"]["top_ns"] if plan else GatePolicy.model_validate(spec.gate).top_ns,
        start["round_index"])
    branch.store.put("loop_arm_results", "result", cached, immutable=True)
    branch.store.event("evaluation_arm_completed", {"arm": arm, "spec_ref": spec.spec_ref})
    return cached


def prepare_evaluation(campaign, cases, *, target="workflow", promote=False, patch_id=None, team_factory=None,
                     scope="declared_cases", start_overrides=None, defer_publication=False):
    """Resume a frozen comparison; final confirmation never selects or publishes."""
    from proteinrsi.harness_evaluation import _episode_identity, _saved_report
    store = campaign.store
    cases = deepcopy(cases)
    if target not in {"workflow", "meta"}:
        raise ValueError("Unknown evaluation target")
    protocol = store.get("configuration", "research", {}).get("evaluation_protocol", "task-loop-v1")
    if target == "meta" and protocol != "method-loop-v1":
        raise ValueError("Multi-round Meta evaluation requires a new method-loop-v1 study")
    if protocol not in {"task-loop-v1", "method-loop-v1"}:
        raise ValueError("Task-loop evaluator cannot silently migrate the study protocol")
    cls = Workflow if target == "workflow" else MetaPolicy
    pending_key = "pending_patch" if target == "workflow" else "pending_meta"
    confirmation = patch_id is not None
    campaign.methods.require_evaluation(target, confirmation=confirmation)
    if confirmation and promote:
        raise ValueError("Confirmation cannot publish")
    with store.lock():
        state = campaign.state
        task = TaskSpec.model_validate(state["task"])
        manifest = digest([c.model_dump(mode="json") for c in cases])
        raw = store.get("patches", patch_id) if confirmation else state[pending_key]
        if raw is None and not confirmation:
            matches = [(k, a) for k, a in store.all("harness_attempts").items()
                       if a.get("protocol") == protocol and a.get("target", "workflow") == target and a["manifest"] == manifest
                       and a["state"] == "completed" and a["promote_requested"] == promote and not a["confirmation"]]
            if matches:
                return _saved_report(store, *matches[-1])
        if raw is None or state["pending_batch"] or state["status"] not in {"ready", "complete"}:
            raise Conflict("Comparison requires a recorded candidate at a safe boundary")
        patch = Patch.model_validate(raw)
        if patch.target != target:
            raise ValueError("Patch target differs from the requested comparison")
        record = store.get("method_candidates", patch.patch_id)
        if record is None:
            raise Conflict("Executable candidate snapshots required")
        if confirmation and store.get("method_candidate_states", patch.patch_id, {}).get("status") != "accepted":
            raise Conflict("Confirm only an already accepted version")
        base = cls.model_validate(campaign.methods._validated_snapshot(target, record["base_snapshot_ref"])["definition"])
        child = cls.model_validate(campaign.methods._validated_snapshot(target, record["candidate_snapshot_ref"])["definition"])
        if child.version != apply_patch(base, patch).version:
            raise Conflict("Candidate differs from patch")
        if not confirmation and cls.model_validate(state[target]).version != base.version:
            raise Conflict("Active parent changed")
        eid = "harness-loop-" + digest({"patch": patch.patch_id, "manifest": manifest,
            "confirmation": confirmation, "scope": scope,
            **({"target": target, "protocol": protocol} if protocol == "method-loop-v1" else {})})
        prior = store.get("harness_attempts", eid)
        if prior and (prior["promote_requested"] != promote
                      or prior.get("defer_publication", False) != defer_publication):
            raise Conflict("Cannot change frozen publication intent")
        if prior and prior["state"] == "completed":
            return _saved_report(store, eid, prior)
        if prior and prior["state"] not in {"started", "paused_provider"}:
            raise Conflict("Reconcile the blocked comparison before retrying")
        if any(k != eid and a.get("state") in {"started", "paused_provider", "blocked"}
               for k, a in store.all("harness_attempts").items()):
            raise Conflict("Another comparison is unfinished")
        if prior is None:
            task = _validate(campaign, cases, confirmation, scope)
        campaign.methods.assert_active(state)
        policy = GatePolicy.model_validate(state["gate"])
        settings = EvaluationPolicy.model_validate(store.get("configuration", "research", {}).get("auto_evaluation") or {})
        draft = store.get("evaluation_drafts", eid)
        if draft is None:
            if not confirmation:
                campaign.methods.require_target(target)
                campaign.methods.assert_plannable(state)
            protein_config = store.get("configuration", "protein_model")
            plm_cap = None
            if protein_config:
                finite = [protein_config["max_model_inputs"]] if protein_config["max_model_inputs"] is not None else []
                if settings.budget_scope == "shared_campaign":
                    available = store.remaining("plm_inputs")
                    if available is not None:
                        finite.append(available // (2 * len(cases)))
                plm_cap = min(finite) if finite else None
            case_caps = {c.case_id: {"experimental_wells": c.query_budget,
                "llm_calls": c.task.budget.llm_calls, "tool_calls": c.task.budget.tool_calls,
                **({"plm_inputs": plm_cap} if protein_config else {})}
                for c in cases}
            evaluator_limits = {"experimental_wells": 0,
                "llm_calls": settings.evaluator_llm_calls if policy.criterion == "llm_adjudicated_v1" else 0,
                "tool_calls": settings.evaluator_tool_calls if policy.criterion == "llm_adjudicated_v1" else 0}
            floor = settings.main_reserve_wells
            if floor is None:
                floor = task.batch_size if state["status"] != "complete" and state["round_index"] < task.max_rounds else 0
            if settings.budget_scope == "independent":
                floor = 0
            allocations = {eid + "/" + c.case_id + "/" + a: case_caps[c.case_id]
                           for c in cases for a in ("baseline", "challenger")}
            allocations[eid + "/evaluator"] = evaluator_limits
            # A comparison that cannot start must not require optional model assets.
            if settings.budget_scope == "shared_campaign":
                store.check_allocation_capacity(allocations, keep_free={"experimental_wells": floor})
            resources, _memory, refs = freeze_resources(campaign)
            # No live-task structural artifacts silently reused for another protein.
            if resources["artifacts"] and any(c.task.reference_sequence != task.reference_sequence for c in cases):
                raise ValueError("Cross-protein evaluation needs case-scoped artifacts")
            starts = []
            for case in cases:
                overrides = (start_overrides or {}).get(case.case_id, {})
                ref = freeze_start(store, case, **overrides)
                starts.append(EvaluationCaseSpec(case_id=case.case_id, group_id=case.group_id,
                    start_ref=ref, rounds=case.task.max_rounds, resource_limits=case_caps[case.case_id]))
            draft_spec = EvaluationSpec(protocol=protocol, target=target,
                initial_workflow_ref=state["method_governance"]["active"]["workflow"] if target == "meta" else None, purpose="confirmation" if confirmation else "selection", scope=scope,
                budget_scope=settings.budget_scope,
                patch_id=patch.patch_id, baseline_version=base.version, candidate_version=child.version,
                manifest_hash=manifest, resource_snapshot_ref=refs[0], memory_snapshot_ref=refs[1],
                cases=starts, gate=policy.model_dump(), evaluator_limits=evaluator_limits,
                main_reserve_wells=floor, promotion_requested=promote)
            if (all(n is None for caps in case_caps.values() for r, n in caps.items()
                    if r != "experimental_wells")
                    and not any(n for r, n in evaluator_limits.items() if r != "experimental_wells")):
                draft_spec = draft_spec.model_copy(update={"stopping_conditions": [
                    c for c in draft_spec.stopping_conditions if c != "compute_budget"]})
            draft = {"spec": draft_spec.model_dump(mode="json"), "meta": state["meta"],
                     "workflow": state["workflow"], "evaluation_id": eid,
                     "parent_context": {"campaign_id": state["campaign_id"],
                         "observations": len(state["observations"]), "task": state["task"]}}
            # Immutable input draft, including E's authorized maximum resources, precedes calls.
            store.put("evaluation_drafts", eid, draft, immutable=True)
        provisional = EvaluationSpec.model_validate(draft["spec"])
        initial_workflow = Workflow.model_validate(draft.get("workflow", state["workflow"]))
        if provisional.gate != state["gate"]:
            raise Conflict("Acceptance policy changed after the evaluation was frozen")
        if provisional.initial_workflow_ref:
            initial_snapshot = campaign.methods._validated_snapshot("workflow", provisional.initial_workflow_ref)
            if initial_snapshot["definition"] != initial_workflow.model_dump(mode="json"):
                raise Conflict("Initial Task Harness differs from the frozen M comparison")
        allocations = {eid + "/" + c.case_id + "/" + a: c.resource_limits
                       for c in provisional.cases for a in ("baseline", "challenger")}
        allocations[eid + "/evaluator"] = provisional.evaluator_limits
        budget_store = evaluation_budget_store(store, eid, provisional)
        if prior is None:
            with store.transaction():
                budget_store.reserve_allocations(eid, allocations, keep_free={"experimental_wells": provisional.main_reserve_wells})
                prior = {"protocol": protocol, "target": target, "state": "started", "patch_id": patch.patch_id,
                    "manifest": manifest, "confirmation": confirmation, "promote_requested": promote,
                    "defer_publication": defer_publication,
                    "budget_scope": provisional.budget_scope,
                    "budget_before": budget_store.usage(), "main_budget_before": store.usage()}
                if not confirmation:
                    campaign.methods.begin(state, patch)
                store.put("harness_attempts", eid, prior)
                for case in cases:
                    store.put("harness_case_bindings", _episode_identity(case),
                        {"phase": "confirmation" if confirmation else "validation"}, immutable=True)
        return EvaluationRun(campaign, cases, patch, base, child, provisional, draft, prior)


def execute_evaluation(run, *, team_factory=None):
    """Serialize duplicate coordinators, without locking normal task execution."""
    from proteinrsi.harness_evaluation import _saved_report
    store, eid = run.campaign.store, run.draft["evaluation_id"]
    with store.lock(".evaluation-" + digest(eid) + ".lock"):
        attempt = store.get("harness_attempts", eid)
        if attempt["state"] == "completed":
            return _saved_report(store, eid, attempt)
        return _execute_evaluation(run, team_factory=team_factory)


def _execute_evaluation(run, *, team_factory=None):
    """Run frozen branches outside the parent lock; commit a durable verdict."""
    from proteinrsi.harness_evaluation import _episode_identity
    from proteinrsi.llm_evaluation import ensure_evaluation_plan, adjudicate_evaluation
    campaign, cases, store = run.campaign, run.cases, run.campaign.store
    patch, base, child = run.patch, run.baseline, run.challenger
    provisional, draft, prior = run.spec, run.draft, run.attempt
    eid, target, protocol = draft["evaluation_id"], provisional.target, provisional.protocol
    manifest, scope = provisional.manifest_hash, provisional.scope
    confirmation, promote = prior["confirmation"], prior["promote_requested"]
    defer_publication = prior.get("defer_publication", False)
    cls = Workflow if target == "workflow" else MetaPolicy
    pending_key = "pending_patch" if target == "workflow" else "pending_meta"
    raw = patch.model_dump(mode="json")
    task = TaskSpec.model_validate(draft.get("parent_context", {}).get("task", campaign.state["task"]))
    policy = GatePolicy.model_validate(provisional.gate)
    initial_workflow = Workflow.model_validate(draft["workflow"])
    budget_store = evaluation_budget_store(store, eid, provisional)
    eval_store = AllocatedBudgetView(store, eid + "/evaluator", budget_store=budget_store)
    eval_team = make_validation_team(campaign, eval_store)
    context = {"patch": patch.model_dump(mode="json"), "task": task.model_dump(mode="json"),
        "baseline_" + target: base.model_dump(), "candidate_" + target: child.model_dump(),
        "initial_workflow": initial_workflow.model_dump(),
        "evaluation_spec": provisional.model_dump(mode="json"),
        "cases": [{"case_id": c.case_id, "group_id": c.group_id, "query_budget": c.query_budget,
                   "initial": [o.model_dump(mode="json") for o in c.initial]} for c in cases],
        "protocol": ("fixed-H Task Loop; no Improver" if target == "workflow" else
                     "fixed-M Task Loop; validated branch-local W updates; nested costs included"),
        "scope": scope, "primary_metric_requirement": "Prespecify a primary metric, costs, failures, early-stop and missing-result handling. Repeated rounds are not independent cases."}
    context["available_execution_evidence"] = {"authority": "controller_execution_receipts",
        "fields": ["tool_result_failures", "invalid_response_count", "repair_count", "llm_elapsed_seconds",
                   "context_reads", "context_bytes_total", "candidate_checks"],
        "metric_input": "cases[*].execution.baseline/challenger",
        "requirement": "Reliability or efficiency claims require relevant execution evidence in addition to fitness; absence of the triggering failure is not proof of a fix."}
    store.put("evaluation_trial_inputs", eid, {"target": target, "context": context,
              "max_top_n": max(c.query_budget for c in cases)}, immutable=True)
    try:
        plan = ensure_evaluation_plan(eval_store, eval_team, evaluation_id=eid, target=target,
            context=context, max_top_n=max(c.query_budget for c in cases)) if policy.criterion == "llm_adjudicated_v1" else None
        spec = provisional.model_copy(update={"metric_plan_ref": plan["plan_ref"] if plan else None})
        store.put("evaluation_specs", spec.spec_ref, spec.model_dump(mode="json"), immutable=True)
        store.put("harness_protocols", eid, {"gate": spec.gate, "baseline": base.version,
            "challenger": child.version, "manifest": manifest, "spec_ref": spec.spec_ref,
            "plan_ref": spec.metric_plan_ref}, immutable=True)
        # Fork IDs depend on the frozen attempt, not the E-plan's generated text.
        store.put("evaluation_spec_attempts", spec.spec_ref, {"evaluation_id": eid}, immutable=True)
        traces = []
        for case, case_spec in zip(cases, spec.cases):
            # Each arm owns its team, store, memory and feedback. Nested M->W
            # trials use their own pool rather than starving the outer pair.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="harness-arm") as pool:
                futures = {arm: pool.submit(_run_arm, run, spec, case, case_spec, arm,
                                           plan, team_factory)
                           for arm in ("baseline", "challenger")}
                traces.append({arm: future.result() for arm, future in futures.items()})
        if plan:
            from proteinrsi.evaluation_metrics import execute_evaluation_metrics
            facts = execute_evaluation_metrics(eval_store, plan, _envelope(plan, task, cases, traces))
            costs = {"cases": [{a: {k: t[k] for k in ("usage", "loop_result", "completed_new_rounds", "total_submitted_wells", "execution_summary")}
                                 for a, t in pair.items()} for pair in traces]}
            result = adjudicate_evaluation(eval_store, eval_team, evaluation_id=eid, plan_ref=spec.metric_plan_ref,
                evidence={facts["result_ref"]: facts, "harness_costs/" + eid: costs},
                n_baseline=facts["denominators"]["n_groups"], n_challenger=facts["denominators"]["n_groups"])
            validate_saved_metric_evidence(store, result.model_dump(), plan)
        else:
            result = _numeric_result(traces, policy)
        report = {"evaluation_id": eid, "spec_ref": spec.spec_ref, "protocol": protocol,
            "patch_id": patch.patch_id, "target": target, "baseline": base.version,
            "challenger": child.version, "manifest_hash": manifest, "phase": "confirmation" if confirmation else "validation",
            "gate": result.model_dump(mode="json"), "traces": traces, "scope": scope,
            "budget_scope": spec.budget_scope, "copied_evidence_charged": False,
            "budget_before": prior["budget_before"], "promoted": False,
            "publication_deferred": defer_publication, "rsi_efficacy": "not_established_by_adoption",
            "evaluation_scope": "fixed_M_with_branch_local_W_updates" if target == "meta" else "fixed_W",
            "initial_workflow": initial_workflow.version,
            "replay_experiment_cost": {"amount": 0, "basis": "historical_replay_no_new_wetlab"}
                if task.feedback_source == "measured_replay" else None}
        if budget_store is not store:
            usage = budget_store.usage()
            if any(v["reserved"] for v in usage.values()):
                raise Conflict("Reconcile reserved comparison calls before publication")
            # Record actual charges. Unspent holds are released after the
            # parent commits; resuming a completed report reconciles cleanup.
            report["budget_after"] = {r: {k: v for k, v in counts.items() if k != "allocated_unspent"}
                                      for r, counts in usage.items()}
            report["main_budget_before"] = prior["main_budget_before"]
            report["main_budget_after"] = store.usage()
        with (nullcontext() if defer_publication else store.lock()), store.transaction():
            if store.get("harness_attempts", eid)["state"] == "abandoned":
                raise Conflict("Evaluation was abandoned while executing")
            current = campaign.state
            if not confirmation and not defer_publication:
                if current[pending_key] != raw or cls.model_validate(current[target]).version != base.version:
                    raise Conflict("Parent changed before publication")
                if promote:
                    campaign.memory.record(patch, result, campaign_id=current["campaign_id"],
                        observations=run.draft.get("parent_context", {}).get("observations", len(case.initial)),
                        source=task.feedback_source)
                    campaign.methods.complete(current, patch, result, child, evaluation_ref="harness_evaluations/" + eid)
                    report["promoted"] = result.decision == "accepted"
                else:
                    campaign.methods.transition(current, patch, "staged", {"report_only": eid})
            for case in cases:
                store.put("harness_case_bindings", _episode_identity(case),
                    {"phase": "confirmation" if confirmation else "validation"}, immutable=True)
            if budget_store is store:
                store.release_allocations(eid)
                report["budget_after"] = store.usage()
            store.put("harness_evaluations", eid, report, immutable=True)
            store.put("harness_attempts", eid, {**prior, "state": "completed", "report_digest": digest(report)})
            store.event("harness_evaluated", {"evaluation_id": eid, "spec_ref": spec.spec_ref,
                "target": target, "decision": result.decision, "promoted": report["promoted"], "confirmation": confirmation})
        if budget_store is not store:
            budget_store.release_allocations(eid)
        return report
    except Exception as exc:
        if store.get("harness_attempts", eid, {}).get("state") in {"completed", "abandoned"}:
            # Publication has committed; cleanup failure cannot undo it.
            raise
        paused = is_provider_paused(exc)
        store.put("harness_attempts", eid, {**prior, "state": "paused_provider" if paused else "blocked",
                                           "pause_reason": getattr(exc, "reason", "provider_paused") if paused else None,
                                           "error_type": type(exc).__name__})
        raise


def evaluate_method(campaign, cases, **kwargs):
    """One publication path; persistent memory sync never re-runs paid evaluation."""
    run = prepare_evaluation(campaign, cases, **kwargs)
    report = run if isinstance(run, dict) else execute_evaluation(run, team_factory=kwargs.get("team_factory"))
    from proteinrsi.memory import sync_shared_memory
    sync_shared_memory(campaign)
    return report


def evaluate_workflow_loop(campaign, cases, **kwargs):
    """Compatibility entry point for the fixed-Workflow comparison."""
    return evaluate_method(campaign, cases, target="workflow", **kwargs)
