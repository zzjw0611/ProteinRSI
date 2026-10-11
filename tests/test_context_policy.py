"""Bounded evidence transport, not model quality or live landscape experiments."""
import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from proteinrsi.contracts import canonical
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.research.context import ContextBuilder, MAX_CONTEXT_BYTES, complete
from proteinrsi.storage import Store


def make(tmp_path, handler):
    store = Store(tmp_path)
    store.configure_budget({'llm_calls': 50, 'experimental_wells': 500})
    store.put('configuration', 'research', {'context_policy': 'evidence-v1'})
    return JSONLLM(store, model='fixture', base_url='https://example.invalid', api_key='secret',
                   transport=httpx.MockTransport(handler), max_attempts=1)


def response(value):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}}]})


def view(rounds):
    return {'view': {'round_index': rounds, 'task': {'goal': 'optimize experimentally',
        'mutable_positions': [39, 40, 41, 54], 'batch_size': 100},
        'remaining_queries': 100,
        'observations': [{'id': i, 'sequence': 'A'*56, 'fitness': i / 13,
                          'qc': 'valid' if i % 5 else 'missing'} for i in range(rounds*100)],
        'history': [{'round': i, 'analysis': 'history '*2000} for i in range(rounds)]}}


@pytest.mark.parametrize('rounds', [5, 20, 100])
def test_round_growth_bounded_exact_source(tmp_path, rounds):
    sent = []
    llm = make(tmp_path, lambda request: sent.append(request.content) or response({'ok': True}))
    context = view(rounds)
    assert llm.complete('A-plan', 'Plan', context, {}) == {'ok': True}
    frame = next(iter(llm.store.all('context_frames').values()))
    assert frame['context_bytes'] < MAX_CONTEXT_BYTES
    assert len(sent[0]) < 16000
    assert frame['context']['view']['task'] == context['view']['task']
    source = frame['context']['_context']['source']['evidence_ref']
    assert llm.store.get('context_evidence', source)['value'] == context
    assert llm.store.usage()['experimental_wells']['committed'] == 0


def test_paging_lossless_cross_scope_denied_and_notes_validated(tmp_path):
    store = Store(tmp_path)
    first = ContextBuilder(store, 'first')
    values = [{'id': i, 'data': '🧬'*800} for i in range(65)]
    ref = first.reference(values, '$.observations')['evidence_ref']
    other = ContextBuilder(store, 'second')
    with pytest.raises(ValueError, match='not available'):
        other.read({'ref': ref})
    with pytest.raises(ValueError, match='not available'):
        first.read({'ref': '../../measurements.csv'})
    collected, offset = [], 0
    while True:
        page = first.read({'ref': ref, 'offset': offset, 'limit': 20})
        for item in page['value']:
            data_ref = item['data']['evidence_ref']
            chunks, cursor = [], 0
            while True:
                text = first.read({'ref': data_ref, 'offset': cursor, 'limit': 2})
                chunks.append(text['value'])
                if text['next_offset'] is None:
                    break
                cursor = text['next_offset']
            collected.append({'id': item['id'], 'data': ''.join(chunks)})
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert collected == values
    with pytest.raises(ValueError, match='cite'):
        first.note({'kind': 'note', 'hypothesis': '', 'evidence_refs': ['hidden'],
                    'counterevidence': '', 'failures': '', 'open_questions': '', 'next_step': ''})


def test_read_pause_restart_reuses_paid_call_and_frozen_notes(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.content)
        frame = json.loads(json.loads(request.content)['messages'][1]['content'])
        if len(calls) == 1:
            ref = frame['view']['observations']['evidence_ref']
            return response({'_context_action': {'kind': 'read', 'ref': ref, 'offset': 40, 'limit': 2}})
        if len(calls) == 2:
            raise KeyboardInterrupt('simulate controller death before provider charge')
        assert frame['_context']['last_read']['value'][0]['id'] == 40
        return response({'ok': True})
    llm = make(tmp_path, handler)
    args = ('B', 'Design', view(5), {})
    # Interrupt before starting the second provider call (an actual uncertain
    # network completion must NOT be retried automatically).
    original = llm._complete
    def pause(role, instructions, context, schema):
        if context['_context']['actions_used'] == 1:
            raise ProviderPaused('local pause')
        return original(role, instructions, context, schema)
    llm._complete = pause
    with pytest.raises(ProviderPaused, match='local pause'):
        llm.complete(*args)
    llm.store.put('research_notes', 'future', {'scope': {'task': 'wrong', 'round': 99}})
    def resumed_handler(request):
        frame = json.loads(json.loads(request.content)['messages'][1]['content'])
        assert frame['_context']['last_read']['value'][0]['id'] == 40
        assert frame['_context']['recent_notes'] == []
        return response({'ok': True})
    resumed = make(tmp_path, resumed_handler)
    assert resumed.complete(*args) == {'ok': True}
    assert resumed.complete(*args) == {'ok': True}
    assert resumed.store.usage()['llm_calls']['committed'] == 2
    assert len(resumed.store.all('context_reads')) == 1


