#!/usr/bin/env python3
"""Reconcile only two reviewed interruption recipes, never arbitrary started work.

The private recipes and proof outputs are deliberately not published. Their
canonical hashes are fixed below; changing a recipe requires a separate review
and code change. Dry-run is read-only; apply needs its exact proof hash. Only a
single started receipt can become paused_provider, with immutable original
receipt evidence. The unmodified scheduler decides what happens on resume.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.dont_write_bytecode = True

# Reuse only low-level safety primitives, never the original case's validator.
_spec = importlib.util.spec_from_file_location(
    '_original_reconciliation_safety', Path(__file__).with_name('reconcile_interrupted_design.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
ReconciliationError = _base.ReconciliationError
require = _base.require
canonical = _base.canonical
sha = _base.sha
source_hash = _base.source_hash
campaign_lock = _base.campaign_lock
no_live_controller = _base.no_live_controller
read_mailbox_file = _base.read_mailbox_file
NAMESPACE = 'research_step_outputs'
SOURCE_HASH = '2d9dafe3a9640af7d61a1ff05618f23a56fee9107c1f3614358f53ac0a209869'
REVIEWED_RECIPES = frozenset({
    '88028e7efd1264021874d8b1f881738d61a3412be368d3390e16ed81821c6161',
    'ce8ed127d6fc6fe275635c58658b7ad8faa46af2c3758c1f914313bce9a8a08f',
})
ALLOWED_EFFECTS = frozenset({'research_step_started', 'context_prepared', 'llm_started',
    'llm_bridge_waiting', 'llm_completed', 'context_read_completed', 'tool_requested',
    'tool_completed', 'generated_code_completed', 'resource_created'})


def read_recipe(path):
    recipe = json.loads(read_mailbox_file(Path(path)))
    require(sha(recipe) in REVIEWED_RECIPES, 'Recipe has not been independently reviewed')
    require(recipe['source_sha256'] == SOURCE_HASH, 'Recipe targets an unaccepted runtime')
    return recipe


def snapshot(con):
    """Fingerprint every durable row, including scheduler/history and empirical data.

    Values are hashed inside SQLite; empirical values are never returned, parsed,
    printed, or used by reconstruction. No external measurement file is opened.
    """
    con.create_function('_reconcile_sha256', 1,
        lambda value: hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest())
    return {
        'kv_sha256': sha([tuple(r) for r in con.execute(
            'SELECT namespace,key,_reconcile_sha256(value) FROM kv ORDER BY namespace,key')]),
        'charges': [tuple(r) for r in con.execute('SELECT * FROM charges ORDER BY key')],
        'limits': [tuple(r) for r in con.execute('SELECT * FROM limits ORDER BY resource')],
        'events_sha256': sha([tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')]),
        'event_count': con.execute('SELECT count(*) FROM events').fetchone()[0],
        'schema_sha256': sha([tuple(r) for r in con.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]),
        'empirical_sha256': sha([tuple(r) for r in con.execute(
            "SELECT namespace,key,_reconcile_sha256(value) FROM kv "
            "WHERE namespace IN ('batches','measurements') ORDER BY namespace,key")]),
    }


class ReplayStore(_base.ReadOnlyStore):
    def __init__(self, con):
        super().__init__(con)
        self.cache_hits = []

    def reserve(self, *args, **kwargs):
        raise ReconciliationError('Replay would create a charge or execute a tool')

    def settle(self, *args, **kwargs):
        raise ReconciliationError('Replay would change a charge')

    def event(self, kind, payload):
        if kind == 'tool_requested':
            require(payload.get('cache_hit') is True, 'Replay encountered a tool cache miss')
            self.cache_hits.append(payload['key'])
        else:
            require(kind in {'context_prepared', 'context_read_completed'},
                    'Unexpected reconstruction effect: ' + kind)


def validate_mailbox(store, mailbox, recipe):
    request_id = recipe['request_id']
    pending = store.get('llm', 'llm-' + request_id)
    require(pending is not None and pending.get('state') == 'pending'
            and pending.get('request_hash') == recipe['request_hash']
            and pending.get('model') == recipe['model'] and pending.get('role') == 'B'
            and pending.get('round') == recipe['round']
            and pending.get('transport') == 'assistant_bridge', 'Pending bridge record mismatch')
    request = pending['request']
    require(request['request_id'] == request_id and request['request_hash'] == recipe['request_hash']
            and request['model'] == recipe['model'], 'Request identity mismatch')
    require(json.loads(read_mailbox_file(mailbox / 'requests' / (request_id + '.json'))) == request,
            'Mailbox request differs from durable request')
    response_bytes = read_mailbox_file(mailbox / 'responses' / (request_id + '.json'))
    require(hashlib.sha256(response_bytes).hexdigest() == recipe['response_sha256'],
            'Response byte hash mismatch')
    response = json.loads(response_bytes)
    require(set(response) == {'protocol_version', 'transport', 'request_id', 'request_hash', 'model', 'result'},
            'Unexpected response envelope fields')
    require(all(type(response[k]) is type(request[k]) and response[k] == request[k]
                for k in response if k != 'result'), 'Response identity mismatch')
    from jsonschema import Draft202012Validator
    Draft202012Validator(request['schema']).validate(response['result'])
    require(isinstance(response['result'].get('_context_action'), dict)
            and response['result']['_context_action'].get('kind') == 'read',
            'Response is not the reviewed genuine context-read reply')
    return request


def validate(con, runtime, mailbox, campaign, recipe):
    require(source_hash(runtime) == SOURCE_HASH == recipe['source_sha256'],
            'Runtime source fingerprint mismatch')
    no_live_controller(campaign)
    state = snapshot(con)
    require(sha(state) == recipe['state_sha256'], 'Metadata/ledger/history snapshot differs')
    store = ReplayStore(con)
    step_id, run_id, scope = (recipe[k] for k in ('step', 'run', 'scope'))
    require(store.get(NAMESPACE, step_id) == {'status': 'started'}, 'Receipt is not exactly started')
    journal = store.get(NAMESPACE, run_id)
    require(journal and journal.get('scope') == scope and journal.get('status') == 'running'
            and journal.get('completed') == [] and journal.get('reviewed') == []
            and journal.get('revisions') == [], 'Run journal mismatch')
    require(not any(journal.get(k) for k in ('active_protocol', 'repairs', 'refs', 'failed_step', 'validation_error')),
            'Unexpected protocol continuation')
    protocol = journal['protocol']
    require(len(protocol['steps']) == 1 and not protocol['agent_operations'], 'Unexpected protocol shape')
    step = protocol['steps'][0]
    require(step['operation'] == 'agent:propose' and step['step_id'] == 'design'
            and not step['bindings'] and not step['depends_on'] and not step['outputs'],
            'Step is outside narrow recovery scope')
    require(con.execute("SELECT count(*) FROM kv WHERE namespace='llm' "
        "AND json_extract(value,'$.state')='pending'").fetchone()[0] == 1,
        'There is not exactly one pending request')
    request = validate_mailbox(store, mailbox, recipe)
    # Verify before importing, and refuse previously loaded foreign runtime modules.
    sys.path.insert(0, str(runtime / 'src'))
    for name, module in list(sys.modules.items()):
        if name == 'proteinrsi' or name.startswith('proteinrsi.'):
            path = getattr(module, '__file__', None)
            require(path and Path(path).resolve().is_relative_to(runtime / 'src'),
                    'Foreign runtime module loaded')
    from proteinrsi.contracts import TaskView, digest
    from proteinrsi.agents import DesignerAgent, Plan
    from proteinrsi.research.context import complete
    from proteinrsi.tools import ToolGateway, ToolSpec
    from proteinrsi.dataflow.resources import standard_registry, SEQUENCES, scope_for
    from jsonschema import Draft202012Validator
    view_rows = con.execute("SELECT value FROM kv WHERE namespace=? AND json_extract(value,'$.scope')=? "
        "AND json_extract(value,'$.schema_ref')='context.task/v1'", (NAMESPACE, scope)).fetchall()
    require(len(view_rows) == 1, 'Task-view input is not unique')
    view = TaskView.model_validate(json.loads(view_rows[0][0])['data'])
    require(scope_for(view) == scope and view.round_index == recipe['round']
            and len(view.history) == recipe['history_count'], 'Scientific scope/history mismatch')
    root = store.get('context_evidence', request['context']['_context']['source']['evidence_ref'])['value']
    require(sha(root) == recipe['context_root_sha256'], 'Context root mismatch')
    knowledge = root['tool_results'][0]['method_knowledge']
    implementation = 'typed-designer-v3-final-selection:' + digest(knowledge)
    require(implementation == recipe['implementation'], 'Implementation identity mismatch')
    calculated = 'protocol-step:' + digest({'scope': scope, 'operation': 'agent:propose',
        'implementation': implementation, 'arguments': step['arguments'], 'parents': [],
        'output_schema': standard_registry().fingerprint(SEQUENCES)})
    require(calculated == step_id, 'Operation arguments/schema mismatch')
    pending_charge = con.execute('SELECT resource,amount,state,fingerprint FROM charges WHERE key=?',
        ('llm-' + request['request_id'],)).fetchone()
    require(pending_charge is not None and tuple(pending_charge) ==
        ('llm_calls', 1, 'committed', digest(request)), 'Pending request charge mismatch')
    totals = [list(r) for r in con.execute('SELECT resource,state,sum(amount) FROM charges '
        'GROUP BY resource,state ORDER BY resource,state')]
    require(totals == recipe['charge_totals'], 'Charge totals mismatch')
    jobs = {k: json.loads(v) for k, v in con.execute("SELECT key,value FROM kv WHERE namespace='tool_jobs'")}
    require(len(jobs) == recipe['job_count'] and all(j.get('state') == 'done' for j in jobs.values()),
            'Unresolved or unexpected tool job')
    for key, job in jobs.items():
        payload = {k: job[k] for k in ('spec', 'call', 'context')}
        fingerprint = digest(payload)
        require(key == 'tool-' + fingerprint, 'Tool job cache fingerprint mismatch')
        charge = con.execute('SELECT resource,amount,state,fingerprint FROM charges WHERE key=?', (key,)).fetchone()
        require(charge is not None and tuple(charge) == ('tool_calls', 1, 'committed', fingerprint),
                'Tool charge fingerprint mismatch')
        Draft202012Validator(job['spec']['output_schema']).validate(job['result'])
    effects = [(r[0], r[1], json.loads(r[2])) for r in con.execute(
        'SELECT id,kind,payload FROM events WHERE id>=? ORDER BY id', (recipe['start_event'],))]
    require(sha(effects) == recipe['effect_sha256'], 'Interrupted effect history changed')
    require(effects and effects[0][0] == recipe['start_event']
            and effects[0][1] == 'research_step_started'
            and effects[0][2].get('run_id') == run_id
            and effects[0][2].get('cache_hit') is False, 'Original step-start audit mismatch')
    require(all(kind in ALLOWED_EFFECTS for _, kind, _ in effects), 'Unexpected effect after step started')
    requested = [p['key'] for _, k, p in effects if k == 'tool_requested']
    completed = [p['key'] for _, k, p in effects if k == 'tool_completed']
    require(requested == completed == recipe['cache_hits'], 'Completed tool effect sequence mismatch')
    generated = [p for _, k, p in effects if k == 'generated_code_completed']
    require(len(generated) == len(recipe['cache_hits']) and all(
        p.get('status') == 'ok' and p.get('execution_backend') == 'bwrap_seccomp_generated_v1'
        for p in generated), 'Generated tool completion proof mismatch')
    for key, result in zip(recipe['cache_hits'], generated):
        require(jobs[key]['call']['name'] == 'research_python'
            and result['code_sha256'] == digest(jobs[key]['call']['arguments']['code']),
            'Generated code identity mismatch')
    class ReachedPending(Exception):
        pass
    class ReplayedLLM:
        model = recipe['model']
        base_url = 'assistant-bridge://local'
        cache_settings = {'llm_transport': 'assistant_bridge', 'llm_bridge_protocol': request['protocol_version']}
        def __init__(self):
            self.store = store
            self.requests = []
        def complete(self, role, instructions, context, schema):
            return complete(self, role, instructions, context, schema)
        def _complete(self, role, instructions, context, schema):
            payload = {'protocol_version': request['protocol_version'], 'transport': 'assistant_bridge',
                'model': self.model, 'role': role, 'instructions': instructions, 'context': context, 'schema': schema}
            h = digest(payload)
            req = {**payload, 'request_hash': h, 'request_id': 'assistant-' + digest({
                'scope': store.get('assistant_bridge', 'scope'), 'request_hash': h})}
            saved = store.get('llm', 'llm-' + req['request_id'])
            require(saved is not None and saved['request'] == req, 'Replay requires a different request')
            self.requests.append(req['request_id'])
            if saved['state'] == 'pending':
                require(req == request, 'Replay reached another pending request')
                raise ReachedPending
            require(saved['state'] == 'done', 'Replay requires an unfinished response')
            return saved['result']
    llm = ReplayedLLM()
    designer = DesignerAgent(llm, store)
    gateway = ToolGateway(store)
    catalog = root['available_tools']
    def forbidden(_arguments):
        raise ReconciliationError('Forbidden new tool execution')
    for item in catalog:
        gateway.register(ToolSpec.model_validate(item), forbidden)
    evidence = [{'method_knowledge': knowledge}]
    try:
        for _ in range(view.workflow.design_tool_rounds):
            result = designer.propose(view, Plan(rationale=step['arguments']['question']), evidence, catalog)
            require(result.tool_calls, 'Design completed before exact pending request')
            for call in result.tool_calls:
                evidence.append(gateway.call(call, view.task, allowed=[i['name'] for i in catalog], context_key=step_id))
    except ReachedPending:
        pass
    else:
        raise ReconciliationError('Replay did not reach exact pending request')
    require(llm.requests == recipe['replayed_requests'] and store.cache_hits == recipe['cache_hits'],
            'Context/tool cache continuation differs')
    require(snapshot(con) == state, 'Read-only reconstruction changed state')
    return {'version': 'reviewed-design-interruption-v1', 'recipe_sha256': sha(recipe),
        'step': step_id, 'run': run_id, 'scope': scope, 'source_sha256': SOURCE_HASH,
        'implementation': implementation, 'request_id': request['request_id'],
        'request_hash': request['request_hash'], 'response_sha256': recipe['response_sha256'],
        'original_receipt': {'status': 'started'},
        'original_receipt_sha256': hashlib.sha256(con.execute(
            'SELECT value FROM kv WHERE namespace=? AND key=?', (NAMESPACE, step_id)).fetchone()[0].encode()).hexdigest(),
        'replayed_requests': llm.requests,
        'cache_hits': store.cache_hits, 'snapshot': state,
        'scheduler_policy': 'unchanged; request reuse is not assumed; orphan charges remain charged'}


def reconcile(campaign, runtime, mailbox, recipe_path, *, apply=False, expected=None):
    campaign, runtime, mailbox = (Path(p).resolve(strict=True) for p in (campaign, runtime, mailbox))
    recipe = read_recipe(recipe_path)
    step = recipe['step']
    record = 'operator-reviewed-reconciliation:' + step
    require(not apply or expected is not None, 'Apply requires the dry-run proof hash')
    with campaign_lock(campaign):
        no_live_controller(campaign)
        con = sqlite3.connect((campaign / 'state.sqlite3').as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            require(not con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall(),
                    'Unexpected database triggers')
            store = _base.ReadOnlyStore(con)
            saved = store.get(NAMESPACE, record)
            after = {'status': 'paused_provider', 'error_type': 'ReconciledInterruptedAssistantBridge',
                     'reconciliation_ref': record}
            if saved is not None:
                require(expected is not None and saved.get('proof_hash') == expected,
                        'Existing reconciliation has a different or unspecified proof')
                require(saved.get('original_receipt') == {'status': 'started'}
                        and saved.get('proof', {}).get('recipe_sha256') == sha(recipe)
                        and sha(saved['proof']) == expected
                        and isinstance(saved.get('original_receipt_bytes'), str)
                        and hashlib.sha256(saved['original_receipt_bytes'].encode()).hexdigest()
                        == saved['proof'].get('original_receipt_sha256')
                        and json.loads(saved['original_receipt_bytes']) == saved['original_receipt'],
                        'Reconciliation evidence damaged')
                require(store.get(NAMESPACE, step) == after, 'Receipt advanced or changed; inspect ordinary resume separately')
                audits = [json.loads(r[0]) for r in con.execute(
                    "SELECT payload FROM events WHERE kind='operator_reviewed_design_reconciled'")]
                matches = [a for a in audits if a.get('record_ref') == record]
                require(len(matches) == 1 and matches[0] == audit_payload(recipe, expected, record),
                        'Reconciliation audit missing or inconsistent')
                con.rollback()
                return {'status': 'already_reconciled', 'proof_hash': expected, 'no_changes': True}
            proof = validate(con, runtime, mailbox, campaign, recipe)
            proof_hash = sha(proof)
            require(expected is None or expected == proof_hash, 'Dry-run proof is stale or does not match')
            if not apply:
                con.rollback()
                return {'status': 'validated_dry_run', 'proof_hash': proof_hash, 'proof': proof, 'no_changes': True}
            # Recheck external inputs immediately before the bounded transaction writes.
            require(read_recipe(recipe_path) == recipe, 'Recipe changed during validation')
            require(source_hash(runtime) == recipe['source_sha256'], 'Runtime source changed during validation')
            validate_mailbox(store, mailbox, recipe)
            no_live_controller(campaign)
            before = con.execute('SELECT value FROM kv WHERE namespace=? AND key=?', (NAMESPACE, step)).fetchone()[0]
            require(json.loads(before) == {'status': 'started'}, 'Receipt changed during reconciliation')
            evidence = {'proof_hash': proof_hash, 'proof': proof, 'original_receipt': {'status': 'started'},
                        'original_receipt_bytes': before, 'recorded_at': time.time()}
            con.execute('INSERT INTO kv(namespace,key,value) VALUES(?,?,?)', (NAMESPACE, record, canonical(evidence)))
            changed = con.execute('UPDATE kv SET value=? WHERE namespace=? AND key=? AND value=?',
                (canonical(after), NAMESPACE, step, before)).rowcount
            require(changed == 1, 'Compare-and-swap failed')
            con.execute('INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)',
                (time.time(), 'operator_reviewed_design_reconciled', canonical(audit_payload(recipe, proof_hash, record))))
            con.commit()
            return {'status': 'reconciled', 'proof_hash': proof_hash, 'record_ref': record,
                    'next': 'Use unchanged normal runtime; audit whether it reuses or orphans the charged request.'}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def audit_payload(recipe, proof_hash, record):
    return {'step': recipe['step'], 'run': recipe['run'], 'request_id': recipe['request_id'],
            'proof_hash': proof_hash, 'record_ref': record, 'from': 'started', 'to': 'paused_provider',
            'request_reuse': 'decided_by_unchanged_scheduler', 'budget_reset': False,
            'tool_executions': 0, 'response_consumed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--runtime-root', required=True, type=Path)
    parser.add_argument('--mailbox', required=True, type=Path)
    parser.add_argument('--recipe', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-proof-sha256')
    args = parser.parse_args()
    try:
        result = reconcile(args.campaign, args.runtime_root, args.mailbox, args.recipe,
            apply=args.apply, expected=args.expected_proof_sha256)
    except Exception as exc:
        print(canonical({'status': 'rejected', 'error_type': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        return 1
    print(canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
