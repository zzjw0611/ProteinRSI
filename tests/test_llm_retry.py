import json

import httpx
import pytest

from proteinrsi.llm import JSONLLM, LLMError
from proteinrsi.storage import BudgetExceeded, Store
from proteinrsi.trajectory import read_trace


ARGS = ('A', 'test', {}, {})


def client(tmp_path, monkeypatch, handler, limit=20, **kwargs):
    store = Store(tmp_path / 'retry')
    store.configure_budget({'llm_calls': limit, 'lab_wells': 24})
    monkeypatch.setattr('proteinrsi.llm.time.sleep', lambda _: None)
    return JSONLLM(store, model='test', base_url='https://example.invalid/v1',
                   api_key='TEST_SECRET', transport=httpx.MockTransport(handler), **kwargs)


def success():
    return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": true}'}}],
                                   'usage': {'total_tokens': 12}})


def test_gateway_retry_preserves_attempts_cost_and_trace(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.content)
        return (httpx.Response(502, text='upstream TEST_SECRET failed',
                              headers={'x-request-id': 'req-502'}) if len(calls) == 1 else success())
    llm = client(tmp_path, monkeypatch, handler)
    assert llm.complete(*ARGS) == {'ok': True}
    assert llm.complete(*ARGS) == {'ok': True}
    assert len(calls) == 2 and calls[0] == calls[1]
    assert llm.store.usage()['llm_calls']['committed'] == 2
    assert llm.store.usage()['lab_wells']['committed'] == 0
    attempts = list(llm.store.all('llm_attempts').values())
    assert [a['state'] for a in attempts] == ['failed', 'done']
    assert attempts[0]['response_headers']['x-request-id'] == 'req-502'
    trace = read_trace(llm.store.root)
    assert 'TEST_SECRET' not in json.dumps(trace)
    failure = next(e for e in trace['events'] if e['kind'] == 'llm_failed')
    assert failure['details']['llm_attempts']['http_status'] == 502


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
def test_permanent_errors_do_not_retry(tmp_path, monkeypatch, status):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(status))
    for _ in range(2):
        with pytest.raises(LLMError):
            llm.complete(*ARGS)
    assert llm.store.usage()['llm_calls']['committed'] == 1


def test_retry_limit_survives_restart(tmp_path, monkeypatch):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(503), max_attempts=3)
    with pytest.raises(LLMError, match='retry limit'):
        llm.complete(*ARGS)
    resumed = client(tmp_path, monkeypatch, lambda _: pytest.fail('must not send'), max_attempts=3)
    with pytest.raises(LLMError, match='retry limit'):
        resumed.complete(*ARGS)
    assert resumed.store.usage()['llm_calls']['committed'] == 3


def test_legacy_502_resumes_without_erasing_old_failure(tmp_path, monkeypatch):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(502), max_attempts=1)
    with pytest.raises(LLMError):
        llm.complete(*ARGS)
    key, old = next(iter(llm.store.all('llm').items()))
    old.pop('attempt')
    old.pop('request_key')
    with llm.store.connect() as con:
        con.execute("DELETE FROM kv WHERE namespace='llm_attempts'")
    llm.store.put('llm', key, old)
    resumed = client(tmp_path, monkeypatch, lambda _: success())
    assert resumed.complete(*ARGS) == {'ok': True}
    assert resumed.store.get('llm_attempts', key + '/attempt-1') == old
    assert resumed.store.usage()['llm_calls']['committed'] == 2


def test_budget_stops_retry_before_http(tmp_path, monkeypatch):
    llm = client(tmp_path, monkeypatch, lambda _: httpx.Response(502), limit=1)
    with pytest.raises(BudgetExceeded):
        llm.complete(*ARGS)
    assert llm.store.usage()['llm_calls']['committed'] == 1
    assert len(llm.store.all('llm_attempts')) == 1


def test_read_timeout_retry_and_uncertain_started_record(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout('secret exception text', request=request)
        return success()
    llm = client(tmp_path, monkeypatch, handler)
    assert llm.complete(*ARGS) == {'ok': True}
    key, record = next(iter(llm.store.all('llm').items()))
    record['state'] = 'started'
    llm.store.put('llm', key, record)
    with pytest.raises(LLMError, match='uncertain'):
        llm.complete(*ARGS)
    assert len(calls) == 2


def test_retry_after_and_backoff(monkeypatch):
    monkeypatch.setattr('proteinrsi.llm.random.uniform', lambda a, b: 0)
    assert JSONLLM._retry_delay({}, 1) == 2
    assert JSONLLM._retry_delay({}, 2) == 4
    assert JSONLLM._retry_delay({'response_headers': {'retry-after': '15'}}, 1) == 15
    assert JSONLLM._retry_delay({'response_headers': {'retry-after': '999'}}, 1) == 60
    assert JSONLLM._retry_delay({'response_headers': {'retry-after': 'bad'}}, 1) == 2


def test_sponsored_retry_charges_and_audit_reach_parent(tmp_path, monkeypatch):
    from proteinrsi.storage import SponsoredStore
    sponsor = Store(tmp_path / 'sponsor')
    sponsor.configure_budget({'llm_calls': 10})
    branch = SponsoredStore(tmp_path / 'branch', sponsor, 'trial')
    branch.configure_budget({'llm_calls': 5})
    monkeypatch.setattr('proteinrsi.llm.time.sleep', lambda _: None)
    responses = iter([httpx.Response(502), success()])
    llm = JSONLLM(branch, model='test', base_url='https://example.invalid', api_key='test',
                  transport=httpx.MockTransport(lambda _: next(responses)))
    assert llm.complete(*ARGS) == {'ok': True}
    assert sponsor.usage()['llm_calls']['committed'] == 2
    assert branch.usage()['llm_calls']['committed'] == 2
    assert len(sponsor.all('validation_llm_attempts')) == 2


def test_token_report_does_not_double_count_attempts(campaign, monkeypatch):
    from proteinrsi.reporting import study_details
    monkeypatch.setattr('proteinrsi.llm.time.sleep', lambda _: None)
    responses = iter([httpx.Response(502), success()])
    llm = JSONLLM(campaign.store, model='test', base_url='https://example.invalid', api_key='test',
                  transport=httpx.MockTransport(lambda _: next(responses)))
    llm.complete(*ARGS)
    details = study_details(campaign)
    assert details['provider_reported_tokens']['total_tokens'] == 12


def test_output_allowance_can_be_configured_for_large_batches(tmp_path, monkeypatch):
    from proteinrsi.storage import Store
    monkeypatch.setenv('PROTEINRSI_MODEL', 'test')
    monkeypatch.setenv('PROTEINRSI_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('PROTEINRSI_API_KEY', 'fake')
    monkeypatch.setenv('PROTEINRSI_API_PROTOCOL', 'responses')
    monkeypatch.setenv('PROTEINRSI_LLM_MAX_OUTPUT_TOKENS', '32768')
    store = Store(tmp_path/'large-batch')
    store.configure_budget({'llm_calls': 2})
    client = JSONLLM.from_env(store)
    def handler(request):
        assert json.loads(request.content)['max_output_tokens'] == 32768
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': '{"ok": true}'}]}]})
    client.transport = httpx.MockTransport(handler)
    assert client.complete(*ARGS) == {'ok': True}
    monkeypatch.setenv('PROTEINRSI_LLM_MAX_OUTPUT_TOKENS', '0')
    with pytest.raises(ValueError, match='max_tokens'):
        JSONLLM.from_env(store)
