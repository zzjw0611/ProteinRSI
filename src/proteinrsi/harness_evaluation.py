# SPDX-License-Identifier: MIT
"""Independent fixed-H comparisons, using the existing isolated runner and sponsor.

H0/H1 see identical initial evidence, not campaign memory or each other's results.
This does not claim that a program change is a general capability or full RSI.
"""
from collections import defaultdict

import numpy as np

from proteinrsi.contracts import GatePolicy, MetaPolicy, Patch, TaskSpec, Workflow, digest
from proteinrsi.evaluation import (_offspring_score, _offline_metric_envelope,
    compare_grouped_metrics, validate_saved_metric_evidence)
from proteinrsi.improvement import apply_patch, compare_scores
from proteinrsi.online_meta import make_validation_team
from proteinrsi.recovery import is_provider_paused
from proteinrsi.storage import Conflict


def _episode_identity(case):
    # Renaming a case or changing the seed, budget or sample IDs does not make
    # the same scientific starting evidence an independent confirmation case.
    task = case.task.model_dump(mode="json")
    for field in ("seed", "budget", "max_rounds", "name", "description"):
        task.pop(field, None)
    initial = sorted((o.sequence, o.value, o.metric, o.unit, o.assay_protocol) for o in case.initial)
    return digest({"task": task, "initial": initial})


def _saved_report(store, evaluation_id, attempt):
    saved = store.get("harness_evaluations", evaluation_id)
    if saved is None or digest(saved) != attempt["report_digest"]:
        raise Conflict("Missing or modified comparison report")
    plan_ref = (saved["gate"].get("details") or {}).get("evaluation_plan_ref")
    if plan_ref:
        from proteinrsi.llm_evaluation import load_evaluation_plan
        plan = load_evaluation_plan(store, plan_ref)
        validate_saved_metric_evidence(store, saved["gate"], plan)
    return saved


def _validate_cases(campaign, cases, *, confirmation):
    if not cases or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Nonempty, unique independent cases are required")
    task = TaskSpec.model_validate(campaign.state["task"])
    seen = {o["sequence"] for o in campaign.state["observations"]}
    groups = {}
    for c in cases:
        from proteinrsi.tasks import validate_task, validate_candidate
        from proteinrsi.contracts import Candidate
        validate_task(c.task)
        for observed in c.initial:
            validate_candidate(c.task, Candidate(sequence=observed.sequence))
            if (observed.metric, observed.unit, observed.assay_protocol) != (
                    c.task.metric, c.task.unit, c.task.assay_protocol):
                raise ValueError("Case evidence assay and units must match its task")
        if any(not np.isfinite(value) for value in c.labels.values()):
            raise ValueError("Case labels must be finite measured values")
        episode = _episode_identity(c)
        if episode in groups and groups[episode] != c.group_id:
            raise ValueError("Repeated starting evidence must share one evaluation group")
        groups[episode] = c.group_id
        if c.split != ("test" if confirmation else "validation"):
            raise ValueError("Use validation cases for version selection; test cases for report-only confirmation")
        if (c.task.kind, c.task.metric, c.task.unit, c.task.direction) != (
                task.kind, task.metric, task.unit, task.direction):
            raise ValueError("Cases must use the same task kind, metric, unit and direction")
        if c.query_budget < 1 or len(c.initial) + c.query_budget > c.task.budget.experimental_wells:
            raise ValueError("Case initial disclosures and queries exceed its budget")
        if c.task.budget.llm_calls is None or c.task.budget.tool_calls is None:
            raise ValueError("Independent comparisons require finite per-arm LLM/tool caps")
        initial = {o.sequence for o in c.initial}
        if len(initial) != len(c.initial) or any(o.qc != "valid" or o.sequence not in c.labels
                or o.value != c.labels[o.sequence] for o in c.initial):
            raise ValueError("Initial case evidence must be unique valid measured labels")
        if c.history:
            raise ValueError("Independent H cases start from initial observations, not supplied task/Meta history")
        # Same-protein held-out cases must not hide labels that generated the patch.
        if c.task.reference_sequence == task.reference_sequence and (set(c.labels) & seen) - initial:
            raise ValueError("Previously disclosed campaign labels must be declared as initial evidence")
    return task


