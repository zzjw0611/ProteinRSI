import json

import pytest

from proteinrsi.contracts import MetaPolicy, Observation, Patch, Workflow
from proteinrsi.lab import CSVOracle, UnknownMeasurement, read_results
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, Store
from conftest import finish_round


def test_complete_real_feedback_cycle_and_restart(campaign, oracle):
    batch = campaign.prepare()
    with pytest.raises(Conflict):
        campaign.ingest(oracle.measure(batch))
    restarted = Campaign(Store(campaign.store.root))
    assert restarted.prepare() == batch
    restarted.approve(batch.batch_id, operator="test")
    restarted.approve(batch.batch_id, operator="test")
    measured = oracle.measure(batch)
    restarted.ingest(measured)
    restarted.ingest(measured)
    assert restarted.state["round_index"] == 1
    assert len(restarted.state["observations"]) == len(batch.samples)
    next_batch = restarted.prepare()
    assert next_batch.evidence_version != batch.evidence_version
    assert next_batch.round_index == 1
    assert restarted.store.usage()["experimental_wells"]["committed"] == len(batch.samples)


@pytest.mark.parametrize("field,value", [("unit", "wrong"), ("source", "wetlab"),
                                         ("assay_protocol", "changed"), ("sequence", "ACDE")])
def test_result_identity_validation(campaign, oracle, field, value):
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    measured = oracle.measure(batch)
    data = measured[0].model_dump()
    data[field] = value
    measured[0] = Observation.model_validate(data)
    with pytest.raises(ValueError):
        campaign.ingest(measured)
    assert not campaign.state["observations"]


def test_partial_duplicate_and_changed_imports(campaign, oracle):
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    measured = oracle.measure(batch)
    with pytest.raises(ValueError):
        campaign.ingest(measured[:-1])
    with pytest.raises(ValueError):
        campaign.ingest(measured[:-1] + [measured[0]])
    campaign.ingest(measured)
    measured[0].value += 1
    with pytest.raises(Conflict):
        campaign.ingest(measured)


def test_oracle_never_impersonates_wetlab(fixture_data):
    task, labels = fixture_data
    task.feedback_source = "wetlab"
    with pytest.raises(ValueError):
        CSVOracle(labels, task)


def test_unmeasured_variant_does_not_get_fake_label(campaign, oracle):
    batch = campaign.prepare()
    oracle._labels.pop(batch.samples[0].candidate.sequence)
    with pytest.raises(UnknownMeasurement):
        oracle.measure(batch)


def test_export_has_identity_and_empty_measurements(campaign):
    batch = campaign.prepare()
    path = campaign.store.root / "batches" / batch.batch_id / "results.template.csv"
    assert "sequence_sha256" in path.read_text()
    with pytest.raises(ValueError):
        read_results(path)  # A blank template is not valid feedback.


def test_hidden_labels_do_not_change_initial_decisions(tmp_path, fixture_data):
    task, labels = fixture_data
    c1 = Campaign.initialize(str(tmp_path / "a"), task, meta=MetaPolicy(enabled=False))
    c2 = Campaign.initialize(str(tmp_path / "b"), task, meta=MetaPolicy(enabled=False))
    first = c1.prepare()
    import csv
    with labels.open() as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["value"] = float(row["value"]) + 100000
    with labels.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sequence", "value", "qc"])
        writer.writeheader()
        writer.writerows(rows)
    second = c2.prepare()
    assert [s.candidate.sequence for s in first.samples] == [s.candidate.sequence for s in second.samples]
    context = c1.view().model_dump(mode="json")
    assert "labels" not in context and str(labels) not in json.dumps(context)


def test_revealed_feedback_changes_next_predictions(tmp_path, fixture_data):
    task, labels = fixture_data
    oracle = CSVOracle(labels, task)
    predictions = []
    for i in range(2):
        c = Campaign.initialize(str(tmp_path / str(i)), task, meta=MetaPolicy(enabled=False))
        b = c.prepare()
        c.approve(b.batch_id, operator="test")
        obs = oracle.measure(b)
        for o in obs:
            o.value += 10 * i
        c.ingest(obs)
        next_b = c.prepare()
        predictions.append([s.candidate.predicted_value for s in next_b.samples if s.arm != "control"])
    assert predictions[0] != predictions[1]