def test_schema_definitions_and_model_notes(tmp_path):
    schema = {'type': 'object', '$defs': {'flag': {'type': 'boolean'}},
              'properties': {'ok': {'$ref': '#/$defs/flag'}},
              'required': ['ok'], 'additionalProperties': False}
    llm = make(tmp_path, lambda _: response({'ok': True}))
    seen = []
    def fake(role, instructions, context, output_schema):
        Draft202012Validator.check_schema(output_schema)
        Draft202012Validator(output_schema).validate({'ok': True})
        seen.append(context)
        if len(seen) == 1:
            return {'_context_action': {'kind': 'note', 'hypothesis': 'test',
                'evidence_refs': [context['_context']['source']['evidence_ref']],
                'counterevidence': 'unknown', 'failures': 'none',
                'open_questions': 'unknown', 'next_step': 'measure'}}
        return {'ok': True}
    llm._complete = fake
    assert complete(llm, 'C-feedback', 'Analyze', view(5), schema) == {'ok': True}
    assert len(llm.store.all('research_notes')) == 2
    assert complete(llm, 'A-plan', 'Plan', view(6), schema) == {'ok': True}
    assert seen[-1]['_context']['recent_notes']
    assert 'model_claim_not_measurement' in canonical(llm.store.all('research_notes'))


def test_notebook_does_not_import_future_or_other_lineage(tmp_path):
    llm = make(tmp_path, lambda _: response({'summary': 'model claim'}))
    llm.complete('C-feedback', 'Analyze', view(6), {})
    llm.complete('A-plan', 'Plan older evidence', view(5), {})
    older = [x for x in llm.store.all('context_frames').values() if x['role'] == 'A-plan'][0]
    assert older['context']['_context']['recent_notes'] == []
    alternative = view(6)
    alternative['view']['observations'][0]['fitness'] = 999
    llm.complete('M', 'Consider alternative lineage', alternative, {})
    frame = [x for x in llm.store.all('context_frames').values() if x['role'] == 'M'][0]
    assert frame['context']['_context']['recent_notes'] == []


def test_sandbox_bulk_evidence_over_pipe_limit_without_oracle(tmp_path, monkeypatch):
    from proteinrsi.contracts import TaskSpec
    from proteinrsi.replay.sandbox import probe
    from proteinrsi.research.code import execute_code
    from proteinrsi.research.contracts import ResearchConfig
    from proteinrsi.runtime import Campaign
    if not probe()['available']:
        pytest.skip('Sandbox unavailable')
    task = TaskSpec(name='Bulk evidence test', reference_sequence='ACDE', mutable_positions=[2])
    campaign = Campaign.initialize(str(tmp_path/'run'), task,
        research_config=ResearchConfig(context_policy='evidence-v1'))
    full = campaign.view().model_copy(update={'research_context': {'large': 'x' * (5*1024**2)}})
    hidden = tmp_path/'oracle.csv'
    hidden.write_text('DO_NOT_REVEAL')
    monkeypatch.setenv('PROTEINRSI_API_KEY', 'secret')
    source = f'''import os
from pathlib import Path
try:
    Path({str(hidden)!r}).read_text()
    blocked = False
except PermissionError:
    blocked = True
result = {{'length': len(context['research_context']['large']),
          'blocked': blocked, 'key': os.environ.get('PROTEINRSI_API_KEY')}}
'''
    result = execute_code(campaign.store, full, {'code': source})
    assert result['status'] == 'ok'
    assert result['output'] == {'length': 5*1024**2, 'blocked': True, 'key': None}


def test_sponsored_context_trace_and_shared_cost(tmp_path):
    from proteinrsi.storage import SponsoredStore
    from proteinrsi.trajectory import read_trace
    sponsor = Store(tmp_path/'sponsor')
    sponsor.configure_budget({'llm_calls': 20, 'experimental_wells': 100})
    child = SponsoredStore(tmp_path/'child', sponsor=sponsor, prefix='trial')
    child.configure_budget({'llm_calls': 20, 'experimental_wells': 100})
    child.put('configuration', 'research', {'context_policy': 'evidence-v1'})
    llm = JSONLLM(child, model='test', base_url='https://example.invalid', api_key='secret',
                  transport=httpx.MockTransport(lambda _: response({'ok': True})))
    llm.complete('E-verdict', 'Evaluate', view(5), {})
    trace = read_trace(sponsor.root)
    assert trace['records']['validation_context_frames']
    assert sponsor.usage()['llm_calls']['committed'] == 1
    assert sponsor.usage()['experimental_wells']['committed'] == 0


