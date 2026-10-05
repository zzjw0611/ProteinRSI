# SPDX-License-Identifier: MIT
"""Stable sequence identities and bounded repair of model-returned orderings."""
from proteinrsi.contracts import sequence_hash


def request_ranking(llm, store, role, instructions, context, field, candidates, response_type):
    ids = {'seq-' + sequence_hash(c.sequence)[:16]: c for c in candidates}
    if len(ids) != len(candidates):
        raise ValueError('Ranking candidates must have unique, unambiguous identities')
    by_sequence = {c.sequence: key for key, c in ids.items()}
    context = {**context, field: [{'candidate_id': key, **c.model_dump(mode='json')} for key, c in ids.items()]}
    schema = response_type.model_json_schema()
    schema['properties']['ranking']['items'] = {'type': 'string', 'enum': list(ids)}
    instructions += '\nReturn each candidate_id exactly once in ranking; never transcribe or edit protein sequences. Prediction references use candidate_id keys.'
    for attempt in range(3):
        raw = llm.complete(role, instructions, context, schema)
        try:
            result = response_type.model_validate(raw)
            # Exact old sequence responses remain readable; never fuzzy-match or repair a sequence.
            order = [by_sequence.get(item, item) for item in result.ranking]
            if len(order) != len(ids) or set(order) != set(ids):
                raise ValueError('Ranking must contain every supplied candidate_id exactly once, with no extra or duplicate IDs')
            if any(k not in ids and k not in by_sequence for k in result.prediction_refs):
                raise ValueError('Prediction reference names an unknown candidate')
            result.ranking = [ids[key].sequence for key in order]
            result.prediction_refs = {ids[by_sequence.get(k, k)].sequence: v for k, v in result.prediction_refs.items()}
            return result
        except ValueError as exc:
            store.event('ranking_repair_requested', {'role': role, 'attempt': attempt + 1,
                'reason': str(exc)[:500], 'candidate_count': len(ids), 'exhausted': attempt == 2})
            if attempt == 2:
                raise ValueError('Ranking repair exhausted; no candidate identity was changed') from exc
            context = {**context, 'ranking_repair': {'attempt': attempt + 1,
                'error': str(exc)[:500], 'previous_response': raw,
                'instruction': 'Correct the ID permutation only. Keep all supplied candidate identities immutable.'}}
