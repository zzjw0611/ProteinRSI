#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Finish one existing C checkpoint on the original pinned runtime, then STOP.

Operator-only preparation for the reviewed selection-source upgrade. Dry-run is
read-only. This driver never plans/approves/measures a batch, invokes M, changes a
method/source pin, refunds charges, or answers a scientific decision itself.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

sys.dont_write_bytecode = True

OLD_COMMIT = '25312ffea4bde4241707a73216a8f13a0703682d'
OLD_FILES = 'f7a1b7e72029d044ae23ed088eab0f07ce6131e2e0b5ffcb1eb504193faae342'
CONFIG_KEYS = ('prompt_bundle', 'research', 'know_how', 'protein_model', 'local_tools')
LIMITS = {'experimental_wells': 2000, 'llm_calls': 600, 'tool_calls': 400}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _bundle():
    from importlib import metadata, util
    import platform
    from proteinrsi.governance import _packaged_source
    root = Path(util.find_spec('proteinrsi').origin).resolve(strict=True).parent
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Runtime source symlinks are unsupported')
        if path.is_file() and path.suffix in {'.py', '.md', '.json'}:
            files[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
    versions = {}
    for name in _packaged_source()['dependencies']:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {'files': files, 'python': platform.python_version(), 'dependencies': versions}


def _read(directory):
    root = Path(directory).resolve(strict=True)
    path = root/'state.sqlite3'
    if not path.is_file() or path.is_symlink():
        raise ValueError('An existing regular campaign database is required')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        schema = list(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        if ({row[1] for row in schema if row[0] == 'table'} !=
                {'kv', 'events', 'limits', 'charges', 'sqlite_sequence'}
                or any(row[0] in {'trigger', 'view'} for row in schema)):
            raise ValueError('Unexpected campaign database schema')
        rows = list(db.execute('SELECT namespace,key,value FROM kv ORDER BY namespace,key'))
        events = list(db.execute('SELECT id,timestamp,kind,payload FROM events ORDER BY id'))
        limits = list(db.execute('SELECT resource,amount FROM limits ORDER BY resource'))
        charges = list(db.execute('SELECT key,resource,amount,fingerprint,state FROM charges ORDER BY key'))
    data = {(ns, key): json.loads(value) for ns, key, value in rows}
    fingerprint = sha({'schema': schema, 'rows': [[ns, key, hashlib.sha256(value.encode()).hexdigest()]
                                for ns, key, value in rows],
                       'events': events, 'limits': limits, 'charges': charges})
    return root, data, limits, charges, fingerprint


def inspect_checkpoint(directory):
    bundle = _bundle()
    if sha(bundle['files']) != OLD_FILES:
        raise ValueError('C-only driver requires the exact original25312 packaged source')
    root, data, limits, charges, fingerprint = _read(directory)
    state = data.get(('campaign', 'state'))
    if (not state or state.get('status') != 'ready' or state.get('round_index', 0) < 1
            or any(state.get(key) for key in ('pending_batch', 'pending_patch', 'pending_meta'))):
        raise ValueError('C-only completion requires a committed idle round boundary')
    if len(state.get('history', [])) != state['round_index']:
        raise ValueError('Round/history boundary differs')
    if dict(limits) != LIMITS or state.get('task', {}).get('budget') != LIMITS:
        raise ValueError('Budget caps must remain exactly 2000/600/400')
    for key, resource, amount, charge_fingerprint, status in charges:
        if (resource not in LIMITS or type(amount) is not int or amount < 0
                or not isinstance(charge_fingerprint, str) or len(charge_fingerprint) != 64
                or status not in {'committed', 'released'}):
            raise ValueError('Malformed or unsettled budget charge: ' + key)
    if any(sum(r[2] for r in charges if r[1] == resource and r[4] == 'committed') > limit
           for resource, limit in LIMITS.items()):
        raise ValueError('Budget is overspent')
    active = state.get('method_governance', {}).get('active', {})
    if set(active) != {'workflow', 'meta'}:
        raise ValueError('Both original governed method snapshots are required')
    for target, ref in active.items():
        record = data.get(('method_snapshots', ref))
        if not record or ref != 'method-' + sha(record) or record['target'] != target:
            raise ValueError('Method snapshot integrity failure')
        asset = data.get(('method_assets', record['source_ref']))
        if not asset or sha(asset) != record['source_ref'] or asset != bundle:
            raise ValueError('Use the original executable and dependency environment')
        if record['definition'] != state[target]:
            raise ValueError('Active scientific definition differs from its snapshot')
        if record['configuration'] != {key: data.get(('configuration', key)) for key in CONFIG_KEYS}:
            raise ValueError('Frozen method configuration differs')
        if record.get('acceptance_policy') != data.get(('configuration', 'acceptance_policy')):
            raise ValueError('Frozen acceptance policy differs')
    if any(ns.startswith(('evaluation_', 'trial', 'meta_attempt', 'meta_online_attempt'))
           for ns, _ in data):
        raise ValueError('This narrow repair driver does not handle formal evaluations or trials')
    if any(row[4] == 'reserved' for row in charges):
        raise ValueError('Unsettled budget reservations must be reconciled first')
    from proteinrsi.contracts import Observation
    observed = [Observation.model_validate(row).model_dump(mode='json') for row in state['observations']]
    identity = {'campaign_id': state['campaign_id'], 'round': state['round_index'],
                'evidence_version': sha(observed)}
    checkpoint = 'feedback-' + sha(identity)
    saved = data.get(('feedback_inputs', checkpoint))
    result = data.get(('feedback_results', checkpoint))
    if not saved or saved.get('identity') != identity:
        raise ValueError('An already persisted C checkpoint for this exact evidence is required')
    frozen_view = saved.get('view', {})
    if (frozen_view.get('round_index') != state['round_index']
            or frozen_view.get('observations') != observed
            or frozen_view.get('workflow') != state['workflow']
            or frozen_view.get('meta') != state['meta']):
        raise ValueError('Frozen C view differs from committed evidence or scientific methods')
    historical = deepcopy(state['history'])
    historical[-1].pop('analyst_feedback', None)
    if (frozen_view.get('history') != historical
            or frozen_view.get('task') != state['task']):
        raise ValueError('Frozen C task/history differs from the full-plate committed boundary')
    provenance = saved.get('provenance', {})
    if (saved.get('provenance_digest') != sha(provenance)
            or provenance.get('method_snapshots') != active
            or provenance.get('client') != {'llm_transport': 'assistant_bridge', 'llm_bridge_protocol': 1}
            or provenance.get('url') != 'assistant-bridge://local'):
        raise ValueError('C checkpoint must retain its original assistant-bridge/method identity')
    if not data.get(('assistant_bridge', 'scope')):
        raise ValueError('Existing assistant-bridge scope is required; no new identity')
    # Other active scientific requests would make a C-only stop ambiguous.
    for (ns, _), row in data.items():
        if ns == 'llm' and row.get('state') in {'pending', 'started', 'running', 'uncertain'}:
            if (row.get('role') != 'C-feedback' or row.get('transport') != 'assistant_bridge'
                    or row.get('round') != state['round_index']):
                raise ValueError('Another outstanding scientific request exists')
    if result is not None:
        if (result.get('identity') != identity or result.get('input_digest') != sha(saved)
                or result.get('feedback') != state['history'][-1].get('analyst_feedback')):
            raise ValueError('Completed feedback checkpoint differs from historical evidence')
        from proteinrsi.agents import FeedbackAnalysis
        FeedbackAnalysis.model_validate(result['feedback'])
    elif state['history'][-1].get('analyst_feedback') is not None:
        raise ValueError('Unreconciled feedback/history state')
    return {'action': 'complete-existing-C-only', 'campaign': str(root), 'identity': identity,
            'checkpoint_ref': checkpoint, 'source_commit': OLD_COMMIT, 'source_ref': sha(bundle),
            'model': provenance.get('model'), 'source_files_sha256': OLD_FILES,
            'method_snapshot_refs': active, 'status': 'complete' if result is not None else 'pending',
            'limits': dict(limits), 'charges_sha256': sha(charges), 'database_sha256': fingerprint,
            'preflight_sha256': sha({'database': fingerprint, 'source': sha(bundle), 'identity': identity})}


def _open_store(root):
    # Avoid Store.__init__ schema/PRAGMA writes merely to open an existing store.
    from proteinrsi.storage import Store
    store = Store.__new__(Store)
    store.root, store.path = Path(root), Path(root)/'state.sqlite3'
    store.event_sink, store._transaction = None, threading.local()
    return store


def _bind_pending_context(store, context, frozen_view):
    expected = {'view': frozen_view}
    if not isinstance(context, dict):
        raise ValueError('Missing original C request context')
    if '_context' not in context:
        if context != expected:
            raise ValueError('Pending C request differs from frozen feedback input')
        return
    wrapper = context['_context']
    session = wrapper.get('session')
    record = store.get('context_sessions', session)
    root = wrapper.get('source', {}).get('evidence_ref')
    evidence = store.get('context_evidence', root)
    if (not record or record.get('role') != 'C-feedback' or record.get('state') != 'pending'
            or record.get('source_ref') != root or not evidence
            or evidence != {'session': session, 'path': '$', 'value': expected}
            or root != 'ev-' + sha(evidence)):
        raise ValueError('Pending C context session differs from frozen feedback input')
    index = wrapper.get('actions_used')
    frame = store.get('context_frames', str(session) + '/' + str(index))
    if (not frame or frame.get('session') != session or frame.get('role') != 'C-feedback'
            or frame.get('index') != index or frame.get('context') != context):
        raise ValueError('Pending C context frame differs from original request')


def _make_campaign(store, plan):
    if not os.environ.get('PROTEINRSI_ASSISTANT_BRIDGE_DIR'):
        raise ValueError('Original assistant-bridge mailbox is required; native API fallback is forbidden')
    if os.environ.get('PROTEINRSI_MODEL') != plan['model']:
        raise ValueError('Keep the original assistant model identity')
    from proteinrsi.assistant_bridge import AssistantBridgeLLM
    from proteinrsi.llm import JSONLLM
    from proteinrsi.agents import Team
    from proteinrsi.replay.broker import GuardedTeam
    from proteinrsi.replay.sandbox import selected_backend, probe, backend_identity
    from proteinrsi.runtime import Campaign
    if (selected_backend() != 'bwrap' or not probe()['available']
            or store.get('configuration', 'replay_security') !=
               {'backend': 'bwrap', 'identity': backend_identity()}):
        raise ValueError('Actual original explicitly selected bwrap identity/enforcement is required')
    mailbox = Path(os.environ['PROTEINRSI_ASSISTANT_BRIDGE_DIR'])
    if not mailbox.is_absolute() or '..' in mailbox.parts or not mailbox.is_dir():
        raise ValueError('Original existing absolute assistant mailbox is required')
    pending = [(key, row) for key, row in store.all('llm').items()
               if row.get('state') == 'pending' and row.get('role') == 'C-feedback']
    if len(pending) != 1:
        raise ValueError('Exactly one existing pending C bridge request is required')
    pending_key, pending_record = pending[0]
    request = pending_record.get('request')
    request_id = pending_record.get('request_id', '')
    import re
    if not re.fullmatch(r'assistant-[0-9a-f]{64}', request_id):
        raise ValueError('Invalid existing bridge request identity')
    if not isinstance(request, dict):
        raise ValueError('Missing original bridge request envelope')
    payload = {key: value for key, value in request.items()
               if key not in {'request_id', 'request_hash'}}
    expected_hash = sha(payload)
    expected_id = 'assistant-' + sha({'scope': store.get('assistant_bridge', 'scope'),
                                     'request_hash': expected_hash})
    if (request.get('request_hash') != expected_hash or request_id != expected_id
            or request.get('request_id') != expected_id
            or pending_record.get('request_hash') != expected_hash
            or payload.get('role') != 'C-feedback' or payload.get('model') != plan['model']
            or payload.get('context', {}).get('view', {}).get('round_index') != plan['identity']['round']):
        raise ValueError('Original scoped bridge request integrity differs')
    if pending_key != 'llm-' + request_id or pending_record.get('request_key') != pending_key:
        raise ValueError('Pending C record key differs from its original bridge identity')
    with store.connect() as con:
        charge = con.execute('SELECT resource,amount,fingerprint,state FROM charges WHERE key=?',
                             (pending_key,)).fetchone()
    if charge is None or tuple(charge) != ('llm_calls', 1, sha(request), 'committed'):
        raise ValueError('Pending C request does not retain its exact settled call charge')
    saved = store.get('feedback_inputs', plan['checkpoint_ref'])
    _bind_pending_context(store, payload.get('context'), saved['view'])
    request_path = mailbox/'requests'/(request_id + '.json')
    if (not request_path.is_file() or request_path.is_symlink()
            or request_path.stat().st_size > 32 * 1024**2
            or json.loads(request_path.read_text()) != request):
        raise ValueError('Mailbox does not contain the exact original pending C request')
    llm = JSONLLM.from_env(store)
    if not isinstance(llm, AssistantBridgeLLM):
        raise ValueError('Only the original assistant bridge is permitted')
    original_complete = llm.complete
    original_turn = llm._complete
    original_session = payload.get('context', {}).get('_context', {}).get('session')
    resumed = False
    def feedback_turn(role, instructions, context, schema):
        nonlocal resumed
        if role != 'C-feedback':
            raise ValueError('This bounded driver cannot request M or new scientific planning')
        _bind_pending_context(store, context, saved['view'])
        if context.get('_context', {}).get('session') != original_session:
            raise ValueError('C continuation changed its original evidence-context session')
        regenerated = llm._request(role, instructions, context, schema)
        key = 'llm-' + regenerated['request_id']
        cached = store.get('llm', key)
        if not resumed:
            if regenerated == request:
                resumed = True
            elif not (original_session and cached and cached.get('state') == 'done'
                      and cached.get('request') == regenerated):
                raise ValueError('C continuation did not resume its exact pending request')
        elif not original_session and regenerated != request:
            raise ValueError('Plain C continuation cannot create another request')
        return original_turn(role, instructions, context, schema)
    def feedback_only(role, *args, **kwargs):
        if role != 'C-feedback':
            raise ValueError('This bounded driver cannot request M or new scientific planning')
        return original_complete(role, *args, **kwargs)
    llm._complete = feedback_turn
    llm.complete = feedback_only
    team = GuardedTeam.from_team(Team(store, llm))
    # Restarts must recreate canonical per-view tool registration. A previous
    # controller may have registered metric/library tools during planning; a
    # fresh Team has not. Binding registers capabilities, never invokes a tool.
    from proteinrsi.contracts import TaskView
    team.bind_tools(TaskView.model_validate(saved['view']))
    return Campaign(store, team)


def complete_checkpoint(directory, *, expected_preflight, operator, reason):
    if not operator.strip() or not reason.strip() or not expected_preflight:
        raise ValueError('Explicit operator, reason and expected dry-run fingerprint are required')
    plan = inspect_checkpoint(directory)
    store = _open_store(plan['campaign'])
    with store.lock():
        plan = inspect_checkpoint(directory)
        if plan['status'] == 'complete':
            return {**plan, 'action': 'already-complete', 'new_calls': False}
        if plan['preflight_sha256'] != expected_preflight:
            raise ValueError('Stale dry-run fingerprint; inspect again before authorizing')
        before_state = store.get('campaign', 'state')
        before_usage = store.usage()
        before_bridge = store.get('assistant_bridge', 'scope')
        campaign = _make_campaign(store, plan)
        campaign.methods.assert_plannable(before_state)
        # The sole scientific continuation entry point. Deliberately do NOT call
        # prepare(), consider_improvement(), run_replay(), approve() or ingest().
        store.event('operator_C_checkpoint_started', {'operator': operator, 'reason': reason,
            'checkpoint_ref': plan['checkpoint_ref'], 'source_commit': OLD_COMMIT,
            'preflight_sha256': expected_preflight, 'stop_before_M_and_planning': True})
        state = deepcopy(before_state)
        campaign._checkpoint_feedback(state, campaign.view(state))
        after = store.get('campaign', 'state')
        stripped = deepcopy(after)
        stripped['history'][-1].pop('analyst_feedback', None)
        original = deepcopy(before_state)
        original['history'][-1].pop('analyst_feedback', None)
        if stripped != original or store.get('assistant_bridge', 'scope') != before_bridge:
            raise RuntimeError('Unexpected state mutation; stop and reconcile without undoing evidence')
        after_usage = store.usage()
        if ({key: value for key, value in before_usage.items() if key != 'llm_calls'} !=
                {key: value for key, value in after_usage.items() if key != 'llm_calls'}):
            raise RuntimeError('Unexpected non-LLM charge; stop and reconcile without refunds')
        done = inspect_checkpoint(directory)
        if done['status'] != 'complete' or done['limits'] != plan['limits']:
            raise RuntimeError('C checkpoint did not complete at its original bounded state')
        store.event('operator_C_checkpoint_completed', {'operator': operator, 'reason': reason,
            'checkpoint_ref': plan['checkpoint_ref'], 'source_commit': OLD_COMMIT,
            'stop_before_M_and_planning': True, 'usage_before': before_usage, 'usage_after': after_usage})
        return {**inspect_checkpoint(directory), 'action': 'completed-existing-C-only',
                'stop_before_M_and_planning': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--complete', action='store_true')
    parser.add_argument('--expected-preflight')
    parser.add_argument('--operator')
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    try:
        result = (complete_checkpoint(args.campaign, expected_preflight=args.expected_preflight,
                  operator=args.operator or '', reason=args.reason or '') if args.complete
                  else inspect_checkpoint(args.campaign))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, type(exc).__name__ + ': ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
