# SPDX-License-Identifier: MIT
"""Capability proposal contract shared by model repair and controller admission."""
from proteinrsi.contracts import Patch


def validate_capability_claim(patch: Patch, config: dict) -> None:
    if config.get("improvement_contract") != "capability-v1":
        return
    if patch.capability is None or not patch.evidence_refs:
        raise ValueError("Capability proposals require evidence_refs and a capability claim: "
                         "problem, expected_behavior, applicability and validation_plan")
    # These are claimed links, not verified causality. Evidence is assessed by E.
    if any(not ref.strip() for ref in patch.evidence_refs):
        raise ValueError("Capability evidence references cannot be blank")
