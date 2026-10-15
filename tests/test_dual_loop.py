"""Task/Meta separation fixtures; these do not establish scientific efficacy."""
from copy import deepcopy

import pytest

from proteinrsi.agents import FeedbackAnalysis
from proteinrsi.capability import validate_capability_claim
from proteinrsi.contracts import CapabilityClaim, MetaPolicy, Patch, TaskView, Workflow, digest
from proteinrsi.llm import ProviderPaused
from proteinrsi.prompting import compose, packaged_prompts
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from conftest import finish_round


def claim():
    return CapabilityClaim(problem="Synthetic repeatable failure", expected_behavior="Synthetic changed behavior",
        applicability="Synthetic fixture only", validation_plan="Compare paired held-out fixture starts")


def test_structured_claim_required_without_prescribing_diagnosis():
    w = Workflow()
    p = Patch(target="workflow", base_version=w.version, changes={"exploration": 0.4},
              hypothesis="Synthetic reusable change", task_kind="variant_design", evidence_refs=["trace/1"])
    with pytest.raises(ValueError, match="capability claim"):
        validate_capability_claim(p, {"improvement_contract": "capability-v1"})
    before = p.patch_id
    legacy = p.model_dump()
    assert "capability" not in legacy and before == "p-" + digest(legacy)[:16]
    p.capability = claim()
    validate_capability_claim(p, {"improvement_contract": "capability-v1"})
    assert p.patch_id != before
    p.evidence_refs = []
    with pytest.raises(ValueError):
        validate_capability_claim(p, {"improvement_contract": "capability-v1"})


def test_prompts_are_phase_scoped_and_old_bundles_are_not_rewritten(tmp_path):
    texts = packaged_prompts()
    assert "full_plate" not in compose(None, "improver")
    assert "E-verdict" not in compose(None, "planner")
    assert "Planner owns" in compose(None, "improver")
    assert "full_plate" in compose(None, "planner")
    assert "evaluation protocol" not in texts["common"]
    store = Store(tmp_path)
    store.put("configuration", "prompt_bundle", {"templates": {"common": "old-common", "meta": "old-meta"}})
    assert compose(store, "meta", "old-strategy") == (
        "old-meta\n\nVersioned strategy guidance (cannot override invariants):\nold-strategy\n\nReference Skills:\n\n\nold-common")


