"""v0.4 tests use synthetic data / scripted clients; not biological validation."""
import json
from pathlib import Path

import httpx
import pytest
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

from proteinrsi.agents import Team
from proteinrsi.cli import main
from proteinrsi.contracts import Candidate, MetaPolicy, Observation, TaskSpec, TaskView, Workflow, digest
from proteinrsi.llm import JSONLLM
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, Store
from proteinrsi.tools import ToolCall, ToolGateway, ToolSpec
from proteinrsi.research.analysis import (ANALYSIS_TOOLS, combination_effects, evidence_summary,
                                          prediction_errors, register_analysis_tools)
from proteinrsi.research.biomni_retriever import ToolRetriever
from proteinrsi.research.contracts import ResearchConfig, ResearchPlan, ResearchStep, default_plan
from proteinrsi.research.knowledge import load_documents, snapshot_documents
from proteinrsi.research.resources import ResourceSelector
from proteinrsi.research.runner import ResearchRunner


def enable(campaign, **config):
    campaign.store.put("configuration", "research", ResearchConfig(**config).model_dump(), immutable=True)
    snapshot_documents(campaign.store)
    return campaign


def test_adaptive_round_is_real_control_flow_and_preserves_approvals(campaign, oracle, tmp_path):
    # Governed studies freeze configuration at creation, not after initialization.
    from proteinrsi.contracts import GatePolicy
    campaign = Campaign.initialize(str(tmp_path/"adaptive"), campaign.view().task,
        meta=campaign.view().meta, gate=GatePolicy.model_validate(campaign.state["gate"]),
        research_config=ResearchConfig())
    batch = campaign.prepare()
    assert campaign.report()["status"] == "awaiting_approval"
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
    run = next(iter(campaign.store.all("research_runs").values()))
    assert run["status"] == "complete"
    assert [x["owner"] for x in run["completed"]] == ["C", "B", "C", "A"]
    assert campaign.store.usage()["tool_calls"]["committed"] == 2
    assert run["state"]["final"]
    with pytest.raises(Conflict):
        campaign.ingest(oracle.measure(batch))
    assert campaign.prepare().batch_id == batch.batch_id
    campaign.approve(batch.batch_id, operator="synthetic-test")
    campaign.ingest(oracle.measure(batch))
    assert campaign.state["history"][-1]["research_analysis"]["qc"]["n_observations"] == len(batch.samples)
    assert campaign.state["history"][-1]["research_traces"]
    new = campaign.prepare()
    assert new.evidence_version != batch.evidence_version
    assert len(campaign.store.all("research_runs")) == 2


def test_completed_adaptive_plan_reuses_outputs_without_charges(campaign):
    enable(campaign)
    view = campaign.view()
    first = campaign.team.run(view)
    before = campaign.store.usage()
    second = Team(Store(campaign.store.root)).run(view)
    assert first == second
    assert campaign.store.usage() == before


def test_old_python_campaign_remains_fixed(campaign):
    campaign.prepare()
    assert campaign.store.get("configuration", "research") is None
    assert not campaign.store.all("research_runs")


@pytest.mark.parametrize("mutation", ["cycle", "duplicate", "no_finalize", "tool_args"])
def test_plan_contract_rejects_invalid_shapes(mutation):
    plan = default_plan().model_dump()
    if mutation == "cycle":
        plan["steps"][0]["depends_on"] = ["finalize"]
    elif mutation == "duplicate":
        plan["steps"][1]["step_id"] = "evidence"
    elif mutation == "no_finalize":
        plan["steps"] = plan["steps"][:-1]
    else:
        plan["steps"][0]["tool_call"] = {"name": "x", "arguments": {}}
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(plan)


