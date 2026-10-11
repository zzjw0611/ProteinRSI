# SPDX-License-Identifier: MIT
"""Paired W/M comparisons through the public Task Loop.

Frozen input manifests contain no future labels. Cases/feedback providers are
trusted controller inputs; child Campaigns never receive a full measurement map.
"""
from collections import defaultdict

import numpy as np

from proteinrsi.contracts import (Candidate, EvaluationCaseSpec, EvaluationPolicy, EvaluationSpec,
    GatePolicy, GateResult, MetaPolicy, Observation, Patch, TaskSpec, Workflow, digest)
from proteinrsi.evaluation import _measurement_denominators, validate_saved_metric_evidence
from proteinrsi.evaluation_fork import freeze_resources, freeze_start, fork_for_evaluation
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
        if t.budget.llm_calls is None or t.budget.tool_calls is None:
            raise ValueError("Comparisons require finite equal compute caps")
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
        aligned.append({"case_id": case.case_id, "group_id": case.group_id, "arms": arms})
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


def _evaluate_method(campaign, cases, *, target="workflow", promote=False, patch_id=None, team_factory=None,
                     scope="declared_cases", start_overrides=None):
    """Resume a frozen comparison; final confirmation never selects or publishes."""
    from proteinrsi.harness_evaluation import _episode_identity, _saved_report
    from proteinrsi.llm_evaluation import ensure_evaluation_plan, adjudicate_evaluation
    store = campaign.store
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
        if prior and prior["promote_requested"] != promote:
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
            resources, _memory, refs = freeze_resources(campaign)
            # No live-task structural artifacts silently reused for another protein.
            if resources["artifacts"] and any(c.task.reference_sequence != task.reference_sequence for c in cases):
                raise ValueError("Cross-protein evaluation needs case-scoped artifacts")
            if resources["configuration"]["protein_model"] and not resources["protein_pin"]:
                raise ValueError("Resolve the declared protein model before comparison")
            starts, allocations = [], {}
            for case in cases:
                overrides = (start_overrides or {}).get(case.case_id, {})
                ref = freeze_start(store, case, **overrides)
                caps = {"experimental_wells": case.query_budget, "llm_calls": case.task.budget.llm_calls,
                        "tool_calls": case.task.budget.tool_calls}
                if resources["configuration"]["protein_model"]:
                    caps["plm_inputs"] = resources["configuration"]["protein_model"]["max_model_inputs"]
                starts.append(EvaluationCaseSpec(case_id=case.case_id, group_id=case.group_id,
                    start_ref=ref, rounds=case.task.max_rounds, resource_limits=caps))
            evaluator_limits = {"experimental_wells": 0,
                "llm_calls": settings.evaluator_llm_calls if policy.criterion == "llm_adjudicated_v1" else 0,
                "tool_calls": settings.evaluator_tool_calls if policy.criterion == "llm_adjudicated_v1" else 0}
            floor = settings.main_reserve_wells
            if floor is None:
                floor = task.batch_size if state["status"] != "complete" and state["round_index"] < task.max_rounds else 0
            draft_spec = EvaluationSpec(protocol=protocol, target=target,
                initial_workflow_ref=state["method_governance"]["active"]["workflow"] if target == "meta" else None, purpose="confirmation" if confirmation else "selection", scope=scope,
                patch_id=patch.patch_id, baseline_version=base.version, candidate_version=child.version,
                manifest_hash=manifest, resource_snapshot_ref=refs[0], memory_snapshot_ref=refs[1],
                cases=starts, gate=policy.model_dump(), evaluator_limits=evaluator_limits,
                main_reserve_wells=floor, promotion_requested=promote)
            draft = {"spec": draft_spec.model_dump(mode="json"), "meta": state["meta"],
                     "workflow": state["workflow"], "evaluation_id": eid}
            # Immutable input draft, including E's authorized maximum resources, precedes calls.
            store.put("evaluation_drafts", eid, draft, immutable=True)
        provisional = EvaluationSpec.model_validate(draft["spec"])
        meta = MetaPolicy.model_validate(draft["meta"])
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
        if prior is None:
            with store.transaction():
                store.reserve_allocations(eid, allocations, keep_free={"experimental_wells": provisional.main_reserve_wells})
                prior = {"protocol": protocol, "target": target, "state": "started", "patch_id": patch.patch_id,
                    "manifest": manifest, "confirmation": confirmation, "promote_requested": promote,
                    "budget_before": store.usage()}
                if not confirmation:
                    campaign.methods.begin(state, patch)
                store.put("harness_attempts", eid, prior)
                for case in cases:
                    store.put("harness_case_bindings", _episode_identity(case),
                        {"phase": "confirmation" if confirmation else "validation"}, immutable=True)
        eval_store = AllocatedBudgetView(store, eid + "/evaluator")
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
                pair = {}
                for arm, method in (("baseline", base), ("challenger", child)):
                    workflow = method if target == "workflow" else initial_workflow
                    arm_meta = meta if target == "workflow" else method
                    branch = fork_for_evaluation(campaign, spec, case_spec, workflow, arm_meta, arm=arm,
                        team_factory=team_factory or (lambda s: make_validation_team(campaign, s)))
                    cached = branch.store.get("loop_arm_results", "result")
                    if cached is None:
                        feedback = CaseFeedback(case)
                        def checkpoint(current):
                            from proteinrsi.evaluation_scheduler import schedule_evaluation
                            current._optional_improvement()
                            schedule_evaluation(current, feedback, team_factory=team_factory)
                        result = run_task_loop(branch, feedback, operator="authorized-method-evaluation",
                            on_checkpoint=checkpoint if target == "meta" else None)
                        if result.status in {"paused", "waiting_feedback", "waiting_approval"}:
                            from proteinrsi.llm import ProviderPaused
                            raise ProviderPaused("Validation branch paused; resume the same attempt")
                        if any(a.get("state") in {"started", "paused_provider", "blocked"}
                               for a in branch.store.all("harness_attempts").values()):
                            raise Conflict("Nested evaluation is unfinished; reconcile before closing the M arm")
                        start = store.get("evaluation_starts", case_spec.start_ref)
                        cached = _trace(branch, case, result, workflow, arm_meta,
                            plan["plan"]["top_ns"] if plan else policy.top_ns, start["round_index"])
                        branch.store.put("loop_arm_results", "result", cached, immutable=True)
                    pair[arm] = cached
                traces.append(pair)
            if plan:
                from proteinrsi.evaluation_metrics import execute_evaluation_metrics
                facts = execute_evaluation_metrics(eval_store, plan, _envelope(plan, task, cases, traces))
                costs = {"cases": [{a: {k: t[k] for k in ("usage", "loop_result", "completed_new_rounds", "total_submitted_wells")}
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
                "budget_scope": "shared_campaign", "copied_evidence_charged": False,
                "budget_before": prior["budget_before"], "promoted": False, "rsi_efficacy": "not_established_by_adoption",
                "evaluation_scope": "fixed_M_with_branch_local_W_updates" if target == "meta" else "fixed_W",
                "initial_workflow": initial_workflow.version,
                "replay_experiment_cost": {"amount": 0, "basis": "historical_replay_no_new_wetlab"}
                    if task.feedback_source == "measured_replay" else None}
            with store.transaction():
                current = campaign.state
                if not confirmation:
                    if current[pending_key] != raw or cls.model_validate(current[target]).version != base.version:
                        raise Conflict("Parent changed before publication")
                    if promote:
                        campaign.memory.record(patch, result, campaign_id=state["campaign_id"],
                            observations=len(state["observations"]), source=task.feedback_source)
                        campaign.methods.complete(current, patch, result, child, evaluation_ref="harness_evaluations/" + eid)
                        report["promoted"] = result.decision == "accepted"
                    else:
                        campaign.methods.transition(current, patch, "staged", {"report_only": eid})
                for case in cases:
                    store.put("harness_case_bindings", _episode_identity(case),
                        {"phase": "confirmation" if confirmation else "validation"}, immutable=True)
                store.release_allocations(eid)
                report["budget_after"] = store.usage()
                store.put("harness_evaluations", eid, report, immutable=True)
                store.put("harness_attempts", eid, {**prior, "state": "completed", "report_digest": digest(report)})
                store.event("harness_evaluated", {"evaluation_id": eid, "spec_ref": spec.spec_ref,
                    "target": target, "decision": result.decision, "promoted": report["promoted"], "confirmation": confirmation})
            return report
        except Exception as exc:
            store.put("harness_attempts", eid, {**prior, "state": "paused_provider" if is_provider_paused(exc) else "blocked",
                                               "error_type": type(exc).__name__})
            raise


def evaluate_method(campaign, cases, **kwargs):
    """One publication path; persistent memory sync never re-runs paid evaluation."""
    report = _evaluate_method(campaign, cases, **kwargs)
    from proteinrsi.memory import sync_shared_memory
    sync_shared_memory(campaign)
    return report


def evaluate_workflow_loop(campaign, cases, **kwargs):
    """Compatibility entry point for the fixed-Workflow comparison."""
    return evaluate_method(campaign, cases, target="workflow", **kwargs)
