# SPDX-License-Identifier: MIT
"""Design handoff: typed edits or existing resource IDs, never repeated sequence copying."""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated

from pydantic import Field, StrictInt, ValidationError, model_validator

from proteinrsi.contracts import Candidate, DecisionNotes, Model, digest
from proteinrsi.tasks import apply_mutations, validate_candidate
from proteinrsi.tools import ToolCall
from .resources import ResourceStore, NAMESPACE, scope_for, standard_registry, RESOURCE_ID_PATTERN
from .schema import ContractError

AA = Annotated[str, Field(pattern=r"^[ACDEFGHIKLMNPQRSTVWY]$")]


class MutationEdit(Model):
    position: Annotated[StrictInt, Field(ge=1)]
    from_residue: AA = Field(alias="from")
    to_residue: AA = Field(alias="to")

    @model_validator(mode="after")
    def changes_residue(self):
        if self.from_residue == self.to_residue:
            raise ValueError("No-op mutation")
        return self


class Design(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    candidates: list[Candidate] = Field(default_factory=list, max_length=384)
    edits: list[list[MutationEdit]] = Field(default_factory=list, max_length=384)
    candidate_refs: list[Annotated[str, Field(pattern=RESOURCE_ID_PATTERN)]] = Field(
        default_factory=list, max_length=32)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def unique_edits(self):
        for edits in self.edits:
            if not edits or len({e.position for e in edits}) != len(edits):
                raise ValueError("Each edit group must contain distinct positions")
        if len(self.candidate_refs) != len(set(self.candidate_refs)):
            raise ValueError("Duplicate candidate-set reference")
        return self


DESIGN_HANDOFF = """
Use candidate_refs to adopt candidate sets already produced by a successful tool or code.
Do not retype those sequences or invent alternative edit encodings. Use only resource IDs
provided in available_candidate_sets. An existing candidate set can be accepted with
candidate_refs=[its resource_id] and tool_calls=[]. New substitutions must use a list
of {position: integer, from: amino_acid, to: amino_acid} objects for each variant.
Tools are optional; no request means no protein model or predictor will run. Do not
invent fitness values. A validation repair changes your output, not the task constraints.
"""


def _candidate_payload(result: dict):
    if result.get("status") == "failed":
        return None
    for key in ("candidates", "current_candidates"):
        if key in result:
            return result[key]
    output = result.get("output")
    if isinstance(output, dict) and "candidates" in output:
        return output["candidates"]
    return None


def prepare_handoff(resources: ResourceStore, tool_results: list[dict]) -> tuple[list, dict]:
    """Keep native results in storage; present only bounded candidate descriptors to B."""
    compact, allowed = [], {}
    for index, original in enumerate(tool_results):
        result = deepcopy(original)
        payload = _candidate_payload(result)
        if payload is not None:
            if not isinstance(payload, list):
                raise ContractError("Candidate output must be an array")
            descriptor = resources.sequences(payload, producer="handoff:" + digest(original))
            ref = descriptor["resource_id"]
            allowed[ref] = descriptor
            result.pop("candidates", None)
            result.pop("current_candidates", None)
            if isinstance(result.get("output"), dict):
                result["output"].pop("candidates", None)
            result["candidate_set"] = descriptor
            result["handoff_index"] = index
        compact.append(result)
    return compact, allowed


def resolve_design(design: Design, resources: ResourceStore, allowed_refs: set[str], view) -> Design:
    if set(design.candidate_refs) - allowed_refs:
        raise ContractError("Design referenced a candidate set not offered in this decision")
    candidates = list(design.candidates)
    for edits in design.edits:
        sequence = apply_mutations(view.task.reference_sequence,
                                   [e.model_dump(by_alias=True) for e in edits])
        candidates.append(Candidate(sequence=sequence, source="llm_edits"))
    for ref in design.candidate_refs:
        candidates.extend(resources.candidates(ref))
    unique = {}
    task = view.task
    if task.candidate_access == "catalogue":
        from proteinrsi.research.library import catalogue_task
        task = catalogue_task(resources.store, view)
    for candidate in candidates:
        validate_candidate(task, candidate, enforce_universe=task.candidate_access != "open")
        # A design proposal has no authority to manufacture a numerical prediction.
        unique.setdefault(candidate.sequence, candidate.model_copy(update={
            "predicted_value": None, "uncertainty": None, "evidence_kind": "none"}))
    if len(unique) > 384:
        raise ContractError("Resolved candidate union exceeds 384; select smaller resources")
    # Compatibility boundary: old runners consume resolved sequences, never typed edits twice.
    return design.model_copy(update={"candidates": list(unique.values()), "edits": []})


def _issues(exc) -> list[dict]:
    if isinstance(exc, ValidationError):
        return [{"path": "/" + "/".join(map(str, e["loc"])),
                 "code": e["type"], "message": e["msg"]}
                for e in exc.errors(include_input=False, include_url=False)[:10]]
    if isinstance(exc, ContractError):
        return [exc.detail()]
    return [{"path": "", "code": "scientific_constraint", "message": str(exc)[:1200]}]


def request_design(llm, store, instructions, view, plan, tool_results, catalog) -> Design:
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    compact, allowed = prepare_handoff(resources, tool_results)
    context = {"view": view.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
               "tool_results": compact, "available_tools": catalog,
               "available_candidate_sets": list(allowed.values())}
    instructions = instructions + "\n" + DESIGN_HANDOFF
    settings = store.get("configuration", "research", {}) or {}
    repairs = min(3, max(0, int(settings.get("max_format_repairs", 2))))
    key = "design-handoff:" + digest({"scope": resources.scope, "context": context,
        "instructions": instructions, "model": getattr(llm, "model", None),
        "url": getattr(llm, "base_url", None), "client": getattr(llm, "cache_settings", {}),
        "repairs": repairs})
    record = store.get(NAMESPACE, key, {"attempts": 0, "errors": []})
    if record.get("result") is not None:
        return Design.model_validate(record["result"])
    while record["attempts"] <= repairs:
        attempt = record["attempts"]
        # Includes repair identity in the LLM request/cache. Provider failures use the
        # existing client retry policy; they are not misclassified as schema errors.
        request = {**context, "format_attempt": attempt, "validation_errors": record["errors"]}
        record["attempts"] += 1
        store.put(NAMESPACE, key, record)
        raw = llm.complete("B", instructions, request, Design.model_json_schema())
        try:
            result = resolve_design(Design.model_validate(raw), resources, set(allowed), view)
            record["result"] = result.model_dump(mode="json", by_alias=True)
            store.put(NAMESPACE, key, record)
            return result
        except (ValidationError, ContractError, ValueError) as exc:
            record["errors"] = _issues(exc)
            store.put(NAMESPACE, key, record)
            store.event("candidate_validation_feedback", {"phase": "design_handoff",
                "round": view.round_index, "attempt": attempt, "errors": record["errors"],
                "upstream_tools_rerun": False})
    raise ContractError("Design format/constraint repair budget exhausted; successful resources retained")