def test_planner_cannot_skip_ranking_or_authorize_tools(campaign):
    runner = ResearchRunner(campaign.team, ResearchConfig())
    plan = default_plan().model_dump()
    plan["steps"][2]["operation"] = "evidence"
    with pytest.raises(ValueError, match="ranking"):
        runner._validate(ResearchPlan.model_validate(plan), [])
    plan["steps"][2].update(operation="tool", tool_call={"name": "hidden_tool", "arguments": {}})
    with pytest.raises(PermissionError):
        runner._validate(ResearchPlan.model_validate(plan), [])
    plan = default_plan()
    changed = plan.model_copy(deep=True)
    changed.steps[0].question = "Rewrite completed result"
    with pytest.raises(ValueError, match="executed"):
        runner._validate(changed, [], prefix=[plan.steps[0].model_dump(mode="json")])


class ScriptedResearchLLM:
    model = "scripted-test"
    base_url = "https://example.invalid/v1"
    def __init__(self, revision=False):
        self.roles = []
        self.revision = revision
        self.did_revise = False
    def complete(self, role, instructions, context, schema):
        self.roles.append(role)
        if role == "A-plan":
            return default_plan().model_dump(mode="json")
        if role == "A-review":
            if self.revision and not self.did_revise:
                self.did_revise = True
                steps = default_plan().steps[1:]
                check = ResearchStep(step_id="combo", operation="tool", depends_on=["evidence"],
                    question="Are there measured combinations?", expected_output="Descriptive evidence or missing data",
                    tool_call=ToolCall(name=ANALYSIS_TOOLS[2], arguments={"scale": "linear"}))
                steps[0].depends_on = ["combo"]
                return {"rationale": "Inspect available combination evidence before designing", "pending_steps":
                        [check.model_dump(mode="json"), *[s.model_dump(mode="json") for s in steps]]}
            return {"rationale": "Continue with current plan", "pending_steps": None}
        if role == "B":
            assert context["view"]["research_context"]["selected_resources"]
            return {"candidates": [{"sequence": s} for s in context["view"]["task"]["candidates"][:8]]}
        if role == "C":
            return {"ranking": [c["sequence"] for c in context["candidates"]], "summary": "Scripted review"}
        if role == "A-selection":
            return {"ranking": [c["sequence"] for c in context["ranked_candidates"]], "summary": "No experiment submitted"}
        if role == "C-tools":
            return {"rationale": "No additional tools", "tool_calls": []}
        raise AssertionError(role)


def test_llm_replans_after_real_analysis_without_workflow_change(campaign):
    enable(campaign)
    before = campaign.view().workflow.version
    llm = ScriptedResearchLLM(revision=True)
    result = Team(campaign.store, llm).run(campaign.view())
    assert len(result) == 8
    record = next(iter(campaign.store.all("research_runs").values()))
    assert record["completed"][1]["step_id"] == "combo"
    assert len(record["revisions"]) == 1
    assert any(e["kind"] == "research_plan_revised" for e in campaign.store.events())
    assert campaign.view().workflow.version == before
    assert not campaign.store.all("patches")
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


def test_interrupted_step_resumes_from_completed_plan(campaign):
    enable(campaign)
    llm = ScriptedResearchLLM()
    team = Team(campaign.store, llm)
    def interrupt(*args):
        raise KeyboardInterrupt("test interruption before rank side effects")
    team.analyst.rank = interrupt
    with pytest.raises(KeyboardInterrupt):
        team.run(campaign.view())
    assert llm.roles.count("B") == 1
    counts = campaign.store.usage()
    result = Team(campaign.store, llm).run(campaign.view())
    assert result and llm.roles.count("B") == 1
    assert campaign.store.usage() == counts


def test_bad_model_does_not_silently_fall_back(campaign):
    enable(campaign)
    llm = ScriptedResearchLLM()
    def invalid(*args):
        raise RuntimeError("realistic tool/model failure")
    team = Team(campaign.store, llm)
    team.analyst.rank = invalid
    with pytest.raises(RuntimeError):
        team.run(campaign.view())
    assert next(iter(campaign.store.all("research_runs").values()))["status"] == "blocked"
    with pytest.raises(Conflict, match="blocked"):
        Team(campaign.store, llm).run(campaign.view())
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


