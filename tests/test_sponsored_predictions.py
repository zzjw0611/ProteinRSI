# SPDX-License-Identifier: MIT
"""Synthetic online-M descendants retain their actual frozen prediction evidence."""
import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import Candidate, GatePolicy, MetaPolicy, Observation, Patch, digest
from proteinrsi.research.analysis import prediction_errors
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.research.prediction import PREDICT_TOOL, attach_predictions
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Conflict, SponsoredStore, Store
from proteinrsi.tools import ToolCall


class FittedDescendants(Team):
    def run(self, view):
        self.bind_tools(view)
        observed = {o.sequence for o in view.observations} | {view.task.reference_sequence}
        sequences = [s for s in view.task.candidates if s not in observed]
        if view.workflow.strategy == 'pairwise':
            sequences.reverse()
        sequences = sequences[:8]
        result = self.tools.call(ToolCall(name=PREDICT_TOOL,
            arguments={'sequences': sequences, 'features': 'mutation'}), view.task,
            allowed=[PREDICT_TOOL], context_key='descendant:' + view.workflow.version)
        candidates = {s: Candidate(sequence=s) for s in sequences}
        attach_predictions(candidates, {s: result['artifact_ref'] for s in sequences},
                           view, [result], self.store)
        return list(candidates.values())


def test_online_meta_actual_fits_remain_available_to_sponsor_feedback(campaign, tmp_path, monkeypatch):
    original = campaign.view()
    workflow = original.workflow.model_copy(update={'tool_names': [PREDICT_TOOL]})
    campaign = Campaign.initialize(str(tmp_path / 'sponsored'), original.task, workflow=workflow,
        meta=MetaPolicy(min_observations=100), gate=GatePolicy.model_validate(campaign.state['gate']),
        research_config=ResearchConfig(protocol_mode='typed'))
    view = campaign.view()
    state = campaign.state
    state['observations'] = [Observation(sample_id=f'revealed-{i}', batch_id='revealed', sequence=s,
        value=float(i), metric=view.task.metric, unit=view.task.unit, source='synthetic',
        assay_protocol=view.task.assay_protocol).model_dump(mode='json')
        for i, s in enumerate(view.task.candidates[:2])]
    campaign.store.put('campaign', 'state', state)
    campaign.stage_patch(Patch(target='meta', base_version=view.meta.version,
        changes={'min_observations': 2}, task_kind=view.task.kind,
        hypothesis='Synthetic test of sponsored prediction provenance', author_backend='synthetic-test'))
    monkeypatch.setattr('proteinrsi.online_meta.make_validation_team',
                        lambda campaign, store: FittedDescendants(store, register_tools=False))
    batch = campaign.prepare()
    assert batch.patch_id is not None
    research = [s for s in batch.samples if s.arm != 'control']
    assert {s.arm for s in research} == {'baseline', 'challenger'}
    assert all(s.candidate.prediction_ref is not None for s in research)
    # Both arms ran a real fitter, charged only through their study sponsor.
    assert campaign.store.usage()['tool_calls']['committed'] == 2
    assert campaign.prepare() == batch
    campaign.approve(batch.batch_id, operator='synthetic-test')
    feedback = [Observation(sample_id=s.sample_id, batch_id=batch.batch_id, sequence=s.candidate.sequence,
        value=(s.candidate.predicted_value or 0) + 2, metric=view.task.metric, unit=view.task.unit,
        source='synthetic', assay_protocol=view.task.assay_protocol) for s in batch.samples]
    campaign.ingest(feedback)
    campaign.ingest(feedback)
    report = prediction_errors(campaign.view(), campaign.store)
    assert report['n'] == len(research)
    assert report['mae'] == pytest.approx(2)
    assert campaign.state['history'][-1]['research_analysis']['prediction_errors']['n'] == len(research)
    assert len(campaign.store.all('task_predictions')) == 2
    attempt_id, attempt = next(iter(campaign.store.all('meta_online_attempts').items()))
    assert attempt['state'] == 'completed'
    for sample in research:
        key = sample.candidate.prediction_ref.split('/')[1]
        branch = Store(campaign.store.root / 'meta-validation' / attempt_id / sample.arm)
        assert campaign.store.get('task_predictions', key) == branch.get('task_predictions', key)
    assert campaign.store.usage()['tool_calls']['committed'] == 2
    assert campaign.store.usage()['experimental_wells']['committed'] == len(batch.samples)


def test_sponsored_prediction_copy_is_content_addressed_immutable_and_idempotent(tmp_path):
    sponsor = Store(tmp_path / 'sponsor')
    branch = SponsoredStore(tmp_path / 'branch', sponsor, 'test-arm')
    artifact = {'scope': 'fixture', 'predictions': [], 'evidence_kind': 'proxy'}
    key = digest(artifact)
    branch.put('task_predictions', key, artifact, immutable=True)
    branch.put('task_predictions', key, artifact, immutable=True)
    resumed = SponsoredStore(tmp_path / 'branch', Store(sponsor.root), 'test-arm')
    resumed.put('task_predictions', key, artifact, immutable=True)
    assert sponsor.all('task_predictions') == {key: artifact}
    assert branch.all('task_predictions') == {key: artifact}
    with pytest.raises(ValueError, match='content-addressed'):
        branch.put('task_predictions', key, {**artifact, 'scope': 'other'}, immutable=False)
    assert sponsor.get('task_predictions', key) == artifact
    assert branch.get('task_predictions', key) == artifact
    other = {**artifact, 'scope': 'another'}
    other_key = digest(other)
    sponsor.put('task_predictions', other_key, {'corrupt': True})
    with pytest.raises(Conflict):
        branch.put('task_predictions', other_key, other, immutable=False)
    assert sponsor.get('task_predictions', other_key) == {'corrupt': True}
    assert branch.get('task_predictions', other_key) is None