def evaluate_workflow(campaign, cases, *, promote=False, patch_id=None, team_factory=None):
    """Validate a pending W; --patch-id confirms a frozen archived W, never publishes it."""
    from proteinrsi.llm_evaluation import ensure_evaluation_plan, adjudicate_evaluation
    store = campaign.store
    confirmation = patch_id is not None
    if confirmation and promote:
        raise ValueError("Confirmation is report-only; it cannot select or publish a version")
    with store.lock():
        state = campaign.state
        task = _validate_cases(campaign, cases, confirmation=confirmation)
        if state["pending_batch"] or state["status"] not in {"ready", "complete"}:
            raise Conflict("Independent evaluation requires a safe boundary without a pending batch")
        raw = store.get("patches", patch_id) if confirmation else state["pending_patch"]
        if raw is None and not confirmation:
            manifest = digest([c.model_dump(mode="json") for c in cases])
            completed = [(key, a) for key, a in store.all("harness_attempts").items()
                if a["state"] == "completed" and a["manifest"] == manifest
                and a["promote_requested"] == promote and not a["confirmation"]]
            if completed:
                key, attempt = completed[-1]
                return _saved_report(store, key, attempt)
        if raw is None:
            raise Conflict("Stage a workflow patch first, or select an archived patch for confirmation")
        patch = Patch.model_validate(raw)
        if patch.target != "workflow":
            raise ValueError("This evaluator compares Task Harness versions, not Meta descendants")
        record = store.get("method_candidates", patch.patch_id)
        if not record:
            raise Conflict("Independent comparisons require recorded executable snapshots")
        if confirmation and store.get("method_candidate_states", patch.patch_id, {}).get("status") != "accepted":
            raise Conflict("Confirm an already accepted version; do not select versions using test feedback")
        if not confirmation and (state["status"] != "ready" or state["pending_meta"]):
            raise Conflict("Validate the pending workflow at an idle method boundary")
        base = Workflow.model_validate(campaign.methods._validated_snapshot(
            "workflow", record["base_snapshot_ref"])["definition"])
        child = Workflow.model_validate(campaign.methods._validated_snapshot(
            "workflow", record["candidate_snapshot_ref"])["definition"])
        if child.version != apply_patch(base, patch).version:
            raise Conflict("Candidate snapshot differs from the proposed change")
        policy = GatePolicy.model_validate(state["gate"])
        meta = MetaPolicy.model_validate(state["meta"])
        manifest = digest([c.model_dump(mode="json") for c in cases])
        evaluation_id = "harness-" + digest({"patch": patch.patch_id, "manifest": manifest,
            "phase": "confirmation" if confirmation else "validation"})
        prior = store.get("harness_attempts", evaluation_id)
        if prior and prior["promote_requested"] != promote:
            raise Conflict("Do not repurpose a measured comparison's frozen publication intent")
        if prior and prior["state"] == "completed":
            return _saved_report(store, evaluation_id, prior)
        if prior and prior["state"] not in {"started", "paused_provider"}:
            raise Conflict("Reconcile the blocked comparison before retrying")
        for key, attempt in store.all("harness_attempts").items():
            if key != evaluation_id and attempt["state"] in {"started", "paused_provider", "blocked"}:
                raise Conflict("Another independent comparison is unfinished")
        campaign.methods.assert_active(state)
        if prior is None and not confirmation:
            campaign.methods.assert_plannable(state)
        local = store.get("configuration", "local_tools", {})
        if any(e.get("enabled") for e in local.get("engines", {}).values()):
            raise ValueError("Independent cases need case-scoped external artifacts; cannot reuse live-task structures")
        protein = store.get("configuration", "protein_model")
        pin = store.get("protein_backend", "snapshot")
        if protein and not pin:
            raise ValueError("Resolve the configured protein model before comparison")
        required = sum(2 * (len(c.initial) + c.query_budget) for c in cases)
        if prior is None and required > store.remaining("experimental_wells"):
            raise ValueError("Independent comparison exceeds the shared experimental budget")
        if prior is None:
            # Reserve comparison headroom logically before either arm starts. The
            # actual calls still reserve/settle individually on the shared ledger.
            for resource in ("llm_calls", "tool_calls"):
                cap = sum(2 * getattr(c.task.budget, resource) for c in cases)
                if resource == "llm_calls" and policy.criterion == "llm_adjudicated_v1":
                    cap += 6  # E-plan/E-verdict plus bounded format repairs.
                available = store.remaining(resource)
                if available is not None and cap > available:
                    raise ValueError("Declared equal-arm " + resource + " caps exceed the shared budget")
        for case in cases:
            episode = _episode_identity(case)
            binding = store.get("harness_case_bindings", episode)
            phase = "confirmation" if confirmation else "validation"
            if binding and binding["phase"] != phase:
                raise ValueError("Selection cases cannot be reused as independent confirmation cases")
        research = store.get("configuration", "research")
        # No initial fitting data or hidden labels appear in E/worker context beyond the explicit initial set.
        context = {"patch": patch.model_dump(mode="json"), "task": task.model_dump(mode="json"),
            "baseline_workflow": base.model_dump(), "candidate_workflow": child.model_dump(),
            "cases": [{"case_id": c.case_id, "group_id": c.group_id, "split": c.split,
                "task": c.task.model_dump(mode="json"), "query_budget": c.query_budget,
                "initial": [o.model_dump(mode="json") for o in c.initial]} for c in cases],
            "protocol": "fixed-H paired independent cases; no Improver call; equal arm compute caps",
            "scope": "declared_cases_only", "required_query_slots": required,
            "primary_metric_requirement": "Choose the primary task metric before outcomes; report costs and failures too."}
        store.put("evaluation_trial_inputs", evaluation_id, {"target": "workflow", "context": context,
            "max_top_n": max(c.query_budget for c in cases)}, immutable=True)
        if prior is None:
            for case in cases:
                episode = _episode_identity(case)
                store.put("harness_case_bindings", episode, {"phase": "confirmation" if confirmation else "validation"}, immutable=True)
            prior = {"state": "started", "patch_id": patch.patch_id, "manifest": manifest,
                "confirmation": confirmation, "promote_requested": promote, "budget_before": store.usage()}
            if not confirmation:
                campaign.methods.begin(state, patch)
            store.put("harness_attempts", evaluation_id, prior)
        try:
            llm_gate = policy.criterion == "llm_adjudicated_v1"
            plan = ensure_evaluation_plan(store, campaign.team, evaluation_id=evaluation_id,
                target="workflow", context=context, max_top_n=max(c.query_budget for c in cases)) if llm_gate else None
            # Numeric protocol is also frozen before any branch executes.
            store.put("harness_protocols", evaluation_id, {"gate": policy.model_dump(),
                "baseline": base.version, "challenger": child.version, "manifest": manifest,
                "plan_ref": plan["plan_ref"] if plan else None}, immutable=True)
            traces = []
            for i, case in enumerate(cases):
                pair = {}
                for arm, workflow in (("baseline", base), ("challenger", child)):
                    prefix = evaluation_id + f"/{i}/{arm}"
                    _, trace = _offspring_score(case, workflow, meta,
                        str(store.root / "harness-validation" / evaluation_id / str(i) / arm),
                        team_factory or (lambda branch: make_validation_team(campaign, branch)),
                        protein, pin, research, store.get("configuration", "know_how"), store,
                        prefix, store.get("configuration", "prompt_bundle"), policy,
                        plan["plan"]["top_ns"] if plan else None, improve=False)
                    pair[arm] = trace
                traces.append(pair)
            if llm_gate:
                from proteinrsi.evaluation_metrics import execute_evaluation_metrics
                envelope = _offline_metric_envelope(plan, task, cases, traces)
                facts = execute_evaluation_metrics(store, plan, envelope)
                # Authoritative measured-derived facts plus actual compute cost receipts.
                costs = {"case_costs": [{arm: pair[arm]["usage"] for arm in pair} for pair in traces]}
                result = adjudicate_evaluation(store, campaign.team, evaluation_id=evaluation_id,
                    plan_ref=plan["plan_ref"], evidence={facts["result_ref"]: facts,
                        "harness_costs/" + evaluation_id: costs},
                    n_baseline=facts["denominators"]["n_groups"], n_challenger=facts["denominators"]["n_groups"])
            elif policy.criterion == "observed_pareto_v1":
                result = compare_grouped_metrics(traces, policy)
            else:
                grouped = defaultdict(list)
                for case, pair in zip(cases, traces):
                    grouped[case.group_id].append([pair[a]["metric_summary"]["signed_metrics"]["avg"]
                                                  for a in ("baseline", "challenger")])
                if any(v is None for values in grouped.values() for row in values for v in row):
                    from proteinrsi.contracts import GateResult
                    result = GateResult(decision="inconclusive", reason="Missing measured case outcomes",
                                        n_baseline=len(grouped), n_challenger=len(grouped))
                else:
                    means = [np.mean(rows, axis=0) for rows in grouped.values()]
                    result = compare_scores([float(x[0]) for x in means], [float(x[1]) for x in means], policy, paired=True)
            report = {"evaluation_id": evaluation_id, "patch_id": patch.patch_id, "target": "workflow",
                "baseline": base.version, "challenger": child.version, "manifest_hash": manifest,
                "phase": "confirmation" if confirmation else "validation", "gate": result.model_dump(mode="json"),
                "traces": traces, "required_query_slots": required, "budget_scope": "shared_campaign",
                "budget_before": prior["budget_before"], "budget_after": store.usage(), "promoted": False,
                "scope": "declared_cases_only", "rsi_efficacy": "not_evaluated",
                "interpretation": "Matched-case Task Harness evidence; not a proof of improved Meta or cross-protein generality."}
            if llm_gate:
                validate_saved_metric_evidence(store, result.model_dump(), plan)
            with store.transaction():
                current = campaign.state
                if not confirmation:
                    if current["pending_patch"] != patch.model_dump(mode="json") or current["workflow"] != state["workflow"]:
                        raise Conflict("Pending candidate or active method changed; do not publish")
                    if promote:
                        campaign.memory.record(patch, result, campaign_id=state["campaign_id"],
                            observations=len(state["observations"]), source=task.feedback_source)
                        campaign.methods.complete(current, patch, result, child,
                            evaluation_ref="harness_evaluations/" + evaluation_id)
                        report["promoted"] = result.decision == "accepted"
                    else:
                        campaign.methods.transition(current, patch, "staged", {"report_only": evaluation_id})
                store.put("harness_evaluations", evaluation_id, report, immutable=True)
                store.put("harness_attempts", evaluation_id, {**prior, "state": "completed", "report_digest": digest(report)})
                store.event("harness_evaluated", {"evaluation_id": evaluation_id, "patch_id": patch.patch_id,
                    "decision": result.decision, "promoted": report["promoted"], "confirmation": confirmation})
            return report
        except Exception as exc:
            # Never silently start a fresh comparison after failure. Paid branch
            # calls persist; only known provider pauses resume the same request.
            status = "paused_provider" if is_provider_paused(exc) else "blocked"
            store.put("harness_attempts", evaluation_id, {**prior, "state": status,
                      "error_type": type(exc).__name__})
            store.event("harness_evaluation_paused", {"evaluation_id": evaluation_id,
                "error_type": type(exc).__name__, "state": status})
            raise