def test_object_pages_stable_after_canonical_store_roundtrip(tmp_path):
    store = Store(tmp_path)
    original = {'z': 'z'*3000, 'a': 'a'*3000, 'middle': [1, 2]}
    first = ContextBuilder(store, 'same-session')
    descriptor = first.reference(original, '$.object')
    page = first.read({'ref': descriptor['evidence_ref'], 'offset': 0, 'limit': 2})
    restored = store.get('context_evidence', descriptor['evidence_ref'])['value']
    resumed = ContextBuilder(store, 'same-session')
    assert resumed.reference(restored, '$.object') == descriptor
    assert resumed.read({'ref': descriptor['evidence_ref'], 'offset': 0, 'limit': 2}) == page


def test_read_loop_has_hard_cap_and_no_free_retries(tmp_path):
    calls = []
    def handler(request):
        frame = json.loads(json.loads(request.content)['messages'][1]['content'])
        calls.append(frame)
        return response({'_context_action': {'kind': 'read',
            'ref': frame['_context']['source']['evidence_ref'], 'limit': 1}})
    llm = make(tmp_path, handler)
    args = ('A-plan', 'Read loop fixture', view(5), {})
    with pytest.raises(ProviderPaused, match='action limit'):
        llm.complete(*args)
    assert len(calls) == 13
    assert max(len(canonical(frame).encode()) for frame in calls) <= MAX_CONTEXT_BYTES
    with pytest.raises(ProviderPaused, match='action limit'):
        llm.complete(*args)
    assert len(calls) == 13
    assert llm.store.usage()['experimental_wells']['committed'] == 0


def test_named_catalogue_reads_and_bounded_working_set(tmp_path):
    class LLM:
        model, base_url, cache_settings = 'fixture', 'fixture', {}
        def __init__(self):
            self.store = Store(tmp_path)
            self.frames = []
        def _complete(self, role, instructions, context, schema):
            self.frames.append(context)
            index = len(self.frames)
            if index <= 3:
                ref = context['operations']['evidence_ref']
                return {'_context_action': {'kind': 'read', 'ref': ref, 'name': f'op{index}'}}
            assert context['_context']['last_read']['name'] == 'op3'
            assert [p['name'] for p in context['_context']['working_set']] == ['op1', 'op2']
            return {'ok': True}
    llm = LLM()
    context = {'operations': [{'name': f'op{i}', 'description': 'fixture ' * 70}
                              for i in range(30)]}
    assert complete(llm, 'A-plan', 'choose', context, {}) == {'ok': True}
    assert 'op2' in llm.frames[0]['operations']['names']
    assert all(len(canonical(f).encode()) < MAX_CONTEXT_BYTES for f in llm.frames)
    builder = ContextBuilder(llm.store, 'other')
    with pytest.raises(ValueError, match='not available'):
        builder.read({'ref': llm.frames[0]['operations']['evidence_ref'], 'name': 'op1'})


def test_large_read_survives_followup_until_explicit_release(tmp_path):
    class LLM:
        model, base_url, cache_settings = 'fixture', 'fixture', {}
        def __init__(self):
            self.store = Store(tmp_path)
            self.calls = 0
        def _complete(self, role, instructions, context, schema):
            self.calls += 1
            ref = context['rows']['evidence_ref']
            if self.calls == 1:
                return {'_context_action': {'kind': 'read', 'ref': ref, 'limit': 10}}
            if self.calls == 2:
                assert len(canonical(context['_context']['last_read']).encode()) > 8000
                return {'_context_action': {'kind': 'read', 'ref': ref, 'offset': 10, 'limit': 1}}
            if self.calls == 3:
                assert len(context['_context']['working_set'][0]['value']) == 10
                return {'_context_action': {'kind': 'release', 'ref': ref, 'offset': 0}}
            assert len(context['_context']['working_set']) == 1
            assert context['_context']['working_set'][0]['offset'] == 10
            assert context['_context']['last_read']['released_pages'] == 1
            return {'ok': True}
    assert complete(LLM(), 'A-plan', 'retain', {'rows': [{'text': 'x'*900} for _ in range(30)]}, {}) == {'ok': True}


def test_dictionary_named_reads_are_exact_scoped_and_lossless(tmp_path):
    builder = ContextBuilder(Store(tmp_path), 'named-dictionary')
    value = {'protein.sequence_set/v1': {'schema': {'type': 'object'}, 'sha256': 'fixture'},
             'large': 'x'*20000}
    ref = builder.reference(value, '$.schemas')
    assert ref['names'] == sorted(value)
    page = builder.read({'ref': ref['evidence_ref'], 'name': 'protein.sequence_set/v1'})
    assert page['value'] == value['protein.sequence_set/v1']
    with pytest.raises(ValueError, match='exact'):
        builder.read({'ref': ref['evidence_ref'], 'name': 'sequence_set'})
    other = ContextBuilder(builder.store, 'other')
    with pytest.raises(ValueError, match='not available'):
        other.read({'ref': ref['evidence_ref'], 'name': 'protein.sequence_set/v1'})