def test_workflow_patch_is_trial_not_immediate_promotion(campaign, oracle):
    finish_round(campaign, oracle)
    base = Workflow.model_validate(campaign.state["workflow"])
    patch = Patch(target="workflow", base_version=base.version, changes={"strategy": "pairwise"},
                  hypothesis="Test combination feedback", task_kind="variant_design")
    campaign.stage_patch(patch)
    assert campaign.state["workflow"] == base.model_dump()
    batch = campaign.prepare()
    assert batch.patch_id == patch.patch_id
    arms = [s.arm for s in batch.samples]
    assert arms.count("baseline") == arms.count("challenger")
    campaign.approve(batch.batch_id, operator="test")
    obs = oracle.measure(batch)
    by_id = {s.sample_id: s.arm for s in batch.samples}
    for o in obs:
        o.value = 10.0 if by_id[o.sample_id] == "challenger" else 0.0
    campaign.ingest(obs)
    assert campaign.state["workflow"]["strategy"] == "pairwise"
    assert campaign.store.get("trial_results", batch.batch_id)["decision"] == "accepted"
    assert campaign.memory.retrieve(campaign.view().task.kind)[0]["transfer_validated"] is False


def test_inconclusive_patch_preserves_workflow_and_budget(campaign, oracle):
    finish_round(campaign, oracle)
    base = Workflow.model_validate(campaign.state["workflow"])
    campaign.stage_patch(Patch(target="workflow", base_version=base.version, changes={"strategy": "pairwise"},
                              hypothesis="Test a revised model", task_kind="variant_design"))
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    obs = oracle.measure(batch)
    for o in obs:
        o.value = 1.0
    before = campaign.store.usage()["experimental_wells"]["committed"]
    campaign.ingest(obs)
    assert campaign.state["workflow"] == base.model_dump()
    assert campaign.store.get("trial_results", batch.batch_id)["decision"] == "inconclusive"
    assert campaign.store.usage()["experimental_wells"]["committed"] == before


def test_no_mid_batch_patch(campaign):
    campaign.prepare()
    with pytest.raises(Conflict):
        campaign.stage_patch(Patch(target="workflow", base_version=Workflow().version,
            changes={"strategy": "pairwise"}, hypothesis="A valid hypothesis", task_kind="variant_design"))


def test_unregistered_tool_patch_rejected(campaign):
    with pytest.raises(ValueError):
        campaign.stage_patch(Patch(target="workflow", base_version=Workflow().version,
            changes={"tool_names": ["invented"]}, hypothesis="Try a nonexistent tool", task_kind="variant_design"))


def test_budget_and_round_limit_are_real(campaign, oracle):
    while (batch := campaign.prepare()) is not None:
        campaign.approve(batch.batch_id, operator="test")
        campaign.ingest(oracle.measure(batch))
    assert campaign.state["status"] == "complete"
    assert campaign.state["round_index"] <= campaign.view().task.max_rounds
    assert campaign.store.remaining("experimental_wells") >= 0


def test_only_unsubmitted_batches_can_be_cancelled(campaign):
    batch = campaign.prepare()
    reserved = campaign.store.remaining("experimental_wells")
    campaign.cancel_prepared(batch.batch_id, operator="test")
    assert campaign.store.remaining("experimental_wells") == reserved + len(batch.samples)
    replacement = campaign.prepare()
    assert replacement.batch_id != batch.batch_id
    campaign.approve(replacement.batch_id, operator="test")
    with pytest.raises(Conflict):
        campaign.cancel_prepared(replacement.batch_id, operator="test")


def test_wetlab_csv_roundtrip_requires_explicit_results(tmp_path, fixture_data):
    import csv
    from proteinrsi.lab import read_results
    task, _ = fixture_data
    task.feedback_source = "wetlab"
    task.assay_protocol = "test-only-protocol"
    c = Campaign.initialize(str(tmp_path / "wet"), task, meta=MetaPolicy(enabled=False))
    batch = c.prepare()
    path = c.store.root / "batches" / batch.batch_id / "results.template.csv"
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    for i, row in enumerate(rows):
        row["qc"], row["value"] = "valid", str(1.0 + i / 10)
    filled = path.parent / "results.csv"
    with filled.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    c.approve(batch.batch_id, operator="test-only")
    c.ingest(read_results(filled))
    assert c.report()["evidence_source"] == "wetlab"
    assert c.state["round_index"] == 1
    assert c.prepare().evidence_version != batch.evidence_version
