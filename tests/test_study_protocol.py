"""No real protein labels or live models are used in these accounting tests."""
import csv

import pytest

from proteinrsi.contracts import TaskSpec, BudgetSpec, MetaPolicy
from proteinrsi.lab import CSVOracle, UnknownMeasurement
from proteinrsi.runtime import Campaign
from proteinrsi.replay.controller import run_replay
from proteinrsi.replay.broker import dispatch
from proteinrsi.replay.sandbox import probe, SandboxUnavailable
from proteinrsi.storage import BudgetExceeded, SponsoredStore, Store, Conflict


def parent_once(tmp_path,fixture_data):
    task,labels=fixture_data
    config=task.model_dump(mode="json")
    config.update(initial_observation_policy="parent_once",controls_per_batch=0,max_rounds=2,
                  batch_size=24,budget=BudgetSpec(experimental_wells=48).model_dump())
    c=Campaign.initialize(str(tmp_path/"once"),TaskSpec.model_validate(config),meta=MetaPolicy(enabled=False))
    return c,labels


def test_parent_once_is_paid_and_not_an_extra_round(tmp_path,fixture_data):
    c,labels=parent_once(tmp_path,fixture_data)
    oracle=CSVOracle(labels,c.view().task)
    batch=c.prepare()
    assert batch.phase=="initialization" and len(batch.samples)==1
    c.approve(batch.batch_id,operator="test")
    obs=oracle.measure(batch)
    c.ingest(obs)
    c.ingest(obs)
    assert c.state["round_index"]==0
    assert c.store.usage()["experimental_wells"]["committed"]==1
    batch2=c.prepare()
    assert batch2.phase=="research" and batch2.round_index==0
    assert len(batch2.samples)==23
    assert all(s.candidate.sequence!=c.view().task.reference_sequence for s in batch2.samples)
    c.approve(batch2.batch_id,operator="test")
    c.ingest(oracle.measure(batch2))
    assert c.state["round_index"]==1
    batch3=c.prepare()
    assert len(batch3.samples)==24
    c.approve(batch3.batch_id,operator="test")
    c.ingest(oracle.measure(batch3))
    report=c.report()
    assert report["completed_rounds"]==2
    assert report["unique_measured_variants"]==48
    assert report["repeat_queries"]==0
    assert report["budget"]["experimental_wells"]["committed"]==48
    assert len(report["round_progress"])==3
    assert report["best_variant"]["value"]==report["best_measured_value"]
    assert report["best_variant"]["sequence"]


def test_replay_preflight_unknown_catalogue_costs_nothing(campaign,fixture_data,tmp_path):
    _,labels=fixture_data
    with labels.open() as f:
        rows=list(csv.DictReader(f))
    broken=tmp_path/"subset.csv"
    with broken.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows[1:])
    with pytest.raises(UnknownMeasurement):
        run_replay(campaign,broken,guarded=False)
    assert campaign.store.usage()["experimental_wells"]["committed"]==0
    assert not campaign.store.all("research_runs")


def test_sponsor_blocks_free_new_budget(tmp_path):
    sponsor=Store(tmp_path/"sponsor")
    sponsor.configure_budget({"experimental_wells":3})
    a=SponsoredStore(tmp_path/"a",sponsor,"a")
    b=SponsoredStore(tmp_path/"b",sponsor,"b")
    for store in (a,b):
        store.configure_budget({"experimental_wells":100})
    a.reserve("x","experimental_wells",2,{"seq":"A"})
    a.settle("x")
    with pytest.raises(BudgetExceeded):
        b.reserve("x","experimental_wells",2,{"seq":"B"})
    assert sponsor.usage()["experimental_wells"]["committed"]==2
    assert sponsor.remaining("experimental_wells")==1


def test_meta_initial_and_validation_queries_share_parent(campaign):
    from test_rsi import stage_meta,make_meta_cases,ScriptedOffspringTeam
    from proteinrsi.evaluation import evaluate_meta
    campaign, _ = stage_meta(campaign)
    report=evaluate_meta(campaign,make_meta_cases(campaign),promote=True,team_factory=ScriptedOffspringTeam)
    assert report["budget_scope"]=="shared_campaign"
    assert report["required_query_slots"]==32  # 4 cases * 2 arms * (2 initial+2 selected)
    assert campaign.store.usage()["experimental_wells"]["committed"]==32


@pytest.mark.parametrize("rpc_request",[
    {"rpc":"get","namespace":"oracle","key":"labels"},
    {"rpc":"put","namespace":"campaign","key":"state","value":{}},
    {"rpc":"put","namespace":"configuration","key":"prompt_bundle","value":{}},
    {"rpc":"event","kind":"workflow_trial_completed","payload":{"accepted":True}},
    {"rpc":"reserve","resource":"experimental_wells","amount":-100},
    {"rpc":"llm","role":"read_all_labels"},
])
def test_worker_cannot_grant_itself_trusted_capabilities(campaign,rpc_request):
    with pytest.raises(PermissionError):
        dispatch(campaign.team,campaign.team.tools,campaign.view(),[],rpc_request)


