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
POLICY_REVISION = 12
MAX_CONTEXT_BYTES = 1024 * 1024
PAGE_BYTES = 64 * 1024
NOTE_ROLES = {'C-feedback', 'A-review', 'M'}
INSTRUCTIONS = '''
Context protocol evidence-v1: in full mode the complete stage input is already
present. Make the requested decision directly; do not fetch a duplicate copy.
Values represented by immutable evidence_ref descriptors are scoped references,
not missing data or literal scientific values. The original evidence is retained.
To read one, return only {"_context_action":{"kind":"read","ref":"...",
"offset":0,"limit":20}}. Lists page by rows, objects by keys, strings by characters.
Prefer a single batch read: {"_context_action":{"kind":"read_many","reads":[
{"ref":"..."},{"ref":"...","name":"exact operation name"}]}}.
The runtime fills a bounded page and returns remaining_reads with continuation
cursors. Reuse those requests to continue; you need not choose page limits.
execution_facts and observation_summary are computed from the supplied task and
revealed evidence, not model hypotheses. Summary coverage does not imply every
measurement was read by you. Inspect current_measurements for the latest feedback.
Historical evidence is intentionally not repeated in the default stage input.
Use view.research_context.research_state and current_measurements first. Retrieve history only
for a concrete question. You can query the revealed snapshot directly with
{"_context_action":{"kind":"query","filters":{"variants":["..."],"limit":20}}}.
Filters may use sequences, variants, round, batch_id, or background with positions
and residues. Queries reveal only previously measured rows, never new fitness.
no_matching_observations is a normal result: no record matches these filters in
this revealed snapshot. It does not mean a candidate is invalid or has zero fitness.
Continue the requested decision with fitness unknown; unknown designs may still
be proposed for an experiment. Only experimental submission through the Controller
can reveal new measurements. Repeating an unchanged query against this immutable
snapshot cannot reveal new fitness. Change filters only for a different question.
Use next_offset until null when completeness matters. Read tool/operation schemas
before using an abbreviated specification. Only refs issued in this decision work.
tool_results exposes each result separately. Inspect prior successes and candidate_set
references before requesting more tools; read referenced details only when needed.
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
Otherwise return the original requested decision JSON. Reads can continue while
they reveal new evidence. After repeated actions without progress, the runtime
requests a decision-only recovery turn using the evidence already supplied.
Never infer unread values from a preview/count.
'''
RECOVERY_INSTRUCTIONS = '''
Decision-only recovery: the preceding context actions made no progress. Their
requests and results are attached in last_read and working_set. Context actions
are unavailable for this turn; return the original requested decision JSON.
Use the evidence already supplied. An empty history lookup is completed work,
not an unfinished experiment: unobserved fitness remains unknown. You may propose
unmeasured candidates or request an offered execution tool through the original
decision contract. If the task needs replanning, use that contract's failure or
replan field when available. Do not repeat a lookup, invent evidence, or claim
that unread information or an unexecuted check was inspected.
'''
REPAIR_CONTEXT_INSTRUCTIONS = '''
Execution repair input: error and arguments describe the failed call;
context_shape, input_shape and resource_shapes describe actual injected data,
not experiment records or a TaskView to summarize. Follow execution_contract.
Repair only this call, using the requested response schema. All inline input is
already available. If a field is an evidence_ref, read it with
{"_context_action":{"kind":"read","ref":"...","offset":0,"limit":20}}
or read_many; do not infer missing values or retrieve experimental labels.
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
        if isinstance(value, list) and path == '$.tool_results':
            # One large knowledge/input item must not hide later completed tool results.
            return [self.pack(row, f'{path}[{i}]', threshold) for i, row in enumerate(value)]
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
            return self.pack(row, path, threshold=6000)
        result = {k: deepcopy(row[k]) for k in (
            'name', 'input_schema', 'output_schema_ref', 'resource_inputs', 'question_from_step',
            'execution_contract', 'output_views') if k in row}
        result['full_spec'] = self.reference(row, path)
        reply = row.get('agent_reply_contract')
        if isinstance(reply, dict):
            result['reply_guidance'] = {k: v for k, v in reply.items() if k != 'agent_reply_schema'}
        return self.pack(result, path + '.execution_contract', threshold=6000)

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
        while self.page_size(page) > 1 and count > 1:
            count //= 2
            if isinstance(page, dict):
                page = dict(list(page.items())[:count])
            else:
                page = page[:count]
        return {'ref': ref, 'offset': start, 'total': len(value), 'value': page,
                'next_offset': start + count if start + count < len(value) else None}

    @staticmethod
    def page_size(value):
        from proteinrsi.llm import estimate_tokens
        return max(size(value) / PAGE_BYTES, estimate_tokens(canonical(value)) / 16384)

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

    def read_many(self, reads):
        if not isinstance(reads, list) or not 1 <= len(reads) <= 16:
            raise ValueError('Batch reads require 1..16 requests')
        for request in reads:
            if (not isinstance(request, dict) or set(request) - {'ref', 'name', 'offset'}
                    or not isinstance(request.get('ref'), str) or request['ref'] not in self.issued):
                raise ValueError('Batch reads require issued references and valid fields')
        # A batch cursor streams a reference to its end. Overlapping starts for
        # the same reference are the same request, not separate page ranges.
        pending, positions = [], {}
        for request in reads:
            name, offset = request.get('name'), request.get('offset', 0)
            value = self.issued[request['ref']]['value']
            if ('name' in request and (not isinstance(name, str) or 'offset' in request)
                    or type(offset) is not int or not 0 <= offset <= len(value)):
                raise ValueError('Named reads cannot have offsets; offsets must fit the evidence')
            identity = (request['ref'], name)
            if identity in positions:
                if name is None:
                    previous = pending[positions[identity]]
                    previous['offset'] = min(previous.get('offset', 0), offset)
            else:
                positions[identity] = len(pending)
                pending.append(deepcopy(request))
        pages = []
        while pending:
            request = pending[0]
            limit = 50
            while True:
                page = self.read({'kind': 'read', **request,
                                 **({} if 'name' in request else {'limit': limit})})
                if self.page_size(pages + [page]) <= 1:
                    break
                if limit == 1 or 'name' in request:
                    return {'pages': pages, 'remaining_reads': pending}
                limit = max(1, limit // 2)
            pages.append(page)
            if page['next_offset'] is None:
                pending.pop(0)
            else:
                pending[0] = {'ref': request['ref'], 'offset': page['next_offset']}
        return {'pages': pages, 'remaining_reads': []}


def enabled(store):
    return store.get('configuration', 'research', {}).get('context_policy') in {VERSION, 'state-v1'}


def complete(llm, role, instructions, context, schema):
    """Durable local continuation around the existing audited provider client."""
    from proteinrsi.llm import ProviderPaused

    store = llm.store
    policy = store.get('configuration', 'research', {}).get('context_policy', VERSION)
    identity = {'revision': POLICY_REVISION, 'version': policy, 'role': role, 'instructions': instructions,
                'context': context, 'schema': schema, 'model': llm.model,
                'url': llm.base_url, 'settings': llm.cache_settings,
                'capacity': getattr(llm, 'capacity_settings', {}),
                'max_tokens': getattr(llm, 'max_tokens', None)}
    key = 'ctx-' + digest(identity)
    builder = ContextBuilder(store, key)
    root = builder.reference(context, '$')
    view = {} if role == 'B-repair' else context.get('view', {})
    task = view.get('task') if isinstance(view, dict) else None
    round_index = view.get('round_index') if isinstance(view, dict) else None
    scope = {'task': digest(task), 'round': round_index}
    # Notes are advisory and only shared inside the same task/evidence lineage.
    from .context_frame import observation_rows
    observations = observation_rows(view.get('observations', []), task or {}) if isinstance(view, dict) else []
    visible = {digest(row) for row in observations}
    scope['observations'] = sorted(visible)
    saved = store.get('context_sessions', key)
    if saved is None:
        # Freeze notes at first entry: retries never acquire later-round evidence.
        notes = []
        if policy != 'state-v1' and task is not None and type(round_index) is int and not role.startswith('E'):
            with store.connect() as con:
                rows = con.execute(
                    "SELECT value FROM kv WHERE namespace='research_notes' "
                    "AND json_extract(value,'$.scope.task')=? "
                    "AND json_extract(value,'$.scope.round')<=? ORDER BY rowid DESC",
                    (scope['task'], round_index))
                for row in rows:
                    note = json.loads(row['value'])
                    if (role == 'M' or note['role'] != 'M') and set(note['scope']['observations']) <= visible:
                        notes.append(note)
                    if len(notes) == 3:
                        break
        saved = {'version': policy, 'revision': POLICY_REVISION, 'role': role, 'source_ref': root['evidence_ref'],
                 'notes': notes, 'state': 'pending'}
        store.put('context_sessions', key, saved)
    if saved['state'] == 'done':
        return saved['result']
    from .context_frame import stage_frame, state_frame
    if role == 'B-repair':
        # Execution repair has its own input contract, not a scientific TaskView.
        # Preserve code, errors and type descriptions exactly for the repairer.
        frame = deepcopy(context)
    else:
        frame = state_frame(builder, context, role) if policy == 'state-v1' else stage_frame(builder, context, inline=True)
    mode = 'full'
    note_summaries = []
    for index, note in enumerate(saved['notes']):
        # Reissue old capabilities in this scope only after the lineage check.
        old_source = store.get('context_evidence', note.get('source_ref', ''))
        source = builder.reference(old_source['value'], f'$.note_sources[{index}]') if old_source else None
        note_summaries.append({'role': note['role'], 'kind': note['kind'],
            'round': note['scope']['round'], 'source': source,
            'content': note.get('note', note.get('decision')),
            'authority': 'model_claim_not_measurement'})
    note_frame = deepcopy(note_summaries)
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
    batch_schema = {'type': 'object', 'additionalProperties': False,
        'required': ['kind', 'reads'], 'properties': {'kind': {'const': 'read_many'},
            'reads': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                'items': {'type': 'object', 'additionalProperties': False,
                    'required': ['ref'], 'properties': {'ref': {'type': 'string'},
                        'name': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}}}}}}
    note_properties = {name: {'type': 'string'} for name in
        ('hypothesis', 'counterevidence', 'failures', 'open_questions', 'next_step')}
    note_properties.update({'kind': {'const': 'note'}, 'evidence_refs': {
        'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 20}})
    note_schema = {'type': 'object', 'additionalProperties': False,
                   'required': list(note_properties), 'properties': note_properties}
    action_schema = {'type': 'object', 'required': ['_context_action'],
                     'additionalProperties': False,
                     'properties': {'_context_action': {'oneOf': [read_schema, batch_schema,
                                                                  note_schema, release_schema]}}}
    if role == 'B-repair':
        action_schema['properties']['_context_action']['oneOf'] = [read_schema, batch_schema, release_schema]
    from .analysis import QUERY_PROPERTIES
    query_schema = {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'filters'],
        'properties': {'kind': {'const': 'query'}, 'filters': {'type': 'object',
            'properties': QUERY_PROPERTIES, 'additionalProperties': False}}}
    if task is not None and not role.startswith('E'):
        action_schema['properties']['_context_action']['oneOf'].append(query_schema)
    # Preserve root definitions for original #/$defs/... references.
    response_schema = {'anyOf': [deepcopy(schema), action_schema]}
    for name in ('$defs', 'definitions'):
        if name in schema:
            response_schema[name] = deepcopy(schema[name])
    page = None
    ledger = []
    retained = []
    seen_pages = set()
    stalled = 0

    def page_key(value):
        return value.get('ref'), value.get('offset'), value.get('name'), value.get('next_offset')

    def frame_for(latest, pages, index):
        latest_pages = (latest or {}).get('pages', [latest or {}])
        keys = {page_key(p) for p in latest_pages}
        previous = [p for p in pages if page_key(p) not in keys]
        return {**frame, '_context': {'version': policy, 'revision': POLICY_REVISION,
            'session': key, 'mode': mode, 'source': root, 'recent_notes': note_frame,
            'last_read': latest, 'working_set': previous, 'actions_used': index,
            'read_ledger': list(ledger)}}

    def turn_contract(recovery=False):
        context_instructions = REPAIR_CONTEXT_INSTRUCTIONS if role == 'B-repair' else INSTRUCTIONS
        return (instructions + context_instructions + (RECOVERY_INSTRUCTIONS if recovery else ''),
                schema if recovery else response_schema)

    def capacity(value, *, recovery=False):
        check = getattr(llm, 'request_capacity', None)
        if check is not None:
            prompt, output_schema = turn_contract(recovery)
            return check(prompt, value, output_schema)
        return {'fits': size(value) <= getattr(llm, 'max_request_bytes', MAX_CONTEXT_BYTES)}

    if not capacity(frame_for(None, [], 0))['fits']:
        # Batch local snapshot writes into a short transaction, never across LLM I/O.
        with store.transaction():
            frame = (stage_frame(builder, context) if policy != 'state-v1' and role != 'B-repair'
                     else builder.pack(frame))
            note_frame = [builder.pack(note, f'$.research_notes[{i}]', threshold=512)
                          for i, note in enumerate(note_summaries)]
            if size(note_frame) > 6000:
                note_frame = builder.reference(note_summaries, '$.research_notes')
        mode = 'referenced'

    index = 0
    while True:
        recovery = stalled >= 3
        current = frame_for(page, retained, index)
        if recovery:
            current['_context']['recovery'] = {'reason': 'repeated_context_actions',
                'stalled_actions': stalled, 'context_actions_available': False,
                'next_action': 'complete_requested_decision'}
        limits = capacity(current, recovery=recovery)
        if not limits['fits']:
            raise ProviderPaused('Context frame exceeds request capacity; inspect context_sessions')
        step_key = f'{key}/{index}'
        audit = {'session': key, 'role': role, 'index': index, 'context': current,
                 'context_bytes': size(current), 'source_bytes': size(context), 'capacity': limits}
        store.put('context_frames', step_key, audit, immutable=True)
        store.event('context_prepared', {'context_frame_ref': step_key, 'role': role,
                    'context_bytes': audit['context_bytes'], 'source_bytes': audit['source_bytes'],
                    'estimated_input_tokens': limits.get('estimated_input_tokens'), 'context_actions': index})
        if recovery:
            store.event('context_recovery_started', {'session': key, 'role': role,
                'context_frame_ref': step_key, 'stalled_actions': stalled})
        prompt, output_schema = turn_contract(recovery)
        result = llm._complete(role, prompt, current, output_schema)
        if '_context_action' not in result:
            # The caller retains its original scientific contract validation.
            with store.transaction():
                if role in NOTE_ROLES:
                    store.put('research_notes', key, {'session': key, 'role': role,
                        'kind': 'model_decision', 'source_ref': root['evidence_ref'], 'scope': scope,
                        'decision': result, 'authority': 'model_claim_not_measurement'}, immutable=True)
                store.put('context_sessions', key, {**saved, 'state': 'done', 'result': result})
            return result
        if recovery:
            pause = {'reason': 'context_stalled',
                'message': 'Context actions made no progress and decision-only recovery failed; inspect context_reads before continuing',
                'stalled_actions': stalled, 'context_read_ref': f'{key}/{index - 1}',
                'recovery_frame_ref': step_key}
            if saved.get('pause') != pause:
                store.put('context_sessions', key, {**saved, 'pause': pause})
                store.event('context_stalled', {'session': key, 'role': role, **pause})
            raise ProviderPaused(pause['message'], reason=pause['reason'])
        progressed = False
        action = result.get('_context_action')
        try:
            if set(result) != {'_context_action'} or not isinstance(result['_context_action'], dict):
                raise ValueError('Return either one context action or the final decision')
            action = result['_context_action']
            if action.get('kind') in {'read', 'read_many'}:
                if action['kind'] == 'read_many':
                    if set(action) != {'kind', 'reads'}:
                        raise ValueError('Unknown batch read fields')
                    incoming = builder.read_many(action['reads'])
                    pages = incoming['pages']
                else:
                    incoming = builder.read(action)
                    pages = [incoming]
                keys = {page_key(p) for p in pages}
                updated = [p for p in retained if page_key(p) not in keys] + pages
                entries = [{'ref': p['ref'], 'offset': p['offset'],
                            'name': p.get('name'), 'next_offset': p['next_offset']} for p in pages]
                if not capacity(frame_for(incoming, updated, index + 1))['fits']:
                    raise ValueError('Context capacity reached. Release unused retained pages before reading more; no evidence was evicted.')
                new_pages = {digest(p) for p in pages} - seen_pages
                progressed = bool(new_pages)
                if new_pages:
                    ledger.extend(entry for entry, p in zip(entries, pages) if digest(p) in new_pages)
                    seen_pages.update(new_pages)
                retained, page = updated, incoming
            elif action.get('kind') == 'query':
                if task is None or role.startswith('E') or set(action) != {'kind', 'filters'}:
                    raise ValueError('Revealed queries require a task snapshot and filters')
                from .analysis import query_observations
                incoming = query_observations(observations, task, view.get('history', []), action['filters'])
                query_ref = builder.reference(incoming, '$.query.' + digest(action))['evidence_ref']
                page = {'ref': query_ref, 'offset': incoming['coverage']['offset'],
                        'next_offset': incoming['next_offset'], 'value': incoming}
                progressed = digest(page) not in seen_pages
                seen_pages.add(digest(page))
                if not progressed:
                    page['notice'] = (
                        'This query already returned the same result in this immutable snapshot. '
                        'Repeating it will not reveal new measurements. '
                        'Continue the requested decision using known evidence, or make a different '
                        'lookup needed for a concrete question; keep unobserved fitness unknown.')
                retained = [p for p in retained if p.get('ref') != query_ref] + [page]
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
        # Provider requests are stateless. Carry the originating request alongside
        # its result so an empty query still identifies which designs were checked.
        page['action'] = deepcopy(action)
        store.put('context_reads', step_key, {'session': key, 'role': role,
            'request': result, 'response': page}, immutable=True)
        store.event('context_read_completed', {'context_read_ref': step_key, 'role': role,
            'status': 'error' if 'error' in page else 'done', 'offset': page.get('offset'),
            'next_offset': page.get('next_offset')})
        stalled = 0 if progressed else stalled + 1
        index += 1
