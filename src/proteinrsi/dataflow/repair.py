# SPDX-License-Identifier: MIT
"""Retain valid design contributions without publishing an incomplete repair."""
from proteinrsi.contracts import AMINO_ACIDS, Candidate
from .schema import ContractError


def inspect_reply(raw, resources, allowed, view):
    from pydantic import ValidationError
    from .design import Design, resolve_design, _issues
    if not isinstance(raw, dict):
        raise ContractError("Designer reply must be an object")
    groups = ("candidates", "edits", "candidate_refs")
    # Validate metadata and envelope before retaining any contribution.
    header = Design.model_validate({k: v for k, v in raw.items() if k not in groups})
    if header.replan_reason:
        raise ContractError(header.replan_reason, code="plan_infeasible")
    candidates, errors = {}, []
    for name in groups:
        values = raw.get(name, [])
        if not isinstance(values, list) or len(values) > (32 if name == "candidate_refs" else 384):
            raise ContractError("Invalid design array: " + name)
        for index, value in enumerate(values):
            try:
                contribution = resolve_design(Design.model_validate({name: [value]}), resources, set(allowed), view)
                for candidate in contribution.candidates:
                    candidates.setdefault(candidate.sequence, candidate)
            except (ValidationError, ContractError, ValueError) as exc:
                detail = {"path": f"/{name}/{index}", "issues": _issues(exc), "original": value}
                if name == "candidates" and isinstance(value, dict) and isinstance(value.get("sequence"), str):
                    seq = value["sequence"]
                    detail["sequence_checks"] = {"length": len(seq),
                        "reference_length": len(view.task.reference_sequence),
                        "noncanonical": [{"position": i + 1, "character": aa, "codepoint": f"U+{ord(aa):04X}"}
                            for i, aa in enumerate(seq) if aa not in AMINO_ACIDS][:20],
                        "fixed_position_differences": [i + 1 for i, (a, b) in enumerate(zip(seq, view.task.reference_sequence))
                            if a != b and i + 1 not in view.task.mutable_positions][:20]}
                errors.append(detail)
    return header, list(candidates.values()), errors


def merge_retained(record, candidates):
    retained = {c["sequence"]: Candidate.model_validate(c) for c in record.get("retained", [])}
    for candidate in candidates:
        retained.setdefault(candidate.sequence, candidate)
    if len(retained) > 384:
        raise ContractError("Combined retained design exceeds 384; revise the panel")
    record["retained"] = [c.model_dump(mode="json") for c in retained.values()]
    return list(retained.values())
