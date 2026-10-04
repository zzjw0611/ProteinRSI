# SPDX-License-Identifier: MIT
"""Independent improver evaluation: compare descendants, not self-assessed reflections."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
import json

import numpy as np

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import (GatePolicy, MetaPolicy, Model, Observation, Patch,
                                  TaskSpec, TaskView, Workflow, digest)
from proteinrsi.improvement import apply_patch, compare_scores
from proteinrsi.storage import Conflict, Store


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
                     research_config: dict | None = None, know_how: list | None = None) -> tuple[float, dict]:
    store = Store(directory)
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
    team = team_factory(store) if team_factory else Team(store)
    # Read only observed data in the policy context. No labels, global extrema or paths.
    view = TaskView(task=case.task, round_index=1, observations=case.initial,
                    history=case.history, remaining_wells=case.task.budget.experimental_wells,
                    workflow=workflow, meta=meta)
    proposal = MetaAgent(team.llm).propose(view)
    child = workflow
    if proposal.patch and proposal.patch.target == "workflow":
        if proposal.patch.task_kind != case.task.kind:
            raise ValueError("Meta proposal has incorrect scope")
        child = apply_patch(workflow, proposal.patch)
    # Freeze the improver for this one-step diagnostic. Full recursive campaigns use runtime.py.
    view.workflow = child
    candidates = team.run(view)
    observed = {o.sequence for o in case.initial}
    selected = [c for c in candidates if c.sequence not in observed][:case.query_budget]
    if len(selected) != case.query_budget or any(c.sequence not in case.labels for c in selected):
        raise ValueError("Cannot fairly evaluate this descendant with the stated measurement budget")
    store.reserve("evaluation-query", "experimental_wells", case.query_budget, [c.sequence for c in selected])
    store.settle("evaluation-query")
    sign = 1 if case.task.direction == "maximize" else -1
    score = sign * float(np.mean([case.labels[c.sequence] for c in selected]))
    return score, {"case_id": case.case_id, "meta": meta.version, "descendant": child.version,
                   "selected": [c.sequence for c in selected], "signed_score": score, "usage": store.usage()}


def evaluate_meta(campaign, cases: list[MetaCase], *, promote: bool = False,
                  team_factory: Callable[[Store], Team] | None = None) -> dict:
    """Trusted entrypoint. There is deliberately no 'promote arbitrary report.json' API."""
    snapshot = campaign.state
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
    with TemporaryDirectory(prefix="proteinrsi-meta-") as directory:
        for i, case in enumerate(cases):
            old, trace_old = _offspring_score(case, base_w, base_m, f"{directory}/{i}-old", team_factory,
                                          protein_config, protein_pin, research_config, know_how)
            new, trace_new = _offspring_score(case, base_w, child_m, f"{directory}/{i}-new", team_factory,
                                          protein_config, protein_pin, research_config, know_how)
            grouped[case.group_id].append((old, new))
            traces.append({"baseline": trace_old, "challenger": trace_new})
    # Seeds/related cases from one protein/group do not count as independent proteins.
    means = [np.asarray(scores).mean(axis=0) for scores in grouped.values()]
    gate = compare_scores([float(x[0]) for x in means], [float(x[1]) for x in means],
                          GatePolicy.model_validate(snapshot["gate"]), paired=True)
    manifest_hash = digest([c.model_dump(mode="json") for c in cases])
    report = {"patch_id": patch.patch_id, "base_meta": base_m.version, "candidate_meta": child_m.version,
              "base_workflow": base_w.version, "case_manifest_sha256": manifest_hash,
              "protein_model": protein_pin, "protein_configuration": protein_config,
              "research_configuration": research_config, "know_how_snapshot_sha256": digest(know_how),
              "gate": gate.model_dump(), "traces": traces, "promoted": False,
              "protocol": "one-step frozen-improver, paired group means, equal query/call limits",
              "scope": {"kind": task.kind.value, "metric": task.metric, "unit": task.unit},
              "sources": sorted({c.task.feedback_source for c in cases})}
    key = digest({"patch": patch.patch_id, "manifest": manifest_hash, "base_w": base_w.version,
                  "protein_config": protein_config, "protein_pin": protein_pin,
                  "research": research_config, "know_how": digest(know_how)})
    with campaign.store.lock():
        current = campaign.state
        if (current["workflow"] != snapshot["workflow"] or current["meta"] != snapshot["meta"]
            or current["pending_meta"] != snapshot["pending_meta"] or current["status"] != "ready"):
            raise Conflict("Campaign changed while evaluator was running; no version was published")
        if promote:
            if gate.decision == "accepted":
                current["meta"] = child_m.model_dump()
                campaign.store.put("meta_versions", child_m.version, child_m.model_dump(), immutable=True)
                report["promoted"] = True
            # One prespecified look: inconclusive candidates are archived, not silently retried.
            current["pending_meta"] = None
            campaign.store.put("campaign", "state", current)
        campaign.store.put("meta_evaluations", key, report)
        campaign.store.event("meta_evaluated", {"evaluation_id": key, "promoted": report["promoted"],
                                               "gate": gate.model_dump()})
    return report
