# SPDX-License-Identifier: MIT
"""Controller-owned, bounded model context over an immutable visible-evidence snapshot.

References are capabilities for this decision only, never paths or database keys.
The oracle and worker RPC namespaces are deliberately not used here.
"""
from __future__ import annotations

from copy import deepcopy
import json

from proteinrsi.contracts import canonical, digest

VERSION = 'evidence-v1'
POLICY_REVISION = 4
MAX_ACTIONS = 12
MAX_CONTEXT_BYTES = 48000
PAGE_BYTES = 12000
NOTE_ROLES = {'C-feedback', 'A-review', 'M'}
INSTRUCTIONS = '''
Context protocol evidence-v1: large values are immutable evidence_ref descriptors,
not missing data or literal scientific values. The original evidence is retained.
To read one, return only {"_context_action":{"kind":"read","ref":"...",
"offset":0,"limit":20}}. Lists page by rows, objects by keys, strings by characters.
Use next_offset until null when completeness matters. Read tool/operation schemas
before using an abbreviated specification. Only refs issued in this decision work.
Successful reads stay in working_set or last_read until you explicitly release them.
Do not reread retained evidence. If capacity is full, release an unused page with
{"_context_action":{"kind":"release","ref":"...","offset":0}} or a named
contract with {"_context_action":{"kind":"release","ref":"...","name":"..."}}.
Releasing without offset/name removes all retained pages of that ref. Original evidence
remains available. The runtime never silently evicts data to fit a new read.
Catalogue descriptors list names. Read a named specification with
{"_context_action":{"kind":"read","ref":"...","name":"exact operation name"}}.
Select relevant capabilities by name instead of repeatedly scanning every schema.
Named operation reads expose the execution input contract and a full_spec reference
for optional detail. Binding types are inferred from source ports: omit schema_ref.
Read full_spec only if that extra detail is necessary for the current decision.
When research_python is offered to your role, it can analyze the complete revealed
TaskView via context (observations, history, task); do not transcribe a table into
generated code. Use only operations offered to your role, never invent tool access.
Local evidence reads spend no experiment wells; model turns still cost LLM calls.
You may save a concise research note using only {"_context_action":{"kind":"note",
"hypothesis":"...","evidence_refs":["..."],"counterevidence":"...",
"failures":"...","open_questions":"...","next_step":"..."}}.
Notes are model claims, not measurements, and must cite issued evidence refs.
Otherwise return the original requested decision JSON. At most 12 context actions
per decision; after that finalize. Never infer unread values from a preview/count.
'''


def size(value):
    return len(canonical(value).encode('utf-8'))


