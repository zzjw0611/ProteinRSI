"""Disposable database tests; never modify a campaign or consult hidden labels."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'reconcile_reviewed_designs.py'
spec = importlib.util.spec_from_file_location('reconcile_reviewed_designs', SCRIPT)
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
    con.execute('INSERT INTO kv VALUES(?,?,?)', (mod.NAMESPACE, 'protocol-step:synthetic', '{"status":"started"}'))
    con.execute('INSERT INTO charges VALUES(?,?,?,?,?)', ('prior', 'llm_calls', 1, 'unchanged', 'committed'))
    con.commit()
    con.close()
    recipe = {'step': 'protocol-step:synthetic', 'run': 'protocol-run:synthetic',
        'scope': 'synthetic', 'request_id': 'assistant-synthetic', 'request_hash': 'a' * 64,
        'response_sha256': 'b' * 64, 'source_sha256': mod.SOURCE_HASH, 'state_sha256': 'd' * 64,
        'round': 0, 'start_event': 1, 'implementation': 'synthetic',
        'replayed_requests': ['assistant-synthetic'], 'cache_hits': [], 'model': 'fixture-model',
        'history_count': 0, 'effect_sha256': 'e' * 64, 'job_count': 0, 'charge_totals': [],
        'context_root_sha256': 'f' * 64}
    recipe_path = tmp_path / 'synthetic-recipe.json'
    recipe_path.write_text(json.dumps(recipe))
    monkeypatch.setattr(mod, 'REVIEWED_RECIPES', {mod.sha(recipe)})
    proof = {'original_receipt_sha256': hashlib.sha256(b'{"status":"started"}').hexdigest(), 'recipe_sha256': mod.sha(recipe), 'step': recipe['step'], 'original_receipt': {'status': 'started'}, 'validated': 'fixture'}
    monkeypatch.setattr(mod, 'validate', lambda *args: proof)
    monkeypatch.setattr(mod, 'source_hash', lambda *args: mod.SOURCE_HASH)
    monkeypatch.setattr(mod, 'validate_mailbox', lambda *args: {})
    return SimpleNamespace(campaign=campaign, runtime=runtime, mailbox=mailbox, proof=proof, recipe=recipe, recipe_path=recipe_path)


def run(c, **kwargs):
    return mod.reconcile(c.campaign, c.runtime, c.mailbox, c.recipe_path, **kwargs)


def record(c):
    return 'operator-reviewed-reconciliation:' + c.recipe['step']


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
        receipt = json.loads(con.execute('SELECT value FROM kv WHERE key=?', (case.recipe['step'],)).fetchone()[0])
        evidence = json.loads(con.execute('SELECT value FROM kv WHERE key=?', (record(case),)).fetchone()[0])
        assert receipt['status'] == 'paused_provider'
        assert evidence['original_receipt'] == {'status': 'started'}
        assert evidence['proof_hash'] == proof
        assert con.execute('SELECT * FROM charges').fetchall() == [('prior', 'llm_calls', 1, 'unchanged', 'committed')]
        assert con.execute('SELECT kind FROM events').fetchall() == [('operator_reviewed_design_reconciled',)]
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


@pytest.mark.parametrize('damage', ['receipt', 'audit', 'original_receipt_bytes'])
def test_idempotency_checks_receipt_and_audit(case, damage):
    proof = run(case)['proof_hash']
    run(case, apply=True, expected=proof)
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        if damage == 'receipt':
            con.execute('UPDATE kv SET value=? WHERE key=?', ('{"status":"started"}', case.recipe['step']))
        elif damage == 'audit':
            con.execute('DELETE FROM events')
        else:
            evidence = json.loads(con.execute('SELECT value FROM kv WHERE key=?', (record(case),)).fetchone()[0])
            evidence['original_receipt_bytes'] = '{"status":"done"}'
            con.execute('UPDATE kv SET value=? WHERE key=?', (json.dumps(evidence), record(case)))
    before = dump(case)
    with pytest.raises(mod.ReconciliationError):
        run(case, apply=True, expected=proof)
    assert dump(case) == before


def test_cas_refuses_changed_receipt_even_if_validator_compromised(case):
    with sqlite3.connect(case.campaign / 'state.sqlite3') as con:
        con.execute('UPDATE kv SET value=? WHERE key=?', ('{"status":"done","resource_id":"existing"}', case.recipe['step']))
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



def test_unreviewed_recipe_is_rejected(case, monkeypatch):
    monkeypatch.setattr(mod, 'REVIEWED_RECIPES', set())
    before = dump(case)
    with pytest.raises(mod.ReconciliationError):
        run(case)
    assert dump(case) == before


def test_recipe_hash_is_canonical_and_content_bound(case):
    case.recipe_path.write_text(json.dumps(case.recipe, indent=4, sort_keys=True))
    assert mod.read_recipe(case.recipe_path) == case.recipe
    changed = {**case.recipe, 'request_hash': '0' * 64}
    case.recipe_path.write_text(json.dumps(changed))
    with pytest.raises(mod.ReconciliationError):
        mod.read_recipe(case.recipe_path)


@pytest.fixture(params=[False, True], ids=['no-tools', 'cached-tool'])
def semantic_case(tmp_path, monkeypatch, request):
    """Entirely synthetic: real context/bridge code, no private campaign inputs."""
    with_cache = request.param
    real_sandbox = request.param == 'sandbox'
    if real_sandbox:
        monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
        from proteinrsi.replay.sandbox import probe
        result = probe()
        if not result['available']:
            import os
            if os.environ.get('PROTEINRSI_REQUIRE_BWRAP') == '1':
                pytest.fail(result['reason'])
            pytest.skip(result['reason'])
    import hashlib
    from proteinrsi.agents import DesignerAgent, Plan
    from proteinrsi.assistant_bridge import AssistantBridgeLLM
    from proteinrsi.contracts import TaskSpec, TaskView, Workflow, MetaPolicy, digest
    from proteinrsi.dataflow.protocol import Protocol
    from proteinrsi.dataflow.resources import ResourceStore, standard_registry, scope_for, SEQUENCES
    from proteinrsi.storage import Store

    campaign = tmp_path / 'campaign'
    store = Store(campaign)
    (campaign / '.campaign.lock').touch()
    mailbox = tmp_path / 'mailbox'
    mailbox.mkdir()
    store.configure_budget({'llm_calls': 10, 'tool_calls': 10, 'experimental_wells': 10})
    store.put('configuration', 'research', {'context_policy': 'evidence-v1'})
    view = TaskView(task=TaskSpec(name='Synthetic reconciliation fixture', reference_sequence='ACDE',
        mutable_positions=[2], candidates=['ACDE', 'AVDE'], batch_size=2, controls_per_batch=0),
        round_index=0, observations=[], history=[], remaining_wells=10,
        workflow=Workflow(), meta=MetaPolicy(enabled=False))
    scope = scope_for(view)
    resources = ResourceStore(store, standard_registry(), scope)
    resources.put('context.task/v1', view.model_dump(mode='json'), producer='task-view', is_input=True)
    knowledge = {'fixture': 'synthetic method knowledge'}
    store.put('resource_selections', 'fixture', {'method_knowledge': knowledge})
    implementation = 'typed-designer-v3-final-selection:' + digest(knowledge)
    arguments = {'question': 'Synthetic design question'}
    step = 'protocol-step:' + digest({'scope': scope, 'operation': 'agent:propose',
        'implementation': implementation, 'arguments': arguments, 'parents': [],
        'output_schema': standard_registry().fingerprint(SEQUENCES)})
    run_id = 'protocol-run:' + digest({'synthetic': 'run'})
    protocol = Protocol.model_validate({'hypothesis': 'Synthetic only', 'steps': [{'step_id': 'design',
        'operation': 'agent:propose', 'question': arguments['question'], 'arguments': arguments}],
        'final_outputs': {'candidates': {'source': 'step:design.result', 'schema_ref': SEQUENCES}}})
    store.put(mod.NAMESPACE, step, {'status': 'started'})
    store.put(mod.NAMESPACE, run_id, {'scope': scope, 'status': 'running', 'completed': [],
        'reviewed': [], 'revisions': [], 'protocol': protocol.model_dump(mode='json')})
    store.event('research_step_started', {'run_id': run_id, 'step_id': 'design', 'cache_hit': False})
    with store.connect() as con:
        start_event = con.execute('SELECT max(id) FROM events').fetchone()[0]
    bridge = AssistantBridgeLLM(store, model='fixture-model', mailbox=mailbox)
    captured = []
    catalog = []
    evidence = [{'method_knowledge': knowledge}]
    tool_keys = []
    if with_cache:
        from proteinrsi.tools import ToolGateway, ToolSpec
        from proteinrsi.tools import ToolCall
        gateway = ToolGateway(store)
        spec = ToolSpec(name='research_python', capability='analysis.program',
            description='Synthetic cache fixture only; no sandbox execution claim',
            implementation_version='synthetic-v1', task_kinds=[view.task.kind],
            input_schema={'type': 'object'}, output_schema={'type': 'object'})
        call = ToolCall(name='research_python', arguments={'code': "result = {'synthetic': True}"},
            purpose='Synthetic cache fixture')
        def synthetic_tool(arguments):
            # Model a previously completed sandbox audit without executing a
            # scientific tool. The entire campaign and recipe are test-only.
            store.event('generated_code_completed', {'code_sha256': digest(arguments['code']),
                'status': 'ok', 'execution_backend': 'bwrap_seccomp_generated_v1'})
            return {'status': 'ok', 'synthetic_fixture_only': True}
        if real_sandbox:
            from proteinrsi.research.code import register_code_tool
            store.put('configuration', 'research', {'context_policy': 'evidence-v1', 'enable_generated_code': True})
            register_code_tool(gateway, view)
        else:
            gateway.register(spec, synthetic_tool)
        catalog = gateway.catalog(view.task, ['research_python'])
    class SyntheticInterruption(BaseException):
        pass
    def stop_at_pending(role, instructions, context, schema):
        request = bridge._request(role, instructions, context, schema)
        bridge._pending(request)
        captured.append(request)
        if with_cache and len(captured) == 1:
            response = {key: request[key] for key in ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
            response['result'] = {'tool_calls': [call.model_dump(mode='json')]}
            key = 'llm-' + request['request_id']
            return bridge._accept(key, store.get('llm', key), request, json.dumps(response).encode())
        raise SyntheticInterruption
    monkeypatch.setattr(bridge, '_complete', stop_at_pending)
    designer = DesignerAgent(bridge, store)
    if with_cache:
        design = designer.propose(view, Plan(rationale=arguments['question']), evidence, catalog)
        evidence.append(gateway.call(design.tool_calls[0], view.task,
            allowed=['research_python'], context_key=step))
        tool_keys = list(store.all('tool_jobs'))
    with pytest.raises(SyntheticInterruption):
        designer.propose(view, Plan(rationale=arguments['question']), evidence, catalog)
    request = captured[-1]
    for name in ('requests', 'responses'):
        (mailbox / name).mkdir()
    (mailbox / 'requests' / (request['request_id'] + '.json')).write_text(json.dumps(request))
    root_ref = request['context']['_context']['source']['evidence_ref']
    root = store.get('context_evidence', root_ref)['value']
    response = {key: request[key] for key in ('protocol_version', 'transport', 'request_id', 'request_hash', 'model')}
    response['result'] = {'_context_action': {'kind': 'read', 'ref': root_ref, 'offset': 0, 'limit': 1}}
    response_bytes = json.dumps(response).encode()
    (mailbox / 'responses' / (request['request_id'] + '.json')).write_bytes(response_bytes)
    runtime = SCRIPT.parents[1]
    with store.connect() as con:
        state = mod.snapshot(con)
        effects = [(r[0], r[1], json.loads(r[2])) for r in con.execute(
            'SELECT id,kind,payload FROM events WHERE id>=? ORDER BY id', (start_event,))]
        totals = [list(r) for r in con.execute('SELECT resource,state,sum(amount) FROM charges GROUP BY resource,state ORDER BY resource,state')]
    recipe = {'step': step, 'run': run_id, 'scope': scope, 'request_id': request['request_id'],
        'request_hash': request['request_hash'], 'response_sha256': hashlib.sha256(response_bytes).hexdigest(),
        'source_sha256': mod.source_hash(runtime), 'state_sha256': mod.sha(state), 'round': 0,
        'start_event': start_event, 'implementation': implementation, 'replayed_requests': [request['request_id']],
        'cache_hits': tool_keys, 'model': bridge.model, 'history_count': 0, 'effect_sha256': mod.sha(effects),
        'job_count': len(tool_keys), 'charge_totals': totals, 'context_root_sha256': mod.sha(root)}
    recipe_path = tmp_path / 'synthetic-recipe.json'
    recipe_path.write_text(json.dumps(recipe))
    monkeypatch.setattr(mod, 'REVIEWED_RECIPES', {mod.sha(recipe)})
    return SimpleNamespace(campaign=campaign, runtime=runtime, mailbox=mailbox, recipe=recipe,
        recipe_path=recipe_path, store=store, request=request)


def test_synthetic_semantic_proof_apply_idempotent(semantic_case):
    case = semantic_case
    before = dump(case)
    dry = run(case)
    assert dry['status'] == 'validated_dry_run'
    assert dump(case) == before
    usage = case.store.usage()
    assert run(case, apply=True, expected=dry['proof_hash'])['status'] == 'reconciled'
    assert run(case, apply=True, expected=dry['proof_hash'])['status'] == 'already_reconciled'
    assert case.store.usage() == usage
    assert case.store.get('llm', 'llm-' + case.request['request_id'])['state'] == 'pending'


@pytest.mark.parametrize('change', ['state', 'response', 'source', 'request', 'charge',
    'cache_hit', 'unexpected_effect', 'scope', 'tool_pending', 'empirical_value'])
def test_synthetic_semantic_mutation_refused_without_writes(semantic_case, change, tmp_path):
    case = semantic_case
    if change == 'response':
        path = case.mailbox / 'responses' / (case.request['request_id'] + '.json')
        path.write_bytes(path.read_bytes() + b' ')
    elif change == 'request':
        path = case.mailbox / 'requests' / (case.request['request_id'] + '.json')
        payload = json.loads(path.read_text())
        payload['model'] = 'other-fixture-model'
        path.write_text(json.dumps(payload))
    elif change == 'source':
        case.runtime = tmp_path / 'wrong-runtime'
        case.runtime.mkdir()
    elif change == 'state':
        case.store.put('scheduler', 'fixture', {'changed': True})
    elif change == 'cache_hit':
        case.store.event('llm_cache_hit', {'key': 'llm-unexpected', 'role': 'B'})
    elif change == 'unexpected_effect':
        case.store.event('tool_started', {'tool': 'synthetic'})
    elif change == 'scope':
        journal = case.store.get(mod.NAMESPACE, case.recipe['run'])
        case.store.put(mod.NAMESPACE, case.recipe['run'], {**journal, 'scope': 'changed'})
    elif change == 'tool_pending':
        case.store.put('tool_jobs', 'fixture-job', {'state': 'started'})
    elif change == 'empirical_value':
        case.store.put('measurements', 'synthetic', {'synthetic_only': True})
    elif change == 'charge':
        with case.store.connect() as con:
            con.execute('UPDATE charges SET amount=2')
    before = dump(case)
    with pytest.raises(mod.ReconciliationError):
        run(case)
    assert dump(case) == before


def repin_synthetic_recipe(case, monkeypatch, *, effects=False):
    """Tests alone can approve changed synthetic metadata to reach inner guards."""
    with case.store.connect() as con:
        case.recipe['state_sha256'] = mod.sha(mod.snapshot(con))
        if effects:
            rows = [(r[0], r[1], json.loads(r[2])) for r in con.execute(
                'SELECT id,kind,payload FROM events WHERE id>=? ORDER BY id',
                (case.recipe['start_event'],))]
            case.recipe['effect_sha256'] = mod.sha(rows)
    case.recipe_path.write_text(json.dumps(case.recipe))
    monkeypatch.setattr(mod, 'REVIEWED_RECIPES', {mod.sha(case.recipe)})


@pytest.mark.parametrize('damage,reason', [
    ('receipt', 'Receipt'), ('pending_fingerprint', 'charge'),
    ('unexpected_effect', 'Unexpected effect'), ('cache_sequence', 'tool effect sequence'),
    ('replay_sequence', 'continuation differs'),
])
def test_semantic_guards_beyond_snapshot_pin(semantic_case, monkeypatch, damage, reason):
    case = semantic_case
    if damage == 'receipt':
        case.store.put(mod.NAMESPACE, case.recipe['step'], {'status': 'done'})
    elif damage == 'pending_fingerprint':
        with case.store.connect() as con:
            con.execute('UPDATE charges SET fingerprint=? WHERE key=?',
                ('synthetic-changed', 'llm-' + case.recipe['request_id']))
    elif damage == 'unexpected_effect':
        case.store.event('experiment_started', {'synthetic_only': True})
    elif damage == 'cache_sequence':
        case.recipe['cache_hits'] = ['tool-not-replayed']
    elif damage == 'replay_sequence':
        case.recipe['replayed_requests'] = ['assistant-not-replayed']
    repin_synthetic_recipe(case, monkeypatch, effects=True)
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match=reason):
        run(case)
    assert dump(case) == before


@pytest.mark.parametrize('semantic_case', [True], indirect=True)
def test_tool_job_cache_corruption_rejected_beyond_snapshot_pin(semantic_case, monkeypatch):
    case = semantic_case
    key = case.recipe['cache_hits'][0]
    job = case.store.get('tool_jobs', key)
    job['call']['arguments']['code'] += '\n# changed synthetic program'
    case.store.put('tool_jobs', key, job)
    repin_synthetic_recipe(case, monkeypatch)
    before = dump(case)
    with pytest.raises(mod.ReconciliationError, match='cache fingerprint'):
        run(case)
    assert dump(case) == before


@pytest.mark.parametrize('external', ['source', 'mailbox', 'recipe'])
def test_late_external_input_change_aborts_before_writes(case, monkeypatch, external):
    proof = run(case)['proof_hash']
    if external == 'source':
        monkeypatch.setattr(mod, 'source_hash', lambda *args: 'changed')
    elif external == 'mailbox':
        def changed_mailbox(*args):
            raise mod.ReconciliationError('Mailbox changed during validation')
        monkeypatch.setattr(mod, 'validate_mailbox', changed_mailbox)
    else:
        original = mod.read_recipe
        count = []
        def changed_recipe(*args):
            count.append(1)
            result = original(*args)
            return result if len(count) == 1 else {**result, 'scope': 'changed'}
        monkeypatch.setattr(mod, 'read_recipe', changed_recipe)
    before = dump(case)
    with pytest.raises(mod.ReconciliationError):
        run(case, apply=True, expected=proof)
    assert dump(case) == before


@pytest.mark.parametrize('semantic_case', ['sandbox'], indirect=True)
def test_real_bwrap_cache_replay_without_reexecution(semantic_case, monkeypatch):
    case = semantic_case
    key = case.recipe['cache_hits'][0]
    result = case.store.get('tool_jobs', key)['result']
    assert result['status'] == 'ok'
    assert result['execution_backend'] == 'bwrap_seccomp_generated_v1'
    assert result['output'] == {'synthetic': True}
    def forbidden(*args, **kwargs):
        raise AssertionError('Reconciliation must never execute generated code')
    monkeypatch.setattr('proteinrsi.research.code.execute_code', forbidden)
    before = dump(case)
    usage = case.store.usage()
    proof = run(case)['proof_hash']
    assert dump(case) == before
    assert run(case, apply=True, expected=proof)['status'] == 'reconciled'
    assert case.store.usage() == usage
    assert case.store.get('tool_jobs', key)['result'] == result
