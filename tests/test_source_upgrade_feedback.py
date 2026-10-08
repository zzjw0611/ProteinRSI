"""Synthetic C-only driver tests; fixture doubles are not real migration evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import Candidate, MetaPolicy, TaskSpec
from proteinrsi.lab import CSVOracle
from proteinrsi.llm import ProviderPaused
from proteinrsi.runtime import Campaign
from proteinrsi.synthetic import make_fixture
from test_source_upgrade import old_source_bundle

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'complete_source_upgrade_feedback.py'
spec = importlib.util.spec_from_file_location('checkpoint_driver', SCRIPT)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
REAL_FACTORY = driver._make_campaign


class SyntheticBridge:
    model = 'synthetic-C-only'
    base_url = 'assistant-bridge://local'
    cache_settings = {'llm_transport': 'assistant_bridge', 'llm_bridge_protocol': 1}

    def __init__(self, store):
        self.store, self.pause, self.calls = store, True, []

    def complete(self, role, instructions, context, schema):
        assert role == 'C-feedback'
        self.calls.append(role)
        key = 'synthetic-existing-C'
        if self.store.get('llm', key) is None:
            self.store.reserve(key, 'llm_calls', 1, {'synthetic': 'existing C'})
            self.store.settle(key)
            self.store.put('llm', key, {'state': 'pending', 'role': role,
                                       'transport': 'assistant_bridge', 'round': context['view']['round_index']})
        if self.pause:
            raise ProviderPaused('Synthetic pause with already charged C request')
        answer = {'summary': 'Synthetic existing C completed, without M or planning.'}
        self.store.put('llm', key, {'state': 'done', 'role': role,
                                   'transport': 'assistant_bridge', 'response': answer})
        return answer


@pytest.fixture
def pending(tmp_path, monkeypatch):
    # Only test fixture construction substitutes old runtime metadata. The actual
    # source hashes are verified by old_source_bundle; production dry-run tests
    # separately reject the wrong real import path.
    old = old_source_bundle()
    monkeypatch.setattr('proteinrsi.governance._packaged_source', lambda: deepcopy(old))
    monkeypatch.setattr(driver, '_bundle', lambda: deepcopy(old))
    task_file, labels = make_fixture(tmp_path/'fixture', rounds=20)
    task_data = json.loads(task_file.read_text())
    task_data['budget'] = deepcopy(driver.LIMITS)
    task = TaskSpec.model_validate(task_data)
    campaign = Campaign.initialize(tmp_path/'campaign', task, meta=MetaPolicy(enabled=False))
    campaign.team.run = lambda view: [Candidate(sequence=s) for s in view.task.candidates
                                     if s != view.task.reference_sequence]
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator='synthetic-fixture')
    bridge = SyntheticBridge(campaign.store)
    campaign.team = Team(campaign.store, bridge)
    campaign.store.put('assistant_bridge', 'scope', 'existing-synthetic-scope', immutable=True)
    with pytest.raises(ProviderPaused):
        campaign.ingest(CSVOracle(labels, task).measure(batch))
    def forbidden(*args, **kwargs):
        pytest.fail('Bounded C driver crossed into M, planning, approval or assays')
    monkeypatch.setattr(campaign, 'prepare', forbidden)
    monkeypatch.setattr(campaign, 'approve', forbidden)
    monkeypatch.setattr(campaign, 'ingest', forbidden)
    monkeypatch.setattr(campaign, 'consider_improvement', forbidden)
    monkeypatch.setattr(campaign.meta_agent, 'propose', forbidden)
    monkeypatch.setattr(campaign.team, 'run', forbidden)
    monkeypatch.setattr(driver, '_make_campaign', lambda store, plan: campaign)
    return campaign, bridge


def test_c_driver_dry_run_is_read_only_and_preserves_pending_identity(pending):
    campaign, bridge = pending
    before = driver._read(campaign.store.root)[-1]
    plan = driver.inspect_checkpoint(campaign.store.root)
    assert plan['status'] == 'pending'
    assert plan['identity']['round'] == 1
    assert plan['source_files_sha256'] == driver.OLD_FILES
    assert driver._read(campaign.store.root)[-1] == before
    assert bridge.calls == ['C-feedback']


def test_c_driver_finishes_exact_checkpoint_then_stops_and_is_idempotent(pending):
    campaign, bridge = pending
    plan = driver.inspect_checkpoint(campaign.store.root)
    before = deepcopy(campaign.state)
    usage = campaign.store.usage()
    pins = deepcopy(campaign.store.all('method_snapshots'))
    inputs = deepcopy(campaign.store.all('feedback_inputs'))
    bridge.pause = False
    result = driver.complete_checkpoint(campaign.store.root, expected_preflight=plan['preflight_sha256'],
                                       operator='synthetic-operator', reason='Complete only existing C')
    assert result['stop_before_M_and_planning'] is True
    assert result['status'] == 'complete'
    assert campaign.state['round_index'] == before['round_index']
    assert campaign.state['considered_round'] == before['considered_round']
    assert campaign.store.all('method_snapshots') == pins
    assert campaign.store.all('feedback_inputs') == inputs
    assert campaign.store.usage() == usage  # Reuses the already charged synthetic request.
    assert campaign.store.get('assistant_bridge', 'scope') == 'existing-synthetic-scope'
    assert bridge.calls == ['C-feedback', 'C-feedback']
    state = deepcopy(campaign.state)
    state['history'][-1].pop('analyst_feedback')
    assert state == before
    fingerprint = driver._read(campaign.store.root)[-1]
    again = driver.complete_checkpoint(campaign.store.root, expected_preflight=plan['preflight_sha256'],
                                      operator='synthetic-operator', reason='Idempotent recheck')
    assert again['action'] == 'already-complete'
    assert bridge.calls == ['C-feedback', 'C-feedback']
    assert driver._read(campaign.store.root)[-1] == fingerprint


def test_c_driver_pause_keeps_checkpoint_and_all_charges(pending):
    campaign, bridge = pending
    plan = driver.inspect_checkpoint(campaign.store.root)
    usage = campaign.store.usage()
    inputs = deepcopy(campaign.store.all('feedback_inputs'))
    with pytest.raises(ProviderPaused):
        driver.complete_checkpoint(campaign.store.root, expected_preflight=plan['preflight_sha256'],
                                   operator='synthetic-operator', reason='Bounded retry')
    assert campaign.store.usage() == usage
    assert campaign.store.all('feedback_inputs') == inputs
    assert driver.inspect_checkpoint(campaign.store.root)['status'] == 'pending'
    assert campaign.state['considered_round'] != campaign.state['round_index']


def test_c_driver_rejects_stale_fingerprint_before_any_scientific_call(pending):
    campaign, bridge = pending
    plan = driver.inspect_checkpoint(campaign.store.root)
    campaign.store.event('synthetic-intervening-event', {})
    with pytest.raises(ValueError, match='Stale'):
        driver.complete_checkpoint(campaign.store.root, expected_preflight=plan['preflight_sha256'],
                                   operator='operator', reason='Must not run')
    assert bridge.calls == ['C-feedback']


@pytest.mark.parametrize('fault', ['new_source', 'pending_batch', 'pending_patch', 'missing_checkpoint',
                                   'different_input', 'model_identity', 'active_other_role',
                                   'evaluation', 'reserved', 'definition', 'configuration'])
def test_c_driver_rejects_unsafe_boundary_or_identity(pending, monkeypatch, fault):
    campaign, bridge = pending
    store = campaign.store
    plan = driver.inspect_checkpoint(store.root)
    if fault == 'new_source':
        monkeypatch.setattr(driver, '_bundle', lambda: {'files': {'wrong.py': 'not old25312'}})
    elif fault in {'pending_batch', 'pending_patch'}:
        state = campaign.state
        state[fault] = 'synthetic-pending'
        store.put('campaign', 'state', state)
    elif fault in {'missing_checkpoint', 'different_input', 'model_identity'}:
        value = store.get('feedback_inputs', plan['checkpoint_ref'])
        if fault == 'missing_checkpoint':
            value['identity']['round'] = 999
        elif fault == 'different_input':
            value['view']['round_index'] = 999
        else:
            value['provenance']['client']['llm_transport'] = 'not-the-original-bridge'
            value['provenance_digest'] = driver.sha(value['provenance'])
        store.put('feedback_inputs', plan['checkpoint_ref'], value)
    elif fault == 'active_other_role':
        store.put('llm', 'synthetic-other', {'state': 'pending', 'role': 'M', 'transport': 'assistant_bridge', 'round': campaign.state['round_index']})
    elif fault == 'evaluation':
        store.put('evaluation_plans', 'synthetic', {'status': 'frozen'})
    elif fault == 'reserved':
        store.reserve('synthetic-reservation', 'experimental_wells', 1, {'fixture': True})
    elif fault == 'definition':
        state = campaign.state
        state['workflow']['principal_prompt'] += '\nchanged'
        store.put('campaign', 'state', state)
    elif fault == 'configuration':
        store.put('configuration', 'research', {'changed': True})
    before = driver._read(store.root)[-1]
    with pytest.raises(ValueError):
        driver.inspect_checkpoint(store.root)
    assert driver._read(store.root)[-1] == before
    assert bridge.calls == ['C-feedback']


def test_real_new_package_is_refused_without_reading_campaign(tmp_path):
    # No metadata double here: tests import corrected40e1, not old25312.
    with pytest.raises(ValueError, match='original25312'):
        driver.inspect_checkpoint(tmp_path/'missing-campaign')


def test_authentic_bridge_context_receipts_complete_then_upgrade(tmp_path, monkeypatch):
    from test_source_upgrade import _make_legacy, upgrade, apply_plan
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True,
                            bridge=True, pause_current=True)
    old = old_source_bundle()
    monkeypatch.setattr(driver, '_bundle', lambda: deepcopy(old))
    monkeypatch.setattr(driver, '_make_campaign', lambda store, plan: campaign)
    plan = driver.inspect_checkpoint(campaign.store.root)
    usage = campaign.store.usage()
    history = deepcopy(campaign.state['history'])
    original_inputs = deepcopy(campaign.store.all('feedback_inputs'))
    campaign.team.llm.pause_round = None
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr('proteinrsi.governance._packaged_source', lambda: deepcopy(old))
        result = driver.complete_checkpoint(campaign.store.root,
            expected_preflight=plan['preflight_sha256'], operator='fixture', reason='C-only')
    assert result['status'] == 'complete'
    assert campaign.state['history'][:-1] == history[:-1]
    assert campaign.store.all('feedback_inputs') == original_inputs
    assert campaign.store.usage()['experimental_wells'] == usage['experimental_wells']
    assert campaign.store.usage()['tool_calls'] == usage['tool_calls']
    completed = deepcopy(campaign.store.all('feedback_results'))
    proposal = upgrade.dry_run(campaign.store.root)
    apply_plan(campaign, proposal)
    assert campaign.store.all('feedback_results') == completed
    assert campaign.state['round_index'] == 2


def test_c_driver_rejects_pending_feedback_from_wrong_round(pending):
    campaign, bridge = pending
    row = campaign.store.get('llm', 'synthetic-existing-C')
    row['round'] = 999
    campaign.store.put('llm', 'synthetic-existing-C', row)
    with pytest.raises(ValueError, match='outstanding'):
        driver.inspect_checkpoint(campaign.store.root)


@pytest.mark.parametrize("fault", ["missing_mailbox", "wrong_model"])
def test_real_factory_rejects_provider_identity_before_any_constructor(pending, monkeypatch, fault):
    campaign, _ = pending
    plan = driver.inspect_checkpoint(campaign.store.root)
    if fault == "missing_mailbox":
        monkeypatch.delenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", raising=False)
    else:
        monkeypatch.setenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", str(campaign.store.root))
        monkeypatch.setenv("PROTEINRSI_MODEL", "wrong-model")
    with pytest.raises(ValueError, match="mailbox|model"):
        REAL_FACTORY(campaign.store, plan)


@pytest.mark.parametrize('typed,first_action', [(False, False), (True, False), (True, True)])
def test_real_factory_consumes_pending_mailbox_response_then_upgrade(tmp_path, monkeypatch, typed, first_action):
    """Real factory, bridge receipts and canonical C; only sandbox launch is doubled."""
    from test_source_upgrade import _make_legacy, upgrade, apply_plan
    campaign = _make_legacy(tmp_path, monkeypatch, typed=typed, with_meta=True,
                            bridge=True, pause_current=True)
    old = old_source_bundle()
    monkeypatch.setattr(driver, '_bundle', lambda: deepcopy(old))
    monkeypatch.setattr('proteinrsi.governance._packaged_source', lambda: deepcopy(old))
    monkeypatch.setattr('proteinrsi.replay.sandbox.probe', lambda: {'available': True})
    # Factory and bridge are real. Namespace execution is covered separately by
    # bwrap acceptance CI; this portable fixture must not need host namespaces.
    monkeypatch.setattr('proteinrsi.replay.broker.GuardedTeam.from_team', lambda team: team)
    mailbox = campaign.team.llm.mailbox
    monkeypatch.setenv('PROTEINRSI_ASSISTANT_BRIDGE_DIR', str(mailbox))
    monkeypatch.setenv('PROTEINRSI_MODEL', campaign.team.llm.model)
    monkeypatch.setenv('PROTEINRSI_ASSISTANT_BRIDGE_TIMEOUT', '0.1')
    pending = [r for r in campaign.store.all('llm').values() if r['state'] == 'pending']
    assert len(pending) == 1
    request = pending[0]['request']
    if typed:
        assert '_context' in request['context']
    for name in ('requests', 'responses'):
        (mailbox/name).mkdir(exist_ok=True)
    (mailbox/'requests'/(request['request_id'] + '.json')).write_text(json.dumps(request))
    response = {k: request[k] for k in
                ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
    response['result'] = ({'_context_action': {'kind': 'read',
        'ref': request['context']['_context']['source']['evidence_ref'], 'offset': 0, 'limit': 1}}
        if first_action else {'summary': 'Synthetic final feedback consumed from the real mailbox.'})
    (mailbox/'responses'/(request['request_id'] + '.json')).write_text(json.dumps(response))
    plan = driver.inspect_checkpoint(campaign.store.root)
    factory = REAL_FACTORY(campaign.store, plan)
    # A changed prompt must not open a new scientific decision or charge.
    fingerprint = driver._read(campaign.store.root)[-1]
    with pytest.raises(ValueError, match='exact pending request'):
        factory.team.llm._complete('C-feedback', request['instructions'] + ' changed',
                                   request['context'], request['schema'])
    assert driver._read(campaign.store.root)[-1] == fingerprint
    usage = campaign.store.usage()
    before = deepcopy(campaign.state)
    if first_action:
        with pytest.raises(ProviderPaused):
            driver.complete_checkpoint(campaign.store.root,
                expected_preflight=plan['preflight_sha256'], operator='fixture', reason='Read then pause')
        assert campaign.state == before
        next_pending = [r for r in campaign.store.all('llm').values() if r['state'] == 'pending']
        assert len(next_pending) == 1
        follow = next_pending[0]['request']
        assert follow['context']['_context']['session'] == request['context']['_context']['session']
        assert follow['context']['_context']['actions_used'] == 1
        next_response = {k: follow[k] for k in
            ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
        next_response['result'] = {'summary': 'Synthetic final feedback after an evidence read.'}
        (mailbox/'responses'/(follow['request_id'] + '.json')).write_text(json.dumps(next_response))
        after_read = campaign.store.usage()
        assert after_read['experimental_wells'] == usage['experimental_wells']
        assert after_read['tool_calls'] == usage['tool_calls']
        assert after_read['llm_calls']['committed'] == usage['llm_calls']['committed'] + 1
        usage = after_read
        plan = driver.inspect_checkpoint(campaign.store.root)
    result = driver.complete_checkpoint(campaign.store.root,
        expected_preflight=plan['preflight_sha256'], operator='fixture', reason='Real factory test')
    assert result['status'] == 'complete'
    assert campaign.store.usage() == usage
    assert campaign.state['round_index'] == before['round_index']
    assert campaign.state['considered_round'] == before['considered_round']
    assert campaign.store.get('llm', 'llm-' + request['request_id'])['state'] == 'done'
    apply_plan(campaign, upgrade.dry_run(campaign.store.root))


@pytest.mark.parametrize('metric_tool', [False, True])
def test_original_runtime_guarded_mailbox_completion_then_upgrade(tmp_path, monkeypatch, metric_tool):
    """Actual old source subprocess, bwrap worker, bridge and source-upgrade join."""
    import os
    import subprocess
    import sys
    from test_source_upgrade import _make_legacy, upgrade, apply_plan
    from proteinrsi.replay.sandbox import backend_identity, probe
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
    sandbox = probe()
    if not sandbox['available']:
        if os.environ.get('PROTEINRSI_REQUIRE_BWRAP') == '1':
            pytest.fail(sandbox['reason'])
        pytest.skip(sandbox['reason'])
    actual_backend = backend_identity()
    from proteinrsi.contracts import Workflow
    workflow = Workflow()
    if metric_tool:
        workflow.tool_names.append('research_metric_extract')
        workflow.skill_names.append('protein-metrics')
    with monkeypatch.context() as fixture:
        campaign = _make_legacy(tmp_path, fixture, typed=True, with_meta=True,
                                bridge=True, pause_current=True, workflow=workflow)
    campaign.store.put('configuration', 'replay_security',
                       {'backend': 'bwrap', 'identity': actual_backend})
    old = old_source_bundle()
    old_root = tmp_path/'old-runtime'/'proteinrsi'
    for name, source in old['files'].items():
        path = old_root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    mailbox = campaign.team.llm.mailbox
    pending = [r for r in campaign.store.all('llm').values() if r['state'] == 'pending']
    assert len(pending) == 1
    request = pending[0]['request']
    for name in ('requests', 'responses'):
        (mailbox/name).mkdir(exist_ok=True)
    (mailbox/'requests'/(request['request_id'] + '.json')).write_text(json.dumps(request))
    response = {k: request[k] for k in
                ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
    response['result'] = {'summary': 'Synthetic real guarded completion under original source.'}
    (mailbox/'responses'/(request['request_id'] + '.json')).write_text(json.dumps(response))
    env = {**os.environ, 'PYTHONPATH': str(old_root.parent) + os.pathsep + os.environ.get('PYTHONPATH', ''),
           'PROTEINRSI_ASSISTANT_BRIDGE_DIR': str(mailbox), 'PROTEINRSI_MODEL': campaign.team.llm.model,
           'PROTEINRSI_ASSISTANT_BRIDGE_TIMEOUT': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    command = [sys.executable, str(SCRIPT), '--campaign', str(campaign.store.root)]
    dry = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0, dry.stderr
    plan = json.loads(dry.stdout)
    usage = campaign.store.usage()
    completed = subprocess.run(command + ['--complete', '--expected-preflight', plan['preflight_sha256'],
        '--operator', 'synthetic-test', '--reason', 'Test the actual isolated C-only path'],
        env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['status'] == 'complete'
    assert campaign.store.usage() == usage
    with campaign.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='guarded_worker_completed'").fetchone()[0] > 0
    apply_plan(campaign, upgrade.dry_run(campaign.store.root))


@pytest.mark.parametrize('fault', ['row_key', 'request_key', 'missing_charge', 'amount', 'fingerprint', 'released'])
def test_real_factory_refuses_displaced_request_or_changed_charge(tmp_path, monkeypatch, fault):
    from test_source_upgrade import _make_legacy
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True,
                            bridge=True, pause_current=True)
    old = old_source_bundle()
    monkeypatch.setattr(driver, '_bundle', lambda: deepcopy(old))
    monkeypatch.setattr('proteinrsi.replay.sandbox.probe', lambda: {'available': True})
    monkeypatch.setenv('PROTEINRSI_ASSISTANT_BRIDGE_DIR', str(campaign.team.llm.mailbox))
    monkeypatch.setenv('PROTEINRSI_MODEL', campaign.team.llm.model)
    plan = driver.inspect_checkpoint(campaign.store.root)
    key, record = next((k, r) for k, r in campaign.store.all('llm').items() if r['state'] == 'pending')
    with campaign.store.connect() as db:
        if fault == 'row_key':
            db.execute("UPDATE kv SET key='displaced' WHERE namespace='llm' AND key=?", (key,))
        elif fault == 'request_key':
            record['request_key'] = 'displaced'
            campaign.store.put('llm', key, record)
        elif fault == 'missing_charge':
            db.execute('DELETE FROM charges WHERE key=?', (key,))
        else:
            field, value = {'amount': ('amount', 0), 'fingerprint': ('fingerprint', '0'*64),
                            'released': ('state', 'released')}[fault]
            db.execute('UPDATE charges SET ' + field + '=? WHERE key=?', (value, key))
    fingerprint = driver._read(campaign.store.root)[-1]
    with pytest.raises(ValueError, match='record key|settled call charge'):
        REAL_FACTORY(campaign.store, plan)
    assert driver._read(campaign.store.root)[-1] == fingerprint


@pytest.mark.parametrize('fault', ['limit', 'task_limit', 'negative', 'overspent'])
def test_c_driver_refuses_changed_caps_or_invalid_charge(pending, fault):
    campaign, bridge = pending
    if fault == 'task_limit':
        state = campaign.state
        state['task']['budget']['llm_calls'] += 1
        campaign.store.put('campaign', 'state', state)
    else:
        with campaign.store.connect() as db:
            if fault == 'limit':
                db.execute("UPDATE limits SET amount=601 WHERE resource='llm_calls'")
            else:
                db.execute("UPDATE charges SET amount=? WHERE resource='llm_calls'",
                           (-1 if fault == 'negative' else 601,))
    before = driver._read(campaign.store.root)[-1]
    with pytest.raises(ValueError, match='Budget|budget'):
        driver.inspect_checkpoint(campaign.store.root)
    assert driver._read(campaign.store.root)[-1] == before
    assert bridge.calls == ['C-feedback']
