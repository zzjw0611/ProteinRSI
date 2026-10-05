# SPDX-License-Identifier: MIT
"""Strict, JSON-serializable contracts. Hidden labels never belong in TaskView."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sequence_hash(sequence: str) -> str:
    return hashlib.sha256(sequence.encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class DecisionNotes(Model):
    """Concise, returned scientific rationale; never a claim to hidden model thoughts."""
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    alternatives: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(default="", max_length=3000)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    next_check: str = Field(default="", max_length=1000)


class TaskKind(str, Enum):
    VARIANT = "variant_design"
    RANKING = "variant_ranking"
    BINDER = "binder_design"
    AFFINITY = "affinity_prediction"


class BudgetSpec(Model):
    experimental_wells: int = Field(default=96, ge=0)
    llm_calls: int = Field(default=200, ge=0)
    tool_calls: int = Field(default=200, ge=0)


class InitialParentMeasurement(Model):
    """Operator-supplied pre-study measurement, not a newly purchased query."""
    value: float
    source_ref: str = Field(min_length=1, max_length=2000)


class TaskSpec(Model):
    name: str = Field(min_length=1, max_length=120)
    objective_description: str = Field(default="", max_length=12000)
    initial_observation_policy: Literal["none", "parent_once", "provided_parent"] = "none"
    initial_parent_measurement: InitialParentMeasurement | None = None
    candidate_access: Literal["open", "pool", "catalogue"] = "pool"
    kind: TaskKind = TaskKind.VARIANT
    reference_sequence: str = ""
    target_sequence: str | None = None
    mutable_positions: list[int] = Field(default_factory=list)
    max_mutations: int | None = Field(default=2, ge=0)
    execution_mode: Literal["experimental", "computational"] = "experimental"
    allow_indels: bool = False
    min_length: int | None = Field(default=None, ge=1, le=10000)
    max_length: int | None = Field(default=None, ge=1, le=10000)
    repeat_policy: Literal["exclude", "allow"] = "exclude"
    candidates: list[str] = Field(default_factory=list)
    metric: str = "fitness"
    unit: str = "a.u."
    direction: Literal["maximize", "minimize"] = "maximize"
    feedback_source: Literal["synthetic", "measured_replay", "wetlab", "computational"] = "synthetic"
    assay_protocol: str = "unspecified-v1"
    batch_size: int = Field(default=12, ge=2, le=384)
    batch_fill_policy: Literal["flexible", "full_plate"] = "flexible"
    controls_per_batch: int = Field(default=1, ge=0)
    max_rounds: int = Field(default=6, ge=1)
    seed: int = 17
    proposal_pool_size: int = Field(default=128, ge=4, le=384)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    @model_serializer(mode="wrap")
    def serialize_task(self, handler):
        data = handler(self)
        if self.candidate_access == "open":
            data.pop("proposal_pool_size", None)
        return data

    @field_validator("reference_sequence", "target_sequence")
    @classmethod
    def valid_sequence(cls, value: str | None) -> str | None:
        if value is not None and set(value) - AMINO_ACIDS:
            raise ValueError("Use nonempty uppercase canonical amino-acid sequences")
        return value

    @model_validator(mode="after")
    def consistency(self) -> TaskSpec:
        if self.batch_fill_policy == "full_plate":
            if self.execution_mode != "experimental" or self.initial_observation_policy == "parent_once":
                raise ValueError("Full plates require experimental mode and no separate paid parent batch")
            if self.budget.experimental_wells % self.batch_size:
                raise ValueError("Full-plate query budget must contain whole plates")
        if self.candidate_access == "open" and self.candidates:
            raise ValueError("Open design cannot contain a supplied candidate library")
        if self.candidate_access == "open" and self.kind == TaskKind.RANKING:
            raise ValueError("Ranking requires explicitly supplied candidates, not open design")
        if not self.reference_sequence and self.kind not in (TaskKind.BINDER, TaskKind.RANKING):
            raise ValueError("This task requires a supplied reference sequence")
        if self.feedback_source == "computational" and self.execution_mode != "computational":
            raise ValueError("Computed evidence cannot enter an experimental campaign")
        if not self.reference_sequence and self.controls_per_batch:
            raise ValueError("A de novo task has no implicit reference control")
        if self.kind == TaskKind.BINDER and not self.reference_sequence:
            if self.min_length is None or self.max_length is None or self.mutable_positions:
                raise ValueError("De novo binders require length bounds and no scaffold positions")
        if self.min_length and self.max_length and self.min_length > self.max_length:
            raise ValueError("Invalid length interval")
        if self.allow_indels and (self.mutable_positions or self.max_mutations is not None):
            raise ValueError("Variable-length design requires explicit length bounds, no positional mask or substitution cap")
        if self.allow_indels and (self.min_length is None or self.max_length is None):
            raise ValueError("Variable-length design requires length bounds")
        if self.execution_mode == "experimental" and self.budget.experimental_wells == 0:
            raise ValueError("Experimental studies require a positive query budget")
        if self.initial_observation_policy != "none" and not self.reference_sequence:
            raise ValueError("Initial parent evidence requires a reference sequence")
        if (self.initial_observation_policy == "provided_parent") != (self.initial_parent_measurement is not None):
            raise ValueError("provided_parent requires an explicit measurement and source; other policies cannot carry it")
        if self.initial_observation_policy != "none" and self.controls_per_batch:
            raise ValueError("Initial parent policies require controls_per_batch=0; no implicit repeated parent queries")
        if self.initial_observation_policy != "none" and self.kind not in (TaskKind.VARIANT, TaskKind.RANKING):
            raise ValueError("Initial parent policies are supported for variant design/ranking")
        if self.controls_per_batch >= self.batch_size:
            raise ValueError("A batch must leave room for research candidates")
        if len(set(self.mutable_positions)) != len(self.mutable_positions):
            raise ValueError("Duplicate mutable positions")
        if any(p < 1 or p > len(self.reference_sequence) for p in self.mutable_positions):
            raise ValueError("Positions are 1-based sequence indices")
        if (self.kind in (TaskKind.VARIANT, TaskKind.RANKING) and self.reference_sequence
                and not self.mutable_positions and not self.allow_indels):
            raise ValueError("Mutation tasks require explicit mutable_positions")
        if self.kind in (TaskKind.BINDER, TaskKind.AFFINITY) and not self.target_sequence:
            raise ValueError("Binder/affinity tasks require an immutable target_sequence")
        if self.kind == TaskKind.AFFINITY and self.mutable_positions:
            raise ValueError("Affinity prediction cannot mutate its inputs")
        if len(self.candidates) != len(set(self.candidates)):
            raise ValueError("Duplicate candidate sequences")
        return self


class Candidate(Model):
    sequence: str
    rationale: str = ""
    source: str = "agent"
    predicted_value: float | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    evidence_kind: Literal["proxy", "calibrated_prediction", "none"] = "none"

    @field_validator("sequence")
    @classmethod
    def valid_sequence(cls, value: str) -> str:
        if not value or set(value) - AMINO_ACIDS:
            raise ValueError("Invalid candidate sequence")
        return value


class Workflow(Model):
    strategy: Literal["additive", "pairwise", "diverse"] = "additive"
    exploration: float = Field(default=0.2, ge=0, le=1)
    ridge_alpha: float = Field(default=1.0, gt=0, le=100)
    designer_prompt: str = Field(default="Propose legal variants using only the supplied evidence.", min_length=1, max_length=12000)
    analyst_prompt: str = Field(default="Separate proxy scores, predictions and experimental measurements.", min_length=1, max_length=12000)
    principal_prompt: str = Field(default="Pursue the task objective within budget: generate proposals for design tasks and assess supplied sequences for ranking tasks.", min_length=1, max_length=12000)
    design_tool_rounds: int = Field(default=4, ge=1, le=12)
    analysis_tool_rounds: int = Field(default=0, ge=0, le=6)
    tool_names: list[str] = Field(default_factory=list, max_length=32)
    skill_names: list[str] = Field(default_factory=lambda: ["direct-sequence-design", "fitness-modeling"])

    @property
    def version(self) -> str:
        data = self.model_dump(mode="json")
        # Preserve v0.2 workflow identities when new loop options retain defaults.
        if self.design_tool_rounds == 4:
            data.pop("design_tool_rounds")
        if self.analysis_tool_rounds == 0:
            data.pop("analysis_tool_rounds")
        return "w-" + digest(data)[:16]


class MetaPolicy(Model):
    enabled: bool = True
    mode: Literal["plateau", "diagnostic"] = "plateau"
    min_observations: int = Field(default=6, ge=2, le=5000)
    cooldown_rounds: int = Field(default=1, ge=1, le=20)
    min_remaining_wells: int = Field(default=10, ge=4)
    prompt: str = Field(default="Diagnose workflow failures; propose one falsifiable bounded change.", min_length=1, max_length=12000)

    @property
    def version(self) -> str:
        return "m-" + digest(self)[:16]


class GatePolicy(Model):
    """Trusted configuration, never an evolvable agent component."""
    min_per_arm: int = Field(default=4, ge=2)
    min_effect: float = Field(default=0.0, ge=0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_samples: int = Field(default=2000, ge=100)
    max_qc_failure_fraction: float = Field(default=0.25, ge=0, lt=1)


class Observation(Model):
    sample_id: str
    sequence: str
    value: float | None = None
    metric: str
    unit: str
    qc: Literal["valid", "failed", "inconclusive", "unavailable"] = "valid"
    source: Literal["synthetic", "measured_replay", "wetlab"]
    batch_id: str
    assay_protocol: str

    @model_validator(mode="after")
    def value_matches_qc(self) -> Observation:
        if self.qc == "unavailable" and self.source != "measured_replay":
            raise ValueError("Unavailable denotes a missing historical record, not wet-lab QC")
        if self.qc == "valid" and self.value is None:
            raise ValueError("A valid observation needs a value")
        if self.qc != "valid" and self.value is not None:
            raise ValueError("Non-valid feedback is not a numeric zero")
        return self


class Sample(Model):
    sample_id: str
    candidate: Candidate
    arm: Literal["baseline", "challenger", "control", "research"]
    workflow_version: str
    replicate: int = Field(default=1, ge=1)


class Batch(Model):
    batch_id: str
    campaign_id: str
    round_index: int
    evidence_version: str
    meta_version: str
    samples: list[Sample]
    patch_id: str | None = None
    phase: Literal["research", "initialization"] = "research"


class Patch(Model):
    target: Literal["workflow", "meta"]
    base_version: str
    changes: dict[str, Any]
    hypothesis: str = Field(min_length=8, max_length=4000)
    task_kind: TaskKind
    evidence_refs: list[str] = Field(default_factory=list)
    author_backend: str = "unknown"

    @property
    def patch_id(self) -> str:
        return "p-" + digest(self)[:16]


class GateResult(Model):
    decision: Literal["accepted", "rejected", "inconclusive"]
    effect: float | None = None
    interval: tuple[float, float] | None = None
    reason: str
    n_baseline: int
    n_challenger: int


class TaskView(Model):
    """The only task context handed to LLMs/agents; no dataset path or hidden labels."""
    task: TaskSpec
    round_index: int
    observations: list[Observation]
    history: list[dict[str, Any]]
    remaining_wells: int
    workflow: Workflow
    meta: MetaPolicy
    experience: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    research_context: dict[str, Any] = Field(default_factory=dict)

    capacity: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def resource_horizon(self):
        rounds = max(0, self.task.max_rounds - self.round_index)
        effective = min(self.remaining_wells, rounds * self.task.batch_size)
        if self.task.batch_fill_policy == "full_plate":
            effective = (effective // self.task.batch_size) * self.task.batch_size
        object.__setattr__(self, "capacity", {
            "remaining_rounds": rounds, "plate_wells": self.task.batch_size,
            "remaining_query_budget": self.remaining_wells,
            "usable_queries_before_round_limit": effective,
            "unusable_query_budget": self.remaining_wells - effective,
            "required_plate_wells": self.task.batch_size if self.task.batch_fill_policy == "full_plate" and rounds else 0,
            "unused_wells_carry_to_future_rounds": False})
        return self

    @property
    def evidence_version(self) -> str:
        return digest([o.model_dump(mode="json") for o in self.observations])
