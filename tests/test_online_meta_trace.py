"""Artificial mechanism checks: never scientific GB1 experiments."""
import json

import pytest

from proteinrsi.contracts import GatePolicy, MetaPolicy, Observation, Patch
from proteinrsi.trajectory import export_html, read_trace
from test_rsi import ScriptedOffspringTeam


def stage(campaign):
    from proteinrsi.runtime import Campaign
    campaign = Campaign.initialize(str(campaign.store.root / "online-meta-fixture"), campaign.view().task,
        workflow=campaign.view().workflow, meta=MetaPolicy(min_observations=100),
        gate=GatePolicy.model_validate(campaign.state["gate"]))
    state = campaign.state
    view = campaign.view()
    state['observations'] = [Observation(sample_id=f'known-{i}', batch_id='known', sequence=s,
        value=0, metric=view.task.metric, unit=view.task.unit, source='synthetic',
        assay_protocol=view.task.assay_protocol).model_dump(mode='json')
        for i, s in enumerate(view.task.candidates[:2])]
    campaign.store.put('campaign', 'state', state)
    base = campaign.view().meta
    campaign.stage_patch(Patch(target='meta', base_version=base.version,
        changes={'min_observations': 2}, task_kind=view.task.kind,
        hypothesis='Artificial test of promotion only', author_backend='synthetic-test'))
    return campaign, base


@pytest.mark.parametrize('better,expected', [(True, 'accepted'), (False, 'rejected'), (None, 'inconclusive')])
def test_online_meta_uses_shared_queries_and_promotes_only_with_evidence(campaign, monkeypatch, better, expected):
    campaign, base = stage(campaign)
    monkeypatch.setattr('proteinrsi.online_meta.make_validation_team', lambda campaign, store: ScriptedOffspringTeam(store))
    before = campaign.view().workflow.version
    batch = campaign.prepare()
    assert batch.patch_id
    assert campaign.prepare() == batch
    assert campaign.store.usage()['experimental_wells']['committed'] == 0
    submitted = [s.candidate.sequence for s in batch.samples if s.arm != 'control']
    assert len(set(submitted)) == len(submitted)
    campaign.approve(batch.batch_id, operator='test')
    observations = [Observation(sample_id=s.sample_id, batch_id=batch.batch_id,
        sequence=s.candidate.sequence, value=(1 if better is None else
            (10 if (s.arm == 'challenger') == better else 0)), metric=campaign.view().task.metric,
        unit=campaign.view().task.unit, source='synthetic', assay_protocol=campaign.view().task.assay_protocol)
        for s in batch.samples]
    campaign.ingest(observations)
    campaign.ingest(observations)  # No duplicate measurements or debit.
    report = next(iter(campaign.store.all('meta_evaluations').values()))
    assert report['result']['decision'] == expected
    assert report['scope'] == 'current_task_only' and not report['transfer_validated']
    assert (campaign.view().meta.version != base.version) == (better is True)
    assert campaign.view().workflow.version == before
    assert campaign.store.usage()['experimental_wells']['committed'] == len(batch.samples)
    assert campaign.store.all('validation_agent_snapshots')


def test_meta_insufficient_batch_defers_without_promotion(campaign, monkeypatch):
    campaign, base = stage(campaign)
    state = campaign.state
    state['gate']['min_per_arm'] = 100
    campaign.store.put('campaign', 'state', state)
    campaign.prepare()
    assert campaign.view().meta.version == base.version
    assert campaign.state['pending_meta']
    assert not campaign.store.all('meta_online_attempts')
    assert any(e['kind'] == 'meta_validation_deferred' for e in campaign.store.events())


def test_meta_branch_failure_keeps_old_policy_and_records_error(campaign, monkeypatch):
    campaign, base = stage(campaign)
    def fail(campaign, store):
        raise ValueError('artificial branch failure')
    monkeypatch.setattr('proteinrsi.online_meta.make_validation_team', fail)
    batch = campaign.prepare()
    assert batch.patch_id is None
    assert campaign.view().meta.version == base.version
    assert campaign.state['pending_meta'] is None
    attempt = next(iter(campaign.store.all('meta_online_attempts').values()))
    assert attempt['state'] == 'failed'


def test_trace_is_readonly_and_html_does_not_execute_model_text(campaign, tmp_path):
    campaign.store.event('model_text', {'text': '</script><script>alert(1)</script>'})
    path = tmp_path/'timeline.html'
    export_html(campaign.store.root, path)
    assert '</script><script>alert(1)' not in path.read_text()
    trace = read_trace(campaign.store.root)
    assert trace['events'][-1]['payload']['text'].startswith('</script>')
    assert trace['source'] == 'synthetic'
    with pytest.raises(FileNotFoundError):
        read_trace(tmp_path/'missing')
    assert not (tmp_path/'missing').exists()
    assert json.loads(json.dumps(trace)) == trace


