# SPDX-License-Identifier: MIT
"""Task-specific result contracts at the boundary, not in the generic executor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from proteinrsi.contracts import Candidate, sequence_hash, digest
from proteinrsi.tasks import validate_candidate
from .resources import SEQUENCES, RANKING, ESTIMATES, ResourceStore
from .schema import ContractError


@dataclass(frozen=True)
class TaskContract:
    profile: str
    goal: str
    required_outputs: dict[str, str]
    feedback_mode: str
    # Hard rules are an operator-created snapshot, not part of the LLM protocol.
    constraints: dict


@dataclass(frozen=True)
class TaskProfile:
    required_outputs: dict[str, str]
    validate: Callable[[object, ResourceStore, dict], list[Candidate]]


def _validate_candidates(view, resources, outputs):
    candidates = resources.candidates(outputs["candidates"])
    if not candidates:
        raise ContractError("A design result must contain candidates")
    task = view.task
    if task.candidate_access == "catalogue":
        from proteinrsi.research.library import catalogue_task
        task = catalogue_task(resources.store, view)
    for candidate in candidates:
        validate_candidate(task, candidate, enforce_universe=task.candidate_access != "open")
    return candidates


def _validate_ranking(view, resources, outputs):
    candidates = resources.ranked_candidates(outputs["ranking"])
    if {c.sequence for c in candidates} != set(view.task.candidates):
        raise ContractError("Ranking changed the supplied candidate set")
    return candidates


def _validate_affinity(view, resources, outputs):
    data = resources.get(outputs["estimates"], schema_ref=ESTIMATES)["data"]
    expected = {"seq:" + sequence_hash(s) for s in
                (view.task.reference_sequence, view.task.target_sequence) if s}
    for estimate in data["estimates"]:
        if set(estimate["subject_refs"]) != expected:
            raise ContractError("Prediction changed the fixed molecular inputs")
        if estimate["property"] != view.task.metric or estimate["unit"] != view.task.unit:
            raise ContractError("Prediction property/unit differs from the task; no implicit conversion")
        # Quantitative estimates need a real execution receipt with matching values,
        # not a reference to another LLM-authored assertion.
        for ref in estimate["support_refs"]:
            resources.get(ref)
        if estimate["value"] is not None:
            supported = False
            subjects = expected if len(expected) == 1 else {"complex:" + digest(sorted(expected))}
            for ref in estimate["support_refs"]:
                record = resources.get(ref)
                roots = [record] + [resources.get(p) for p in record["parents"]]
                if not any(r["producer"].startswith("tool:") for r in roots):
                    continue
                for row in record["data"].get("rows", []) if isinstance(record["data"], dict) else []:
                    if (row.get("subject_ref") in subjects
                            and row.get("name") == estimate["property"] and row.get("unit") == estimate["unit"]
                            and row.get("value") == estimate["value"] and row.get("method") == estimate["method"]):
                        supported = True
            if not supported:
                raise ContractError("Numerical estimate lacks matching tool-derived metric evidence")
    # Prediction is not sequence generation. Computational output is retained separately.
    return []


class TaskProfiles:
    def __init__(self):
        self._profiles: dict[str, TaskProfile] = {}

    def register(self, name: str, profile: TaskProfile):
        if name in self._profiles:
            raise ContractError("Task profile already registered")
        self._profiles[name] = profile

    def contract(self, view) -> TaskContract:
        kind = view.task.kind.value
        if kind not in self._profiles:
            raise ContractError("No task profile is installed for this task")
        return TaskContract(profile=kind, goal=view.task.objective_description or view.task.name,
            required_outputs=dict(self._profiles[kind].required_outputs),
            feedback_mode=view.task.feedback_source,
            constraints=view.task.model_dump(mode="json"))

    def accept(self, view, resources, outputs):
        profile = self._profiles[view.task.kind.value]
        for name, schema in profile.required_outputs.items():
            if name not in outputs:
                raise ContractError(f"Missing task result port: {name}")
            resources.get(outputs[name], schema_ref=schema)
        return profile.validate(view, resources, outputs)


def default_profiles():
    registry = TaskProfiles()
    registry.register("variant_design", TaskProfile({"candidates": SEQUENCES}, _validate_candidates))
    registry.register("binder_design", TaskProfile({"candidates": SEQUENCES}, _validate_candidates))
    registry.register("variant_ranking", TaskProfile({"ranking": RANKING}, _validate_ranking))
    registry.register("affinity_prediction", TaskProfile({"estimates": ESTIMATES}, _validate_affinity))
    return registry
