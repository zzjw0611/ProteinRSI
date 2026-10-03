import json

import pytest

from proteinrsi.contracts import GatePolicy, MetaPolicy, TaskSpec
from proteinrsi.lab import CSVOracle
from proteinrsi.runtime import Campaign
from proteinrsi.synthetic import make_fixture


@pytest.fixture
def fixture_data(tmp_path):
    task_path, labels = make_fixture(tmp_path / "fixture", rounds=5)
    task = TaskSpec.model_validate(json.loads(task_path.read_text()))
    return task, labels


@pytest.fixture
def campaign(tmp_path, fixture_data):
    task, _ = fixture_data
    return Campaign.initialize(str(tmp_path / "campaign"), task, meta=MetaPolicy(enabled=False),
                               gate=GatePolicy(min_per_arm=2, bootstrap_samples=200))


@pytest.fixture
def oracle(fixture_data):
    task, labels = fixture_data
    return CSVOracle(labels, task)


def finish_round(campaign, oracle):
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="pytest")
    observations = oracle.measure(batch)
    campaign.ingest(observations)
    return batch, observations