class ContextBuilder:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.issued = {}

    def reference(self, value, path):
        ref = 'ev-' + digest({'session': self.session, 'path': path, 'value': value})
        record = {'session': self.session, 'path': path, 'value': value}
        self.store.put('context_evidence', ref, record, immutable=True)
        self.issued[ref] = record
        result = {'evidence_ref': ref, 'path': path, 'bytes': size(value),
                  'type': type(value).__name__, 'count': len(value)}
        if isinstance(value, dict):
            result['keys_preview'] = sorted(value)[:12]
            if size(sorted(value)) <= 3000:
                result['names'] = sorted(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            result['row_fields_preview'] = sorted(value[0])[:16]
        if isinstance(value, list) and value and all(
                isinstance(row, dict) and isinstance(row.get('name'), str) for row in value):
            names = [row['name'] for row in value]
            if size(names) <= 3000:
                result['names'] = names
        return result

    def pack(self, value, path='$', threshold=2000):
        if size(value) <= threshold:
            return deepcopy(value)
        if isinstance(value, (list, str)):
            return self.reference(value, path)
        if isinstance(value, dict):
            result = {k: self.pack(v, f'{path}.{k}', threshold) for k, v in value.items()}
            # Keep the top-level and TaskView skeleton; cap large catalogues/maps.
            if path not in ('$', '$.view', '$.view.task', '$.task') and size(result) > 6000:
                return self.reference(value, path)
            return result
        return value

    def operation_contract(self, row, path):
        if not isinstance(row, dict) or not {'name', 'input_schema', 'output_schema_ref'} <= row.keys():
            return self.pack(row, path)
        result = {k: deepcopy(row[k]) for k in (
            'name', 'input_schema', 'output_schema_ref', 'resource_inputs', 'question_from_step') if k in row}
        result['full_spec'] = self.reference(row, path)
        reply = row.get('agent_reply_contract')
        if isinstance(reply, dict):
            result['reply_guidance'] = {k: v for k, v in reply.items() if k != 'agent_reply_schema'}
        return self.pack(result, path + '.execution_contract')

    def read(self, action):
        if set(action) - {'kind', 'ref', 'offset', 'limit', 'name'}:
            raise ValueError('Unknown read fields')
        ref = action.get('ref')
        if not isinstance(ref, str) or ref not in self.issued:
            raise ValueError('Reference is not available in this decision')
        if 'name' in action:
            if 'offset' in action or 'limit' in action or not isinstance(action['name'], str):
                raise ValueError('Named reads require a string name without pagination fields')
            record = self.issued[ref]
            value = record['value']
            if isinstance(value, dict):
                if action['name'] not in value:
                    raise ValueError('Named read requires one exact catalogue match')
                name = action['name']
                return {'ref': ref, 'name': name, 'offset': sorted(value).index(name),
                        'next_offset': None, 'total': len(value),
                        'value': self.pack(value[name], f'{record["path"]}.{name}')}
            matches = ([(i, row) for i, row in enumerate(value)
                        if isinstance(row, dict) and row.get('name') == action['name']]
                       if isinstance(value, list) else [])
            if len(matches) != 1:
                raise ValueError('Named read requires one exact catalogue match')
            i, row = matches[0]
            return {'ref': ref, 'name': action['name'], 'offset': i, 'next_offset': None,
                    'total': len(value), 'value': self.operation_contract(row, f'{record["path"]}[{i}]')}
        start, limit = action.get('offset', 0), action.get('limit', 20)
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('offset must be nonnegative; limit must be 1..50')
        record = self.issued[ref]
        value = record['value']
        if start > len(value):
            raise ValueError('offset exceeds evidence length')
        count = min(limit, len(value) - start)
        # Text can be read in useful chunks, with an explicit character cursor.
        if isinstance(value, str):
            count = min(limit * 100, len(value) - start)
            page = value[start:start+count]
        elif isinstance(value, list):
            page = [self.pack(v, f'{record["path"]}[{i}]')
                    for i, v in enumerate(value[start:start+count], start)]
        else:
            keys = sorted(value)[start:start+count]
            page = {k: self.pack(value[k], f'{record["path"]}.{k}') for k in keys}
        # Shrink by whole records, not by truncating evidence. A large record
        # becomes a child reference and can itself be read losslessly.
        while size(page) > PAGE_BYTES and count > 1:
            count //= 2
            if isinstance(page, dict):
                page = dict(list(page.items())[:count])
            else:
                page = page[:count]
        return {'ref': ref, 'offset': start, 'total': len(value), 'value': page,
                'next_offset': start + count if start + count < len(value) else None}

    def note(self, action):
        fields = {'kind', 'hypothesis', 'evidence_refs', 'counterevidence', 'failures',
                  'open_questions', 'next_step'}
        if set(action) != fields or size(action) > 6000:
            raise ValueError('Note requires all documented fields and at most 6000 UTF-8 bytes')
        refs = action['evidence_refs']
        if (not isinstance(refs, list) or not refs or len(refs) > 20
                or any(not isinstance(r, str) or r not in self.issued for r in refs)):
            raise ValueError('Note must cite 1..20 references issued in this decision')
        if any(not isinstance(action[k], str) for k in fields - {'kind', 'evidence_refs'}):
            raise ValueError('Note text fields must be strings')
        return {k: v for k, v in action.items() if k != 'kind'}


def enabled(store):
    return store.get('configuration', 'research', {}).get('context_policy') == VERSION


def complete(llm, role, instructions, context, schema):
    """Durable local continuation around the existing audited provider client."""
    from proteinrsi.llm import ProviderPaused

    store = llm.store
    identity = {'revision': POLICY_REVISION, 'version': VERSION, 'role': role, 'instructions': instructions,
                'context': context, 'schema': schema, 'model': llm.model,
                'url': llm.base_url, 'settings': llm.cache_settings,
                'max_tokens': getattr(llm, 'max_tokens', None)}
    key = 'ctx-' + digest(identity)
    builder = ContextBuilder(store, key)
    root = builder.reference(context, '$')
    view = context.get('view', context)
    task = view.get('task') if isinstance(view, dict) else None
    round_index = view.get('round_index') if isinstance(view, dict) else None
    scope = {'task': digest(task), 'round': round_index}
    # Notes are advisory and only shared inside the same task/evidence lineage.
    observations = view.get('observations', []) if isinstance(view, dict) else []
    visible = {digest(row) for row in observations} if isinstance(observations, list) else set()
    scope['observations'] = sorted(visible)
    saved = store.get('context_sessions', key)
    if saved is None:
        # Freeze notes at first entry: retries never acquire later-round evidence.
        notes = []
        if task is not None and type(round_index) is int and not role.startswith('E'):
            with store.connect() as con:
                rows = con.execute(
                    "SELECT value FROM kv WHERE namespace='research_notes' "
                    "AND json_extract(value,'$.scope.task')=? "
                    "AND json_extract(value,'$.scope.round')<=? ORDER BY rowid DESC",
                    (scope['task'], round_index))
                for row in rows:
                    note = json.loads(row['value'])
                    if set(note['scope']['observations']) <= visible:
                        notes.append(note)
                    if len(notes) == 3:
                        break
        saved = {'version': VERSION, 'revision': POLICY_REVISION, 'role': role, 'source_ref': root['evidence_ref'],
                 'notes': notes, 'state': 'pending'}
        store.put('context_sessions', key, saved)
    if saved['state'] == 'done':
        return saved['result']
    frame = builder.pack(context)
    # An unusually wide input must still be accessible, without dropping fields.
    if size(frame) > MAX_CONTEXT_BYTES - PAGE_BYTES - 18000:
        frame = {'context_source': root}
    note_summaries = []
    for index, note in enumerate(saved['notes']):
        # Reissue old capabilities in this scope only after the lineage check.
        old_source = store.get('context_evidence', note.get('source_ref', ''))
        source = builder.reference(old_source['value'], f'$.note_sources[{index}]') if old_source else None
        note_summaries.append({'role': note['role'], 'kind': note['kind'],
            'round': note['scope']['round'], 'source': source,
            'content': note.get('note', note.get('decision')),
            'authority': 'model_claim_not_measurement'})
    note_frame = [builder.pack(note, f'$.research_notes[{i}]', threshold=512)
                  for i, note in enumerate(note_summaries)]
    if size(note_frame) > 6000:
        note_frame = builder.reference(note_summaries, '$.research_notes')
    read_schema = {'type': 'object', 'additionalProperties': False,
        'required': ['kind', 'ref'], 'properties': {
            'kind': {'const': 'read'}, 'ref': {'type': 'string'},
            'name': {'type': 'string'},
            'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}}
    release_schema = {'type': 'object', 'additionalProperties': False,
        'required': ['kind', 'ref'], 'properties': {
            'kind': {'const': 'release'}, 'ref': {'type': 'string'},
            'name': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}}}
    note_properties = {name: {'type': 'string'} for name in
        ('hypothesis', 'counterevidence', 'failures', 'open_questions', 'next_step')}
    note_properties.update({'kind': {'const': 'note'}, 'evidence_refs': {
        'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 20}})
    note_schema = {'type': 'object', 'additionalProperties': False,
                   'required': list(note_properties), 'properties': note_properties}
    action_schema = {'type': 'object', 'required': ['_context_action'],
                     'additionalProperties': False,
                     'properties': {'_context_action': {'oneOf': [read_schema, note_schema, release_schema]}}}
    # Preserve root definitions for original #/$defs/... references.
    response_schema = {'anyOf': [deepcopy(schema), action_schema]}
    for name in ('$defs', 'definitions'):
        if name in schema:
            response_schema[name] = deepcopy(schema[name])
    page = None
    ledger = []
    retained = []

    def page_key(value):
        return value.get('ref'), value.get('offset'), value.get('name'), value.get('next_offset')

    def frame_for(latest, pages, index):
        previous = [p for p in pages if page_key(p) != page_key(latest or {})]
        return {**frame, '_context': {'version': VERSION, 'revision': POLICY_REVISION,
            'session': key, 'source': root, 'recent_notes': note_frame,
            'last_read': latest, 'working_set': previous, 'actions_used': index,
            'actions_remaining': MAX_ACTIONS - index, 'read_ledger': list(ledger)}}

    for index in range(MAX_ACTIONS + 1):
        current = frame_for(page, retained, index)
        if size(current) > MAX_CONTEXT_BYTES:
            raise ProviderPaused('Bounded context frame exceeds its policy; inspect context_sessions')
        step_key = f'{key}/{index}'
        audit = {'session': key, 'role': role, 'index': index, 'context': current,
                 'context_bytes': size(current), 'source_bytes': size(context)}
        store.put('context_frames', step_key, audit, immutable=True)
        store.event('context_prepared', {'context_frame_ref': step_key, 'role': role,
                    'context_bytes': audit['context_bytes'], 'source_bytes': audit['source_bytes']})
        result = llm._complete(role, instructions + INSTRUCTIONS, current, response_schema)
        if '_context_action' not in result:
            # The caller retains its original scientific contract validation.
            with store.transaction():
                if role in NOTE_ROLES:
                    store.put('research_notes', key, {'session': key, 'role': role,
                        'kind': 'model_decision', 'source_ref': root['evidence_ref'], 'scope': scope,
                        'decision': result, 'authority': 'model_claim_not_measurement'}, immutable=True)
                store.put('context_sessions', key, {**saved, 'state': 'done', 'result': result})
            return result
        if index == MAX_ACTIONS:
            raise ProviderPaused('Context action limit reached; inspect context_reads before continuing')
        try:
            if set(result) != {'_context_action'} or not isinstance(result['_context_action'], dict):
                raise ValueError('Return either one context action or the final decision')
            action = result['_context_action']
            if action.get('kind') == 'read':
                incoming = builder.read(action)
                updated = [p for p in retained if page_key(p) != page_key(incoming)] + [incoming]
                entry = {'ref': incoming['ref'], 'offset': incoming['offset'],
                         'name': incoming.get('name'), 'next_offset': incoming['next_offset']}
                ledger.append(entry)
                if size(frame_for(incoming, updated, index + 1)) > MAX_CONTEXT_BYTES - 2000:
                    ledger.pop()
                    raise ValueError('Context capacity reached. Release unused retained pages before reading more; no evidence was evicted.')
                retained, page = updated, incoming
            elif action.get('kind') == 'release':
                if (set(action) - {'kind', 'ref', 'name', 'offset'}
                        or not isinstance(action.get('ref'), str) or action['ref'] not in builder.issued
                        or ('offset' in action and (type(action['offset']) is not int or action['offset'] < 0))
                        or ('name' in action and not isinstance(action['name'], str))):
                    raise ValueError('Release requires an issued ref and valid optional name/offset')
                def matches(item):
                    return (item['ref'] == action['ref']
                            and all(item.get(k) == action[k] for k in ('name', 'offset') if k in action))
                remaining = [p for p in retained if not matches(p)]
                count = len(retained) - len(remaining)
                retained = remaining
                page = {'released_pages': count, 'released_ref': action['ref']}
            elif action.get('kind') == 'note':
                note = builder.note(action)
                store.put('research_notes', step_key, {'session': key, 'role': role,
                    'kind': 'research_note', 'scope': scope, 'source_ref': root['evidence_ref'],
                    'note': note, 'authority': 'model_claim_not_measurement'}, immutable=True)
                page = {'note_saved': step_key, 'note': note}
            else:
                raise ValueError('Unknown context action')
        except ValueError as exc:
            page = {'error': str(exc)}
        store.put('context_reads', step_key, {'session': key, 'role': role,
            'request': result, 'response': page}, immutable=True)
        store.event('context_read_completed', {'context_read_ref': step_key, 'role': role,
            'status': 'error' if 'error' in page else 'done', 'offset': page.get('offset'),
            'next_offset': page.get('next_offset')})
    raise AssertionError('unreachable')
