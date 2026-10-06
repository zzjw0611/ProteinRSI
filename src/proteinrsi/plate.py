# SPDX-License-Identifier: MIT
"""Controller-owned plate completion. No assay access or synthetic filler candidates."""
from proteinrsi.contracts import Candidate, digest
from proteinrsi.tasks import validate_candidate


def complete_candidates(campaign, view, count, *, excluded=(), initial=(), max_attempts=6):
    """Ask the team to complete a real plate before reserving any experiment slots."""
    excluded = set(excluded)
    key = digest({'evidence': view.evidence_version, 'round': view.round_index,
                  'workflow': view.workflow.version, 'count': count,
                  'excluded': sorted(excluded), 'initial': [c.model_dump() for c in initial]})
    record = campaign.store.get('plate_plans', key, {'attempts': 0, 'candidates': [], 'state': 'planning'})
    selected = {c.sequence: c for c in [*initial, *[Candidate.model_validate(c) for c in record['candidates']]]
                if c.sequence not in excluded}
    selected = dict(list(selected.items())[:count])
    for candidate in selected.values():
        validate_candidate(view.task, candidate)
    while len(selected) < count and (record['attempts'] < max_attempts or record.get('pending_request')):
        resuming = bool(record.get('pending_request'))
        attempt = record['attempts'] if resuming else record['attempts'] + 1
        request = {'required_new_candidates': count - len(selected), 'target_count': count,
                   'already_selected': [c.model_dump(mode='json') for c in selected.values()],
                   'exclude_sequences': sorted(excluded | set(selected)), 'attempt': attempt,
                   'instruction': 'Complete this same experimental plate. Generate additional distinct legal designs; '
                       'multiple scientific panels may share the plate. No experiment has run and no round has elapsed.'}
        if resuming:
            request = record['pending_request']
        context = {**view.research_context, 'plate_completion': request}
        # Persist intent first; ambiguous interrupted work must not silently reset its attempt budget.
        record.update(attempts=attempt, state='planning', pending_request=request)
        campaign.store.put('plate_plans', key, record)
        campaign.store.event('plate_completion_requested', {'key': key, 'round': view.round_index,
            'attempt': attempt, 'missing': count - len(selected), 'target': count})
        try:
            proposed = campaign.team.run(view.model_copy(deep=True, update={'research_context': context}))
        except Exception as exc:
            from proteinrsi.llm import ProviderPaused
            from proteinrsi.replay.broker import WorkerExecutionError
            paused = isinstance(exc, ProviderPaused) or (
                isinstance(exc, WorkerExecutionError) and exc.error_type == 'ProviderPaused')
            record['state'] = 'paused_provider' if paused else 'blocked'
            if not paused:
                record.pop('pending_request', None)
            campaign.store.put('plate_plans', key, record)
            raise
        record.pop('pending_request', None)
        for candidate in proposed:
            validate_candidate(view.task, candidate)
            if candidate.sequence not in excluded:
                selected.setdefault(candidate.sequence, candidate)
        selected = dict(list(selected.items())[:count])
        record['candidates'] = [c.model_dump(mode='json') for c in selected.values()]
        campaign.store.put('plate_plans', key, record)
    if len(selected) != count:
        record['state'] = 'blocked'
        campaign.store.put('plate_plans', key, record)
        campaign.store.event('plate_incomplete', {'key': key, 'round': view.round_index,
            'required': count, 'available': len(selected), 'queries_submitted': 0})
        raise ValueError(f'Full plate incomplete: need {count} distinct candidates, have {len(selected)}; no experiment submitted')
    record['candidates'] = [c.model_dump(mode='json') for c in selected.values()]
    record['state'] = 'complete'
    campaign.store.put('plate_plans', key, record)
    return list(selected.values())
