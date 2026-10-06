"""Resume the post-feedback improver without repeating decisions or experiment charges."""

import pytest

from proteinrsi.agents import MetaResponse
from proteinrsi.contracts import BudgetSpec, GatePolicy, MetaPolicy, Observation, Patch, TaskSpec
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store


def interrupted_feedback(tmp_path, monkeypatch, error):
    task = TaskSpec(
        name="Synthetic post-feedback interruption fixture",
        reference_sequence="ACDE",
        mutable_positions=[2],
        candidates=["AADE", "ADDE", "AEDE", "AFDE", "AGDE", "AHDE", "AIDE", "AKDE"],
        controls_per_batch=0,
        batch_size=2,
        max_rounds=4,
        budget=BudgetSpec(experimental_wells=8),
    )
    campaign = Campaign.initialize(
        str(tmp_path), task,
        meta=MetaPolicy(min_observations=2, min_remaining_wells=4),
        gate=GatePolicy(min_per_arm=2, bootstrap_samples=100),
    )
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="recovery-test")
    observations = [Observation(
        sample_id=sample.sample_id,
        batch_id=batch.batch_id,
        sequence=sample.candidate.sequence,
        value=1.0,
        metric=task.metric,
        unit=task.unit,
        source="synthetic",
        assay_protocol=task.assay_protocol,
    ) for sample in batch.samples]

    def interrupted_improvement():
        raise error("Synthetic interruption after feedback commit")

    monkeypatch.setattr(campaign, "consider_improvement", interrupted_improvement)
    with pytest.raises(error):
        campaign.ingest(observations)
    return observations


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_prepare_recovers_post_feedback_improvement_once(tmp_path, monkeypatch, error):
    observations = interrupted_feedback(tmp_path, monkeypatch, error)
    resumed = Campaign(Store(tmp_path))
    assert resumed.state["round_index"] == 1
    assert resumed.state["considered_round"] != 1
    assert resumed.store.usage()["experimental_wells"]["committed"] == 2
    proposals = []

    def propose(view, last_patch_round):
        proposals.append(view.round_index)
        return MetaResponse(reason="Synthetic no-change decision")

    monkeypatch.setattr(resumed.meta_agent, "propose", propose)
    resumed.ingest(observations)  # Reimporting committed measurements stays idempotent.
    batch = resumed.prepare()
    assert batch.round_index == 1
    assert proposals == [1]
    assert resumed.state["considered_round"] == 1
    assert len(resumed.state["observations"]) == 2
    assert resumed.store.usage()["experimental_wells"]["committed"] == 2
    assert resumed.prepare() == batch
    assert proposals == [1]

    # A further process restart with a prepared batch also cannot repeat M.
    restarted = Campaign(Store(tmp_path))
    monkeypatch.setattr(restarted.meta_agent, "propose", propose)
    assert restarted.prepare() == batch
    assert proposals == [1]
    restarted.cancel_prepared(batch.batch_id, operator="recovery-test")
    assert restarted.prepare().round_index == 1
    assert proposals == [1]


@pytest.mark.parametrize("target", ["workflow", "meta"])
def test_existing_pending_method_is_not_reproposed_on_resume(tmp_path, monkeypatch, target):
    interrupted_feedback(tmp_path, monkeypatch, RuntimeError)
    resumed = Campaign(Store(tmp_path))
    view = resumed.view()
    definition = view.workflow if target == "workflow" else view.meta
    changes = {"strategy": "pairwise"} if target == "workflow" else {"mode": "diagnostic"}
    patch = Patch(
        target=target, base_version=definition.version, changes=changes,
        task_kind=view.task.kind, hypothesis="Synthetic pending method recovery",
        author_backend="synthetic-test",
    )
    resumed.stage_patch(patch)
    resumed = Campaign(Store(tmp_path))

    def unexpected_proposal(*args, **kwargs):
        raise AssertionError("A pending method must be validated before another M proposal")

    monkeypatch.setattr(resumed.meta_agent, "propose", unexpected_proposal)
    # Two-slot batches defer either gate; neither should trigger another proposal.
    batch = resumed.prepare()
    assert batch.round_index == 1
    assert len(resumed.store.all("patches")) == 1
    assert resumed.state["pending_patch" if target == "workflow" else "pending_meta"]


def test_first_round_does_not_run_recovery_improvement(tmp_path, monkeypatch):
    task = TaskSpec(name="Synthetic initial planning fixture", reference_sequence="ACDE",
                    mutable_positions=[2], candidates=["AADE", "ADDE"])
    campaign = Campaign.initialize(str(tmp_path), task)

    def unexpected_improvement():
        raise AssertionError("No committed research feedback exists yet")

    monkeypatch.setattr(campaign, "consider_improvement", unexpected_improvement)
    assert campaign.prepare().round_index == 0
