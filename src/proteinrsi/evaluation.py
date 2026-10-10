# SPDX-License-Identifier: MIT
"""Independent improver evaluation: compare descendants, not self-assessed reflections."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from math import isfinite
from tempfile import TemporaryDirectory
from typing import Callable
import json

import numpy as np

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import (GatePolicy, MetaPolicy, Model, Observation, Patch,
                                  TaskSpec, TaskView, Workflow, digest)
from proteinrsi.improvement import apply_patch, compare_scores, compare_metric_vectors
from proteinrsi.metrics import metric_names, summarize_metrics
from proteinrsi.storage import Conflict, Store, SponsoredStore


class MetaCase(Model):
    case_id: str
    group_id: str
    split: str
    task: TaskSpec
    initial: list[Observation]
    labels: dict[str, float]
    query_budget: int = 6
    history: list[dict] = []


def read_cases(path: str | Path) -> list[MetaCase]:
    """Operator/evaluator-only input. Labels are NEVER forwarded into a TaskView."""
    cases = [MetaCase.model_validate(c) for c in json.loads(Path(path).read_text())]
    if not cases or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Evaluation requires nonempty, uniquely identified cases")
    if any(c.split not in ("development", "validation", "test") or c.query_budget < 1 for c in cases):
        raise ValueError("Invalid split/query budget")
    if any(o.sequence not in c.labels or o.value != c.labels[o.sequence] for c in cases for o in c.initial):
        raise ValueError("Initial observations must agree with evaluator-owned measurements")
    return cases


def _offspring_score(case: MetaCase, workflow: Workflow, meta: MetaPolicy,
                     directory: str, team_factory: Callable[[Store], Team] | None,
                     protein_config: dict | None = None, protein_pin: dict | None = None,
                     research_config: dict | None = None, know_how: list | None = None,
                     sponsor=None, prefix="", prompt_bundle=None, gate_policy: GatePolicy | None = None,
                     selected_top_ns: list[int] | None = None) -> tuple[float | None, dict]:
    store = SponsoredStore(directory, sponsor, prefix) if sponsor is not None else Store(directory)
    llm_evaluation = gate_policy is not None and gate_policy.criterion == "llm_adjudicated_v1"
    if llm_evaluation:
        # Trusted evaluator-only state. Guarded research workers cannot read this
        # namespace or the case labels; their public input remains TaskView only.
        store.put("offline_arm_inputs", "input", {"case_digest": digest(case.model_dump(mode="json")),
            "workflow": workflow.version, "meta": meta.version, "top_ns": selected_top_ns}, immutable=True)
        cached = store.get("offline_arm_results", "result")
        if cached is not None:
            return cached["score"], cached["trace"]
    resources = case.task.budget.model_dump()
    if protein_config:
        resources["plm_inputs"] = protein_config["max_model_inputs"]
        store.put("configuration", "protein_model", protein_config, immutable=True)
        if protein_pin:
            store.put("protein_backend", "snapshot", protein_pin, immutable=True)
    store.configure_budget(resources)
    if research_config is not None:
        store.put("configuration", "research", research_config, immutable=True)
        store.put("configuration", "know_how", know_how or [], immutable=True)
    from proteinrsi.localtools.config import LocalToolsConfig
    store.put("configuration", "local_tools", LocalToolsConfig().model_dump(), immutable=True)
    if prompt_bundle is not None:
        store.put("configuration", "prompt_bundle", prompt_bundle, immutable=True)
    # No free warm-start labels: each arm's initial disclosures are charged explicitly.
    if sponsor is not None and case.initial:
        store.reserve("initial-observations", "experimental_wells", len(case.initial),
                      [o.model_dump(mode="json") for o in case.initial])
        store.settle("initial-observations")
    team = team_factory(store) if team_factory else Team(store)
    # Read only observed data in the policy context. No labels, global extrema or paths.
    view = TaskView(task=case.task, round_index=1, observations=case.initial,
                    history=case.history, remaining_wells=store.remaining("experimental_wells"),
                    workflow=workflow, meta=meta,
                    research_context={"acceptance_policy": gate_policy.model_dump()}
                    if gate_policy and gate_policy.criterion in {"observed_pareto_v1", "llm_adjudicated_v1"} else {})
    if getattr(team, "guarded", False):
        from proteinrsi.replay.broker import GuardedMetaAgent
        proposal = GuardedMetaAgent(team).propose(view)
    else:
        proposal = MetaAgent(team.llm, store, getattr(team, "tools", None)).propose(view)
    child = workflow
    if proposal.patch and proposal.patch.target == "workflow":
        if proposal.patch.task_kind != case.task.kind:
            raise ValueError("Meta proposal has incorrect scope")
        child = apply_patch(workflow, proposal.patch)
    # Freeze the improver for this one-step diagnostic. Full recursive campaigns use runtime.py.
    view.workflow = child
    store.put("campaign", "state", {"task": case.task.model_dump(mode="json")})
    candidates = team.run(view)
    observed = {o.sequence for o in case.initial}
    multimetric = gate_policy is not None and gate_policy.criterion in {"observed_pareto_v1", "llm_adjudicated_v1"}
    if multimetric:
        observed.add(case.task.reference_sequence)
    selected = [c for c in candidates if c.sequence not in observed][:case.query_budget]
    if len(selected) != case.query_budget or (not multimetric and any(c.sequence not in case.labels for c in selected)):
        raise ValueError("Cannot fairly evaluate this descendant with the stated measurement budget")
    store.reserve("evaluation-query", "experimental_wells", case.query_budget, [c.sequence for c in selected])
    store.settle("evaluation-query")
    if multimetric:
        measured_rows = [
            {"sequence": c.sequence, "value": case.labels.get(c.sequence),
             "qc": "valid" if c.sequence in case.labels else "unavailable"} for c in selected]
        summary = (summarize_evaluation_metrics if llm_evaluation else summarize_metrics)(
            measured_rows, direction=case.task.direction,
            top_ns=selected_top_ns if llm_evaluation else gate_policy.top_ns, submitted=case.query_budget)
        trace = {"case_id": case.case_id, "group_id": case.group_id, "meta": meta.version,
            "descendant": child.version, "selected": [c.sequence for c in selected],
            "metric_summary": summary, "parent_excluded": True, "usage": store.usage(),
            "observed_rows": measured_rows, "submitted": case.query_budget,
            "metric_summary_scope": "descriptive_legacy_summaries_not_custom_program_output"}
        score = summary["signed_metrics"]["avg"]
        if llm_evaluation:
            store.put("offline_arm_results", "result", {"score": score, "trace": trace}, immutable=True)
        return score, trace
    sign = 1 if case.task.direction == "maximize" else -1
    score = sign * float(np.mean([case.labels[c.sequence] for c in selected]))
    return score, {"case_id": case.case_id, "meta": meta.version, "descendant": child.version,
                   "selected": [c.sequence for c in selected], "signed_score": score, "usage": store.usage()}


def compare_grouped_metrics(traces: list[dict], policy: GatePolicy):
    """Equal case weights within group, equal group weights in each metric.

    Related seeds remain one independent group. A missing case/metric is never
    silently dropped, and a max is computed within each case, not across groups.
    """
    grouped = defaultdict(list)
    case_ids = set()
    missing = False
    for pair in traces:
        old, new = pair["baseline"], pair["challenger"]
        if (old["case_id"] != new["case_id"] or old["group_id"] != new["group_id"]
                or old["case_id"] in case_ids):
            raise ValueError("Meta metrics require unique, aligned cases and groups")
        case_ids.add(old["case_id"])
        for arm in (old, new):
            denom = arm["metric_summary"]["denominators"]
            missing |= bool(denom["unavailable"] or denom["not_returned"])
            missing |= not denom["submitted"] or denom["other_nonvalid"] / max(1, denom["submitted"]) > policy.max_qc_failure_fraction
        if old["metric_summary"]["denominators"]["submitted"] != new["metric_summary"]["denominators"]["submitted"]:
            raise ValueError("Meta cases require equal submitted arm budgets")
        grouped[old["group_id"]].append(pair)
    names = metric_names(policy.top_ns)
    aggregates = {"baseline": {}, "challenger": {}}
    group_vectors = {}
    for group, pairs in sorted(grouped.items()):
        group_vectors[group] = {}
        for arm in aggregates:
            vector = {}
            for name in names:
                values = [p[arm]["metric_summary"]["signed_metrics"][name] for p in pairs]
                vector[name] = float(np.mean(values)) if all(v is not None for v in values) else None
            group_vectors[group][arm] = vector
    for arm in aggregates:
        for name in names:
            values = [vector[arm][name] for vector in group_vectors.values()]
            aggregates[arm][name] = float(np.mean(values)) if values and all(v is not None for v in values) else None
    return compare_metric_vectors(aggregates["baseline"], aggregates["challenger"], policy,
        n_baseline=len(grouped), n_challenger=len(grouped),
        diagnostics={"aggregation": "equal_case_means_within_group_then_equal_group_means",
            "group_metric_vectors": group_vectors, "n_cases": len(traces),
            "unit": "independent_group", "complete_case_metrics_required": True},
        incomplete_reason="Missing case measurement coverage or excessive case QC failures" if missing else None)


def evaluate_meta(campaign, cases: list[MetaCase], *, promote: bool = False,
                  team_factory: Callable[[Store], Team] | None = None) -> dict:
    """Trusted entrypoint. There is deliberately no 'promote arbitrary report.json' API."""
    snapshot = campaign.state
    if snapshot["gate"].get("criterion") == "llm_adjudicated_v1":
        return _evaluate_meta_llm(campaign, cases, promote=promote, team_factory=team_factory)
    if snapshot.get("execution_semantics") != "on-demand-v1":
        raise Conflict("Start a v0.5 study before evaluating a new Meta policy; no silent migration")
    if snapshot["status"] != "ready" or snapshot["pending_batch"] or not snapshot["pending_meta"]:
        raise Conflict("Evaluate a queued meta patch at an idle round boundary")
    if not cases or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Unique evaluation cases are required")
    if promote and any(c.split != "validation" for c in cases):
        raise ValueError("Only designated validation cases can select versions; test is report-only")
    task = TaskSpec.model_validate(snapshot["task"])
    if any((c.task.kind, c.task.metric, c.task.unit, c.task.direction) !=
           (task.kind, task.metric, task.unit, task.direction) for c in cases):
        raise ValueError("Do not average incomparable tasks/units in a promotion gate")
    local_tools = campaign.store.get("configuration", "local_tools", {})
    if any(e.get("enabled") for e in local_tools.get("engines", {}).values()):
        raise ValueError("External-engine meta evaluation needs case-scoped artifact provisioning; "
                         "the current numeric evaluator cannot silently reuse another protein's structure")
    policy = GatePolicy.model_validate(snapshot["gate"])
    base_w = Workflow.model_validate(snapshot["workflow"])
    base_m = MetaPolicy.model_validate(snapshot["meta"])
    patch = Patch.model_validate(snapshot["pending_meta"])
    child_m = apply_patch(base_m, patch)
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    traces = []
    protein_config = campaign.store.get("configuration", "protein_model")
    protein_pin = campaign.store.get("protein_backend", "snapshot")
    research_config = campaign.store.get("configuration", "research")
    know_how = campaign.store.get("configuration", "know_how")
    if protein_config and not protein_pin:
        raise ValueError("Resolve ESMC once with esmc-check before a paired meta evaluation")
    evaluation_id = digest({"patch": patch.patch_id, "manifest": [c.model_dump(mode="json") for c in cases],
                            "workflow": base_w.version})
    required_queries = sum(2*(len(c.initial)+c.query_budget) for c in cases)
    for case in cases:
        if len(case.initial) + case.query_budget > case.task.budget.experimental_wells:
            raise ValueError("Meta case warm start and test exceed the case budget")
        if any(o.sequence not in case.labels or o.value != case.labels[o.sequence] for o in case.initial):
            raise ValueError("Meta initial observations must match evaluator-owned labels")
    # Atomically claim an evaluation attempt: duplicate controllers cannot perform
    # real calls twice under the same sponsor idempotency keys.
    with campaign.store.lock(), campaign.store.transaction():
        current = campaign.state
        if (current["workflow"] != snapshot["workflow"] or current["meta"] != snapshot["meta"]
                or current["pending_meta"] != snapshot["pending_meta"] or current["status"] != "ready"):
            raise Conflict("Campaign changed before Meta evaluation")
        campaign.methods.assert_plannable(current)
        # The claim belongs to the candidate, not just this case manifest. An
        # interrupted evaluation or a prepared online trial must be reconciled
        # before another evaluator can spend budget or publish that candidate.
        for namespace in ("meta_attempts", "meta_online_attempts"):
            if any(attempt.get("patch_id") == patch.patch_id
                   and attempt.get("state") in {"started", "blocked", "planned", "paused_provider"}
                   for attempt in campaign.store.all(namespace).values()):
                raise Conflict("Candidate already has an unfinished Meta evaluation; reconcile it before continuing")
        if campaign.store.get("meta_attempts", evaluation_id):
            raise Conflict("This meta evaluation was already attempted; inspect its report/charges, no silent fresh-budget retry")
        if required_queries > campaign.store.remaining("experimental_wells"):
            raise ValueError("Meta evaluation does not fit the remaining SHARED experimental query budget")
        before_usage = campaign.store.usage()
        campaign.methods.begin(current, patch)
        campaign.store.put("meta_attempts", evaluation_id, {"state": "started", "patch_id": patch.patch_id,
            "required_queries": required_queries})
    prompt_bundle = campaign.store.get("configuration", "prompt_bundle")
    try:
        with TemporaryDirectory(prefix="proteinrsi-meta-") as directory:
            for i, case in enumerate(cases):
                old, trace_old = _offspring_score(case, base_w, base_m, f"{directory}/{i}-old", team_factory,
                                              protein_config, protein_pin, research_config, know_how,
                                              campaign.store, evaluation_id+f"-{i}-old", prompt_bundle, policy)
                new, trace_new = _offspring_score(case, base_w, child_m, f"{directory}/{i}-new", team_factory,
                                              protein_config, protein_pin, research_config, know_how,
                                              campaign.store, evaluation_id+f"-{i}-new", prompt_bundle, policy)
                grouped[case.group_id].append((old, new))
                traces.append({"baseline": trace_old, "challenger": trace_new})
    except Exception as exc:
        with campaign.store.lock(), campaign.store.transaction():
            current = campaign.state
            same_candidate = current["pending_meta"] == snapshot["pending_meta"]
            recoverable = False
            if same_candidate and campaign.methods.enabled:
                from proteinrsi.recovery import is_provider_paused
                if is_provider_paused(exc):
                    campaign.methods.transition(current, patch, "blocked", {
                        "reason": "Offline Meta evaluation uses temporary branch stores; reconcile before another evaluation",
                        "error_type": type(exc).__name__})
                else:
                    recoverable = campaign.methods.failure(current, patch, exc)
            campaign.store.put("meta_attempts", evaluation_id, {"state": "failed" if recoverable else "blocked",
                "patch_id": patch.patch_id, "required_queries": required_queries,
                "error_type": type(exc).__name__})
            campaign.store.event("meta_evaluation_failed", {"evaluation_id": evaluation_id,
                "patch_id": patch.patch_id, "error_type": type(exc).__name__, "blocked": not recoverable})
        raise
    # Seeds/related cases from one protein/group do not count as independent proteins.
    if policy.criterion == "observed_pareto_v1":
        gate = compare_grouped_metrics(traces, policy)
    else:
        means = [np.asarray(scores).mean(axis=0) for scores in grouped.values()]
        gate = compare_scores([float(x[0]) for x in means], [float(x[1]) for x in means], policy, paired=True)
    manifest_hash = digest([c.model_dump(mode="json") for c in cases])
    report = {"patch_id": patch.patch_id, "base_meta": base_m.version, "candidate_meta": child_m.version,
              "base_workflow": base_w.version, "case_manifest_sha256": manifest_hash,
              "protein_model": protein_pin, "protein_configuration": protein_config,
              "research_configuration": research_config, "know_how_snapshot_sha256": digest(know_how),
              "gate": gate.model_dump(), "traces": traces, "promoted": False,
              "protocol": ("one-step frozen-improver; aligned per-case metric vectors and equal group means; observed Pareto"
                  if policy.criterion == "observed_pareto_v1" else
                  "one-step frozen-improver; paired group means; shared study ledger and equal child limits"),
              "budget_scope": "shared_campaign", "required_query_slots": required_queries,
              "budget_before": before_usage, "budget_after": campaign.store.usage(),
              "scope": {"kind": task.kind.value, "metric": task.metric, "unit": task.unit},
              "sources": sorted({c.task.feedback_source for c in cases})}
    key = digest({"patch": patch.patch_id, "manifest": manifest_hash, "base_w": base_w.version,
                  "protein_config": protein_config, "protein_pin": protein_pin,
                  "research": research_config, "know_how": digest(know_how)})
    with campaign.store.lock(), campaign.store.transaction():
        current = campaign.state
        if (current["workflow"] != snapshot["workflow"] or current["meta"] != snapshot["meta"]
            or current["pending_meta"] != snapshot["pending_meta"] or current["status"] != "ready"):
            raise Conflict("Campaign changed while evaluator was running; no version was published")
        if promote:
            campaign.methods.complete(current, patch, gate, child_m, evaluation_ref="meta_evaluations/"+key)
            report["promoted"] = gate.decision == "accepted"
        elif campaign.methods.enabled:
            campaign.methods.transition(current, patch, "staged", {"report_only": key})
        campaign.store.put("meta_evaluations", key, report)
        campaign.store.put("meta_attempts", evaluation_id, {"state": "completed", "report_key": key})
        campaign.store.event("meta_evaluated", {"evaluation_id": key, "promoted": report["promoted"],
                                               "gate": gate.model_dump()})
    return report


def freeze_trial_plan(campaign, state, view, patch, slots, *, evaluation_id=None):
    """Freeze E's task-local criteria before measuring either arm.

    Persist the input separately from any model response: a provider interruption
    cannot replace the criteria input with later artifacts, balances or outcomes.
    """
    from proteinrsi.llm_evaluation import ensure_evaluation_plan
    evaluation_id = evaluation_id or "workflow-" + digest({
        "patch": patch.patch_id, "round": view.round_index,
        "evidence": view.evidence_version, "attempt": state.get("planning_attempt", 0)})[:24]
    saved = campaign.store.get("evaluation_trial_inputs", evaluation_id)
    if saved is None:
        saved = {"target": patch.target, "max_top_n": slots // 2,
            "context": {"view": view.model_dump(mode="json"),
                "patch": patch.model_dump(mode="json"),
                "protocol": "prospective_equal_disjoint_arms",
                "maximum_submitted_per_arm": slots // 2,
                "source_scope": "current_task_only",
                "assay_and_budgets": "Frozen campaign constraints; E has no authority to alter them",
                "method_snapshots": state.get("method_governance", {}).get("active", {})}}
        campaign.store.put("evaluation_trial_inputs", evaluation_id, saved, immutable=True)
    if saved["target"] != patch.target:
        raise Conflict("Evaluation target differs from frozen input")
    return ensure_evaluation_plan(campaign.store, campaign.team,
        evaluation_id=evaluation_id, target=patch.target,
        context=saved["context"], max_top_n=saved["max_top_n"])



def _measurement_denominators(rows: list[dict], submitted: int) -> dict:
    """Count coverage without assigning a metric or scientific aggregation."""
    if type(submitted) is not int or submitted < len(rows):
        raise ValueError("Submitted count cannot be smaller than returned observations")
    counts = {"submitted": submitted, "returned": len(rows), "valid": 0,
              "unavailable": 0, "other_nonvalid": 0, "not_returned": submitted - len(rows)}
    unique_valid = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sequence"), str) or not row["sequence"]:
            raise ValueError("Measured rows require sequence identity")
        value, qc = row.get("value"), row.get("qc", "valid")
        if qc == "valid":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError("Valid measured rows require finite values")
            counts["valid"] += 1
            unique_valid.add(row["sequence"])
        elif value is not None:
            raise ValueError("Non-valid measurements cannot contain numeric outcomes")
        elif qc == "unavailable":
            counts["unavailable"] += 1
        elif qc in {"failed", "inconclusive"}:
            counts["other_nonvalid"] += 1
        else:
            raise ValueError("Unknown observation QC")
    counts.update(unique_valid=len(unique_valid), technical_repeats=counts["valid"] - len(unique_valid),
                  nonvalid=counts["unavailable"] + counts["other_nonvalid"])
    return counts


def validate_saved_metric_evidence(store, result: dict, plan: dict) -> dict | None:
    """A cached adoption must still cite the exact, intact custom metric artifact."""
    if plan["plan"].get("metric_program") is None:
        return None
    from proteinrsi.evaluation_metrics import load_evaluation_metric_result
    details = result.get("details") or {}
    if details.get("evaluation_plan_ref") != plan["plan_ref"]:
        raise Conflict("Cached result cites a different evaluation plan")
    verdict_ref = details.get("evaluation_verdict_ref")
    if not isinstance(verdict_ref, str) or not verdict_ref.startswith("evaluation_verdicts/"):
        raise Conflict("Cached custom result has no persisted verdict")
    verdict = store.get("evaluation_verdicts", verdict_ref.split("/", 1)[1])
    body = {key: value for key, value in (verdict or {}).items() if key not in {"result", "verdict_ref"}}
    if (not verdict or verdict.get("verdict_ref") != verdict_ref
            or verdict_ref != "evaluation_verdicts/" + digest(body)
            or verdict.get("result") != result or verdict.get("plan_ref") != plan["plan_ref"]
            or verdict.get("evaluation_id") != plan["evaluation_id"]
            or verdict.get("verdict", {}).get("decision") != result.get("decision")
            or verdict.get("verdict", {}).get("reason") != result.get("reason")
            or verdict.get("verdict") != details.get("verdict")):
        raise Conflict("Cached custom result disagrees with its persisted LLM verdict")
    evidence = details.get("evidence") or {}
    refs = [ref for ref in evidence if ref.startswith("evaluation_metric_results/")]
    if len(refs) != 1:
        raise Conflict("Custom evaluation requires exactly one saved metric result")
    if refs[0] not in verdict["verdict"].get("supporting_evidence_refs", []):
        raise Conflict("Cached LLM verdict does not cite the authoritative custom metric result")
    record = load_evaluation_metric_result(store, refs[0])
    if (record != evidence[refs[0]] or record.get("plan_ref") != plan["plan_ref"]
            or record.get("evaluation_id") != plan["evaluation_id"]):
        raise Conflict("Cached evaluation disagrees with its saved custom metric evidence")
    denominators = record["denominators"]
    if denominators.get("unit") == "independent_group":
        expected = {arm: denominators["n_groups"] for arm in ("baseline", "challenger")}
    else:
        expected = {arm: denominators[arm]["unique_valid"] for arm in ("baseline", "challenger")}
    if any(type(result.get("n_" + arm)) is not int or result["n_" + arm] != value
           for arm, value in expected.items()):
        raise Conflict("Cached evaluation denominators disagree with its saved custom metric evidence")
    return record


def _validated_meta_report(store, key: str) -> dict:
    from proteinrsi.llm_evaluation import load_evaluation_plan
    report = store.get("meta_evaluations", key)
    if not isinstance(report, dict):
        raise Conflict("Missing completed Meta evaluation report")
    plan = load_evaluation_plan(store, report["evaluation_plan_ref"])
    metric_result = validate_saved_metric_evidence(store, report["gate"], plan)
    if metric_result is not None and report.get("metric_facts") != metric_result:
        raise Conflict("Meta report disagrees with its authoritative custom metric result")
    return report


def _offline_metric_envelope(plan, task, cases, traces):
    """Assemble only already-disclosed observations, never labels or label paths."""
    if len(traces) != len(cases):
        raise Conflict("Custom Meta metrics require all aligned case observations")
    aligned, denominators, seen, groups = [], [], set(), set()
    for case, pair in zip(cases, traces):
        if case.case_id in seen:
            raise ValueError("Meta evaluation requires uniquely identified cases")
        seen.add(case.case_id)
        groups.add(case.group_id)
        arms, counts = {}, {}
        for arm in ("baseline", "challenger"):
            trace = pair[arm]
            if trace.get("case_id") != case.case_id or trace.get("group_id") != case.group_id:
                raise Conflict("Custom Meta metrics require aligned case identities")
            rows = trace.get("observed_rows")
            if not isinstance(rows, list) or trace.get("submitted") != case.query_budget:
                raise Conflict("Frozen offline trace lacks saved observed rows; cannot remeasure or "
                               "reinterpret it as custom metric evidence")
            if (len(rows) != case.query_budget
                    or [row.get("sequence") for row in rows] != trace.get("selected")
                    or any(row.get("sequence") == case.task.reference_sequence for row in rows)):
                raise Conflict("Saved offline observations differ from selected measurement identities")
            arms[arm] = rows
            counts[arm] = _measurement_denominators(rows, case.query_budget)
        aligned.append({"case_id": case.case_id, "group_id": case.group_id, "arms": arms})
        denominators.append({"case_id": case.case_id, "group_id": case.group_id, **counts})
    return {"protocol": "evaluation_inputs/v1", "evaluation_id": plan["evaluation_id"],
        "target": plan["target"], "task": task.model_dump(mode="json"),
        "top_ns": plan["plan"]["top_ns"], "subject_refs": ["baseline", "challenger"],
        "arms": {"baseline": [], "challenger": []}, "cases": aligned,
        "denominators": {"n_cases": len(aligned), "n_groups": len(groups),
            "unit": "independent_group", "cases": denominators, "parent_excluded": True}}


def evaluate_llm_trial(campaign, batch, observations, trial, task):
    """Execute the frozen metric program; E makes the scientific adoption decision."""
    from proteinrsi.contracts import GateResult
    from proteinrsi.llm_evaluation import adjudicate_evaluation, load_evaluation_plan
    if not trial.get("evaluation_plan_ref") or not trial.get("evaluation_id"):
        raise Conflict("A measured trial cannot create or replace its missing premeasurement E plan")
    plan = load_evaluation_plan(campaign.store, trial["evaluation_plan_ref"])
    if plan["evaluation_id"] != trial["evaluation_id"] or plan["target"] != trial.get("target", "workflow"):
        raise Conflict("Evaluation plan belongs to a different trial")
    saved_result = campaign.store.get("trial_results", batch.batch_id)
    if saved_result is not None:
        validate_saved_metric_evidence(campaign.store, saved_result, plan)
        return GateResult.model_validate(saved_result)
    by_id = {o.sample_id: o for o in observations}
    if len(by_id) != len(observations) or set(by_id) != {s.sample_id for s in batch.samples}:
        raise ValueError("Evaluation requires exactly the submitted sample identities")
    rows = {"baseline": [], "challenger": []}
    parents = {"baseline": 0, "challenger": 0}
    for sample in batch.samples:
        row = by_id[sample.sample_id]
        if row.sequence != sample.candidate.sequence:
            raise ValueError("Evaluation sample sequence mismatch")
        if sample.arm not in rows:
            continue
        if sample.candidate.sequence == task.reference_sequence:
            parents[sample.arm] += 1
            continue
        rows[sample.arm].append(row.model_dump(mode="json"))
    if len(rows["baseline"]) != len(rows["challenger"]):
        raise ValueError("Unequal submitted arm budgets")
    if {r["sequence"] for r in rows["baseline"]} & {r["sequence"] for r in rows["challenger"]}:
        raise ValueError("Trial arms must have disjoint candidate identities")
    if plan["plan"].get("metric_program") is not None:
        from proteinrsi.evaluation_metrics import execute_evaluation_metrics
        denominators = {arm: _measurement_denominators(values, len(values))
                        for arm, values in rows.items()}
        envelope = {"protocol": "evaluation_inputs/v1", "evaluation_id": trial["evaluation_id"],
            "target": plan["target"], "task": task.model_dump(mode="json"),
            "top_ns": plan["plan"]["top_ns"], "subject_refs": ["baseline", "challenger"],
            "arms": rows, "cases": [], "denominators": {
                **denominators, "parent_excluded": parents, "unit": "unique_sequence",
                "controls_and_research_excluded": True}}
        result = execute_evaluation_metrics(campaign.store, plan, envelope)
        return adjudicate_evaluation(campaign.store, campaign.team,
            evaluation_id=trial["evaluation_id"], plan_ref=trial["evaluation_plan_ref"],
            evidence={result["result_ref"]: result,
                "measurements/" + batch.batch_id: {"batch_id": batch.batch_id, "arms": rows,
                    "denominators": envelope["denominators"]},
                "trials/" + batch.batch_id: trial},
            n_baseline=result["denominators"]["baseline"]["unique_valid"],
            n_challenger=result["denominators"]["challenger"]["unique_valid"])
    # Historical plans keep their frozen descriptive evidence shape. They are
    # never relabelled as having executed a newly generated metric program.
    summaries = {arm: summarize_evaluation_metrics(values, direction=task.direction,
                    top_ns=plan["plan"]["top_ns"], submitted=len(values))
                 for arm, values in rows.items()}
    metrics = {}
    for name in metric_names(plan["plan"]["top_ns"]):
        old, new = (summaries[arm]["signed_metrics"][name] for arm in ("baseline", "challenger"))
        metrics[name] = {"baseline": old, "challenger": new,
                         "delta": new - old if old is not None and new is not None else None}
    facts = {"batch_id": batch.batch_id, "evaluation_id": trial["evaluation_id"],
        "evaluation_plan_ref": trial["evaluation_plan_ref"], "arms": summaries,
        "metrics": metrics, "metric_orientation": "direction_adjusted_higher_is_better",
        "parent_excluded": parents, "controls_and_research_excluded": True,
        "unit": "unique_sequence", "technical_repeats": "mean_within_sequence",
        "scope": "descriptive_observed_panel; no statistical significance or transfer guarantee"}
    return adjudicate_evaluation(campaign.store, campaign.team,
        evaluation_id=trial["evaluation_id"], plan_ref=trial["evaluation_plan_ref"],
        evidence={"trial_metrics/" + batch.batch_id: facts,
                  "measurements/" + batch.batch_id: {"batch_id": batch.batch_id, "arms": rows},
                  "trials/" + batch.batch_id: trial},
        n_baseline=summaries["baseline"]["denominators"]["unique_valid"],
        n_challenger=summaries["challenger"]["denominators"]["unique_valid"])


def _grouped_metric_facts(traces, top_ns):
    """Descriptive equal-weight group aggregation, with nulls preserved as facts."""
    grouped = defaultdict(list)
    seen = set()
    for pair in traces:
        old, new = pair["baseline"], pair["challenger"]
        if old["case_id"] != new["case_id"] or old["group_id"] != new["group_id"] or old["case_id"] in seen:
            raise ValueError("Meta evaluation requires unique, aligned cases and groups")
        seen.add(old["case_id"])
        grouped[old["group_id"]].append(pair)
    vectors = {}
    names = metric_names(top_ns)
    for group, pairs in sorted(grouped.items()):
        vectors[group] = {}
        for arm in ("baseline", "challenger"):
            vector = {}
            for name in names:
                values = [p[arm]["metric_summary"]["signed_metrics"][name] for p in pairs]
                vector[name] = float(np.mean(values)) if all(v is not None for v in values) else None
            vectors[group][arm] = vector
    aggregate = {arm: {} for arm in ("baseline", "challenger")}
    for arm in aggregate:
        for name in names:
            values = [v[arm][name] for v in vectors.values()]
            aggregate[arm][name] = float(np.mean(values)) if values and all(v is not None for v in values) else None
    return {"arms": aggregate, "group_metric_vectors": vectors, "n_cases": len(traces),
        "n_groups": len(grouped), "unit": "independent_group",
        "aggregation": "equal_case_means_within_group_then_equal_group_means",
        "metric_orientation": "direction_adjusted_higher_is_better",
        "missing_metrics": "Nulls are disclosed to E without an automatic adoption veto",
        "cases": traces}


def _evaluate_meta_llm(campaign, cases, *, promote=False, team_factory=None):
    """Durable offline M evaluation, with all provider calls outside transactions."""
    from proteinrsi.llm_evaluation import ensure_evaluation_plan, adjudicate_evaluation
    from proteinrsi.recovery import is_provider_paused
    from proteinrsi.online_meta import make_validation_team
    # Serialize this controller operation but not SQLite. Successful branch calls
    # and measurements survive provider pauses and final-commit interruptions.
    with campaign.store.lock():
        state = campaign.state
        manifest_hash = digest([c.model_dump(mode="json") for c in cases])
        if not state.get("pending_meta"):
            completed = [a for a in campaign.store.all("meta_attempts").values()
                if a.get("state") == "completed" and a.get("manifest_hash") == manifest_hash
                and a.get("promote_requested") == promote]
            if completed:
                return _validated_meta_report(campaign.store, completed[-1]["report_key"])
        if state.get("execution_semantics") != "on-demand-v1":
            raise Conflict("New E evaluation requires a new on-demand study")
        if state["status"] != "ready" or state["pending_batch"] or not state["pending_meta"]:
            raise Conflict("Evaluate a queued meta patch at an idle round boundary")
        if not cases or len({c.case_id for c in cases}) != len(cases):
            raise ValueError("Unique evaluation cases are required")
        if promote and any(c.split != "validation" for c in cases):
            raise ValueError("Only validation cases can select versions; test is report-only")
        task = TaskSpec.model_validate(state["task"])
        if any((c.task.kind, c.task.metric, c.task.unit, c.task.direction) !=
               (task.kind, task.metric, task.unit, task.direction) for c in cases):
            raise ValueError("Do not average incomparable tasks/units")
        local_tools = campaign.store.get("configuration", "local_tools", {})
        if any(e.get("enabled") for e in local_tools.get("engines", {}).values()):
            raise ValueError("External-engine Meta evaluation needs case-scoped artifact provisioning")
        for case in cases:
            if case.query_budget < 1 or len(case.initial) + case.query_budget > case.task.budget.experimental_wells:
                raise ValueError("Meta case warm start and test exceed the case budget")
            if any(o.sequence not in case.labels or o.value != case.labels[o.sequence] for o in case.initial):
                raise ValueError("Meta initial observations must match evaluator-owned labels")
        policy = GatePolicy.model_validate(state["gate"])
        base_w, base_m = Workflow.model_validate(state["workflow"]), MetaPolicy.model_validate(state["meta"])
        patch = Patch.model_validate(state["pending_meta"])
        child_m = apply_patch(base_m, patch)
        evaluation_id = "offline-" + digest({"patch": patch.patch_id, "manifest": manifest_hash,
                                            "workflow": base_w.version})
        prior = campaign.store.get("meta_attempts", evaluation_id)
        if prior and prior["state"] == "completed":
            return _validated_meta_report(campaign.store, prior["report_key"])
        if prior and prior.get("promote_requested") != promote:
            raise Conflict("Resume the original report/promotion intent; do not repurpose a measured trial")
        for namespace in ("meta_attempts", "meta_online_attempts"):
            for key, attempt in campaign.store.all(namespace).items():
                if (key != evaluation_id and attempt.get("patch_id") == patch.patch_id
                        and attempt.get("state") in {"started", "blocked", "planned", "paused_provider", "measurements_committed"}):
                    raise Conflict("Candidate already has another unfinished Meta evaluation")
        if prior and prior["state"] not in {"started", "paused_provider", "measurements_committed"}:
            raise Conflict("Reconcile this failed Meta attempt before retrying")
        # Resume checks the immutable engine without treating its own durable
        # started claim as an unrelated evaluator.
        if prior is None:
            campaign.methods.assert_plannable(state)
        elif campaign.methods.enabled:
            for target, ref in state["method_governance"]["active"].items():
                campaign.methods._validated_snapshot(target, ref)
        protein_config = campaign.store.get("configuration", "protein_model")
        protein_pin = campaign.store.get("protein_backend", "snapshot")
        research_config = campaign.store.get("configuration", "research")
        know_how = campaign.store.get("configuration", "know_how")
        if protein_config and not protein_pin:
            raise ValueError("Resolve the protein model before paired Meta evaluation")
        required_queries = sum(2 * (len(c.initial) + c.query_budget) for c in cases)
        if prior is None and required_queries > campaign.store.remaining("experimental_wells"):
            raise ValueError("Meta evaluation exceeds the remaining shared query budget")
        frozen = campaign.store.get("evaluation_trial_inputs", evaluation_id)
        if frozen is None:
            frozen = {"target": "meta", "max_top_n": max(c.query_budget for c in cases),
                "context": {"patch": patch.model_dump(mode="json"),
                    "task": task.model_dump(mode="json"),
                    "baseline_workflow": base_w.model_dump(mode="json"),
                    "baseline_meta": base_m.model_dump(mode="json"),
                    "cases": [{"case_id": c.case_id, "group_id": c.group_id, "split": c.split,
                        "task": c.task.model_dump(mode="json"),
                        "initial": [o.model_dump(mode="json") for o in c.initial],
                        "history": c.history, "query_budget": c.query_budget} for c in cases],
                    "protocol": "one-step frozen improvers; aligned cases; program-defined aggregation",
                    "required_query_slots": required_queries, "scope": "declared_cases_only"}}
            campaign.store.put("evaluation_trial_inputs", evaluation_id, frozen, immutable=True)
        attempt = prior or {"state": "started", "patch_id": patch.patch_id,
            "required_queries": required_queries, "manifest_hash": manifest_hash,
            "promote_requested": promote, "budget_before": campaign.store.usage()}
        with campaign.store.transaction():
            if prior is None:
                campaign.methods.begin(state, patch)
            campaign.store.put("meta_attempts", evaluation_id, attempt)
        try:
            plan = ensure_evaluation_plan(campaign.store, campaign.team,
                evaluation_id=evaluation_id, target="meta", context=frozen["context"],
                max_top_n=frozen["max_top_n"])
            traces = []
            prompt_bundle = campaign.store.get("configuration", "prompt_bundle")
            for i, case in enumerate(cases):
                pair = {}
                for arm, meta in (("baseline", base_m), ("challenger", child_m)):
                    key = evaluation_id + f"/{i}/{arm}"
                    saved = campaign.store.get("meta_arm_results", key)
                    if saved is None:
                        directory = campaign.store.root / "meta-validation-offline" / evaluation_id / str(i) / arm
                        score, trace = _offspring_score(case, base_w, meta, str(directory),
                            team_factory or (lambda store: make_validation_team(campaign, store)),
                            protein_config, protein_pin, research_config, know_how,
                            campaign.store, key, prompt_bundle, policy, plan["plan"]["top_ns"])
                        saved = {"evaluation_id": evaluation_id, "case_id": case.case_id,
                            "arm": arm, "score": score, "trace": trace}
                        campaign.store.put("meta_arm_results", key, saved, immutable=True)
                    pair[arm] = saved["trace"]
                traces.append(pair)
            attempt = {**attempt, "state": "measurements_committed", "evaluation_plan_ref": plan["plan_ref"]}
            campaign.store.put("meta_attempts", evaluation_id, attempt)
            if plan["plan"].get("metric_program") is not None:
                from proteinrsi.evaluation_metrics import execute_evaluation_metrics
                envelope = _offline_metric_envelope(plan, task, cases, traces)
                # One execution receives every aligned case. Aggregation belongs
                # to the frozen program, not an implicit controller group mean.
                facts = execute_evaluation_metrics(campaign.store, plan, envelope)
                evidence = {facts["result_ref"]: facts}
                n_groups = facts["denominators"]["n_groups"]
            else:
                facts = _grouped_metric_facts(traces, plan["plan"]["top_ns"])
                evidence = {"meta_metrics/" + evaluation_id: facts}
                evidence.update({"meta_arm_results/" + key: value
                    for key, value in campaign.store.all("meta_arm_results").items()
                    if value.get("evaluation_id") == evaluation_id})
                n_groups = facts["n_groups"]
            gate = adjudicate_evaluation(campaign.store, campaign.team,
                evaluation_id=evaluation_id, plan_ref=plan["plan_ref"], evidence=evidence,
                n_baseline=n_groups, n_challenger=n_groups)
        except Exception as exc:
            if is_provider_paused(exc):
                campaign.store.put("meta_attempts", evaluation_id, {**attempt,
                    "state": "paused_provider", "error_type": type(exc).__name__})
                campaign.store.event("meta_evaluation_paused", {"evaluation_id": evaluation_id,
                    "patch_id": patch.patch_id, "measurements_preserved": True})
            else:
                with campaign.store.transaction():
                    recoverable = campaign.methods.failure(state, patch, exc) if campaign.methods.enabled else False
                    campaign.store.put("meta_attempts", evaluation_id, {**attempt,
                        "state": "failed" if recoverable else "blocked", "error_type": type(exc).__name__})
            raise
        report = {"patch_id": patch.patch_id, "base_meta": base_m.version,
            "candidate_meta": child_m.version, "base_workflow": base_w.version,
            "case_manifest_sha256": manifest_hash, "evaluation_id": evaluation_id,
            "evaluation_plan_ref": plan["plan_ref"], "gate": gate.model_dump(), "traces": traces,
            "metric_facts": facts, "promoted": promote and gate.decision == "accepted",
            "protocol": "LLM predeclared criteria and actual postmeasurement verdict",
            "budget_scope": "shared_campaign", "required_query_slots": required_queries,
            "budget_before": attempt["budget_before"], "budget_after": campaign.store.usage(),
            "scope": {"kind": task.kind.value, "metric": task.metric, "unit": task.unit},
            "sources": sorted({c.task.feedback_source for c in cases})}
        with campaign.store.transaction():
            current = campaign.state
            if current["pending_meta"] != state["pending_meta"] or current["workflow"] != state["workflow"] or current["meta"] != state["meta"]:
                raise Conflict("Campaign changed while evaluator was running")
            if promote:
                campaign.methods.complete(current, patch, gate, child_m,
                                          evaluation_ref="meta_evaluations/" + evaluation_id)
            elif campaign.methods.enabled:
                campaign.methods.transition(current, patch, "staged", {"report_only": evaluation_id})
            campaign.store.put("meta_evaluations", evaluation_id, report, immutable=True)
            campaign.store.put("meta_attempts", evaluation_id, {**attempt, "state": "completed",
                                                              "report_key": evaluation_id})
            campaign.store.event("meta_evaluated", {"evaluation_id": evaluation_id,
                "promoted": report["promoted"], "gate": gate.model_dump()})
        return report


def summarize_evaluation_metrics(rows, *, direction, top_ns, submitted):
    """Include the raw maximum separately from the best-in-task-direction."""
    result = summarize_metrics(rows, direction=direction, top_ns=top_ns, submitted=submitted)
    groups = defaultdict(list)
    for row in rows:
        if row["qc"] == "valid":
            groups[row["sequence"]].append(float(row["value"]))
    values = [float(np.mean(v)) for v in groups.values()]
    result["maximum"] = max(values) if values else None
    result["best_in_task_direction"] = result["metrics"]["best"]
    return result
