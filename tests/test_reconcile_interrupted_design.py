"""Disposable database tests; never modify a campaign or consult hidden labels."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'reconcile_interrupted_design.py'
spec = importlib.util.spec_from_file_location('reconcile_interrupted_design', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def case(tmp_path, monkeypatch):
    campaign = tmp_path / 'campaign'
    campaign.mkdir()
    (campaign / '.campaign.lock').touch()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    mailbox = tmp_path / 'mailbox'
    mailbox.mkdir()
    con = sqlite3.connect(campaign / 'state.sqlite3')
    con.executescript('''CREATE TABLE kv(namespace TEXT,key TEXT,value TEXT,PRIMARY KEY(namespace,key));
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL,kind TEXT,payload TEXT);
        CREATE TABLE charges(key TEXT PRIMARY KEY,resource TEXT,amount INTEGER,fingerprint TEXT,state TEXT);
        CREATE TABLE limits(resource TEXT PRIMARY KEY,amount INTEGER);''')
    con.execute('INSERT INTO kv VALUES(?,?,?)', (mod.NAMESPACE, mod.STEP, '{"status":"started"}'))
    con.execute('INSERT INTO charges VALUES(?,?,?,?,?)', ('prior', 'llm_calls', 1, 'unchanged', 'committed'))
    con.commit()
    con.close()
    proof = {'step': mod.STEP, 'original_receipt': {'status': 'started'}, 'validated': 'fixture'}
    monkeypatch.setattr(mod, 'validate', lambda *args: proof)
    return SimpleNamespace(campaign=campaign, runtime=runtime, mailbox=mailbox, proof=proof)


def run(c, **kwargs):
    return mod.reconcile(c.campaign, c.runtime, c.mailbox, **kwargs)


def dump(c):
    with sqlite3.connect(c.campaign / 'state.sqlite3') as con:
        return list(con.iterdump())


def test_dry_run_never_changes_database(case):
    before = dump(case)
    result = run(case)
    assert result['status'] == 'validated_dry_run'
    assert result['proof_hash'] == mod.sha(case.proof)
    assert dump(case) == before


def test_apply_requires_fresh_explicit_proof(case):
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match='requires'):
        run(case, apply=True)
    with pytest.raises(mod.ReconciliationError, match='stale'):
        run(case, apply=True, expected='0' * 64)
    assert dump(case) == before


def test_preserves_charge_original_receipt_and_audits_once(case):
    proof = run(case)['proof_hash']
    result = run(case, apply=True, expected=proof)
    assert result['status'] == 'reconciled'
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        receipt = json.loads(con.execute('SELECT value FROM kv WHERE key=?', (mod.STEP,)).fetchone()[0])
        evidence = json.loads(con.execute('SELECT value FROM kv WHERE key=?', (mod.RECORD,)).fetchone()[0])
        assert receipt['status'] == 'paused_provider'
        assert evidence['original_receipt'] == {'status': 'started'}
        assert evidence['proof_hash'] == proof
        assert con.execute('SELECT * FROM charges').fetchall() == [('prior', 'llm_calls', 1, 'unchanged', 'committed')]
        assert con.execute('SELECT kind FROM events').fetchall() == [('operator_interrupted_design_reconciled',)]
    after = dump(case)
    assert run(case, apply=True, expected=proof)['status'] == 'already_reconciled'
    assert dump(case) == after


def test_wrong_idempotency_hash_rejected(case):
    run(case, apply=True, expected=run(case)['proof_hash'])
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match='different'):
        run(case, apply=True, expected='wrong')
    assert dump(case) == before


def test_live_writer_rejected(case):
    child = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys,time; f=open(sys.argv[1],"rb"); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(60)',
        str(case.campaign / '.campaign.lock')], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(mod.ReconciliationError, match='writer'):
            run(case)
    finally:
        child.terminate()
        child.wait()


def test_observable_unlocked_controller_rejected(case):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', str(case.campaign)])
    try:
        with pytest.raises(mod.ReconciliationError, match='Live process'):
            mod.no_live_controller(case.campaign)
    finally:
        child.terminate()
        child.wait()


def test_audit_failure_rolls_back_receipt_and_evidence(case, monkeypatch):
    before = dump(case)
    original = mod.canonical
    def fail_audit(value):
        if isinstance(value, dict) and value.get('from') == 'started':
            raise RuntimeError('injected audit failure')
        return original(value)
    monkeypatch.setattr(mod, 'canonical', fail_audit)
    with pytest.raises(RuntimeError, match='injected'):
        run(case, apply=True, expected=run(case)['proof_hash'])
    assert dump(case) == before


def test_database_trigger_is_rejected(case):
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        con.execute("CREATE TRIGGER mutate_charge AFTER UPDATE ON kv BEGIN UPDATE charges SET amount=0; END")
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match='triggers'):
        run(case, apply=True, expected=mod.sha(case.proof))
    assert dump(case) == before


@pytest.mark.parametrize('damage', ['receipt', 'audit'])
def test_idempotency_checks_receipt_and_audit(case, damage):
    proof = run(case)['proof_hash']
    run(case, apply=True, expected=proof)
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        if damage == 'receipt':
            con.execute('UPDATE kv SET value=? WHERE key=?', ('{"status":"started"}', mod.STEP))
        else:
            con.execute('DELETE FROM events')
    before = dump(case)
    with pytest.raises(mod.ReconciliationError):
        run(case, apply=True, expected=proof)
    assert dump(case) == before


def test_cas_refuses_changed_receipt_even_if_validator_compromised(case):
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        con.execute('UPDATE kv SET value=? WHERE key=?', ('{"status":"done","resource_id":"existing"}', mod.STEP))
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match='Receipt changed'):
        run(case, apply=True, expected=run(case)['proof_hash'])
    assert dump(case) == before


@pytest.mark.parametrize('reason', ['Unexpected effect after step started', 'Response byte hash mismatch',
    'Runtime source fingerprint mismatch', 'Evidence replay requires a different request',
    'Charge ledger differs from diagnosed interruption'])
def test_failed_proof_cannot_mutate(case, monkeypatch, reason):
    def rejected(*args): raise mod.ReconciliationError(reason)
    monkeypatch.setattr(mod, 'validate', rejected)
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match=reason):
        run(case, apply=True, expected='0' * 64)
    assert dump(case) == before


def test_ordinary_executor_reuses_bridge_charge_and_executes_once(tmp_path):
    from proteinrsi.assistant_bridge import AssistantBridgeLLM
    from proteinrsi.storage import Store
    from proteinrsi.contracts import digest
    from proteinrsi.dataflow.protocol import Protocol, Operation, OperationRegistry, ProtocolExecutor
    from proteinrsi.dataflow.resources import ResourceStore, standard_registry, SEQUENCES
    store = Store(tmp_path / 'ordinary')
    store.configure_budget({'llm_calls': 2})
    bridge = AssistantBridgeLLM(store, model='fixture-model', mailbox=str(tmp_path / 'mailbox'), timeout=1)
    schema = {'type': 'object', 'properties': {'result': {'type': 'string'}}, 'required': ['result']}
    request = bridge._request('B', 'fixture instructions', {'view': {'round_index': 3}}, schema)
    bridge._pending(request)
    response = {k: request[k] for k in ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
    response['result'] = {'result': 'existing genuine response'}
    responses = tmp_path / 'mailbox' / 'responses'
    responses.mkdir()
    (responses / (request['request_id'] + '.json')).write_text(json.dumps(response))
    resources = ResourceStore(store, standard_registry(), 'fixture-scope')
    ops = OperationRegistry(resources.registry)
    calls = []
    def invoke(args, key):
        result = bridge._complete('B', 'fixture instructions', {'view': {'round_index': 3}}, schema)
        assert result == response['result']
        calls.append('one downstream execution')
        return {'items': []}
    ops.register(Operation('agent:propose', {'type': 'object'}, SEQUENCES, invoke, 'fixture-impl'))
    protocol = Protocol.model_validate({'hypothesis': 'fixture', 'steps': [{'step_id': 'design',
        'operation': 'agent:propose', 'question': 'fixture'}], 'final_outputs': {'candidates': {
            'source': 'step:design.result', 'schema_ref': SEQUENCES}}})
    key = 'protocol-step:' + digest({'scope': resources.scope, 'operation': 'agent:propose',
        'implementation': 'fixture-impl', 'arguments': {}, 'parents': [],
        'output_schema': resources.registry.fingerprint(SEQUENCES)})
    # Represents exactly the utility's recovery state, not a false done receipt.
    store.put(mod.NAMESPACE, key, {'status': 'paused_provider', 'reconciliation_ref': 'fixture-proof'})
    before = store.usage()
    executor = ProtocolExecutor(resources, ops)
    result = executor.execute(protocol, {})
    again = executor.execute(protocol, {})
    assert result == again
    assert calls == ['one downstream execution']
    assert store.usage() == before
    assert store.get('llm', 'llm-' + request['request_id'])['state'] == 'done'


@pytest.fixture
def semantic_case(tmp_path):
    """Opt-in local semantic evidence, excluded from published scientific data.

    Input must be a specifically marked metadata-only fixture. This fixture has
    exact allowed context/audit rows and placeholders instead of empirical rows.
    Tests copy it to a fresh temporary directory and never touch the input.
    """
    import os
    import shutil
    value = os.environ.get('PROTEINRSI_RECONCILIATION_METADATA_FIXTURE')
    if not value:
        pytest.skip('requires a separately prepared metadata-only fixture')
    source = Path(value).resolve()
    assert (source / 'METADATA_ONLY_FIXTURE').is_file()
    with sqlite3.connect((source / 'campaign' / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as con:
        assert all(json.loads(v) == {'metadata_fixture_only': True} for (v,) in con.execute(
            "SELECT value FROM kv WHERE namespace IN ('measurements','batches')"))
    destination = tmp_path / 'disposable-semantic-case'
    shutil.copytree(source, destination)
    return SimpleNamespace(campaign=destination / 'campaign', mailbox=destination / 'mailbox',
                           runtime=SCRIPT.parents[1])


def test_real_semantic_proof_then_disposable_apply(semantic_case):
    case = semantic_case
    before = dump(case)
    result = run(case)
    assert result['status'] == 'validated_dry_run'
    assert dump(case) == before
    proof = result['proof_hash']
    assert run(case, apply=True, expected=proof)['status'] == 'reconciled'
    assert run(case, apply=True, expected=proof)['status'] == 'already_reconciled'
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        charges = con.execute('SELECT resource,sum(amount) FROM charges GROUP BY resource ORDER BY resource').fetchall()
        assert charges == [('experimental_wells', 300), ('llm_calls', 38), ('tool_calls', 3)]


@pytest.mark.parametrize('change', ['tool_effect', 'tool_pending', 'context_frame', 'response',
    'request', 'charge', 'charge_fingerprint', 'input_scope', 'audit_drift', 'source'])
def test_real_semantic_perturbations_reject_without_changes(semantic_case, change, tmp_path):
    case = semantic_case
    if change in ('response', 'request'):
        sub = 'responses' if change == 'response' else 'requests'
        path = case.mailbox / sub / (mod.REQUEST + '.json')
        data = json.loads(path.read_text())
        data['model'] = 'different-model'
        path.write_text(json.dumps(data))
    elif change == 'source':
        case.runtime = tmp_path / 'wrong-source'
        case.runtime.mkdir()
    else:
        with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
            if change == 'tool_effect':
                con.execute('INSERT INTO events(timestamp,kind,payload) VALUES(0,?,?)', ('tool_started', '{}'))
            elif change == 'tool_pending':
                key, value = con.execute("SELECT key,value FROM kv WHERE namespace='tool_jobs' LIMIT 1").fetchone()
                data = json.loads(value)
                data['state'] = 'started'
                con.execute("UPDATE kv SET value=? WHERE namespace='tool_jobs' AND key=?", (json.dumps(data), key))
            elif change == 'context_frame':
                key, value = con.execute("SELECT key,value FROM kv WHERE namespace='context_frames' AND key LIKE 'ctx-834108%/1'").fetchone()
                data = json.loads(value)
                data['context_bytes'] += 1
                con.execute("UPDATE kv SET value=? WHERE namespace='context_frames' AND key=?", (json.dumps(data), key))
            elif change == 'charge':
                con.execute('UPDATE charges SET amount=2 WHERE key=?', ('llm-' + mod.REQUEST,))
            elif change == 'charge_fingerprint':
                con.execute('UPDATE charges SET fingerprint=? WHERE key=?', ('wrong', 'llm-' + mod.REQUEST))
            elif change == 'input_scope':
                key, value = con.execute("SELECT key,value FROM kv WHERE namespace=? AND json_extract(value,'$.scope')=? AND json_extract(value,'$.schema_ref')='context.task/v1'", (mod.NAMESPACE, mod.SCOPE)).fetchone()
                data = json.loads(value)
                data['data']['round_index'] = 4
                con.execute('UPDATE kv SET value=? WHERE namespace=? AND key=?', (json.dumps(data), mod.NAMESPACE, key))
            elif change == 'audit_drift':
                # Even an otherwise allowed event kind is outside pinned history.
                con.execute('INSERT INTO events(timestamp,kind,payload) VALUES(0,?,?)', ('run_started', '{}'))
    before = dump(case)
    # No stale-hash fallback: the actual semantic proof must reject on its own.
    with pytest.raises(mod.ReconciliationError):
        run(case)
    assert dump(case) == before
