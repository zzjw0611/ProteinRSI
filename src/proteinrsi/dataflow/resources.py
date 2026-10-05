# SPDX-License-Identifier: MIT
"""Immutable, scope-bound resources. A reference is not measurement authority."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from proteinrsi.contracts import Candidate, Model, canonical, digest, sequence_hash
from .schema import ContractError, SchemaRegistry

NAMESPACE = "research_step_outputs"  # Already exposed by the bounded research-worker RPC.
SEQUENCES = "protein.sequence_set/v1"
RANKING = "protein.ranking/v1"
METRICS = "analysis.metric_table/v1"
ESTIMATES = "analysis.estimates/v1"
STRUCTURES = "protein.structure_set/v1"
RESOURCE_ID_PATTERN = r"^resource:[0-9a-f]{64}$"


class SequenceEntry(Model):
    candidate_id: str = Field(pattern=r"^seq:[0-9a-f]{64}$")
    sequence: str
    rationale: str = ""

    @model_validator(mode="after")
    def identity(self):
        Candidate(sequence=self.sequence)
        if self.candidate_id != "seq:" + sequence_hash(self.sequence):
            raise ValueError("Candidate ID does not match the actual sequence")
        return self


class SequenceSet(Model):
    items: list[SequenceEntry] = Field(max_length=384)

    @model_validator(mode="after")
    def unique(self):
        if len({i.candidate_id for i in self.items}) != len(self.items):
            raise ValueError("Duplicate candidates in a sequence resource")
        return self


class RankedSet(Model):
    candidate_set_ref: str = Field(pattern=RESOURCE_ID_PATTERN)
    ordered_ids: list[str] = Field(max_length=384)
    summary: str = Field(default="", max_length=6000)

    @field_validator("ordered_ids")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Ranking cannot duplicate an ID")
        return value


class Metric(Model):
    subject_ref: str
    name: str = Field(min_length=1)
    value: float | None
    unit: str = Field(min_length=1)
    method: str = Field(min_length=1)
    evidence: Literal["computed", "predicted"]
    uncertainty: float | None = Field(default=None, ge=0)


class MetricTable(Model):
    rows: list[Metric] = Field(max_length=10000)


class Estimate(Model):
    subject_refs: list[str] = Field(min_length=1, max_length=8)
    property: str = Field(min_length=1)
    value: float | None
    unit: str = Field(min_length=1)
    method: str = Field(min_length=1)
    support_refs: list[str] = Field(default_factory=list, max_length=32)
    limitations: str = Field(min_length=1)


class EstimateSet(Model):
    estimates: list[Estimate] = Field(min_length=1, max_length=384)


class StructureEntry(Model):
    artifact_ref: str = Field(pattern=r"^artifact:[0-9a-f]{64}\.(pdb|cif)$")
    entity_refs: list[str] = Field(min_length=1)
    chain_map: dict[str, str] = Field(min_length=1)
    structure_role: Literal["backbone", "monomer", "complex"]

    @model_validator(mode="after")
    def explicit_entity_mapping(self):
        if (len(self.entity_refs) != len(set(self.entity_refs))
                or set(self.chain_map.values()) != set(self.entity_refs)
                or any(not chain or not entity for chain, entity in self.chain_map.items())):
            raise ValueError("Every structure chain must map to a declared entity")
        return self


class StructureSet(Model):
    structures: list[StructureEntry] = Field(min_length=1, max_length=384)


MODELS = {SEQUENCES: SequenceSet, RANKING: RankedSet, METRICS: MetricTable,
          ESTIMATES: EstimateSet, STRUCTURES: StructureSet}


def standard_registry() -> SchemaRegistry:
    registry = SchemaRegistry()
    for name, model in MODELS.items():
        registry.register(name, model.model_json_schema())
    registry.register("context.task/v1", {"type": "object"}, input_only=True)
    return registry


def scope_for(view) -> str:
    return digest({"task": view.task.model_dump(mode="json"), "round": view.round_index,
                   "evidence": view.evidence_version, "workflow": view.workflow.version,
                   "meta": view.meta.version, "request_context": view.research_context})


def _preview(value, depth=0):
    if depth >= 3:
        return "<use resource binding for full value>"
    if isinstance(value, dict):
        return {k: _preview(v, depth + 1) for k, v in list(value.items())[:8]}
    if isinstance(value, list):
        return [_preview(v, depth + 1) for v in value[:3]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


class ResourceStore:
    def __init__(self, store, registry: SchemaRegistry, scope: str):
        self.store, self.registry, self.scope = store, registry, scope

    def put(self, schema_ref: str, data: Any, *, producer: str,
            parents: list[str] | None = None, is_input: bool = False) -> dict:
        self.registry.validate(schema_ref, data, output=not is_input)
        if schema_ref in MODELS:
            MODELS[schema_ref].model_validate(data)
        if len(canonical(data).encode()) > 4 * 1024**2:
            raise ContractError("Scientific resource exceeds 4MB; use a registered artifact")
        if schema_ref == STRUCTURES:
            self._check_structure_artifacts(data)
        for parent in parents or []:
            self.get(parent)
        payload = {"scope": self.scope, "schema_ref": schema_ref,
                   "schema_sha256": self.registry.fingerprint(schema_ref), "data": data,
                   "producer": producer, "parents": parents or [],
                   "measurement_authority": False}
        ref = "resource:" + digest(payload)
        self.store.put(NAMESPACE, ref, payload, immutable=True)
        return self.describe(ref)

    def get(self, ref: str, *, schema_ref: str | None = None) -> dict:
        if not isinstance(ref, str) or not ref.startswith("resource:"):
            raise ContractError("Expected a resource reference")
        payload = self.store.get(NAMESPACE, ref)
        if not isinstance(payload, dict) or "resource:" + digest(payload) != ref:
            raise ContractError("Unknown or modified resource reference")
        if payload["scope"] != self.scope:
            raise ContractError("Resource belongs to another task/evidence/workflow scope")
        actual = payload["schema_ref"]
        if schema_ref is not None and not self.registry.compatible(actual, schema_ref):
            raise ContractError(f"Expected {schema_ref}, received {actual}; use an explicit adapter")
        if payload["schema_sha256"] != self.registry.fingerprint(actual):
            raise ContractError("Stored resource schema has changed")
        self.registry.validate(actual, payload["data"])
        if actual in MODELS:
            MODELS[actual].model_validate(payload["data"])
        if actual == STRUCTURES:
            self._check_structure_artifacts(payload["data"])
        return deepcopy(payload)

    def _check_structure_artifacts(self, data):
        # Registry lookup works through the worker RPC. Native tools resolve and
        # hash-check bytes before execution; no arbitrary path reads in this layer.
        for structure in data["structures"]:
            ref = structure["artifact_ref"]
            record = self.store.get("artifacts", ref.split(":", 1)[1])
            if not record or record.get("ref") != ref:
                raise ContractError("Structure resource references an unregistered artifact")

    def describe(self, ref: str) -> dict:
        payload = self.get(ref)
        result = {"resource_id": ref, "schema_ref": payload["schema_ref"],
                  "schema_sha256": payload["schema_sha256"], "producer": payload["producer"],
                  "measurement_authority": False}
        data = payload["data"]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            result["count"] = len(data["items"])
            result["preview"] = data["items"][:3]
        elif payload["schema_ref"] != "context.task/v1":
            result["preview"] = _preview(data)
            result["preview_is_partial"] = result["preview"] != data
        return result

    def sequences(self, candidates: list, *, producer: str) -> dict:
        unique = {}
        for raw in candidates:
            candidate = raw if isinstance(raw, Candidate) else Candidate.model_validate(raw)
            unique.setdefault(candidate.sequence, SequenceEntry(
                candidate_id="seq:" + sequence_hash(candidate.sequence), sequence=candidate.sequence,
                rationale=candidate.rationale))
        data = SequenceSet(items=list(unique.values())).model_dump(mode="json")
        return self.put(SEQUENCES, data, producer=producer)

    def candidates(self, ref: str) -> list[Candidate]:
        payload = self.get(ref, schema_ref=SEQUENCES)
        return [Candidate(sequence=i["sequence"], rationale=i["rationale"], source=ref)
                for i in payload["data"]["items"]]

    def ranked_candidates(self, ref: str, *, require_permutation: bool = True) -> list[Candidate]:
        ranking = RankedSet.model_validate(self.get(ref, schema_ref=RANKING)["data"])
        data = self.get(ranking.candidate_set_ref, schema_ref=SEQUENCES)["data"]
        by_id = {i["candidate_id"]: i for i in data["items"]}
        if set(ranking.ordered_ids) - by_id.keys():
            raise ContractError("Ranking contains unknown IDs")
        if require_permutation and set(ranking.ordered_ids) != by_id.keys():
            raise ContractError("Ranking must contain every input ID exactly once")
        return [Candidate(sequence=by_id[i]["sequence"], source=ref,
                          rationale=by_id[i]["rationale"]) for i in ranking.ordered_ids]
