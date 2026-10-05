"""Whole-plate accounting and repair tests with artificial sequences and model responses."""
import csv
import json
from collections import Counter
from itertools import product

import httpx
import pytest

from proteinrsi.agents import Analysis, Team
from proteinrsi.contracts import BudgetSpec, Candidate, GatePolicy, MetaPolicy, Observation, Patch, TaskSpec, Workflow
from proteinrsi.improvement import evaluate_trial
from proteinrsi.llm import JSONLLM
from proteinrsi.ranking import request_ranking
from proteinrsi.replay.controller import run_replay
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.trial_allocation import allocate_trial


def sequences():
    return [''.join(x) + 'V' for x in product('ACDEFGHIKLMNPQRSTVWY', repeat=3) if ''.join(x) != 'AAA']


def plate_campaign(tmp_path, size=384, rounds=2, research=False):
    task = TaskSpec(name='Artificial full-plate task', candidate_access='open', reference_sequence='AAAV',
        mutable_positions=[1, 2, 3], max_mutations=3, batch_size=size, max_rounds=rounds,
        batch_fill_policy='full_plate', controls_per_batch=0, feedback_source='measured_replay',
        initial_observation_policy='provided_parent', initial_parent_measurement={'value': 1, 'source_ref': 'fixture'},
        budget=BudgetSpec(experimental_wells=size * rounds))
    return Campaign.initialize(str(tmp_path/'plate'), task, workflow=Workflow(analysis_tool_rounds=0),
        meta=MetaPolicy(enabled=False), gate=GatePolicy(min_per_arm=2, bootstrap_samples=200),
        research_config=ResearchConfig(review_after_step=False) if research else None)


@pytest.mark.parametrize('guarded', [False, True])
def test_two_complete_384_plates_from_multiple_design_panels(tmp_path, guarded):
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()['available']:
            pytest.skip('Sandbox unavailable')
    c = plate_campaign(tmp_path, research=True)
    path = tmp_path/'private.csv'
    with path.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['sequence', 'value', 'qc'])
        writer.writerow(['AAAV', 1, 'valid'])
        for i, seq in enumerate(sequences()[:800]):
            writer.writerow([seq, i + 2, 'valid'])
    panels = []
    def respond(request):
        payload = json.loads(request.content)
        ctx = json.loads(payload['messages'][1]['content'])
        instructions = payload['messages'][0]['content']
        view = ctx['view']
        assert view['task']['candidates'] == []
        assert 'proposal_pool_size' not in view['task']
        assert view['capacity']['required_plate_wells'] == 384
        if '# A — research plan' in instructions:
            result = {'hypothesis': 'Complete the plate with generated designs', 'steps': [
                {'step_id': op, 'operation': op, 'question': op, 'expected_output': op}
                for op in ('design', 'rank', 'finalize')]}
        elif '# B — protein design' in instructions:
            fill = view['research_context']['plate_completion']
            excluded = set(fill['exclude_sequences'])
            selected = [s for s in sequences() if s not in excluded][:128]
            panels.append((view['round_index'], fill['required_new_candidates']))
            result = {'candidates': [{'sequence': s} for s in selected]}
        elif 'ranked_candidates' in ctx or 'candidates' in ctx:
            items = ctx.get('ranked_candidates', ctx.get('candidates'))
            result = {'ranking': [x['candidate_id'] for x in items], 'summary': 'Stable IDs'}
        elif '# C — experimental feedback' in instructions:
            result = {'summary': 'One complete plate measured; design the next full plate'}
        else:
            raise AssertionError(instructions[:100])
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    llm = JSONLLM(c.store, model='test', base_url='https://example.invalid', api_key='fake',
                  transport=httpx.MockTransport(respond))
    c.team = Team(c.store, llm)
    report = run_replay(c, path, guarded=guarded)
    assert panels == [(0, 384), (0, 256), (0, 128), (1, 384), (1, 256), (1, 128)]
    assert report['completed_rounds'] == 2
    assert report['budget']['experimental_wells']['committed'] == 768
    assert all(x['plate_utilization'] == 1 for x in report['round_progress'])
    assert len({o['sequence'] for o in c.state['observations']}) == 769
    assert run_replay(c, path, guarded=guarded)['budget']['experimental_wells']['committed'] == 768