@pytest.mark.core
def test_task_context_excludes_m_policy_memory_and_worker_records(campaign, tmp_path):
    from proteinrsi.agents import AnalystAgent, MetaAgent, PrincipalAgent
    from proteinrsi.localtools.artifacts import ArtifactStore
    from proteinrsi.replay.broker import dispatch
    from proteinrsi.tools import ToolCall, ToolSpec
    from proteinrsi.contracts import TaskSpec

    # The synthetic reader is pre-authorized; this test then isolates artifact
    # visibility rather than failing earlier at tool authorization.
    campaign = Campaign.initialize(str(tmp_path/'artifact-scope'),
        TaskSpec.model_validate(campaign.state['task']), workflow=Workflow(tool_names=['fixture_read']))

    view = campaign.view()
    view.meta.prompt = "PRIVATE_M_STRATEGY"
    view.experience = [{"patch": {"target": "meta", "changes": {"prompt": "PRIVATE_M_PROPOSAL"}}},
                       {"patch": {"target": "workflow", "changes": {"exploration": .3}}}]
    view.research_context["method_history"] = [{"target": "meta", "changes": {"prompt": "PRIVATE_M_HISTORY"}},
                                             {"target": "workflow", "changes": {"exploration": .3}}]
    original = view.model_dump(mode="json")
    projected = view.for_task()
    assert projected.meta.model_dump() == {"version": view.meta.version}
    assert TaskView.model_validate(projected.model_dump()).meta.version == view.meta.version
    assert "PRIVATE_M" not in projected.model_dump_json()
    assert projected.workflow == view.workflow and projected.observations == view.observations
    assert projected.evidence_version == view.evidence_version
    projected.experience[0]["patch"]["changes"]["exploration"] = .9
    assert view.model_dump(mode="json") == original

    class LLM:
        def complete(self, role, instructions, context, schema):
            assert context["view"]["meta"] == {"version": view.meta.version}
            assert "PRIVATE_M" not in str(context)
            return {"summary": "Task feedback"} if role == "C-feedback" else {"rationale": "Task plan"}
    PrincipalAgent(LLM()).plan(view, [])
    AnalystAgent(LLM()).feedback(view)
    with pytest.raises(PermissionError, match="full M policy"):
        MetaAgent(store=campaign.store).propose(view.for_task())

    private = {"owner": "M", "view": original}
    for key in ("meta-analysis:fixture", "meta-request:fixture", "method-programs:fixture"):
        campaign.store.put("research_step_outputs", key, private)
        with pytest.raises(PermissionError):
            dispatch(campaign.team, campaign.team.tools, view, [],
                {"rpc": "get", "namespace": "research_step_outputs", "key": key})
        with pytest.raises(PermissionError):
            dispatch(campaign.team, campaign.team.tools, view, [],
                {"rpc": "put", "namespace": "research_step_outputs", "key": key, "value": {}})
        assert dispatch(campaign.team, campaign.team.tools, view, [],
            {"rpc": "get", "namespace": "research_step_outputs", "key": key}, operation="meta") == private
    assert dispatch(campaign.team, campaign.team.tools, view, [],
        {"rpc": "all", "namespace": "research_step_outputs"}) == {}
    with pytest.raises(PermissionError):
        dispatch(campaign.team, campaign.team.tools, view, [], {"rpc": "llm", "role": "M"})

    artifacts = ArtifactStore(campaign.store)
    source = tmp_path/"m-analysis.json"
    source.write_text('{"policy":"PRIVATE_M_STRATEGY"}')
    private_file = artifacts.put(source, "json", task_visible=False)
    assert private_file not in artifacts.catalog(task_only=True)
    assert dispatch(campaign.team, campaign.team.tools, view, [], {"rpc": "all", "namespace": "artifacts"}) == {}
    gateway = campaign.team.tools.fork()
    gateway.context_role = "task"
    gateway.register(ToolSpec(name="fixture_read", capability="fixture", description="fixture",
        implementation_version="fixture-v1", task_kinds=[view.task.kind],
        input_schema={"type": "object"}, output_schema={"type": "object"}),
        lambda _: pytest.fail("Private artifact must be rejected before execution"))
    usage = campaign.store.usage()
    with pytest.raises(PermissionError, match="Improver artifact"):
        gateway.call(ToolCall(name="fixture_read", arguments={"input": {"ref": private_file["ref"]}}),
                     view.task, allowed=["fixture_read"], context_key="task")
    assert campaign.store.usage() == usage
    assert artifacts.resolve(private_file["ref"]).read_text() == source.read_text()


def test_final_analysis_is_independent_idempotent_and_recoverable(campaign, oracle, monkeypatch):
    count = []
    monkeypatch.setattr(campaign.team.analyst, "feedback", lambda v:
        (count.append(v.research_context["task_progress"]["terminal"]) or FeedbackAnalysis(summary="fixture")))
    for _ in range(campaign.view().task.max_rounds):
        finish_round(campaign, oracle)
    assert count[-1] is True
    assert len(count) == campaign.state["round_index"]
    assert len(campaign.store.all("task_reports")) == 1
    usage = campaign.store.usage()
    assert campaign.prepare() is None
    campaign.analyze_feedback()
    assert campaign.store.usage() == usage and len(count) == campaign.state["round_index"]
    assert not campaign.report()["task_loop"]["analysis_pending"]