def test_identical_meta_descendants_skip_validation_queries(campaign, monkeypatch):
    campaign, _ = stage(campaign)
    state = campaign.state
    state['observations'] = []
    campaign.store.put('campaign', 'state', state)
    class MustNotRun(ScriptedOffspringTeam):
        def run(self, view):
            raise AssertionError('Identical workflows must not launch two research arms')
    monkeypatch.setattr('proteinrsi.online_meta.make_validation_team', lambda campaign, store: MustNotRun(store))
    batch = campaign.prepare()
    assert batch.patch_id is None
    assert next(iter(campaign.store.all('meta_online_attempts').values()))['state'] == 'inconclusive'
    assert campaign.store.usage()['experimental_wells']['committed'] == 0


def test_guarded_meta_branches_support_frozen_external_artifacts(campaign, tmp_path):
    from proteinrsi.online_meta import prepare_meta_trial
    from proteinrsi.localtools.artifacts import ArtifactStore
    from proteinrsi.replay.broker import GuardedTeam
    from proteinrsi.replay.sandbox import probe
    if not probe()['available']:
        pytest.skip('Host cannot enforce replay isolation')
    campaign, _ = stage(campaign)
    artifact = tmp_path/'input.pdb'
    artifact.write_text('HEADER synthetic input\nEND\n')
    original = ArtifactStore(campaign.store).put(artifact, 'pdb')
    campaign.team = GuardedTeam(campaign.store)
    result = prepare_meta_trial(campaign, campaign.state, campaign.view(), 8)
    # Identical candidate priorities provide no comparison, but both isolated
    # branches must still receive the frozen input artifacts.
    assert result is None
    evaluation_id, attempt = next(iter(campaign.store.all('meta_online_attempts').items()))
    assert attempt['state'] == 'inconclusive'
    for arm in ['baseline', 'challenger']:
        from proteinrsi.storage import Store
        child = Store(campaign.store.root/'meta-validation'/evaluation_id/arm)
        assert ArtifactStore(child).resolve(original['ref']).read_text() == artifact.read_text()
    assert campaign.store.usage()['experimental_wells']['committed'] == 0


def test_llm_audit_retains_returned_text_usage_and_summary_not_secret(campaign):
    import httpx
    from proteinrsi.llm import JSONLLM, LLMError
    def response(request):
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Returned summary'}],
             'encrypted_content': 'DO_NOT_LOG'},
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': '{"decision_notes": {"rationale": "Test evidence"}}'}]}],
            'usage': {'input_tokens': 12, 'output_tokens': 4}})
    llm = JSONLLM(campaign.store, model='test-model', base_url='https://example.invalid',
        api_key='private-test-token', api_protocol='responses', transport=httpx.MockTransport(response))
    args = ('M', 'test instructions', {'view': {'round_index': 1}}, {})
    result = llm.complete(*args)
    assert llm.complete(*args) == result
    record = next(iter(campaign.store.all('llm').values()))
    assert record['provider_reasoning_summary'] == ['Returned summary']
    assert record['usage']['input_tokens'] == 12
    assert record['round'] == 1
    assert campaign.store.usage()['llm_calls']['committed'] == 1
    serialized = json.dumps(read_trace(campaign.store.root))
    assert 'private-test-token' not in serialized and 'DO_NOT_LOG' not in serialized
    llm.transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
        'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
         'content': [{'type': 'output_text', 'text': 'invalid JSON private-test-token'}]}]}))
    with pytest.raises(LLMError):
        llm.complete('C', 'different', {}, {})
    failed = [r for r in campaign.store.all('llm').values() if r['state'] == 'failed'][0]
    assert failed['raw_output'] == 'invalid JSON [REDACTED]'


def test_worker_cannot_read_or_edit_operator_audit(campaign):
    from proteinrsi.replay.broker import dispatch
    for namespace in ['llm', 'agent_snapshots', 'validation_llm', 'validation_agent_snapshots',
                      'meta_online_attempts', 'meta_evaluations']:
        for operation in ['get', 'all', 'put']:
            with pytest.raises(PermissionError):
                dispatch(campaign.team, campaign.team.tools, campaign.view(), [],
                    {'rpc': operation, 'namespace': namespace, 'key': 'any', 'value': {}})
