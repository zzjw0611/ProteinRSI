# SPDX-License-Identifier: MIT
"""Plans describe a single research round, never a workflow/meta-policy promotion."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator
from proteinrsi.contracts import DecisionNotes, Model
from proteinrsi.tools import ToolCall


class ResearchConfig(Model):
    enabled: bool = True
    context_policy: Literal["legacy", "evidence-v1"] = "legacy"
    protocol_mode: Literal["legacy", "typed"] = "legacy"
    max_format_repairs: int = Field(default=2, ge=0, le=3)
    enable_generated_code: bool = False
    resource_selection: Literal["rules", "llm", "all"] = "rules"
    max_resources: int = Field(default=24, ge=4, le=64)
    max_context_chars: int = Field(default=16000, ge=2000, le=100000)
    max_plan_steps: int = Field(default=12, ge=4, le=24)
    max_revisions: int = Field(default=2, ge=0, le=6)
    max_executed_steps: int = Field(default=20, ge=4, le=48)
    review_after_step: bool = True


class ResearchStep(Model):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    operation: Literal["evidence", "design", "tool", "rank", "finalize"]
    question: str = Field(min_length=1, max_length=2000)
    expected_output: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=24)
    tool_call: ToolCall | None = None

    @model_validator(mode="after")
    def operation_arguments(self):
        if (self.operation == "tool") != (self.tool_call is not None):
            raise ValueError("Only tool steps require a tool_call")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Duplicate dependency")
        return self

    @property
    def owner(self) -> str:
        return {"evidence": "C", "design": "B", "tool": "A", "rank": "C", "finalize": "A"}[self.operation]


class ResearchPlan(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    hypothesis: str = Field(min_length=1, max_length=4000)
    steps: list[ResearchStep] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def acyclic_ordered_plan(self):
        seen = set()
        for step in self.steps:
            if step.step_id in seen or not set(step.depends_on) <= seen:
                raise ValueError("Plan IDs must be unique; dependencies must precede their step")
            seen.add(step.step_id)
        if sum(s.operation == "finalize" for s in self.steps) != 1 or self.steps[-1].operation != "finalize":
            raise ValueError("A plan must end with exactly one finalize step")
        return self


class PlanRevision(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    rationale: str = Field(min_length=1, max_length=3000)
    pending_steps: list[ResearchStep] | None = Field(default=None, max_length=24)


def default_plan() -> ResearchPlan:
    return ResearchPlan(hypothesis="Use revealed evidence to guide constrained design and review.", steps=[
        ResearchStep(step_id="evidence", operation="evidence", question="What do the available measurements support?",
                     expected_output="Observed-only QC, prediction error and combination diagnostics"),
        ResearchStep(step_id="design", operation="design", depends_on=["evidence"],
                     question="Which legal candidates should be considered?", expected_output="Validated candidate pool"),
        ResearchStep(step_id="rank", operation="rank", depends_on=["design"],
                     question="How should current candidates be prioritized?", expected_output="Reviewed candidate ranking"),
        ResearchStep(step_id="finalize", operation="finalize", depends_on=["rank"],
                     question="Which ordering should the experiment controller consider?",
                     expected_output="Priorities only; no experiment submission or approval"),
    ])