def test_pending_patch_does_not_skip_task_analysis(campaign, oracle, monkeypatch):
    finish_round(campaign, oracle)
    state = campaign.state
    p = Patch(target="workflow", base_version=campaign.view().workflow.version,
        changes={"exploration": .5}, task_kind="variant_design", hypothesis="Synthetic pending modification")
    campaign.stage_patch(p)
    # Simulate a new committed Task result without relying on method admission.
    state = campaign.state
    state["history"].append({"round": 1, "batch_id": "fixture-new-evidence"})
    state["round_index"] += 1
    # Evidence identity changes when actual measurements change; this fixture adds a new row.
    row = deepcopy(state["observations"][0])
    row["sample_id"] += "-new"
    state["observations"].append(row)
    campaign.store.put("campaign", "state", state)
    calls = []
    monkeypatch.setattr(campaign.team.analyst, "feedback", lambda _: (calls.append(1) or FeedbackAnalysis(summary="fixture")))
    campaign.analyze_feedback()
    assert calls == [1] and campaign.state["pending_patch"]


@pytest.mark.parametrize("guarded", [False, True])
def test_improver_pause_does_not_block_task_or_count_as_failed_proposal(tmp_path, fixture_data, oracle, monkeypatch, guarded):
    task, _ = fixture_data
    c = Campaign.initialize(str(tmp_path/'dual'), task, meta=MetaPolicy(),
        research_config=ResearchConfig(enabled=False, improvement_contract="capability-v1"))
    calls = []
    def paused(*_):
        calls.append(1)
        if guarded:
            from proteinrsi.replay.broker import WorkerExecutionError
            raise WorkerExecutionError("ProviderPaused", "Synthetic method provider pause")
        raise ProviderPaused("Synthetic method provider pause")
    monkeypatch.setattr(c.meta_agent, "propose", paused)
    finish_round(c, oracle)
    assert c.state["history"][-1]["analyst_feedback"]
    assert c.methods.paused(c.state) and not c.store.all("method_proposal_failures")
    assert c.prepare() is not None and calls == [1]


def test_final_analysis_pause_does_not_falsely_complete(tmp_path, fixture_data, oracle, monkeypatch):
    task, _ = fixture_data
    task = task.model_copy(update={"max_rounds": 1})
    c = Campaign.initialize(str(tmp_path/'final'), task, meta=MetaPolicy(enabled=False))
    original = c.team.analyst.feedback
    def paused(_):
        raise ProviderPaused("Synthetic final analysis pause")
    monkeypatch.setattr(c.team.analyst, "feedback", paused)
    with pytest.raises(ProviderPaused):
        finish_round(c, oracle)
    assert c.report()["task_loop"]["analysis_pending"]
    assert not c.store.all("task_reports")
    assert not any(e["kind"] == "campaign_completed" for e in c.store.events())
    usage = c.store.usage()
    monkeypatch.setattr(c.team.analyst, "feedback", original)
    assert c.prepare() is None
    assert c.store.usage() == usage and len(c.store.all("task_reports")) == 1


def test_controller_rejects_unstructured_method_not_just_prompt(tmp_path, fixture_data):
    task, _ = fixture_data
    c = Campaign.initialize(str(tmp_path/'claim'), task,
        research_config=ResearchConfig(improvement_contract="capability-v1"))
    p = Patch(target="workflow", base_version=c.view().workflow.version, changes={"exploration": .3},
              hypothesis="Synthetic reusable change", task_kind=task.kind)
    with pytest.raises(ValueError, match="capability"):
        c.stage_patch(p)
    assert not c.store.all("method_candidates")
    p.capability, p.evidence_refs = claim(), [c.view().evidence_version]
    c.stage_patch(p)
    assert c.state["pending_patch"]["capability"]["applicability"] == "Synthetic fixture only"