def diagnostic_view(values=None):
    task = TaskSpec(name="Synthetic diagnostic", reference_sequence="ACDE", mutable_positions=[1, 2],
                    metric="synthetic", unit="a.u.", feedback_source="synthetic")
    values = values or [("ACDE", 1.0), ("VCDE", 2.0), ("AVDE", 3.0), ("VVDE", 7.0)]
    observations = [Observation(sample_id=f"s{i}", sequence=s, value=v, metric=task.metric, unit=task.unit,
        batch_id="b", source="synthetic", assay_protocol=task.assay_protocol) for i, (s, v) in enumerate(values)]
    return TaskView(task=task, round_index=1, observations=observations, history=[], remaining_wells=50,
                    workflow=Workflow(), meta=MetaPolicy())


def test_combination_diagnostic_is_measured_only_and_scale_explicit():
    view = diagnostic_view()
    linear = combination_effects(view, scale="linear")
    assert linear["comparisons"][0]["null_on_scale"] == 4
    assert linear["comparisons"][0]["deviation_on_scale"] == 3
    log = combination_effects(view, scale="log")
    assert log["comparisons"][0]["null_on_scale"] == pytest.approx(__import__('math').log(6))
    view.observations = view.observations[1:]
    missing = combination_effects(view, scale="linear")
    assert not missing["comparisons"]
    assert missing["unavailable"][0]["missing_sequences"] == ["ACDE"]


def test_combination_does_not_silently_mix_batches_or_log_negative():
    view = diagnostic_view()
    view.observations[-1].batch_id = "later"
    assert not combination_effects(view, scale="linear")["comparisons"]
    assert combination_effects(view, scale="linear", pooling="pooled")["comparisons"]
    view = diagnostic_view([("ACDE", 0.0), ("VCDE", 1), ("AVDE", 1), ("VVDE", 2)])
    report = combination_effects(view, scale="log")
    assert report["unavailable"][0]["reason"] == "nonpositive_value_for_log_scale"


def test_qc_does_not_treat_failures_as_zeroes():
    view = diagnostic_view()
    failed = view.observations[0].model_copy(update={"sample_id": "failed", "value": None, "qc": "failed"})
    view.observations.append(failed)
    report = evidence_summary(view)
    assert report["batches"][0]["wt_measurements"]["mean"] == 1
    assert report["batches"][0]["n_failed"] == 1
    assert report["n_unique_valid_sequences"] == 4


def test_analysis_refuses_mixed_metric_and_duplicate_ids():
    view = diagnostic_view()
    view.observations[0].unit = "other"
    with pytest.raises(ValueError, match="incompatible"):
        evidence_summary(view)
    view = diagnostic_view()
    view.observations.append(view.observations[0])
    with pytest.raises(ValueError, match="Duplicate"):
        evidence_summary(view)


def test_prediction_diagnostics_use_frozen_batch_values(tmp_path):
    view, store = diagnostic_view(), Store(tmp_path)
    samples = [{"sample_id": o.sample_id, "arm": "baseline", "candidate":
                Candidate(sequence=o.sequence, predicted_value=float(i)).model_dump()} for i, o in enumerate(view.observations)]
    store.put("batches", "b", {"samples": samples})
    report = prediction_errors(view, store)
    assert report["n"] == 4
    assert report["mae"] == pytest.approx(1.75)
    assert report["spearman"] == pytest.approx(1.0)
    # A future row/candidate not in the visible evidence cannot enter retrospective diagnostics.
    store.put("batches", "secret", {"samples": [{"sample_id": "hidden", "candidate": {"predicted_value": 999}}]})
    assert prediction_errors(view, store) == report


def test_analysis_tools_accept_no_arbitrary_data_or_paths(campaign):
    tools = ToolGateway(campaign.store)
    names = register_analysis_tools(tools, campaign.view())
    with pytest.raises(SchemaError):
        tools.call(ToolCall(name=names[0], arguments={"file": "/hidden.csv"}), campaign.view().task,
                   allowed=names, context_key="x")
    assert campaign.store.usage()["tool_calls"]["committed"] == 0


