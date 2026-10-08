# SPDX-License-Identifier: MIT
"""Four logical agents. Deterministic mode is an explicit non-LLM baseline."""
from __future__ import annotations

from importlib.resources import files

from pydantic import Field

from proteinrsi.contracts import Candidate, DecisionNotes, Model, Patch, TaskKind, TaskView, Workflow
from proteinrsi.llm import JSONLLM
from proteinrsi.storage import Store
from proteinrsi.prompting import compose, prompt_version
from proteinrsi.protein.metadata import declared_identity
from proteinrsi.tasks import apply_mutations, rank_candidates, validate_candidate
from proteinrsi.tools import ToolCall, ToolGateway
from proteinrsi.dataflow.design import Design, request_design


class Plan(Model):
    rationale: str
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)


class Analysis(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    ranking: list[str]
    summary: str
    prediction_refs: dict[str, str] = Field(default_factory=dict)


class FeedbackAnalysis(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    summary: str
    alternatives: list[str] = Field(default_factory=list)


class MetaResponse(Model):
    decision_notes: DecisionNotes = Field(default_factory=DecisionNotes)
    reason: str
    patch: Patch | None = None


def skill_text(names: list[str]) -> str:
    texts = []
    for name in names:
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            raise ValueError("Invalid skill name")
        path = files("proteinrsi").joinpath("skills", name, "SKILL.md")
        if not path.is_file():
            raise ValueError(f"Unknown skill: {name}")
        # These two skills are supplied by the frozen, selected knowledge receipt.
        # Do not re-inject their installed text into B after a package update.
        from proteinrsi.research.skill_library import SKILLS
        if name not in SKILLS:
            texts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(texts)


class PrincipalAgent:
    def __init__(self, llm: JSONLLM | None = None, store=None):
        self.llm = llm
        self.store = store or getattr(llm, "store", None)

    def plan(self, view: TaskView, catalog: list[dict]) -> Plan:
        if self.llm is None:
            return Plan(rationale="Deterministic baseline: use revealed measurements only.")
        instructions = compose(self.store, "principal_fixed_plan", view.workflow.principal_prompt)
        return Plan.model_validate(self.llm.complete("A", instructions,
                    {"view": view.model_dump(mode="json"), "available_tools": catalog}, Plan.model_json_schema()))


    def finalize(self, view: TaskView, ranked: list[Candidate]) -> list[Candidate]:
        if self.llm is None:
            return ranked
        from proteinrsi.ranking import request_ranking
        result = request_ranking(self.llm, self.store, "A-selection",
            compose(self.store, "principal_selection", view.workflow.principal_prompt),
            {"view": view.model_dump(mode="json")}, "ranked_candidates", ranked, Analysis)
        by_seq = {c.sequence: c for c in ranked}
        return [by_seq[s] for s in result.ranking]


class DesignerAgent:
    def __init__(self, llm: JSONLLM | None = None, store=None):
        self.llm = llm
        self.store = store or getattr(llm, "store", None)

    def propose(self, view: TaskView, plan: Plan, tool_results: list[dict], catalog: list[dict]) -> Design:
        if self.llm is None:
            if view.task.kind in (TaskKind.BINDER, TaskKind.AFFINITY):
                raise ValueError("Binder/affinity tasks require an actual configured LLM/tool route")
            if not view.task.candidates:
                raise ValueError("Deterministic mode needs an explicit candidate library")
            return Design(candidates=[Candidate(sequence=s, source="library") for s in view.task.candidates])
        instructions = compose(self.store, "designer", view.workflow.designer_prompt,
                               skill_text(view.workflow.skill_names))
        return request_design(self.llm, self.store, instructions, view, plan, tool_results, catalog)


class AnalystAgent:
    def __init__(self, llm: JSONLLM | None = None, protein_model=None, store=None):
        self.llm = llm
        self.store = store or getattr(llm, "store", None)
        self.protein_model = protein_model

    def rank(self, view: TaskView, candidates: list[Candidate], tool_results: list[dict]) -> list[Candidate]:
        if self.llm is None and self.protein_model is not None and view.task.kind in (TaskKind.VARIANT, TaskKind.RANKING):
            from proteinrsi.protein.analysis import rank_with_esmc
            ranked, report = rank_with_esmc(view, candidates, self.protein_model)
            tool_results = [*tool_results, {"esmc600m_analysis": report}]
        elif self.llm is None and view.task.kind in (TaskKind.VARIANT, TaskKind.RANKING):
            ranked = rank_candidates(view.task, [c.sequence for c in candidates], view.observations,
                                     view.workflow, view.task.seed + view.round_index)
        else:
            ranked = candidates
        if self.llm is None:
            return ranked
        # No implicit ESMC/Ridge/predictive scoring in the LLM path, even on cache hits.
        ranked = [c.model_copy(update={"predicted_value": None, "prediction_ref": None, "uncertainty": None,
                                     "evidence_kind": "none"}) for c in ranked]
        from proteinrsi.ranking import request_ranking
        response = request_ranking(self.llm, self.store, "C",
            compose(self.store, "analyst", view.workflow.analyst_prompt),
            {"view": view.model_dump(mode="json"), "tool_results": tool_results},
            "candidates", ranked, Analysis)
        by_seq = {c.sequence: c for c in ranked}
        from proteinrsi.research.prediction import attach_predictions
        attach_predictions(by_seq, response.prediction_refs, view, tool_results, self.store)
        self.store.event("analyst_decision", {"round": view.round_index,
            "summary": response.summary, "prediction_refs": response.prediction_refs,
            "tool_results_used": len(tool_results), "implicit_model_calls": 0})
        return [by_seq[s] for s in response.ranking]


    def feedback(self, view: TaskView) -> FeedbackAnalysis:
        if self.llm is None:
            return FeedbackAnalysis(summary="Deterministic feedback: inspect QC, prediction errors and trial records.")
        return FeedbackAnalysis.model_validate(self.llm.complete("C-feedback",
            compose(self.store, "feedback", view.workflow.analyst_prompt,
                    skill_text(["experimental-feedback"])),
            {"view": view.model_dump(mode="json")}, FeedbackAnalysis.model_json_schema()))


class Team:
    def __init__(self, store: Store, llm: JSONLLM | None = None, tools: ToolGateway | None = None,
                 protein_model=None, register_tools: bool = True):
        from proteinrsi.protein.esmc import from_store
        from proteinrsi.protein.tools import register_esmc_tools
        self.protein_model = (protein_model or from_store(store)) if register_tools else None
        self.store, self.llm = store, llm
        self.tools = tools or ToolGateway(store)
        if self.protein_model is not None:
            register_esmc_tools(self.tools, self.protein_model)
        from proteinrsi.localtools.registry import attach_stored
        if register_tools:
            attach_stored(self.tools, store)
        self.principal = PrincipalAgent(llm, store)
        self.designer = DesignerAgent(llm, store)
        self.analyst = AnalystAgent(llm, self.protein_model, store)

    def bind_tools(self, view: TaskView):
        from proteinrsi.research.prediction import register_prediction_tool
        from proteinrsi.research.library import register_library_tools
        self.tools = self.tools.fork()
        register_prediction_tool(self.tools, view, self.protein_model)
        register_library_tools(self.tools, view)
        from proteinrsi.research.code import register_code_tool
        register_code_tool(self.tools, view)
        from proteinrsi.research.metric_tools import register_metric_tool
        register_metric_tool(self.tools, view)

    def run(self, view: TaskView) -> list[Candidate]:
        self.bind_tools(view)
        config = self.store.get("configuration", "research")
        if config and config.get("enabled"):
            from proteinrsi.research.contracts import ResearchConfig
            from proteinrsi.research.runner import ResearchRunner
            return ResearchRunner(self, ResearchConfig.model_validate(config)).run(view)
        return self._run_fixed(view)

    def _run_fixed(self, view: TaskView) -> list[Candidate]:
        from proteinrsi.contracts import digest
        identity = {"view": view.model_dump(mode="json"), "backend": "llm" if self.llm else "deterministic",
                      "protein_model": declared_identity(self.store, self.protein_model),
                      "execution_semantics": "llm-on-demand-v1", "prompts": prompt_version(self.store),
                      "llm_model": getattr(self.llm, "model", None),
                      "llm_url": getattr(self.llm, "base_url", None),
                      "local_tools": self.store.get("configuration", "local_tools")}
        identity.update(getattr(self.llm, "cache_settings", {}))
        key = digest(identity)
        previous = self.store.get("team_outputs", key)
        if previous is not None:
            return [Candidate.model_validate(c) for c in previous]
        if self.llm is None and self.protein_model is not None:
            self.protein_model.validate(view.task.reference_sequence)
        catalog = self.tools.catalog(view.task, view.workflow.tool_names)
        plan = self.principal.plan(view, catalog)
        results = [self.tools.call(c, view.task, allowed=view.workflow.tool_names, context_key=key)
                   for c in plan.tool_calls]
        candidates = []
        # A bounded design dialogue enables backbone -> sequence -> fold-back chains.
        # No generated shell commands; every requested operation goes through the gateway.
        for step in range(view.workflow.design_tool_rounds):
            design = self.designer.propose(view, plan, results, catalog)
            candidates += list(design.candidates)
            candidates += [Candidate(sequence=apply_mutations(view.task.reference_sequence, edits),
                                     source="llm_edits") for edits in design.edits]
            if not design.tool_calls:
                break
            if step == view.workflow.design_tool_rounds - 1:
                raise ValueError("Design tool-round limit reached; no unreviewed partial batch submitted")
            for call in design.tool_calls:
                result = self.tools.call(call, view.task, allowed=view.workflow.tool_names,
                                         context_key=key)
                results.append(result)
                self.store.event("design_tool_result", {"round": view.round_index,
                    "step": step, "tool": call.name, "result_keys": list(result)})
        else:
            raise ValueError("Design did not terminate")
        for result in results:
            # The operator's binding must normalize tool output to this documented contract.
            for item in result.get("candidates", []):
                candidates.append(Candidate.model_validate(item))
        deduplicated = {}
        for candidate in candidates:
            validate_candidate(view.task, candidate, enforce_universe=view.task.candidate_access == "pool")
            deduplicated.setdefault(candidate.sequence, candidate)
        if not deduplicated:
            raise ValueError("Designer/tools returned no valid candidates")
        if self.llm is not None and view.workflow.analysis_tool_rounds:
            analysis_catalog = [d for d in catalog if d["capability"] not in
                {"sequence.generate", "sequence.inverse_fold", "backbone.generate", "variant.suggest"}]
            analysis_allowed = [d["name"] for d in analysis_catalog]
            for step in range(view.workflow.analysis_tool_rounds):
                request = Plan.model_validate(self.llm.complete("C-tools",
                    compose(self.store, "analysis_tools", view.workflow.analyst_prompt),
                    {"view": view.model_dump(mode="json"), "available_tools": analysis_catalog,
                     "candidates": [c.model_dump() for c in deduplicated.values()], "tool_results": results},
                    Plan.model_json_schema()))
                if not request.tool_calls:
                    break
                if step == view.workflow.analysis_tool_rounds - 1:
                    raise ValueError("Analysis tool-round limit reached")
                results += [self.tools.call(c, view.task, allowed=analysis_allowed, context_key=key)
                            for c in request.tool_calls]
        ranked = self.analyst.rank(view, list(deduplicated.values()), results)
        ranked = self.principal.finalize(view, ranked)
        self.store.put("team_outputs", key, [c.model_dump() for c in ranked], immutable=True)
        self.store.event("team_completed", {"round": view.round_index, "workflow": view.workflow.version,
            "meta": view.meta.version, "evidence": view.evidence_version,
            "backend": "llm" if self.llm else "deterministic", "plan": plan.model_dump(),
            "candidate_count": len(ranked), "trace_id": key})
        return ranked


class MetaAgent:
    def __init__(self, llm: JSONLLM | None = None, store=None):
        self.llm = llm
        self.store = store or getattr(llm, "store", None)

    def propose(self, view: TaskView, last_patch_round: int = -100) -> MetaResponse:
        policy = view.meta
        valid = [o for o in view.observations if o.qc == "valid"]
        if (not policy.enabled or len(valid) < policy.min_observations
                or view.remaining_wells < policy.min_remaining_wells
                or view.round_index - last_patch_round < policy.cooldown_rounds):
            return MetaResponse(reason="Insufficient evidence, cooldown or remaining experimental budget")
        if self.llm:
            instructions = compose(self.store, "meta", policy.prompt)
            context = {"view": view.model_dump(mode="json"),
                "workflow_schema": Workflow.model_json_schema(), "meta_schema": type(policy).model_json_schema()}
            if self.store:
                from proteinrsi.contracts import digest
                key = "meta-request:" + digest({"context": context, "instructions": instructions,
                    "model": getattr(self.llm, "model", None), "url": getattr(self.llm, "base_url", None),
                    "client": getattr(self.llm, "cache_settings", {})})
                saved = self.store.get("research_step_outputs", key)
                if saved is None:
                    saved = {**context, "compute_usage": self.store.usage()}
                    self.store.put("research_step_outputs", key, saved, immutable=True)
                context = saved
            else:
                context["compute_usage"] = {}
            return MetaResponse.model_validate(self.llm.complete("M", instructions,
                context, MetaResponse.model_json_schema()))
        # Explicit scripted baseline: useful for mechanism testing, not an LLM/scientific claim.
        recent = view.history[-1] if view.history else {}
        if policy.mode == "diagnostic" and recent.get("qc_failure_fraction", 0) > 0.2:
            return MetaResponse(reason="Investigate measurement quality before editing design strategy")
        if view.workflow.strategy == "additive":
            return MetaResponse(reason="Test whether a joint-effects surrogate helps",
                patch=Patch(target="workflow", base_version=view.workflow.version,
                    changes={"strategy": "pairwise"}, task_kind=view.task.kind,
                    hypothesis="A pairwise surrogate may rank combinations better than additive effects.",
                    evidence_refs=[view.evidence_version], author_backend="deterministic"))
        if policy.mode == "plateau":
            return MetaResponse(reason="Queue a successor improver for separate evaluation",
                patch=Patch(target="meta", base_version=policy.version, changes={"mode": "diagnostic"},
                    task_kind=view.task.kind, hypothesis="Diagnose measurement failures before editing methods.",
                    evidence_refs=[view.evidence_version], author_backend="deterministic"))
        return MetaResponse(reason="No supported additional bounded change")
