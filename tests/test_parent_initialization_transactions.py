"""Initialization publication must survive interruption without stranded query slots."""

import pytest

from proteinrsi.contracts import BudgetSpec, MetaPolicy, Observation, TaskSpec
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_interrupted_parent_binding_rolls_back_and_restarts(tmp_path, monkeypatch, error):
    task = TaskSpec(
        name="Synthetic parent transaction fixture",
        reference_sequence="ACDE",
        mutable_positions=[2],
        candidates=["AADE"],
        initial_observation_policy="parent_once",
        controls_per_batch=0,
        batch_size=2,
        budget=BudgetSpec(experimental_wells=1),
    )
    campaign = Campaign.initialize(str(tmp_path), task, meta=MetaPolicy(enabled=False))
    bind = campaign.methods.bind_batch

    def interrupted_bind(state, batch):
        bind(state, batch)
        raise error("Synthetic interruption after method binding")

    monkeypatch.setattr(campaign.methods, "bind_batch", interrupted_bind)
    with pytest.raises(error):
        campaign.prepare()

    resumed = Campaign(Store(tmp_path))
    assert resumed.state["status"] == "ready"
    assert resumed.state["pending_batch"] is None
    assert resumed.store.usage()["experimental_wells"] == {
        "limit": 1, "committed": 0, "reserved": 0,
    }
    assert not resumed.store.all("batches")
    assert not resumed.store.all("batch_method_bindings")

    batch = resumed.prepare()
    assert batch.phase == "initialization"
    assert resumed.prepare() == batch
    resumed.approve(batch.batch_id, operator="transaction-test")
    observations = [Observation(
        sample_id=batch.samples[0].sample_id,
        batch_id=batch.batch_id,
        sequence=task.reference_sequence,
        value=1.0,
        metric=task.metric,
        unit=task.unit,
        source="synthetic",
        assay_protocol=task.assay_protocol,
    )]
    resumed.ingest(observations)
    resumed.ingest(observations)
    assert resumed.state["status"] == "complete"
    assert resumed.state["initialization_done"]
    assert resumed.state["round_index"] == 0
    assert len(resumed.state["observations"]) == 1
    assert resumed.store.usage()["experimental_wells"] == {
        "limit": 1, "committed": 1, "reserved": 0,
    }
