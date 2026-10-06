"""Synthetic provider failures must pause, not reject or silently skip method work."""

import json

import httpx
import pytest

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import Candidate, Patch
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.replay.broker import WorkerExecutionError
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from test_feedback_improvement_recovery import interrupted_feedback
from test_online_meta_trace import stage
from test_rsi import ScriptedOffspringTeam


def reply(value):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})


def test_post_feedback_m_pause_keeps_exact_request_and_unconsidered_round(tmp_path, monkeypatch):
    interrupted_feedback(tmp_path, monkeypatch, RuntimeError)
    campaign = Campaign(Store(tmp_path))
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        if "# C — experimental feedback" in payload["messages"][0]["content"]:
            return reply({"summary": "Synthetic feedback"})
        requests.append(payload)
        return httpx.Response(503) if len(requests) == 1 else reply({"reason": "Synthetic no-change decision"})

    llm = JSONLLM(campaign.store, model="fixture", base_url="https://example.invalid", api_key="fake",
                  max_attempts=1, transport=httpx.MockTransport(respond))
    campaign.team = Team(campaign.store, llm)
    campaign.meta_agent = MetaAgent(llm, campaign.store)
    monkeypatch.setattr(campaign.team, "run", lambda view: [Candidate(sequence=s)
        for s in view.task.candidates if s not in {o.sequence for o in view.observations}])
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            campaign.prepare()
    assert len(requests) == 1  # No fresh request/charge bypasses retry authorization.
    assert campaign.state["considered_round"] != 1
    assert campaign.state["method_governance"]["consecutive_failures"] == 0
    assert not campaign.store.all("method_proposal_failures")
    key = next(k for k, v in campaign.store.all("llm").items() if v["role"] == "M")
    authorize_retry(campaign.store, key, operator="test", reason="Synthetic provider restored")
    assert campaign.prepare().round_index == 1
    assert campaign.state["considered_round"] == 1
    assert requests[0] == requests[1]
    assert campaign.store.usage()["llm_calls"]["committed"] == 3  # Feedback + both M attempts.
    assert campaign.store.usage()["experimental_wells"]["committed"] == 2


@pytest.mark.parametrize("guarded_error", [False, True])
def test_workflow_pause_retains_candidate_for_resume(campaign, monkeypatch, guarded_error):
    view = campaign.view()
    patch = Patch(target="workflow", base_version=view.workflow.version, changes={"strategy": "pairwise"},
                  task_kind=view.task.kind, hypothesis="Synthetic workflow provider recovery")
    campaign.stage_patch(patch)
    attempts = []

    def run(view):
        if view.workflow.strategy == "pairwise":
            attempts.append(1)
            if len(attempts) == 1:
                if guarded_error:
                    raise WorkerExecutionError("ProviderPaused", "Synthetic pause")
                raise ProviderPaused("Synthetic pause")
        sequences = view.task.candidates
        if view.workflow.strategy == "pairwise":
            sequences = list(reversed(sequences))
        return [Candidate(sequence=s) for s in sequences]

    monkeypatch.setattr(campaign.team, "run", run)
    with pytest.raises((ProviderPaused, WorkerExecutionError)):
        campaign.prepare()
    assert campaign.state["pending_patch"]
    assert campaign.store.get("method_candidate_states", patch.patch_id)["status"] == "validating"
    assert campaign.state["method_governance"]["consecutive_failures"] == 0
    assert campaign.store.usage()["experimental_wells"]["reserved"] == 0
    assert campaign.prepare().patch_id == patch.patch_id


def test_online_meta_pause_preserves_branch_limits_and_request(campaign, monkeypatch):
    campaign, _ = stage(campaign)
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(503)
        view = json.loads(payload["messages"][1]["content"])["view"]
        from proteinrsi.contracts import Workflow
        return reply({"reason": "Synthetic distinct descendant", "patch": {
            "target": "workflow", "base_version": Workflow.model_validate(view["workflow"]).version,
            "changes": {"strategy": "pairwise"}, "task_kind": view["task"]["kind"],
            "hypothesis": "Synthetic online provider recovery"}})

    def factory(campaign, store):
        team = ScriptedOffspringTeam(store)
        team.llm = JSONLLM(store, model="fixture", base_url="https://example.invalid", api_key="fake",
                           max_attempts=1, transport=httpx.MockTransport(respond))
        return team

    monkeypatch.setattr("proteinrsi.online_meta.make_validation_team", factory)
    with pytest.raises(ProviderPaused):
        campaign.prepare()
    evaluation_id, paused = next(iter(campaign.store.all("meta_online_attempts").items()))
    assert paused["state"] == "paused_provider"
    assert campaign.state["pending_meta"]
    assert campaign.state["method_governance"]["consecutive_failures"] == 0
    assert campaign.store.usage()["experimental_wells"]["reserved"] == 0
    with pytest.raises(ProviderPaused):
        campaign.prepare()
    assert len(requests) == 1
    branch = Store(campaign.store.root / "meta-validation" / evaluation_id / "challenger")
    key = next(iter(branch.all("llm")))
    authorize_retry(branch, key, operator="test", reason="Synthetic provider restored")
    batch = campaign.prepare()
    assert batch.patch_id
    planned = campaign.store.get("meta_online_attempts", evaluation_id)
    assert planned["state"] == "planned"
    assert planned["equal_compute_limits"] == paused["equal_compute_limits"]
    assert requests[0] == requests[1]
    assert campaign.store.usage()["llm_calls"]["committed"] == 2
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0


def test_offline_meta_pause_stays_blocked_without_rejection(campaign):
    from proteinrsi.evaluation import evaluate_meta
    from proteinrsi.storage import Conflict
    from test_rsi import make_meta_cases, stage_meta

    campaign, _ = stage_meta(campaign)
    patch_id = Patch.model_validate(campaign.state["pending_meta"]).patch_id

    class PausedLLM:
        def complete(self, *args):
            raise ProviderPaused("Synthetic offline provider pause")

    def factory(store):
        team = ScriptedOffspringTeam(store)
        team.llm = PausedLLM()
        return team

    with pytest.raises(ProviderPaused):
        evaluate_meta(campaign, make_meta_cases(campaign), team_factory=factory)
    assert campaign.state["pending_meta"]
    assert campaign.state["method_governance"]["consecutive_failures"] == 0
    assert campaign.store.get("method_candidate_states", patch_id)["status"] == "blocked"
    with pytest.raises(Conflict):
        campaign.prepare()