def abandon_evaluation(campaign, evaluation_id, *, operator, reason, acknowledge_uncertain=False):
    """Reconcile an interrupted comparison without deleting results or refunding costs."""
    if not evaluation_id or not operator.strip() or not reason.strip():
        raise ValueError("An evaluation ID, operator and reason are required")
    if not acknowledge_uncertain:
        raise Conflict("Confirm all external work has stopped or been reconciled")
    with campaign.store.lock(), campaign.store.transaction():
        state = campaign.state
        if state["pending_batch"]:
            raise Conflict("Cannot abandon a comparison through a pending experimental batch")
        attempt = campaign.store.get("harness_attempts", evaluation_id)
        if not attempt or attempt["state"] not in {"started", "paused_provider", "blocked"}:
            raise Conflict("No unfinished comparison with this ID")
        pending = state.get("pending_patch")
        if pending and Patch.model_validate(pending).patch_id == attempt["patch_id"]:
            campaign.methods.finish(state, Patch.model_validate(pending), "cancelled",
                detail={"operator": operator, "reason": reason, "evaluation_id": evaluation_id})
        campaign.store.put("harness_attempts", evaluation_id, {**attempt, "state": "abandoned",
            "operator": operator, "reason": reason})
        campaign.store.event("harness_evaluation_abandoned", {"evaluation_id": evaluation_id,
            "operator": operator, "reason": reason, "refunded_queries": 0})
