# SPDX-License-Identifier: MIT
"""Study authority is frozen by the operator; methods only choose how to use it."""
from pydantic import Field

from proteinrsi.contracts import Model, digest

AUTHORIZATION = "execution_authorization"


class StudyAuthorization(Model):
    contract: str = "execution-v1"
    tool_names: list[str] = Field(default_factory=list)
    generated_code: bool = False
    patch_targets: list[str] = Field(default_factory=list)

    @property
    def version(self):
        return "authorization:" + digest(self)


def authorization(store):
    raw = store.get("configuration", AUTHORIZATION)
    return StudyAuthorization.model_validate(raw) if raw is not None else None


def freeze_authorization(campaign):
    """Called once at init, after operator-configured tools have been bound."""
    existing = authorization(campaign.store)
    if existing is not None:  # Evaluation forks inherit the original authority.
        return existing
    view = campaign.view()
    campaign.team.bind_tools(view)
    code = bool(campaign.store.get("configuration", "research", {}).get("enable_generated_code"))
    names = set(campaign.team.tools._tools) | set(view.workflow.tool_names)
    if not code:
        names.discard("research_python")
    policy = campaign.store.get("configuration", "method_governance", {})
    grant = StudyAuthorization(tool_names=sorted(names), generated_code=code,
                               patch_targets=policy.get("allowed_patch_targets", []))
    campaign.store.put("configuration", AUTHORIZATION, grant.model_dump(mode="json"), immutable=True)
    campaign.store.event("execution_authorized", {"version": grant.version,
                                                  **grant.model_dump(mode="json")})
    return grant


def require_tools(store, names):
    grant = authorization(store)
    if grant is not None:
        denied = set(names) - set(grant.tool_names)
        if "research_python" in names and not grant.generated_code:
            denied.add("research_python")
        if denied:
            raise PermissionError(f"Tools outside the frozen study authorization: {sorted(denied)}")
    return grant


def available_tools(gateway, view, legacy_names):
    """Full authorized catalogue for new studies; old studies keep their allowlist."""
    grant = require_tools(gateway.store, view.workflow.tool_names)
    names = ([name for name in grant.tool_names if name in gateway._tools]
             if grant is not None else list(dict.fromkeys(legacy_names)))
    return [item["name"] for item in gateway.catalog(view.task, names)
            if not item["data_egress"] or gateway.allow_egress]
