# SPDX-License-Identifier: MIT
"""Controller-only frozen inputs and private, sponsored Campaign forks."""
from copy import deepcopy
import re

from proteinrsi.contracts import GatePolicy, MetaPolicy, TaskSpec, Workflow, digest
from proteinrsi.governance import GovernancePolicy
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, SponsoredStore, Store

CONFIGURATION = ("research", "know_how", "prompt_bundle", "protein_model", "local_tools", "skill_library",
                 "execution_authorization")


def evaluation_budget_store(parent, evaluation_id, spec):
    """One durable query ledger per independent comparison; audit stays in parent."""
    if spec.budget_scope == "shared_campaign":
        return parent
    caps = [c.resource_limits for c in spec.cases for _ in ("baseline", "challenger")]
    caps.append(spec.evaluator_limits)
    limits = {}
    for resource in {r for cap in caps for r in cap}:
        amounts = [cap.get(resource, 0) for cap in caps]
        limits[resource] = None if None in amounts else sum(amounts)
    ledger = Store(parent.root / "harness-validation" / ("budget-" + digest(evaluation_id)))
    ledger.configure_budget(limits)
    parent.put("evaluation_budget_ledgers", evaluation_id,
               {"scope": spec.budget_scope, "directory": str(ledger.root.relative_to(parent.root)),
                "limits": limits}, immutable=True)
    return ledger


class ModelRevisionUnavailable(ValueError):
    """An optional model lacks the immutable identity needed for a fair comparison."""


