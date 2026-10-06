"""Autonomous design with a private, sparse assay backend; no live API or real labels."""
import csv
import json
from itertools import product

import httpx
import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import BudgetSpec, Candidate, MetaPolicy, Observation, TaskSpec, Workflow
from proteinrsi.lab import CSVOracle
from proteinrsi.llm import JSONLLM
from proteinrsi.replay.broker import dispatch
from proteinrsi.replay.controller import run_replay
from proteinrsi.research.analysis import evidence_summary
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.tasks import validate_candidate


def open_campaign(tmp_path, *, research=False):
    task = TaskSpec(name='Open design fixture', reference_sequence='ACDE', mutable_positions=[2],
        max_mutations=1, candidate_access='open', candidates=[], feedback_source='measured_replay',
        controls_per_batch=0, batch_size=2, max_rounds=2,
        budget=BudgetSpec(experimental_wells=4), initial_observation_policy='provided_parent',
        initial_parent_measurement={'value': 1, 'source_ref': 'artificial fixture'})
    return Campaign.initialize(str(tmp_path/'run'), task, meta=MetaPolicy(enabled=False),
        workflow=Workflow(analysis_tool_rounds=0),
        research_config=ResearchConfig(review_after_step=False) if research else None)


def write_labels(tmp_path):
    path = tmp_path/'private.csv'
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['sequence', 'value', 'qc'])
        w.writerows([['ACDE', 1, 'valid'], ['AVDE', 2, 'valid'], ['ALDE', 3, 'valid'],
                     ['AFDE', 0.5, 'valid'], ['AWDE', 876543.21, 'valid']])
    return path


@pytest.mark.parametrize('guarded', [False, True])
def test_open_design_two_rounds_receive_only_queried_feedback(tmp_path, guarded):
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()['available']:
            pytest.skip('Sandbox unavailable')
    campaign = open_campaign(tmp_path, research=True)
    dataset = write_labels(tmp_path)
    designs = []
    def respond(request):
        raw = request.content.decode()
        assert '876543' not in raw and 'AWDE' not in raw and str(dataset) not in raw
        payload = json.loads(raw)
        context = json.loads(payload['messages'][1]['content'])
        instructions = payload['messages'][0]['content']
        view = context.get('view', {})
        if view:
            assert view['task']['candidate_access'] == 'open'
            assert view['task']['candidates'] == []
        if '# A — research plan' in instructions:
            result = {'hypothesis': 'Design mutations; adapt to actual feedback', 'steps': [
                {'step_id': op, 'operation': op, 'question': op, 'expected_output': op}
                for op in ['design', 'rank', 'finalize']]}
        elif '# B — protein design' in instructions:
            round_index = view['round_index']
            if round_index == 0:
                assert len(view['observations']) == 1
                residues = ['V', 'G']  # G is legal but has no historical measurement.
            else:
                table = view['observations']
                if isinstance(table, dict):
                    rows = [{**table['shared_fields'], **dict(zip(table['columns'], row))}
                            for row in table['rows']]
                    for row in rows:
                        if table.get('sequence_encoding'):
                            sequence = list(view['task']['reference_sequence'])
                            for position, residue in zip(
                                    table['sequence_encoding']['positions_1based'], row['sequence']):
                                sequence[position - 1] = residue
                            row['sequence'] = ''.join(sequence)
                else:
                    rows = table
                observations = {o['sequence']: o for o in rows}
                assert observations['AVDE']['value'] == 2
                assert observations['AGDE']['value'] is None
                assert observations['AGDE']['qc'] == 'unavailable'
                residues = ['L', 'F']
            designs.append(round_index)
            result = {'edits': [[{'position': 2, 'from': 'C', 'to': aa}] for aa in residues]}
        elif 'ranked_candidates' in context or 'candidates' in context:
            candidates = context.get('ranked_candidates', context.get('candidates'))
            result = {'ranking': [c['sequence'] for c in candidates], 'summary': 'Rank generated variants'}
        elif '# C — experimental feedback' in instructions:
            result = {'summary': 'V improves the fixture score; G has no record, not low fitness'}
        else:
            raise AssertionError(instructions[:120])
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    llm = JSONLLM(campaign.store, model='test', base_url='https://example.invalid', api_key='fake',
                  transport=httpx.MockTransport(respond))
    campaign.team = Team(campaign.store, llm)
    report = run_replay(campaign, dataset, guarded=guarded)
    assert designs == [0, 1]
    assert report['completed_rounds'] == 2
    assert report['budget']['experimental_wells']['committed'] == 4
    assert report['valid_query_measurements'] == 3
    assert report['unavailable_queries'] == 1
    assert report['best_measured_value'] == 3
    assert campaign.state['history'][0]['qc_failure_fraction'] == 0
    assert campaign.state['history'][0]['unavailable_queries'] == 1
    assert all('library' not in x for x in campaign.view().workflow.tool_names)
    summary = evidence_summary(campaign.view())
    assert sum(b['n_unavailable'] for b in summary['batches']) == 1
    # Recovering completed work cannot charge queries again.
    assert run_replay(campaign, dataset, guarded=guarded)['budget']['experimental_wells']['committed'] == 4