def test_resource_catalog_has_no_hidden_labels_and_correct_scope(campaign):
    enable(campaign)
    campaign.store.put("hidden_oracle", "labels", {"DO_NOT_EXPOSE": 987654})
    view = campaign.view()
    view.experience = [{"scope": {"task_kind": "binder_design", "evidence_source": "synthetic"},
                        "status": "local_support", "patch": {"hypothesis": "wrong task"}}]
    selector = ResourceSelector(campaign.store, ResearchConfig())
    selected = selector.select(view, [])
    text = json.dumps(selected)
    assert "DO_NOT_EXPOSE" not in text and "wrong task" not in text
    assert all(r["id"] != "know_how:combination-effects" for r in selected["resources"])
    assert any(r["id"] == "know_how:evidence-qc" for r in selected["resources"])
    assert selector.select(view, []) == selected


def test_resource_text_budget_reports_omissions(campaign):
    enable(campaign)
    selected = ResourceSelector(campaign.store, ResearchConfig(max_context_chars=2000, max_resources=4)).select(campaign.view(), [])
    assert selected["context_chars"] <= 2000
    assert len(selected["resources"]) <= 4
    assert selected["omitted"]
    assert campaign.view().task.reference_sequence


def test_know_how_snapshot_survives_package_upgrade(campaign, monkeypatch):
    snapshot_documents(campaign.store)
    old = campaign.store.get("configuration", "know_how")
    monkeypatch.setattr("proteinrsi.research.knowledge.load_documents", lambda: [])
    snapshot_documents(campaign.store)
    assert campaign.store.get("configuration", "know_how") == old


def test_know_how_rejects_unreviewed_file_layout(tmp_path):
    (tmp_path / 'bad.md').write_text('No metadata')
    with pytest.raises(ValueError):
        load_documents(tmp_path)


def test_adapted_biomni_selector_uses_budgeted_json_client(campaign):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"tools": [0]}'}}]})
    client = JSONLLM(campaign.store, model="test", base_url="https://provider.example/v1", api_key="not-real",
                     transport=httpx.MockTransport(handler))
    resources = {"tools": [{"name": "allowed", "description": "test"}]}
    out = ToolRetriever().prompt_based_retrieval("test query", resources, client)
    assert out["tools"][0]["name"] == "allowed"
    assert len(requests) == 1 and campaign.store.usage()["llm_calls"]["committed"] == 1
    assert 'not-real' not in json.dumps(campaign.store.events())


@pytest.mark.parametrize("indices", [[-1], [100], [0, 0], ["0"], [True]])
def test_selector_rejects_invalid_indices(indices):
    class Client:
        def complete(self, *args):
            return {"tools": indices}
    with pytest.raises((ValueError, ValidationError)):
        ToolRetriever().prompt_based_retrieval("x", {"tools": [{"name": "tool"}]}, Client())


def test_llm_selection_requires_explicit_client(campaign):
    enable(campaign)
    with pytest.raises(ValueError, match="explicitly"):
        ResourceSelector(campaign.store, ResearchConfig(resource_selection="llm")).select(campaign.view(), [])


def test_blocked_egress_tool_not_shown_to_planner(campaign):
    enable(campaign)
    gateway = ToolGateway(campaign.store)
    spec = ToolSpec(name="outbound", capability="sequence.analyze", implementation_version="test",
        task_kinds=[campaign.view().task.kind], input_schema={"type": "object"}, output_schema={"type": "object"}, data_egress=True)
    gateway.register(spec, lambda _: {})
    view = campaign.view()
    view.workflow.tool_names = ["outbound"]
    Team(campaign.store, tools=gateway).run(view)
    selections = campaign.store.all("resource_selections")
    assert all("outbound" not in json.dumps(s) for s in selections.values())