def test_incomplete_plate_stays_in_same_round_and_never_reserves_experiments(tmp_path):
    c = plate_campaign(tmp_path, size=8)
    calls = []
    def run(view):
        calls.append(view.research_context['plate_completion']['required_new_candidates'])
        return [Candidate(sequence=sequences()[0])]
    c.team.run = run
    with pytest.raises(ValueError, match='Full plate incomplete'):
        c.prepare()
    assert calls == [8, 7, 7, 7, 7, 7]
    assert c.state['round_index'] == 0 and c.state['pending_batch'] is None
    assert c.store.usage()['experimental_wells'] == {'limit': 16, 'committed': 0, 'reserved': 0}
    with pytest.raises(ValueError, match='Full plate incomplete'):
        c.prepare()
    assert len(calls) == 6  # Restart cannot replenish a failed completion's compute attempts.


def test_small_validation_panel_shares_a_full_384_plate_without_statistical_contamination(tmp_path):
    c = plate_campaign(tmp_path)
    view = c.view()
    patch = Patch(target='workflow', base_version=view.workflow.version, changes={'strategy': 'pairwise'},
                  task_kind=view.task.kind, hypothesis='Artificial method comparison')
    c.stage_patch(patch)
    c.team.run = lambda v: [Candidate(sequence=s) for s in (
        sequences()[384:402] if v.workflow.strategy == 'pairwise' else sequences()[:384])]
    batch = c.prepare()
    assert Counter(s.arm for s in batch.samples) == {'baseline': 18, 'challenger': 18, 'research': 348}
    assert len({s.candidate.sequence for s in batch.samples}) == 384
    assert c.store.get('trials', batch.batch_id)['allocation']['per_arm'] == 18
    assert c.prepare() == batch
    c.approve(batch.batch_id, operator='test')
    rows = [Observation(sample_id=s.sample_id, batch_id=batch.batch_id, sequence=s.candidate.sequence,
        metric=view.task.metric, unit=view.task.unit, assay_protocol=view.task.assay_protocol,
        source='measured_replay', value=10 if s.arm == 'baseline' else (0 if s.arm == 'challenger' else 10000))
        for s in batch.samples]
    result = evaluate_trial(batch, rows, GatePolicy(min_per_arm=2, bootstrap_samples=200), direction='maximize')
    assert result.decision == 'rejected' and result.n_baseline == result.n_challenger == 18
    c.ingest(rows)
    c.ingest(rows)
    assert c.store.usage()['experimental_wells']['committed'] == 384
    assert c.view().workflow.version == view.workflow.version


def test_identical_validation_is_recorded_and_not_repeated_as_pending_patch(tmp_path):
    c = plate_campaign(tmp_path, size=8)
    patch = Patch(target='workflow', base_version=c.view().workflow.version, changes={'strategy': 'pairwise'},
                  task_kind=c.view().task.kind, hypothesis='Artificial identical proposals')
    c.stage_patch(patch)
    c.team.run = lambda view: [Candidate(sequence=s) for s in sequences()[:8]]
    batch = c.prepare()
    assert len(batch.samples) == 8 and batch.patch_id is None
    assert c.state['pending_patch'] is None
    outcome = c.store.get('workflow_validation_outcomes', patch.patch_id)
    assert outcome['decision'] == 'inconclusive' and 'Identical' in outcome['reason']


def test_disjoint_allocation_does_not_starve_an_arm_that_only_has_shared_candidates():
    candidates = [Candidate(sequence=s) for s in sequences()[:8]]
    chosen, info = allocate_trial({'baseline': candidates[:4], 'challenger': candidates},
        {'baseline': 'old', 'challenger': 'new'}, 384, 4, [], 0)
    assert info['per_arm'] == 4
    assert len({c.sequence for c, _, _ in chosen}) == 8
    assert Counter(a for _, a, _ in chosen) == {'baseline': 4, 'challenger': 4}


def test_capacity_horizon_and_hidden_legacy_preview_field(tmp_path):
    c = plate_campaign(tmp_path)
    view = c.view()
    assert 'proposal_pool_size' not in view.model_dump()['task']
    assert view.capacity['remaining_rounds'] == 2
    assert view.capacity['usable_queries_before_round_limit'] == 768
    view.round_index = 1
    assert view.capacity['usable_queries_before_round_limit'] == 384
    assert view.capacity['unusable_query_budget'] == 384
    with pytest.raises(ValueError, match='whole plates'):
        TaskSpec.model_validate({**view.task.model_dump(), 'budget': {'experimental_wells': 767}})


