"""A resumed E request consumes saved paid observations, not a second lookup."""
from copy import deepcopy

import pytest

from proteinrsi.lab import CSVOracle
from proteinrsi.llm import ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.replay.controller import run_replay
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from test_llm_evaluation_integration import make_campaign
from test_custom_metric_integration import custom_plan, synthetic_metric_worker as synthetic_metric_worker


def test_replay_reuses_durable_measurements_after_e_pause(campaign, fixture_data, monkeypatch, synthetic_metric_worker):
    campaign, transport, runs, _, _ = make_campaign(campaign, monkeypatch, pause_verdict=True, plan=custom_plan())
    _, labels = fixture_data
    calls = []
    measure = CSVOracle.measure

    def counted(self, batch):
        calls.append(batch.batch_id)
        return measure(self, batch)

    monkeypatch.setattr(CSVOracle, "measure", counted)
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    with pytest.raises(ProviderPaused):
        run_replay(campaign, labels, guarded=False)
    saved = deepcopy(campaign.store.all("measurements"))
    usage = campaign.store.usage()["experimental_wells"]
    metric_results = campaign.store.all("evaluation_metric_results")
    metric_calls = len(synthetic_metric_worker)
    assert len(calls) == 1 and len(saved) == 1
    reopened = Store(campaign.store.root)
    resumed = Campaign(reopened, transport.team(reopened))
    monkeypatch.setattr(resumed, "consider_improvement", lambda: None)
    monkeypatch.setattr(resumed.team, "run", lambda _: pytest.fail("Do not rerun measured arms"))
    prepare = resumed.prepare
    monkeypatch.setattr(resumed, "prepare", lambda: None if resumed.state["round_index"] else prepare())
    key = next(k for k, value in reopened.all("llm").items() if value["state"] == "failed")
    authorize_retry(reopened, key, operator="synthetic-test", reason="Synthetic evaluator is restored")
    run_replay(resumed, labels, guarded=False)
    assert len(calls) == 1
    assert reopened.all("evaluation_metric_results") == metric_results
    assert len(synthetic_metric_worker) == metric_calls
    assert reopened.all("measurements") == saved
    assert reopened.usage()["experimental_wells"] == usage
    assert resumed.state["round_index"] == 1
    assert len(runs) == 2
