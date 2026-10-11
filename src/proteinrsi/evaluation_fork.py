# SPDX-License-Identifier: MIT
"""Controller-only frozen inputs and private, sponsored Campaign forks."""
from copy import deepcopy

from proteinrsi.contracts import GatePolicy, MetaPolicy, TaskSpec, Workflow, digest
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, SponsoredStore

CONFIGURATION = ("research", "know_how", "prompt_bundle", "protein_model", "local_tools", "skill_library")


def provider_identity(team):
    llm = team.llm
    from proteinrsi.audit import redact
    return redact({"model": getattr(llm, "model", None), "url": getattr(llm, "base_url", None),
        "settings": getattr(llm, "cache_settings", {}), "max_tokens": getattr(llm, "max_tokens", None)},
        (getattr(llm, "api_key", ""),))


def freeze_resources(campaign):
    llm = campaign.team.llm
    resources = {"configuration": {k: campaign.store.get("configuration", k) for k in CONFIGURATION},
        "protein_pin": campaign.store.get("protein_backend", "snapshot"),
        "artifacts": list(campaign.store.all("artifacts").values()),
        "provider": provider_identity(campaign.team),
        "allow_egress": campaign.team.tools.allow_egress,
        "guarded": bool(getattr(campaign.team, "guarded", False))}
    # Persist only provider identity, not keys or headers.
    from proteinrsi.audit import redact
    resources = redact(resources, (getattr(llm, "api_key", ""),))
    memory = campaign.store.all("experience")
    refs = []
    for namespace, value in (("evaluation_resources", resources), ("evaluation_memory", memory)):
        ref = digest(value)
        campaign.store.put(namespace, ref, value, immutable=True)
        refs.append(ref)
    return resources, memory, refs


def freeze_start(store, case, *, history=None, round_index=0, initialization_done=None, first_round_spent=0):
    start = {"task": case.task.model_dump(mode="json"),
        "observations": [o.model_dump(mode="json") for o in case.initial],
        "history": deepcopy(history if history is not None else case.history),
        "round_index": round_index, "first_round_spent": first_round_spent,
        "initialization_done": initialization_done if initialization_done is not None else
            (case.task.initial_observation_policy != "parent_once" or
             any(o.sequence == case.task.reference_sequence for o in case.initial))}
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
    child_store = SponsoredStore(directory, store, branch, allocation_id=branch)
    start = store.get("evaluation_starts", case_spec.start_ref)
    resources = store.get("evaluation_resources", spec.resource_snapshot_ref)
    memory = store.get("evaluation_memory", spec.memory_snapshot_ref)
    for value, ref in ((start, case_spec.start_ref), (resources, spec.resource_snapshot_ref), (memory, spec.memory_snapshot_ref)):
        if value is None or digest(value) != ref:
            raise Conflict("Frozen validation inputs changed")
    binding = {"spec_ref": spec.spec_ref, "case_id": case_spec.case_id, "arm": arm,
        "workflow": workflow.version, "meta": meta.version, "start_ref": case_spec.start_ref,
        "resource_ref": spec.resource_snapshot_ref, "memory_ref": spec.memory_snapshot_ref,
        "method_changes_allowed": False, "free_initial_observations": len(start["observations"])}
    child_store.put("configuration", "evaluation_branch", binding, immutable=True)
    current = child_store.get("campaign", "state")
    if current is None:
        configuration = resources["configuration"]
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
                auto_meta_evaluation=False, governance_config={"allowed_patch_targets": []}, _store=child_store)
            with child_store.transaction():
                state = child.state
                state.update(observations=deepcopy(start["observations"]), history=deepcopy(start["history"]),
                    round_index=start["round_index"], initialization_done=start["initialization_done"],
                    first_round_spent=start["first_round_spent"], considered_round=start["round_index"], last_patch_round=-100)
                child_store.put("campaign", "state", state)
                for key, value in memory.items():
                    child_store.put("experience", key, value, immutable=True)
                child_store.event("evaluation_branch_created", {**binding, "charged_initial_queries": 0})
            from proteinrsi.localtools.artifacts import ArtifactStore
            source, target = ArtifactStore(store), ArtifactStore(child_store)
            for item in resources["artifacts"]:
                target.put(source.resolve(item["ref"]), item["kind"], origin="frozen-evaluation-context")
    child = Campaign(child_store, team=team_factory(child_store))
    if provider_identity(child.team) != resources["provider"]:
        raise Conflict("Task provider settings differ from the frozen comparison")
    if child.team.tools.allow_egress != resources["allow_egress"]:
        raise Conflict("Tool egress authority differs from the frozen comparison")
    if resources["guarded"] and not getattr(child.team, "guarded", False):
        raise Conflict("Cannot downgrade the frozen validation isolation")
    # Authority is enforced on each resume, not merely promised in a prompt.
    if (Workflow.model_validate(child.state["workflow"]).version != workflow.version or
            MetaPolicy.model_validate(child.state["meta"]).version != meta.version or
            child.methods.policy.allowed_patch_targets or child.state["pending_patch"] or child.state["pending_meta"]):
        raise Conflict("Validation branch method or authority changed")
    child.methods.assert_active(child.state)
    return child
