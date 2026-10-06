"""Transport compaction preserves every revealed fact and candidate exclusion."""
from copy import deepcopy
from itertools import product

import pytest

from proteinrsi.agents import Plan
from proteinrsi.contracts import Observation, canonical
from proteinrsi.dataflow.context import compact_view, prepare_evidence
from proteinrsi.dataflow.design import request_design


def measured_view(view):
    reference = view.task.reference_sequence
    sequences = [reference]
    for a, b in product('ACDEFGHIKLMNPQRSTVWY', repeat=2):
        sequence = reference[:38] + a + b + reference[40:]
        if sequence != reference:
            sequences.append(sequence)
        if len(sequences) == 385:
            break
    observations = [Observation(sample_id=f'b-plate-{i:03}', sequence=s,
        value=None if i == 3 else float(i), qc='unavailable' if i == 3 else 'valid',
        metric='fitness', unit='source_units', source='measured_replay',
        batch_id='initial' if i == 0 else 'plate', assay_protocol='original-fitness-v1')
        for i, s in enumerate(sequences)]
    return view.model_copy(deep=True, update={'observations': observations,
        'research_context': {'plate_completion': {'exclude_sequences': sequences,
            'required_new_candidates': 384}}})


def decode_observations(presented):
    table = presented['observations']
    if isinstance(table, list):
        return table
    decoded = []
    for cells in table['rows']:
        row = {**table['shared_fields'], **dict(zip(table['columns'], cells))}
        if table.get('sequence_encoding'):
            sequence = list(presented['task']['reference_sequence'])
            for position, residue in zip(table['sequence_encoding']['positions_1based'], row['sequence']):
                sequence[position - 1] = residue
            row['sequence'] = ''.join(sequence)
        decoded.append(row)
    return decoded


def test_full_plate_facts_and_exclusions_round_trip(view):
    raw = measured_view(view).model_dump(mode='json')
    original = deepcopy(raw)
    presented, evidence = prepare_evidence(raw, [raw])
    assert evidence == [{'context_ref': '/view'}]
    restored = decode_observations(presented)
    assert restored == raw['observations']
    assert len(restored) == 385 and restored[3]['value'] is None
    assert restored[3]['qc'] == 'unavailable'
    plate = presented['research_context']['plate_completion']
    assert plate['exclude_observed_sequences'] is True
    assert set(plate['exclude_sequences']) | {r['sequence'] for r in restored} == set(
        raw['research_context']['plate_completion']['exclude_sequences'])
    assert raw == original
    assert len(canonical({'view': presented, 'tool_results': evidence})) < len(
        canonical({'view': raw, 'tool_results': [raw]})) * 0.3


@pytest.mark.parametrize('changed', ['length', 'fixed_position', 'no_mutable_positions'])
def test_other_task_sequences_remain_lossless(view, changed):
    raw = measured_view(view).model_dump(mode='json')
    if changed == 'length':
        raw['observations'][2]['sequence'] += 'A'
    elif changed == 'fixed_position':
        raw['observations'][2]['sequence'] = 'A' + raw['observations'][2]['sequence'][1:]
    else:
        raw['task']['mutable_positions'] = []
    presented = compact_view(raw)
    assert 'sequence_encoding' not in presented['observations']
    assert decode_observations(presented) == raw['observations']


def test_partial_exclusion_does_not_exclude_additional_observations(view):
    raw = measured_view(view).model_dump(mode='json')
    plate = raw['research_context']['plate_completion']
    plate['exclude_sequences'] = plate['exclude_sequences'][:2]
    presented = compact_view(raw)
    assert presented['research_context']['plate_completion'] == plate


def test_distinct_tool_evidence_and_additional_exclusions_survive(view):
    raw = measured_view(view).model_dump(mode='json')
    extra = 'A' * len(view.task.reference_sequence)
    raw['research_context']['plate_completion']['exclude_sequences'].append(extra)
    separate = deepcopy(raw)
    separate['observations'][1]['value'] = 0.125
    actual_tool = {'status': 'ok', 'output': {'predictions': [1, 2, 3]}}
    presented, evidence = prepare_evidence(raw, [raw, separate, actual_tool])
    assert evidence[1:] == [separate, actual_tool]
    assert presented['research_context']['plate_completion']['exclude_sequences'] == [extra]


def test_real_design_boundary_uses_compaction_without_mutating_tool_inputs(store, view):
    view = measured_view(view)
    raw = view.model_dump(mode='json')
    class LLM:
        def complete(self, role, instructions, context, schema):
            assert 'presentation only' in instructions
            assert context['tool_results'] == [{'context_ref': '/view'}]
            assert decode_observations(context['view']) == raw['observations']
            return {'edits': [[{'position': 39, 'from': 'V', 'to': 'A'}]]}
    result = request_design(LLM(), store, 'Design', view, Plan(rationale='Use revealed evidence'), [raw], [])
    assert len(result.candidates) == 1
    assert view.model_dump(mode='json') == raw
    assert store.usage()['experimental_wells']['committed'] == 0


def test_empty_observations_remain_an_empty_list(view):
    raw = view.model_dump(mode='json')
    assert compact_view(raw) == raw
