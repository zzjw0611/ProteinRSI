# SPDX-License-Identifier: MIT
"""Prospective, task-local improver trials within the next research batch.

The two frozen improvers see identical revealed evidence. Separate sponsored stores
isolate their descendant plans/tools. This module never reads future fitness labels.
"""
from copy import copy

from proteinrsi.agents import MetaAgent, Team, skill_text
from proteinrsi.audit import snapshot
from proteinrsi.contracts import Candidate, GatePolicy, Patch, TaskSpec, digest
from proteinrsi.improvement import apply_patch
from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.storage import SponsoredStore
from proteinrsi.tasks import validate_candidate


def make_validation_team(campaign, store):
    llm = copy(campaign.team.llm) if campaign.team.llm is not None else None
    if llm is not None:
        llm.store = store
    from proteinrsi.tools import ToolGateway
    gateway = ToolGateway(store, allow_egress=campaign.team.tools.allow_egress)
    if getattr(campaign.team, "guarded", False):
        from proteinrsi.replay.broker import GuardedTeam
        return GuardedTeam(store, llm, gateway)
    return Team(store, llm, gateway)


def prepare_meta_trial(campaign, state, view, slots, *, team_factory=None):
    """Return selected candidates + frozen trial record, or a recorded deferral/failure."""
    patch = Patch.model_validate(state["pending_meta"])
    gate = GatePolicy.model_validate(state["gate"])
    per_arm = slots // 2
    evaluation_id = "online-" + digest({"patch": patch.patch_id, "round": view.round_index,
        "evidence": view.evidence_version, "workflow": view.workflow.version})[:24]
    old = campaign.store.get("meta_online_attempts", evaluation_id)
    if old:
        if old["state"] == "planned":
            choices = [(Candidate.model_validate(c), a, w) for c, a, w in old["choices"]]
            return choices, old["trial"]
        if campaign.methods.enabled:
            campaign.methods.transition(state, patch, "blocked", {"evaluation_id": evaluation_id,
                "reason": "Prior Meta attempt did not complete; reconcile before continuing"})
        raise ValueError("This Meta trial was already attempted; inspect its trace before retrying")
    def defer(reason):
        key = digest({"evaluation": evaluation_id, "reason": reason})
        if not campaign.store.get("meta_deferrals", key):
            campaign.store.put("meta_deferrals", key, {"reason": reason, "patch_id": patch.patch_id})
            campaign.store.event("meta_validation_deferred", {"patch_id": patch.patch_id, "reason": reason,
                                                            "round": view.round_index})
        if campaign.methods.enabled:
            campaign.methods.defer(state, patch, reason)
        return None
    if per_arm < gate.min_per_arm:
        return defer("Next batch lacks enough equal-arm query slots")
    usage = campaign.store.usage()
    limits = {k: campaign.store.remaining(k)//2 for k in usage}
    if campaign.team.llm is not None and limits.get("llm_calls", 0) < 2:
        return defer("Insufficient shared LLM budget for two improvers and their descendants")
    # No additional experiment lookup takes place in either child. Queries are
    # submitted/settled ONCE by Campaign for the final disjoint two-arm batch.
    limits["experimental_wells"] = view.remaining_wells
    candidate_meta = apply_patch(view.meta, patch)
    attempt = {"state": "started", "patch_id": patch.patch_id, "round": view.round_index,
        "evidence_version": view.evidence_version, "scope": "current_task_only",
        "protocol": "prospective-disjoint-descendants-v1", "per_arm": per_arm,
        "equal_compute_limits": limits}
    campaign.methods.begin(state, patch)
    campaign.store.put("meta_online_attempts", evaluation_id, attempt)
    campaign.store.event("meta_validation_started", {"evaluation_id": evaluation_id, **attempt})
    full_task = TaskSpec.model_validate(state["task"])
    artifacts = list(campaign.store.all("artifacts").values())
    original_artifacts = ArtifactStore(campaign.store)
    source = campaign.store.get("patch_contexts", patch.patch_id, {})
    last_patch_round = source.get("previous_patch_round", -100)
    arms, descendants, decisions, branches = {}, {}, {}, {}
    try:
        for arm, policy in (("baseline", view.meta), ("challenger", candidate_meta)):
            branch = evaluation_id+"/"+arm
            store = SponsoredStore(campaign.store.root/"meta-validation"/evaluation_id/arm,
                                   campaign.store, branch)
            store.configure_budget(limits)
            for name in ("research", "know_how", "prompt_bundle", "protein_model", "local_tools"):
                value = campaign.store.get("configuration", name)
                if value is not None:
                    store.put("configuration", name, value, immutable=True)
            pin = campaign.store.get("protein_backend", "snapshot")
            if pin:
                store.put("protein_backend", "snapshot", pin, immutable=True)
            store.put("campaign", "state", {"task": full_task.model_dump(mode="json")})
            target_artifacts = ArtifactStore(store)
            for item in artifacts:
                target_artifacts.put(original_artifacts.resolve(item["ref"]), item["kind"], origin="frozen-parent-context")
            team = team_factory(store) if team_factory else make_validation_team(campaign, store)
            child_view = view.model_copy(deep=True, update={"meta": policy, "artifacts": artifacts})
            snapshot(store, "frozen_improver_input", child_view, branch=arm)
            if getattr(team, "guarded", False):
                from proteinrsi.replay.broker import GuardedMetaAgent
                improver = GuardedMetaAgent(team)
            else:
                improver = MetaAgent(team.llm, store)
            response = improver.propose(child_view, last_patch_round)
            workflow = view.workflow
            if response.patch and response.patch.target == "workflow":
                if response.patch.task_kind != full_task.kind:
                    raise ValueError("Descendant patch task scope differs")
                workflow = apply_patch(workflow, response.patch)
                skill_text(workflow.skill_names)
            # A nested meta proposal is recorded but cannot start recursive
            # evaluation inside this frozen one-step validation.
            child_view.workflow = workflow
            child_view.research_context = {**child_view.research_context, "validation_request": {
                "maximum_candidates": per_arm, "minimum_candidates": gate.min_per_arm,
                "instruction": "Generate validation proposals; the controller fills the rest of the plate separately."}}
            decisions[arm] = response.model_dump(mode="json")
            descendants[arm] = workflow.model_dump()
            store.event("frozen_improver_decision", {"response": decisions[arm], "workflow": workflow.version})
            branches[arm] = (store, team, child_view)
        if descendants["baseline"] == descendants["challenger"]:
            campaign.store.put("meta_online_attempts", evaluation_id, {**attempt, "state": "inconclusive",
                "reason": "Identical descendants", "improver_decisions": decisions})
            campaign.store.event("meta_validation_inconclusive", {"evaluation_id": evaluation_id,
                "reason": "Identical descendants; no validation queries submitted"})
            campaign.methods.finish(state, patch, "inconclusive", detail={"evaluation_id": evaluation_id})
            return None
        for arm, (store, team, child_view) in branches.items():
            snapshot(store, "descendant_research_input", child_view, branch=arm)
            candidates = team.run(child_view)
            for candidate in candidates:
                validate_candidate(full_task, candidate)
            arms[arm] = candidates
        excluded = {o.sequence for o in view.observations} | {full_task.reference_sequence}
        from proteinrsi.contracts import Workflow
        versions = {arm: Workflow.model_validate(descendants[arm]).version for arm in arms}
        from proteinrsi.trial_allocation import allocate_trial
        chosen, allocation = allocate_trial(arms, versions, slots, gate.min_per_arm,
                                           excluded, full_task.seed + view.round_index)
        if not chosen:
            campaign.store.put("meta_online_attempts", evaluation_id, {**attempt, "state": "inconclusive", **allocation})
            campaign.store.event("meta_validation_inconclusive", {"evaluation_id": evaluation_id, **allocation})
            campaign.methods.finish(state, patch, "inconclusive", detail={"evaluation_id": evaluation_id})
            return None
        trial = {"target": "meta", "allocation": allocation, "patch": patch.model_dump(mode="json"), "gate": state["gate"],
            "evaluation_id": evaluation_id, "scope": "current_task_only", "transfer_validated": False,
            "baseline_meta": view.meta.model_dump(), "challenger_meta": candidate_meta.model_dump(),
            "descendants": descendants, "improver_decisions": decisions,
            "evidence_version": view.evidence_version, "protocol": attempt["protocol"]}
        campaign.store.put("meta_online_attempts", evaluation_id, {**attempt, "state": "planned", "trial": trial,
            "choices": [(c.model_dump(mode="json"), a, w) for c, a, w in chosen]})
        return chosen, trial
    except Exception as exc:  # Failed candidates never publish; uncertain calls remain blocked.
        with campaign.store.transaction():
            recoverable = campaign.methods.failure(state, patch, exc) if campaign.methods.enabled else True
            campaign.store.put("meta_online_attempts", evaluation_id, {**attempt,
                "state": "failed" if recoverable else "blocked",
                "error_type": type(exc).__name__, "improver_decisions": decisions})
            campaign.store.event("meta_validation_failed", {"evaluation_id": evaluation_id,
                "patch_id": patch.patch_id, "error_type": type(exc).__name__, "blocked": not recoverable})
            if not campaign.methods.enabled:
                state["pending_meta"] = None
                campaign.store.put("campaign", "state", state)
        if not recoverable:
            raise
        return None
