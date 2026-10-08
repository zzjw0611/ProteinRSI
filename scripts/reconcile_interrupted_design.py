#!/usr/bin/env python3
"""One-case operator reconciliation; never a general-purpose started-receipt reset.

Default is read-only. Apply requires the exact dry-run proof hash. This utility
is intentionally pinned to the independently diagnosed TrpB3A interruption.
It reads revealed TaskView and execution metadata only, never measurement rows.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

SOURCE_HASH = '2d9dafe3a9640af7d61a1ff05618f23a56fee9107c1f3614358f53ac0a209869'
STEP = 'protocol-step:8a870b69d86c6a07781d30c934a7c8c3f8f560301151a704f738c24519b4104b'
RUN = 'protocol-run:d798ecf7a16ba4070646e62dcebcb700b9bc0d2258470bac38e170c10961dbbb'
REQUEST = 'assistant-d31b08cf0fe83f7ef8408928e3d0a0219711fdb9b739e46de3adf4dd444c24b2'
REQUEST_HASH = 'eed4de02cb1c19310469ce8f95c97abbec014198ce79281e2d7d3070707220c8'
RESPONSE_HASH = 'eaf98a4252a8659363ed5f0455a0ed433e84ff76fa25bbc837e160774082820b'
MODEL = 'interactive-assistant-session'
# Exact read-only metadata/ledger/audit snapshot approved for this one interruption.
STATE_HASH = '092cd6f45bada2237fc79a249bebd9e0d1e5e6317cff63a0876d01eee9df5b00'
SCOPE = 'e9ad2c86324451b6e9f356b07119e0edaa9f4f457924281ad6c67e47288cd798'
START_EVENT = 233
NAMESPACE = 'research_step_outputs'
RECORD = 'operator-reconciliation:' + STEP
# No oracle/replay data or measurement payload is read or copied.
PROOF_NAMESPACES = ('research_step_outputs', 'context_evidence', 'context_frames',
    'context_reads', 'context_sessions', 'research_notes', 'llm', 'llm_attempts',
    'assistant_bridge', 'resource_selections', 'tool_jobs', 'configuration',
    'agent_snapshots', 'plate_plans', 'batch_method_bindings', 'method_snapshots',
    'method_activations', 'workflow_versions', 'meta_versions')
ALLOWED_AFTER_START = {'research_step_started', 'context_prepared', 'llm_started',
    'llm_bridge_waiting', 'llm_completed', 'context_read_completed', 'run_started',
    'replay_started', 'agent_state', 'plate_completion_requested',
    'research_plan_created', 'research_blocked', 'run_failed'}


class ReconciliationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReconciliationError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_hash(root):
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root / 'src').rglob('*'))
             if p.is_file() and p.suffix in {'.py', '.md', '.json'}}
    return sha(files)


class ReadOnlyStore:
    """Runtime reconstruction permits only exact duplicate immutable writes."""
    def __init__(self, con):
        self.con = con

    def get(self, namespace, key, default=None):
        row = self.con.execute('SELECT value FROM kv WHERE namespace=? AND key=?',
                               (namespace, key)).fetchone()
        return json.loads(row[0]) if row else deepcopy(default)

    def put(self, namespace, key, value, **kwargs):
        require(self.get(namespace, key) == value,
                f'Reconstruction would change {namespace}/{key}')

    def event(self, *args, **kwargs):
        pass  # Read-only simulation, never emit fictitious runtime events.

    @contextmanager
    def connect(self):
        yield self.con


@contextmanager
def campaign_lock(campaign):
    # Existing file only; no creation, database writes, or truncation in dry-run.
    with (campaign / '.campaign.lock').open('rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconciliationError('A campaign writer holds the lock') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def no_live_controller(campaign):
    # Lock is authoritative for normal runtime. Also reject an observable orphan
    # whose command line targets this campaign without holding the standard lock.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise ReconciliationError('Cannot establish absence of live controller') from exc
        if any(str(campaign).encode() == arg for arg in args):
            raise ReconciliationError(f'Live process targets campaign: {entry.name}')


def read_mailbox_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 8 * 1024 * 1024,
                'Mailbox must be a bounded regular file without links')
        return handle.read(8 * 1024 * 1024 + 1)


def snapshot(con):
    marks = ','.join('?' for _ in PROOF_NAMESPACES)
    rows = con.execute(f'SELECT namespace,key,value FROM kv WHERE namespace IN ({marks}) '
                       'ORDER BY namespace,key', PROOF_NAMESPACES).fetchall()
    return {'kv_sha256': sha([tuple(r) for r in rows]),
        'charges': [tuple(r) for r in con.execute('SELECT * FROM charges ORDER BY key')],
        'limits': [tuple(r) for r in con.execute('SELECT * FROM limits ORDER BY resource')],
        'events_sha256': sha([tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')]),
        'event_count': con.execute('SELECT count(*) FROM events').fetchone()[0],
        # Keys/counts only, never values from measurements or empirical batches.
        'empirical_keys': [tuple(r) for r in con.execute(
            "SELECT namespace,key FROM kv WHERE namespace IN ('measurements','batches') ORDER BY namespace,key")]}


def validate(con, runtime, mailbox, campaign):
    require(source_hash(runtime) == SOURCE_HASH, 'Runtime source fingerprint mismatch')
    no_live_controller(campaign)
    store = ReadOnlyStore(con)
    require(store.get(NAMESPACE, STEP) == {'status': 'started'}, 'Receipt is not the exact interrupted started receipt')
    require(store.get(NAMESPACE, RECORD) is None, 'Existing reconciliation requires inspection')
    journal = store.get(NAMESPACE, RUN)
    require(journal is not None and journal.get('scope') == SCOPE and journal.get('completed') == []
            and journal.get('failed_step') == 'design' and journal.get('status') == 'blocked'
            and journal.get('validation_error', {}).get('code') == 'uncertain_completion', 'Run journal mismatch')
    require(not journal.get('active_protocol') and not journal.get('revisions')
            and not journal.get('repairs') and not journal.get('refs'), 'Unexpected protocol continuation')
    pending = store.get('llm', 'llm-' + REQUEST)
    require(pending is not None and pending.get('state') == 'pending'
            and pending.get('request_hash') == REQUEST_HASH and pending.get('model') == MODEL
            and pending.get('role') == 'B' and pending.get('round') == 3
            and pending.get('transport') == 'assistant_bridge', 'Pending bridge record mismatch')
    require(con.execute("SELECT count(*) FROM kv WHERE namespace='llm' AND json_extract(value,'$.state')='pending'").fetchone()[0] == 1,
            'There is not exactly one pending LLM request')
    request = pending['request']
    require(request['request_id'] == REQUEST and request['request_hash'] == REQUEST_HASH
            and request['model'] == MODEL, 'Request identity mismatch')
    req_path = mailbox / 'requests' / (REQUEST + '.json')
    resp_path = mailbox / 'responses' / (REQUEST + '.json')
    for path in (req_path, resp_path):
        require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
                'Mailbox file is missing, linked, or not regular')
    require(json.loads(read_mailbox_file(req_path)) == request, 'Mailbox request differs from durable request')
    response_bytes = read_mailbox_file(resp_path)
    require(hashlib.sha256(response_bytes).hexdigest() == RESPONSE_HASH, 'Response byte hash mismatch')
    response = json.loads(response_bytes)
    require(set(response) == {'protocol_version', 'transport', 'request_id', 'request_hash', 'model', 'result'},
            'Unexpected response envelope fields')
    require(all(type(response[k]) is type(request[k]) and response[k] == request[k]
                for k in response if k != 'result'), 'Response identity mismatch')
    from jsonschema import Draft202012Validator
    Draft202012Validator(request['schema']).validate(response['result'])
    require(response['result'].get('tool_calls') and not response['result'].get('_context_action'),
            'Response is not the diagnosed unconsumed tool request')
    # Import only after verifying the pinned source. Do not accept already-loaded
    # proteinrsi modules from another checkout in an embedded invocation.
    sys.path.insert(0, str(runtime / 'src'))
    for name, module in list(sys.modules.items()):
        if name == 'proteinrsi' or name.startswith('proteinrsi.'):
            path = getattr(module, '__file__', None)
            require(path and Path(path).resolve().is_relative_to(runtime / 'src'), 'Foreign runtime module loaded')
    from proteinrsi.contracts import TaskView, digest
    from proteinrsi.agents import DesignerAgent, Plan
    from proteinrsi.research.context import complete, INSTRUCTIONS
    from proteinrsi.dataflow.resources import scope_for, standard_registry, SEQUENCES
    require(request['instructions'].endswith(INSTRUCTIONS), 'Context policy instructions mismatch')
    session = request['context']['_context']['session']
    root = store.get('context_evidence', request['context']['_context']['source']['evidence_ref'])['value']
    view_rows = con.execute("SELECT value FROM kv WHERE namespace=? AND json_extract(value,'$.scope')=? "
        "AND json_extract(value,'$.schema_ref')='context.task/v1'", (NAMESPACE, SCOPE)).fetchall()
    require(len(view_rows) == 1, 'Task-view input is not unique')
    view = TaskView.model_validate(json.loads(view_rows[0][0])['data'])
    require(scope_for(view) == SCOPE and view.round_index == 3, 'Revealed view scope mismatch')
    protocol = journal['protocol']
    require(len(protocol['steps']) == 1 and not protocol['agent_operations'], 'Unexpected protocol shape')
    step = protocol['steps'][0]
    require(step['operation'] == 'agent:propose' and step['step_id'] == 'design'
            and not step['bindings'] and not step['depends_on'] and not step['outputs'], 'Step is outside narrow recovery scope')
    evidence = root['tool_results']
    require(len(evidence) == 1 and set(evidence[0]) == {'method_knowledge'}, 'Unexpected prior design tool effects')
    knowledge = evidence[0]['method_knowledge']
    require(any(json.loads(row[0]).get('method_knowledge') == knowledge for row in con.execute(
        "SELECT value FROM kv WHERE namespace='resource_selections'")), 'Method knowledge not in durable selection')
    implementation = 'typed-designer-v3-final-selection:' + digest(knowledge)
    calculated = 'protocol-step:' + digest({'scope': SCOPE, 'operation': 'agent:propose',
        'implementation': implementation, 'arguments': step['arguments'], 'parents': [],
        'output_schema': standard_registry().fingerprint(SEQUENCES)})
    require(calculated == STEP, 'Operation implementation/arguments/schema mismatch')
    scoped = [json.loads(row[0]) for row in con.execute("SELECT value FROM kv WHERE namespace=? "
        "AND key LIKE 'resource:%' AND json_extract(value,'$.scope')=?", (NAMESPACE, SCOPE))]
    require(len(scoped) == 1 and scoped[0]['producer'] == 'task-view', 'Step has an output or other resource effect')
    jobs = [json.loads(r[0]) for r in con.execute("SELECT value FROM kv WHERE namespace='tool_jobs'")]
    require(len(jobs) == 3 and all(j.get('state') == 'done' for j in jobs), 'Unexpected tool-job state')
    effects = [(r[0], r[1], json.loads(r[2])) for r in con.execute(
        'SELECT id,kind,payload FROM events WHERE id>=? ORDER BY id', (START_EVENT,))]
    require(effects and effects[0][0] == START_EVENT and effects[0][1] == 'research_step_started'
            and effects[0][2].get('run_id') == RUN and effects[0][2].get('cache_hit') is False,
            'Original step-start audit mismatch')
    require(all(kind in ALLOWED_AFTER_START for _, kind, _ in effects), 'Unexpected effect after step started')
    completed = [p for _, k, p in effects if k == 'llm_completed']
    require(len(completed) == 1 and completed[0]['request_id'] ==
        'assistant-afa32c6c7cab36c5b33dd2afe9ddacdf320b2d1f82022c367a4c59e45e249ae4', 'Unexpected accepted request')
    amounts = [tuple(r) for r in con.execute('SELECT resource,state,sum(amount) FROM charges GROUP BY resource,state ORDER BY resource,state')]
    require(amounts == [('experimental_wells', 'committed', 300), ('llm_calls', 'committed', 38),
                        ('tool_calls', 'committed', 3)], 'Charge ledger differs from diagnosed interruption')
    charge = con.execute('SELECT resource,amount,state,fingerprint FROM charges WHERE key=?', ('llm-' + REQUEST,)).fetchone()
    require(charge is not None and tuple(charge) == ('llm_calls', 1, 'committed', digest(request)), 'Pending request charge missing or different')
    scope = store.get('assistant_bridge', 'scope')
    replay = []
    class ReachedPending(Exception):
        pass
    class MatchedEntry(Exception):
        pass
    class ReplayedLLM:
        model = MODEL
        base_url = 'assistant-bridge://local'
        cache_settings = {'llm_transport': 'assistant_bridge', 'llm_bridge_protocol': request['protocol_version']}
        def __init__(self): self.store = store
        def complete(self, role, instructions, context, schema):
            require(role == 'B' and instructions + INSTRUCTIONS == request['instructions']
                    and context == root and schema == request['schema']['anyOf'][0], 'Designer entry changed')
            raise MatchedEntry
        def _complete(self, role, instructions, context, schema):
            payload = {'protocol_version': request['protocol_version'], 'transport': 'assistant_bridge',
                'model': MODEL, 'role': role, 'instructions': instructions, 'context': context, 'schema': schema}
            h = digest(payload)
            req = {**payload, 'request_id': 'assistant-' + digest({'scope': scope, 'request_hash': h}), 'request_hash': h}
            saved = store.get('llm', 'llm-' + req['request_id'])
            require(saved is not None and saved['request'] == req, 'Evidence replay requires a different request')
            replay.append(req['request_id'])
            if saved['state'] == 'pending':
                require(req == request, 'Evidence replay reached another pending request')
                raise ReachedPending
            require(saved['state'] == 'done' and '_context_action' in saved['result'], 'Unexpected earlier decision')
            return saved['result']
    llm = ReplayedLLM()
    try:
        DesignerAgent(llm, store).propose(view, Plan(rationale=step['arguments']['question']), evidence, root['available_tools'])
    except MatchedEntry:
        pass
    else:
        raise ReconciliationError('Designer returned without reproducing original entry')
    try:
        complete(llm, 'B', request['instructions'][:-len(INSTRUCTIONS)], root, request['schema']['anyOf'][0])
    except ReachedPending:
        pass
    else:
        raise ReconciliationError('Evidence replay did not reach pending request')
    require(replay == ['assistant-afa32c6c7cab36c5b33dd2afe9ddacdf320b2d1f82022c367a4c59e45e249ae4', REQUEST], 'Context continuation differs')
    state = snapshot(con)
    require(sha(state) == STATE_HASH, 'Metadata/ledger/audit snapshot differs from the diagnosed interruption')
    return {'version': 'trpb3a-interruption-reconciliation-v1', 'step': STEP, 'run': RUN,
        'scope': SCOPE, 'source_sha256': SOURCE_HASH, 'implementation': implementation,
        'request_id': REQUEST, 'request_hash': REQUEST_HASH, 'response_sha256': RESPONSE_HASH,
        'model': MODEL, 'context_session': session, 'replayed_requests': replay,
        'original_receipt': {'status': 'started'}, 'snapshot': state}


def reconcile(campaign, runtime, mailbox, *, apply=False, expected=None):
    campaign, runtime, mailbox = (Path(p).resolve(strict=True) for p in (campaign, runtime, mailbox))
    require((campaign / 'state.sqlite3').is_file(), 'Campaign database missing')
    require(not apply or expected is not None, 'Apply requires the dry-run proof hash')
    with campaign_lock(campaign):
        con = sqlite3.connect((campaign / 'state.sqlite3').as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            require(not con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall(),
                    'Unexpected database triggers; refusing reconciliation')
            saved = ReadOnlyStore(con).get(NAMESPACE, RECORD)
            if saved is not None:
                require(expected is not None and saved.get('proof_hash') == expected,
                        'Existing reconciliation has a different or unspecified proof')
                require(saved.get('proof', {}).get('step') == STEP and saved.get('original_receipt') == {'status': 'started'}
                        and sha(saved['proof']) == expected, 'Reconciliation evidence damaged')
                receipt = ReadOnlyStore(con).get(NAMESPACE, STEP)
                expected_receipt = {'status': 'paused_provider', 'error_type': 'ReconciledInterruptedAssistantBridge',
                                    'reconciliation_ref': RECORD}
                require(receipt == expected_receipt, 'Receipt advanced or changed; inspect ordinary resume separately')
                audits = [json.loads(r[0]) for r in con.execute(
                    "SELECT payload FROM events WHERE kind='operator_interrupted_design_reconciled'")]
                matching = [a for a in audits if a.get('record_ref') == RECORD]
                require(len(matching) == 1 and matching[0].get('proof_hash') == expected
                        and matching[0].get('step') == STEP and matching[0].get('from') == 'started'
                        and matching[0].get('to') == 'paused_provider', 'Reconciliation audit missing or inconsistent')
                con.rollback()
                return {'status': 'already_reconciled', 'proof_hash': expected, 'no_changes': True}
            proof = validate(con, runtime, mailbox, campaign)
            proof_hash = sha(proof)
            require(expected is None or expected == proof_hash, 'Dry-run proof is stale or does not match')
            if not apply:
                con.rollback()
                return {'status': 'validated_dry_run', 'proof_hash': proof_hash, 'proof': proof, 'no_changes': True}
            evidence = {'proof_hash': proof_hash, 'proof': proof,
                        'original_receipt': {'status': 'started'}, 'recorded_at': time.time()}
            # Plain INSERT and CAS: immutable evidence, no replace/delete semantics.
            con.execute('INSERT INTO kv(namespace,key,value) VALUES(?,?,?)', (NAMESPACE, RECORD, canonical(evidence)))
            before = con.execute('SELECT value FROM kv WHERE namespace=? AND key=?', (NAMESPACE, STEP)).fetchone()[0]
            require(json.loads(before) == {'status': 'started'}, 'Receipt changed during reconciliation')
            after = {'status': 'paused_provider', 'error_type': 'ReconciledInterruptedAssistantBridge',
                     'reconciliation_ref': RECORD}
            changed = con.execute('UPDATE kv SET value=? WHERE namespace=? AND key=? AND value=?',
                                  (canonical(after), NAMESPACE, STEP, before)).rowcount
            require(changed == 1, 'Compare-and-swap failed')
            con.execute('INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)',
                        (time.time(), 'operator_interrupted_design_reconciled', canonical({
                            'step': STEP, 'run': RUN, 'request_id': REQUEST, 'proof_hash': proof_hash,
                            'record_ref': RECORD, 'from': 'started', 'to': 'paused_provider',
                            'request_reused': True, 'budget_reset': False})))
            con.commit()
            return {'status': 'reconciled', 'proof_hash': proof_hash, 'record_ref': RECORD,
                    'next': 'Resume through the unchanged normal runtime; no tool has been executed by this utility.'}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--runtime-root', required=True, type=Path)
    parser.add_argument('--mailbox', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-proof-sha256')
    args = parser.parse_args()
    try:
        result = reconcile(args.campaign, args.runtime_root, args.mailbox,
                           apply=args.apply, expected=args.expected_proof_sha256)
    except Exception as exc:
        print(canonical({'status': 'rejected', 'error_type': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        return 1
    print(canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