def test_research_cli_defaults_and_report(tmp_path, fixture_data, capsys):
    task, _ = fixture_data
    taskfile = tmp_path / 'task.json'
    taskfile.write_text(task.model_dump_json())
    root = tmp_path / 'campaign'
    main(['init', '--task', str(taskfile), '--out', str(root), '--protein-model', 'none'])
    assert json.loads(capsys.readouterr().out)['research_config']['enabled']
    main(['step', '--campaign', str(root)])
    capsys.readouterr()
    main(['research', 'plans', '--campaign', str(root)])
    assert len(json.loads(capsys.readouterr().out)) == 1
    main(['research', 'resources', '--campaign', str(root)])
    assert json.loads(capsys.readouterr().out)
    main(['research', 'analyze', '--campaign', str(root)])
    assert json.loads(capsys.readouterr().out)['qc']['n_observations'] == 0


def test_both_meta_offspring_get_same_research_context(campaign):
    from test_rsi import stage_meta, make_meta_cases, ScriptedOffspringTeam
    from proteinrsi.evaluation import evaluate_meta
    enable(campaign)
    campaign, _ = stage_meta(campaign)
    snapshots = []
    class InspectTeam(ScriptedOffspringTeam):
        def __init__(self, store):
            super().__init__(store)
            snapshots.append((store.get('configuration', 'research'), store.get('configuration', 'know_how')))
    report = evaluate_meta(campaign, make_meta_cases(campaign), promote=True, team_factory=InspectTeam)
    assert report['promoted']
    assert len(snapshots) == 8 and all(s == snapshots[0] for s in snapshots)
    assert report['know_how_snapshot_sha256'] == digest(snapshots[0][1])
    assert report['research_configuration']['enabled']


def test_upstream_license_is_preserved_and_sources_identified():
    import hashlib
    root = Path(__file__).resolve().parents[1]
    text = (root/'licenses/Biomni-Apache-2.0.txt').read_bytes()
    sha = hashlib.sha1(b'blob ' + str(len(text)).encode() + b'\0' + text).hexdigest()
    assert sha == '261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64'
    source = (root/'src/proteinrsi/research/biomni_retriever.py').read_text()
    assert 'SPDX-License-Identifier: Apache-2.0' in source
    assert '400c1f366b96a35ca253e13c9b06c5076af41d65' in source


def test_adaptive_real_http_client_cannot_exceed_call_budget(tmp_path, fixture_data):
    from proteinrsi.contracts import BudgetSpec
    from proteinrsi.storage import BudgetExceeded
    task, _ = fixture_data
    task.budget = BudgetSpec(experimental_wells=36, llm_calls=0, tool_calls=20)
    camp = Campaign.initialize(str(tmp_path/'budget'), task, research_config=ResearchConfig())
    requests = []
    client = JSONLLM(camp.store, model='test', base_url='https://provider.example/v1', api_key='fake',
        transport=httpx.MockTransport(lambda req: requests.append(req)))
    camp.team = Team(camp.store, client)
    with pytest.raises(BudgetExceeded):
        camp.prepare()
    assert requests == []
    assert camp.store.usage()['experimental_wells']['committed'] == 0


def test_all_mode_and_registered_artifact_metadata(campaign, tmp_path):
    from proteinrsi.localtools.artifacts import ArtifactStore
    enable(campaign, resource_selection='all')
    path = tmp_path/'source.fasta'
    path.write_text('>example\nACDE\n')
    artifact = ArtifactStore(campaign.store).put(path, 'fasta')
    result = ResourceSelector(campaign.store, ResearchConfig(resource_selection='all')).select(campaign.view(), [])
    assert any(r['id'] == 'data:'+artifact['ref'] for r in result['resources'])
    assert str(tmp_path) not in json.dumps(result)


def test_plan_length_rejected_before_spending_tool_budget(campaign):
    runner = ResearchRunner(campaign.team, ResearchConfig(max_plan_steps=4, max_executed_steps=4))
    plan = default_plan().model_dump(mode='json')
    plan['steps'].insert(1, ResearchStep(step_id='again', operation='evidence',
        question='Inspect again?', expected_output='Actual result').model_dump(mode='json'))
    with pytest.raises(ValueError, match='limit'):
        runner._validate(ResearchPlan.model_validate(plan), ANALYSIS_TOOLS)
    assert campaign.store.usage()['tool_calls']['committed'] == 0
