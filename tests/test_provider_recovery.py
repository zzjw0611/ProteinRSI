import csv
import json

import httpx
import pytest

from proteinrsi.agents import Team
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.replay.broker import WorkerExecutionError
from proteinrsi.replay.controller import run_replay
from test_full_plate import plate_campaign


@pytest.mark.parametrize('guarded', [False, True])
def test_paused_plate_resumes_same_request_without_extra_wells(tmp_path, guarded, monkeypatch):
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()['available']:
            pytest.skip('Sandbox unavailable')
    monkeypatch.setattr('proteinrsi.llm.time.sleep', lambda _: None)
    campaign = plate_campaign(tmp_path, size=4, rounds=2, research=True, protocol='typed')
    dataset = tmp_path/'private.csv'
    with dataset.open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(['sequence', 'value', 'qc'])
        writer.writerow(['AAAV', 1, 'valid'])
        for i, aa in enumerate('CDEFGHIK'):
            writer.writerow([aa+'AAV', i+2, 'valid'])
    b_requests, a_requests = [], []
    def respond(request):
        payload = json.loads(request.content)
        context = json.loads(payload['messages'][1]['content'])
        instructions = payload['messages'][0]['content']
        if '# A — resource protocol planner' in instructions:
            a_requests.append(context)
            result = {'hypothesis': 'Synthetic recovery check', 'steps': [
                {'step_id': 'design', 'operation': 'agent:propose', 'question': 'design',
                 'arguments': {'question': 'Generate four variants'}}],
                'final_outputs': {'candidates': {'source': 'step:design.result',
                                                'schema_ref': 'protein.sequence_set/v1'}}}
        elif '# B — protein design' in instructions:
            b_requests.append(context)
            if len(b_requests) == 1:
                return httpx.Response(503)
            residues = 'CDEF' if context['view']['round_index'] == 0 else 'GHIK'
            result = {'edits': [[{'position': 1, 'from': 'A', 'to': aa}] for aa in residues]}
        elif '# C — experimental feedback' in instructions:
            result = {'summary': 'Synthetic measurements received'}
        else:
            raise AssertionError(instructions[:100])
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    llm = JSONLLM(campaign.store, model='test', base_url='https://example.invalid', api_key='fake',
        max_attempts=1, transport=httpx.MockTransport(respond))
    campaign.team = Team(campaign.store, llm)
    with pytest.raises((ProviderPaused, WorkerExecutionError)):
        run_replay(campaign, dataset, guarded=guarded)
    assert campaign.state['round_index'] == 0
    assert campaign.store.usage()['experimental_wells']['committed'] == 0
    plate_before = next(iter(campaign.store.all('plate_plans').values()))
    assert plate_before['state'] == 'paused_provider' and plate_before['attempts'] == 1
    key = next(key for key, r in campaign.store.all('llm').items() if r['role'] == 'B')
    authorize_retry(campaign.store, key, operator='test', reason='Synthetic provider recovery')
    result = run_replay(campaign, dataset, guarded=guarded)
    assert result['completed_rounds'] == 2
    assert result['budget']['experimental_wells']['committed'] == 8
    assert b_requests[0] == b_requests[1]
    assert len(a_requests) == 2  # One plan per round, not one per retry.
    assert all(p['attempts'] == 1 for p in campaign.store.all('plate_plans').values())