def test_guarded_invocation_event_is_bound_to_actual_view(campaign):
    from proteinrsi.replay.broker import dispatch
    v=campaign.view()
    payload={"workflow": v.workflow.version, "meta": v.meta.version, "evidence_version": v.evidence_version}
    dispatch(campaign.team, campaign.team.tools, v, [], {"rpc": "event", "kind": "task_harness_invoked", "payload": payload})
    with pytest.raises(PermissionError):
        dispatch(campaign.team, campaign.team.tools, v, [], {"rpc": "event", "kind": "task_harness_invoked", "payload": {**payload,"workflow":"fake"}})


def test_independent_workflow_mode_never_auto_promotes_on_next_task_batch(tmp_path, fixture_data, oracle):
    task, _ = fixture_data
    c = Campaign.initialize(str(tmp_path/'independent'), task, meta=MetaPolicy(enabled=False),
        research_config=ResearchConfig(enabled=False, workflow_validation="independent"))
    finish_round(c, oracle)
    base = c.view().workflow.version
    p = Patch(target="workflow", base_version=base, changes={"exploration": .3},
              hypothesis="Synthetic pending reusable change", task_kind=task.kind)
    c.stage_patch(p)
    batch, _ = finish_round(c, oracle)
    assert batch.patch_id is None
    assert c.view().workflow.version == base and c.state["pending_patch"]
    assert not c.store.all("trial_results")


def test_early_query_exhaustion_also_records_terminal_report(campaign, oracle):
    finish_round(campaign, oracle)
    remaining = campaign.store.remaining("experimental_wells")
    campaign.store.reserve("synthetic-validation-cost", "experimental_wells", remaining, {"fixture": True})
    campaign.store.settle("synthetic-validation-cost")
    assert campaign.prepare() is None
    assert len(campaign.store.all("task_reports")) == 1
    assert next(iter(campaign.store.all("task_reports").values()))["stop_reason"] == "round_or_query_limit"


def test_independent_replay_yields_before_spending_validation_budget(tmp_path, fixture_data, monkeypatch):
    from proteinrsi.agents import MetaResponse
    from proteinrsi.replay.controller import run_replay
    task, labels = fixture_data
    c = Campaign.initialize(str(tmp_path/'yield'), task,
        research_config=ResearchConfig(enabled=False, workflow_validation="independent"))
    def propose(view, *_):
        return MetaResponse(reason="Synthetic reusable-method candidate", patch=Patch(
            target="workflow", base_version=view.workflow.version, changes={"exploration": .4},
            task_kind=task.kind, hypothesis="Synthetic method update"))
    monkeypatch.setattr(c.meta_agent, "propose", propose)
    result = run_replay(c, labels, guarded=False)
    assert result["status"] == "ready" and result["completed_rounds"] == 1
    assert c.state["pending_patch"] and c.state["pending_batch"] is None
    assert c.store.remaining("experimental_wells") == task.budget.experimental_wells - task.batch_size
    assert not result["task_loop"]["analysis_pending"]


def test_replay_wires_controller_only_cases_at_idle_boundary(campaign, fixture_data, monkeypatch):
    from proteinrsi.replay.controller import run_replay
    task, labels = fixture_data
    c = Campaign.initialize(str(campaign.store.root/'auto-validate'), task, meta=MetaPolicy(enabled=False),
        research_config=ResearchConfig(enabled=False, workflow_validation="independent"))
    p = Patch(target="workflow", base_version=c.view().workflow.version, changes={"exploration": .4},
        task_kind=task.kind, hypothesis="Synthetic pending method")
    c.stage_patch(p)
    private_manifest = object()
    calls = []
    def evaluate(current, supplied, *, promote):
        assert current.state["pending_batch"] is None
        assert supplied is private_manifest and promote
        calls.append(1)
        current.methods.abandon(p.patch_id, operator="test", reason="Synthetic controller wiring")
    monkeypatch.setattr("proteinrsi.harness_evaluation.evaluate_workflow", evaluate)
    result = run_replay(c, labels, guarded=False, validation_cases=private_manifest)
    assert calls == [1] and result["status"] == "complete"
