"""Scripted responses and fake protein backends test mechanisms, not biological performance."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from proteinrsi.agents import Team
from proteinrsi.contracts import Candidate, Observation
from proteinrsi.prompting import compose, prompt_version
from proteinrsi.protein.esmc import ESMCConfig
from proteinrsi.research.contracts import ResearchConfig, default_plan
from proteinrsi.tools import ToolCall


class NoImplicitModel:
    config = ESMCConfig()
    calls = 0
    @property
    def identity(self):
        raise AssertionError("Model identity must not initialize any backend when unused")
    def score_variants(self, *args):
        raise AssertionError("Unrequested score")
    def embed(self, *args):
        raise AssertionError("Unrequested embedding")
    def validate(self, *args):
        raise AssertionError("Unrequested model validation")


class Decisions:
    model = "test-script-not-real-llm"
    base_url = "https://test.example/v1"
    cache_settings = {}
    def __init__(self, request=None, prefer=None):
        self.request, self.prefer = request, prefer
        self.sent = False
        self.roles = []
        self.contexts = []
    def complete(self, role, instructions, context, schema):
        self.roles.append(role)
        self.contexts.append(context)
        assert "OPTIONAL" in instructions
        if role == "A":
            return {"rationale": "No tools needed"}
        if role == "A-plan":
            p = default_plan().model_dump(mode="json")
            p["steps"] = p["steps"][1:]
            p["steps"][0]["depends_on"] = []
            return p
        if role == "A-review":
            return {"rationale": "No change", "pending_steps": None}
        if role == "B":
            if self.request and not self.sent:
                self.sent = True
                return {"tool_calls": [self.request]}
            seqs = self.prefer or context["view"]["task"]["candidates"][:4]
            return {"candidates": [{"sequence":s, "predicted_value":9999.0} for s in seqs]}
        if role == "C-tools":
            return {"rationale":"No further analysis needed","tool_calls":[]}
        if role == "C":
            return {"ranking":[c["sequence"] for c in context["candidates"]],"summary":"No invented prediction"}
        if role == "A-selection":
            return {"ranking":[c["sequence"] for c in context["ranked_candidates"]],"summary":"Priorities"}
        if role == "C-feedback":
            return {"summary":"Observed feedback"}
        if role == "M":
            return {"reason":"Insufficient justification to change the method"}
        raise AssertionError(role)


@pytest.mark.parametrize("adaptive",[False,True])
def test_configured_model_no_request_no_inference(campaign, adaptive):
    if adaptive:
        campaign.store.put("configuration","research",ResearchConfig().model_dump())
    llm = Decisions()
    view = campaign.view()
    view.workflow.tool_names = ["esmc600m_score_variants","esmc600m_embed_sequences","research_fit_predict"]
    view.workflow.analysis_tool_rounds = 3
    model = NoImplicitModel()
    team = Team(campaign.store,llm,protein_model=model)
    ranked = team.run(view)
    assert ranked
    assert all(c.predicted_value is None and c.uncertainty is None for c in ranked)
    assert campaign.store.usage()["tool_calls"]["committed"] == 0
    assert not campaign.store.all("plm_jobs")
    assert not campaign.store.all("plm_analysis")
    assert not campaign.store.all("task_predictions")
    assert "C-tools" in llm.roles
    # A second identical request reuses plan result without loading a model.
    assert team.run(view) == ranked


@pytest.mark.parametrize("adaptive",[False,True])
def test_only_explicit_requested_esmc_operation_runs(campaign, adaptive):
    class RequestedModel:
        config = ESMCConfig()
        identity = {"model_id":"TEST-FAKE","revision":"0"*40}
        calls = 0
        def score_variants(self, reference, sequences):
            self.calls += 1
            return [0.125 for _ in sequences]
        def embed(self, _):
            raise AssertionError("Scoring doesn't authorize embedding")
    if adaptive:
        campaign.store.put("configuration","research",ResearchConfig().model_dump())
    view=campaign.view()
    view.workflow.tool_names=["esmc600m_score_variants"]
    model=RequestedModel()
    request={"name":"esmc600m_score_variants","purpose":"Test sequence prior",
             "arguments":{"reference":view.task.reference_sequence,"sequences":view.task.candidates[:4]}}
    ranked=Team(campaign.store,Decisions(request),protein_model=model).run(view)
    assert model.calls == 1
    assert all(c.predicted_value is None for c in ranked)
    events=campaign.store.events()
    assert sum(e["kind"]=="tool_completed" for e in events)==1
    assert any(e["payload"].get("purpose")=="Test sequence prior" for e in events)


def test_explicit_mutation_predictor_does_not_touch_esmc(campaign):
    view=campaign.view()
    view.observations=[Observation(sample_id=str(i),sequence=s,value=float(i),metric=view.task.metric,
        unit=view.task.unit,source="synthetic",batch_id="fixture",assay_protocol=view.task.assay_protocol)
        for i,s in enumerate(view.task.candidates[:3])]
    team=Team(campaign.store,Decisions(),protein_model=NoImplicitModel())
    team.bind_tools(view)
    result=team.tools.call(ToolCall(name="research_fit_predict",arguments={"sequences":view.task.candidates[3:5],"features":"mutation"}),
        view.task,allowed=["research_fit_predict"],context_key="explicit")
    assert result["training_variants"]==3
    assert result["status"]=="predicted"
    assert all(r["predicted_value"] is not None for r in result["predictions"])
    from proteinrsi.research.prediction import attach_predictions
    candidates={s:Candidate(sequence=s) for s in view.task.candidates[3:5]}
    seq=next(iter(candidates))
    attach_predictions(candidates,{seq:result["artifact_ref"]},view,[result],campaign.store)
    assert candidates[seq].predicted_value is not None
    with pytest.raises(ValueError):
        attach_predictions(candidates,{seq:result["artifact_ref"]},view,[],campaign.store)


def test_zero_observations_fit_predict_has_no_fabricated_number(campaign):
    team=Team(campaign.store,Decisions(),protein_model=NoImplicitModel())
    view=campaign.view()
    team.bind_tools(view)
    result=team.tools.call(ToolCall(name="research_fit_predict",arguments={"sequences":view.task.candidates[:3],"features":"esmc"}),
        view.task,allowed=["research_fit_predict"],context_key="empty")
    assert result["status"]=="insufficient_observations"
    assert all(r["predicted_value"] is None for r in result["predictions"])


def test_prompts_are_snapshot_not_hot_reloaded(campaign,monkeypatch):
    before=compose(campaign.store,"designer")
    ver=prompt_version(campaign.store)
    import proteinrsi.prompting as p
    monkeypatch.setattr(p,"packaged_prompts",lambda:{"designer":"changed"})
    assert compose(campaign.store,"designer")==before
    assert prompt_version(campaign.store)==ver
    assert "OPTIONAL" in before and "private reasoning" in before


def test_strict_registered_model_results_and_documentation():
    from proteinrsi.localtools.catalog import descriptions
    for d in descriptions():
        assert d["when_to_use"] and d["when_not_to_use"] and d["cost_hint"] and d["examples"]
        validator=Draft202012Validator(d["output_schema"])
        with pytest.raises(ValidationError):
            validator.validate({"evidence_kind":"proxy"})


def test_catalogue_mode_accepts_outside_preview_without_unknown_label(campaign):
    state=campaign.state
    state["task"]["candidate_access"]="catalogue"
    state["task"]["proposal_pool_size"]=4
    campaign.store.put("campaign","state",state)
    shown=set(campaign.view().task.candidates)
    outside=next(s for s in state["task"]["candidates"] if s not in shown and s!=state["task"]["reference_sequence"])
    campaign.team=Team(campaign.store,Decisions(prefer=[outside]),protein_model=NoImplicitModel())
    batch=campaign.prepare()
    assert batch.samples[0].candidate.sequence==outside
    assert not campaign.view().observations


def test_svg_is_same_user_asset_and_readme_embeds_it():
    root=Path(__file__).resolve().parents[1]
    import hashlib
    meta=json.loads((root/"docs/assets/architecture.provenance.json").read_text())
    svg=(root/"docs/assets/proteinrsi-architecture-zh.svg").read_bytes()
    assert hashlib.sha256(svg).hexdigest()==meta["sha256"]
    assert b"<script" not in svg and b"foreignObject" not in svg
    assert "docs/assets/proteinrsi-architecture-zh.svg" in (root/"README.md").read_text()


def test_explicit_esmc_head_uses_embeddings_not_implicit_prior(campaign):
    import numpy as np
    class FeatureOnly:
        config = ESMCConfig()
        identity = {"model_id": "FAKE-TEST-ONLY", "revision": "0" * 40}
        calls = 0
        def embed(self, seqs):
            self.calls += 1
            return np.array([[ord(x) for x in seq[:4]] for seq in seqs], dtype=float)
        def score_variants(self, *args):
            raise AssertionError("Explicit embedding/head request does not authorize a prior score")
    view = campaign.view()
    view.observations = [Observation(sample_id=f"head-{i}", sequence=s, value=float(i),
        metric=view.task.metric, unit=view.task.unit, source=view.task.feedback_source,
        batch_id="head-initial", assay_protocol=view.task.assay_protocol)
        for i, s in enumerate(view.task.candidates[:3])]
    model = FeatureOnly()
    team = Team(campaign.store, Decisions(), protein_model=model)
    team.bind_tools(view)
    result = team.tools.call(ToolCall(name="research_fit_predict",
        arguments={"sequences": view.task.candidates[3:6], "features": "esmc"}),
        view.task, allowed=["research_fit_predict"], context_key="explicit-feature-head")
    assert model.calls == 1
    assert result["model"]["model_id"] == "FAKE-TEST-ONLY"
    assert all(np.isfinite(r["predicted_value"]) and r["uncertainty"] is None
               for r in result["predictions"])