def test_id_ranking_repairs_a_mistyped_sequence_without_changing_candidates(tmp_path):
    c = plate_campaign(tmp_path)
    candidates = [Candidate(sequence=s) for s in sequences()[:3]]
    calls = []
    def handler(request):
        ctx = json.loads(json.loads(request.content)['messages'][1]['content'])
        ids = [x['candidate_id'] for x in ctx['candidates']]
        calls.append(ctx)
        ranking = [*ids[:2], 'a-mistyped-sequence'] if len(calls) == 1 else list(reversed(ids))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'ranking': ranking, 'summary': 'test'})}}]})
    llm = JSONLLM(c.store, model='test', base_url='https://example.invalid', api_key='fake',
                  transport=httpx.MockTransport(handler))
    result = request_ranking(llm, c.store, 'C', 'test', {}, 'candidates', candidates, Analysis)
    assert result.ranking == [x.sequence for x in reversed(candidates)]
    assert len(calls) == 2 and 'ranking_repair' in calls[1]
    assert c.store.usage()['llm_calls']['committed'] == 2
    assert c.store.usage()['experimental_wells']['committed'] == 0


def test_invalid_ranking_repairs_are_bounded_and_costed(tmp_path):
    c = plate_campaign(tmp_path)
    llm = JSONLLM(c.store, model='test', base_url='https://example.invalid', api_key='fake',
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {
            'content': json.dumps({'ranking': ['unknown'], 'summary': 'test'})}}]})))
    with pytest.raises(ValueError, match='repair exhausted'):
        request_ranking(llm, c.store, 'A-selection', 'test', {}, 'ranked_candidates',
                        [Candidate(sequence=s) for s in sequences()[:3]], Analysis)
    assert c.store.usage()['llm_calls']['committed'] == 3


def test_meta_small_comparison_fills_plate_and_charges_all_wells(tmp_path, monkeypatch):
    c = plate_campaign(tmp_path, size=24)
    state = c.state
    state['meta'] = MetaPolicy(min_observations=100).model_dump()
    state['observations'].append(Observation(sample_id='known', batch_id='known',
        sequence=sequences()[0], value=1, metric=c.view().task.metric, unit=c.view().task.unit,
        source='measured_replay', assay_protocol=c.view().task.assay_protocol).model_dump(mode='json'))
    c.store.put('campaign', 'state', state)
    base = c.view().meta
    c.stage_patch(Patch(target='meta', base_version=base.version, changes={'min_observations': 2},
        task_kind=c.view().task.kind, hypothesis='Artificial Meta comparison'))

    class SmallBranch:
        llm = None
        def __init__(self, store):
            self.store = store
        def run(self, view):
            start = 10 if view.workflow.strategy == 'pairwise' else 1
            assert view.research_context['validation_request']['maximum_candidates'] == 12
            return [Candidate(sequence=s) for s in sequences()[start:start + 3]]

    monkeypatch.setattr('proteinrsi.online_meta.make_validation_team', lambda campaign, store: SmallBranch(store))
    c.team.run = lambda view: [Candidate(sequence=s) for s in sequences()[30:60]]
    batch = c.prepare()
    assert Counter(s.arm for s in batch.samples) == {'baseline': 3, 'challenger': 3, 'research': 18}
    c.approve(batch.batch_id, operator='test')
    view = c.view()
    c.ingest([Observation(sample_id=s.sample_id, batch_id=batch.batch_id, sequence=s.candidate.sequence,
        metric=view.task.metric, unit=view.task.unit, assay_protocol=view.task.assay_protocol,
        source='measured_replay', value=10 if s.arm == 'baseline' else (0 if s.arm == 'challenger' else 10000))
        for s in batch.samples])
    result = next(iter(c.store.all('meta_evaluations').values()))
    assert result['result']['decision'] == 'rejected'
    assert result['charged_queries'] == 6 and result['total_plate_queries'] == 24
    assert c.store.usage()['experimental_wells']['committed'] == 24
    assert c.view().meta.version == base.version
