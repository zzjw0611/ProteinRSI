# SPDX-License-Identifier: MIT
"""Operator audit helpers. Never synthesize missing model reasoning."""
from proteinrsi.contracts import digest


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in {"authorization", "api_key", "access_token", "password"}
                else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def snapshot(store, phase, view, **extra):
    data = view.model_dump(mode="json")
    # Sequence identities are available through the catalogue; avoid duplicating the
    # complete 149k-row catalogue in each snapshot. Actual LLM requests remain exact.
    candidates = data["task"].pop("candidates", [])
    data["task"]["candidate_count"] = len(candidates)
    data["task"]["candidate_preview"] = candidates[:384]
    record = {"phase": phase, "view": data, "budget": store.usage(), **extra}
    key = digest(record)
    store.put("agent_snapshots", key, record, immutable=True)
    store.event("agent_state", {"phase": phase, "round": view.round_index,
        "workflow": view.workflow.version, "meta": view.meta.version,
        "evidence": view.evidence_version, "snapshot_ref": key})
    return key