def test_worker_campaign_read_returns_only_labelfree_task(campaign):
    result=dispatch(campaign.team,campaign.team.tools,campaign.view(),[],
                    {"rpc":"get","namespace":"campaign","key":"state"})
    assert set(result)=={"task"}
    assert "labels" not in result["task"]


def test_guarded_replay_fails_closed_on_unsupported_kernel(campaign,fixture_data,monkeypatch):
    monkeypatch.setattr("proteinrsi.replay.controller.probe",lambda:{"available":False,"reason":"fixture unsupported"})
    with pytest.raises(SandboxUnavailable):
        run_replay(campaign,fixture_data[1],guarded=True)
    assert campaign.store.usage()["experimental_wells"]["committed"]==0


def test_real_guarded_subprocess_when_kernel_supports_it(campaign,fixture_data):
    if not probe()["available"]:
        pytest.skip("Host kernel lacks Landlock/libseccomp; no unguarded fallback test")
    result=run_replay(campaign,fixture_data[1],guarded=True)
    assert result["status"]=="complete"
    events=campaign.store.events()
    assert any(e["kind"]=="guarded_worker_completed" for e in events)


def test_legacy_campaign_requires_explicit_new_run(campaign):
    state=campaign.state
    state.pop("execution_semantics")
    campaign.store.put("campaign","state",state)
    with pytest.raises(Conflict):
        campaign.prepare()


@pytest.mark.parametrize("operation", ["team", "feedback", "meta"])
def test_worker_rpc_protocol_with_explicit_test_only_security_double(campaign, monkeypatch, operation):
    """Exercise full controller/worker calls; deliberately NOT an OS sandbox test."""
    import io
    import json
    import sys
    from test_autonomy import Decisions
    from proteinrsi.agents import Team
    from proteinrsi.replay import worker
    from proteinrsi.research.analysis import register_analysis_tools
    from proteinrsi.research.contracts import ResearchConfig
    from proteinrsi.research.prediction import register_prediction_tool
    from proteinrsi.research.library import register_library_tools

    campaign.store.put("configuration", "research", ResearchConfig().model_dump())
    view = campaign.view()
    llm = Decisions()
    team = Team(campaign.store, llm)
    gateway = team.tools.fork()
    core = register_analysis_tools(gateway, view)
    register_prediction_tool(gateway, view)
    register_library_tools(gateway, view)
    allowed = list(dict.fromkeys(view.workflow.tool_names + core))
    start = {"work": "/tmp/test-only-no-private-data", "read_roots": [], "operation": operation,
             "view": view.model_dump(mode="json"), "last_patch_round": -100,
             "tools": gateway.catalog(view.task, allowed), "allow_egress": False,
             "llm": {"model": llm.model, "base_url": llm.base_url, "cache_settings": {}}}
    monkeypatch.setattr("proteinrsi.replay.sandbox.restrict", lambda *a: {"TEST_ONLY_SECURITY_DOUBLE": True})
    monkeypatch.setattr(worker, "rpc", lambda op, **kw: dispatch(team, gateway, view, allowed, {"rpc": op, **kw}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(start) + "\n"))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker.main()
    result = json.loads(output.getvalue())
    assert "done" in result
    assert result["sandbox"] == {"TEST_ONLY_SECURITY_DOUBLE": True}
    assert "oracle" not in output.getvalue()


def test_meta_rejects_insufficient_shared_budget_before_running(campaign):
    from test_rsi import stage_meta, make_meta_cases, ScriptedOffspringTeam
    from proteinrsi.evaluation import evaluate_meta
    campaign, _ = stage_meta(campaign)
    store = campaign.store
    amount = store.remaining("experimental_wells") - 1
    store.reserve("already-used", "experimental_wells", amount, {})
    store.settle("already-used")
    with pytest.raises(ValueError, match="SHARED"):
        evaluate_meta(campaign, make_meta_cases(campaign), team_factory=ScriptedOffspringTeam)
    assert store.remaining("experimental_wells") == 1
    assert not store.all("meta_attempts")


def test_duplicate_meta_evaluation_cannot_get_a_fresh_ledger(campaign):
    from test_rsi import stage_meta, make_meta_cases, ScriptedOffspringTeam
    from proteinrsi.evaluation import evaluate_meta
    campaign, _ = stage_meta(campaign)
    cases = make_meta_cases(campaign)
    evaluate_meta(campaign, cases, promote=False, team_factory=ScriptedOffspringTeam)
    before = campaign.store.usage()
    with pytest.raises(Conflict, match="already attempted"):
        evaluate_meta(campaign, cases, promote=False, team_factory=ScriptedOffspringTeam)
    assert campaign.store.usage() == before