def _protein_pin(store, config):
    if not config:
        return None
    pin = store.get("protein_backend", "snapshot")
    if pin is None:
        revision = config["revision"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ModelRevisionUnavailable("Pin the optional protein model to an immutable revision before comparison")
        # A declared commit freezes identity without downloading or running a model.
        return {"model_id": config["model_id"], "revision": revision}
    if (pin["model_id"] != config["model_id"] or not re.fullmatch(r"[0-9a-f]{40}", pin["revision"])
            or (re.fullmatch(r"[0-9a-f]{40}", config["revision"]) and pin["revision"] != config["revision"])):
        raise Conflict("Protein snapshot differs from the declared model identity")
    return pin


def provider_identity(team):
    llm = team.llm
    from proteinrsi.audit import redact
    return redact({"model": getattr(llm, "model", None), "url": getattr(llm, "base_url", None),
        "settings": getattr(llm, "cache_settings", {}), "max_tokens": getattr(llm, "max_tokens", None),
        "capacity": getattr(llm, "capacity_settings", {})},
        (getattr(llm, "api_key", ""),))


def freeze_resources(campaign):
    llm = campaign.team.llm
    resources = {"configuration": {k: campaign.store.get("configuration", k) for k in CONFIGURATION},
        "artifacts": list(campaign.store.all("artifacts").values()),
        "provider": provider_identity(campaign.team),
        "allow_egress": campaign.team.tools.allow_egress,
        "guarded": bool(getattr(campaign.team, "guarded", False))}
    resources["protein_pin"] = _protein_pin(campaign.store, resources["configuration"]["protein_model"])
    if (resources["configuration"]["research"] or {}).get("evaluation_protocol") == "method-loop-v1":
        resources["governance"] = campaign.methods.policy.model_dump(mode="json")
    visibility = campaign.store.all("artifact_visibility")
    if visibility:
        resources["artifact_visibility"] = visibility
    # Persist only provider identity, not keys or headers.
    from proteinrsi.audit import redact
    resources = redact(resources, (getattr(llm, "api_key", ""),))
    memory = campaign.memory.freeze_records()
    refs = []
    for namespace, value in (("evaluation_resources", resources), ("evaluation_memory", memory)):
        ref = digest(value)
        campaign.store.put(namespace, ref, value, immutable=True)
        refs.append(ref)
    return resources, memory, refs


def freeze_start(store, case, *, history=None, round_index=0, initialization_done=None, first_round_spent=0, research_state=None):
    start = {"task": case.task.model_dump(mode="json"),
        "observations": [o.model_dump(mode="json") for o in case.initial],
        "history": deepcopy(history if history is not None else case.history),
        "round_index": round_index, "first_round_spent": first_round_spent,
        "initialization_done": initialization_done if initialization_done is not None else
            (case.task.initial_observation_policy != "parent_once" or
             any(o.sequence == case.task.reference_sequence for o in case.initial))}
    if research_state is not None:
        start["research_state"] = deepcopy(research_state)
    ref = digest(start)
    store.put("evaluation_starts", ref, start, immutable=True)
    return ref


def fork_for_evaluation(campaign, spec, case_spec, workflow, meta, *, arm, team_factory):
    """Reopen the same immutable branch; never seed it from the live parent again."""
    if arm not in {"baseline", "challenger"}:
        raise ValueError("Unknown comparison arm")
    store = campaign.store
    attempt = store.get("evaluation_spec_attempts", spec.spec_ref)
    if not attempt:
        raise Conflict("Missing frozen evaluation attempt")
    branch = attempt["evaluation_id"] + "/" + case_spec.case_id + "/" + arm
    # IDs may be user-supplied; directories always use a digest, never raw paths.
    directory = store.root / "harness-validation" / digest(branch)
    budget_store = evaluation_budget_store(store, attempt["evaluation_id"], spec)
    child_store = SponsoredStore(directory, store, branch, allocation_id=branch, budget_store=budget_store)
    start = store.get("evaluation_starts", case_spec.start_ref)
    resources = store.get("evaluation_resources", spec.resource_snapshot_ref)
    memory = store.get("evaluation_memory", spec.memory_snapshot_ref)
    for value, ref in ((start, case_spec.start_ref), (resources, spec.resource_snapshot_ref), (memory, spec.memory_snapshot_ref)):
        if value is None or digest(value) != ref:
            raise Conflict("Frozen validation inputs changed")
    policy = GovernancePolicy.model_validate(resources.get("governance") or {})
    allowed = ["workflow"] if spec.target == "meta" and "workflow" in policy.allowed_patch_targets else []
    governance = policy.model_copy(update={"allowed_patch_targets": allowed}).model_dump(mode="json")
    binding = {"spec_ref": spec.spec_ref, "case_id": case_spec.case_id, "arm": arm,
        "workflow": workflow.version, "meta": meta.version, "start_ref": case_spec.start_ref,
        "resource_ref": spec.resource_snapshot_ref, "memory_ref": spec.memory_snapshot_ref,
        "target": spec.target, "protocol": spec.protocol,
        "method_changes_allowed": bool(allowed), "free_initial_observations": len(start["observations"])}
    child_store.put("configuration", "evaluation_branch", binding, immutable=True)
    current = child_store.get("campaign", "state")
    if current is None:
        configuration = deepcopy(resources["configuration"])
        if spec.target == "meta" and configuration.get("research"):
            # A nested W trial belongs to its outer M comparison, never to a new quota.
            research = configuration["research"]
            research["auto_evaluation"] = {**(research.get("auto_evaluation") or {}),
                                          "budget_scope": "shared_campaign"}
        for name, value in configuration.items():
            if value is not None:
                child_store.put("configuration", name, value, immutable=True)
        if resources["protein_pin"]:
            child_store.put("protein_backend", "snapshot", resources["protein_pin"], immutable=True)
        data = deepcopy(start["task"])
        data["budget"] = {r: case_spec.resource_limits[r] for r in ("experimental_wells", "llm_calls", "tool_calls")}
        data["max_rounds"] = start["round_index"] + case_spec.rounds
        # Initial evidence is installed explicitly; no implicit paid warm start.
        if start["initialization_done"]:
            data["initial_observation_policy"] = "none"
            data["initial_parent_measurement"] = None
        task = TaskSpec.model_validate(data)
        with child_store.transaction():
            child = Campaign.initialize(str(directory), task, workflow=workflow, meta=meta,
                gate=GatePolicy.model_validate(spec.gate),
                protein_config=configuration.get("protein_model"),
                local_tools=configuration.get("local_tools"), research_config=None,
                auto_meta_evaluation=False, governance_config=governance, _store=child_store)
            with child_store.transaction():
                state = child.state
                state.update(observations=deepcopy(start["observations"]), history=deepcopy(start["history"]),
                    round_index=start["round_index"], initialization_done=start["initialization_done"],
                    first_round_spent=start["first_round_spent"], considered_round=start["round_index"], last_patch_round=-100)
                child_store.put("campaign", "state", state)
                if "research_state" in start:
                    ref = digest(start["research_state"])
                    child_store.put("research_states", ref, start["research_state"], immutable=True)
                    child_store.put("research_state", "active", {"ref": ref})
                for key, value in memory.items():
                    child_store.put("experience", key, value, immutable=True)
                child_store.event("evaluation_branch_created", {**binding, "charged_initial_queries": 0})
            from proteinrsi.localtools.artifacts import ArtifactStore
            source, target = ArtifactStore(store), ArtifactStore(child_store)
            for item in resources["artifacts"]:
                visible = resources.get("artifact_visibility", {}).get(item["ref"].removeprefix("artifact:"), {"task": True})
                target.put(source.resolve(item["ref"]), item["kind"], origin="frozen-evaluation-context",
                           task_visible=visible["task"])
    child = Campaign(child_store, team=team_factory(child_store))
    if provider_identity(child.team) != resources["provider"]:
        raise Conflict("Task provider settings differ from the frozen comparison")
    if child.team.tools.allow_egress != resources["allow_egress"]:
        raise Conflict("Tool egress authority differs from the frozen comparison")
    if resources["guarded"] and not getattr(child.team, "guarded", False):
        raise Conflict("Cannot downgrade the frozen validation isolation")
    # Authority is enforced on each resume, not merely promised in a prompt.
    if (MetaPolicy.model_validate(child.state["meta"]).version != meta.version
            or child.methods.policy.model_dump(mode="json") != governance
            or child.methods.allowed_targets != tuple(allowed) or child.state["pending_meta"]
            or (spec.target == "workflow" and
                (Workflow.model_validate(child.state["workflow"]).version != workflow.version
                 or child.state["pending_patch"]))):
        raise Conflict("Validation branch method or authority changed")
    if getattr(child.team, "guarded", False):
        from proteinrsi.replay.broker import GuardedMetaAgent
        child.meta_agent = GuardedMetaAgent(child.team)
    child.methods.assert_active(child.state)
    return child