def test_no_candidate_menu_or_membership_oracle_in_open_worker(tmp_path):
    campaign = open_campaign(tmp_path)
    team = campaign.team
    view = campaign.view()
    team.bind_tools(view)
    assert 'library_check' not in team.tools._tools and 'library_sample' not in team.tools._tools
    result = dispatch(team, team.tools, view, [], {'rpc': 'get', 'namespace': 'campaign', 'key': 'state'})
    assert result['task']['candidates'] == []
    for namespace in ['oracle', 'replay_index', 'configuration']:
        with pytest.raises(PermissionError):
            dispatch(team, team.tools, view, [], {'rpc': 'get', 'namespace': namespace, 'key': 'replay_dataset'})
    validate_candidate(view.task, Candidate(sequence='AGDE'))
    with pytest.raises(ValueError, match='Fixed residues'):
        validate_candidate(view.task, Candidate(sequence='AGDA'))


def test_empty_open_proposal_is_not_reported_as_search_exhaustion(tmp_path):
    c = open_campaign(tmp_path)
    c.team.run = lambda view: []
    with pytest.raises(ValueError, match='exhaustion has not been established'):
        c.prepare()
    assert c.state['status'] == 'ready'
    assert c.store.usage()['experimental_wells']['committed'] == 0


def test_unavailable_is_neither_zero_nor_wetlab_failure():
    row = dict(sample_id='s', sequence='AGDE', value=None, metric='fitness', unit='a.u.',
               qc='unavailable', source='measured_replay', batch_id='b', assay_protocol='test')
    assert Observation(**row).value is None
    with pytest.raises(ValueError):
        Observation(**{**row, 'value': 0})
    with pytest.raises(ValueError):
        Observation(**{**row, 'source': 'wetlab'})


def test_unavailable_feedback_is_idempotent_and_excludes_repeat(tmp_path):
    c = open_campaign(tmp_path)
    c.team.run = lambda view: [Candidate(sequence='AGDE')]
    batch = c.prepare()
    c.approve(batch.batch_id, operator='test')
    result = CSVOracle(write_labels(tmp_path), c.view().task).measure(batch)
    c.ingest(result)
    c.ingest(result)
    assert c.state['round_index'] == 1
    assert c.store.usage()['experimental_wells']['committed'] == 1
    with pytest.raises(ValueError, match='No new valid proposal'):
        c.prepare()


def test_ranking_keeps_all_user_inputs_instead_of_sampling_128(tmp_path):
    candidates = ['A'+''.join(x) for x in product('ACDEFGHIKLMNPQRSTVWY', repeat=2)][:160]
    task = TaskSpec(name='Explicit ranking', kind='variant_ranking', candidates=candidates,
        reference_sequence='', controls_per_batch=0)
    c = Campaign.initialize(str(tmp_path/'ranking'), task)
    assert c.view().task.candidates == candidates
    with pytest.raises(ValueError):
        TaskSpec.model_validate({**task.model_dump(), 'candidate_access': 'open'})


def test_old_auto_injected_catalogue_cannot_silently_resume(tmp_path):
    c = open_campaign(tmp_path)
    state = c.state
    state['task'].update(candidate_access='catalogue', candidates=['ACDE', 'AVDE'])
    c.store.put('campaign', 'state', state)
    c.store.put('configuration', 'goal_intent', {'task_kind': 'variant_design', 'candidates': []})
    with pytest.raises(ValueError, match='Legacy replay'):
        run_replay(c, write_labels(tmp_path), guarded=False)
    assert c.store.usage()['experimental_wells']['committed'] == 0


def test_missing_trial_record_cannot_promote_from_observed_subset(tmp_path):
    from proteinrsi.contracts import Batch, GatePolicy, Sample
    from proteinrsi.improvement import evaluate_trial
    c = open_campaign(tmp_path)
    samples, observations = [], []
    for i, arm in enumerate(['baseline'] * 4 + ['challenger'] * 4):
        seq = 'A' + 'VGLFYWKR'[i] + 'DE'
        samples.append(Sample(sample_id=str(i), candidate=Candidate(sequence=seq),
                              arm=arm, workflow_version='test'))
        observations.append(Observation(sample_id=str(i), sequence=seq, batch_id='b',
            metric='fitness', unit='a.u.', assay_protocol='test', source='measured_replay',
            qc='unavailable' if i == 7 else 'valid', value=None if i == 7 else (1 if i < 4 else 100)))
    batch = Batch(batch_id='b', campaign_id=c.state['campaign_id'], round_index=0,
                  evidence_version='test', meta_version='test', samples=samples)
    result = evaluate_trial(batch, observations, GatePolicy(min_per_arm=2), direction='maximize')
    assert result.decision == 'inconclusive' and 'coverage' in result.reason
