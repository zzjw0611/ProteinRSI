"""Opt-in, synthetic-only acceptance of an external frozen frontier program.

Set PROTEINRSI_FRONTIER_PROGRAM_JSON to the explicit external JSON path and run
this module with pytest -m ''. No external policy, empirical table, model, or completed run is
loaded without that opt-in. The sibling .py must match the JSON's code exactly.
Optional PROTEINRSI_FRONTIER_EXPECTED_{JSON,PY}_SHA256 values pin both files.
An opted-in run requires actual bwrap enforcement; it never silently skips it.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import time

import pytest

from proteinrsi.contracts import (
    BudgetSpec, Candidate, MetaPolicy, MethodProgram, Observation, TaskSpec, TaskView, Workflow,
)
from proteinrsi.replay import sandbox
from proteinrsi.research.code import execute_code
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_candidate


ROOT = Path(__file__).resolve().parents[1]
AA = 'ACDEFGHIKLMNPQRSTVWY'
SEEDS = (11, 23, 37, 53, 71)


@pytest.fixture(scope='module')
def frontier_program():
    reference = os.environ.get('PROTEINRSI_FRONTIER_PROGRAM_JSON')
    if not reference:
        pytest.skip('Explicit external frontier-program reference was not provided')
    path = Path(reference).resolve(strict=True)
    source = path.with_suffix('.py')
    program = MethodProgram.model_validate_json(path.read_text())
    assert source.read_text() == program.code, 'External JSON and Python source differ'
    for kind, file in (('JSON', path), ('PY', source)):
        expected = os.environ.get(f'PROTEINRSI_FRONTIER_EXPECTED_{kind}_SHA256')
        if expected:
            assert hashlib.sha256(file.read_bytes()).hexdigest() == expected
    return program


@pytest.fixture(scope='module', autouse=True)
def actual_bwrap(frontier_program):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
        state = sandbox.probe()
        assert state['available'], state
        assert state['backend'] == 'bwrap'
        assert state['enforcement'] == [
            {'confined': True, 'generated': False, 'denials': 9},
            {'confined': True, 'generated': True, 'denials': 9},
        ]
        yield


@pytest.fixture(scope='module')
def execution_log(tmp_path_factory):
    entries = []
    yield entries
    output = tmp_path_factory.mktemp('frontier-synthetic-execution') / 'timings.json'
    output.write_text(json.dumps(entries, indent=2) + '\n')
    print(f'\nSynthetic frontier execution timings: {output}')


def _task(sites, seed=11):
    # Noncontiguous sites check that the complete sequence is reconstructed.
    return TaskSpec(
        name='Synthetic frontier execution contract', reference_sequence='A' * (sites * 2 + 1),
        mutable_positions=list(range(2, sites * 2 + 1, 2)), max_mutations=sites,
        candidate_access='open', feedback_source='synthetic', batch_size=16,
        controls_per_batch=0, max_rounds=8, seed=seed, batch_fill_policy='full_plate',
        budget=BudgetSpec(experimental_wells=128, llm_calls=0, tool_calls=16),
    )


def _sequence(task, code):
    result = list(task.reference_sequence)
    for position, residue in zip(task.mutable_positions, code):
        result[position - 1] = residue
    return ''.join(result)


def _observation(task, code, value, *, batch='synthetic-prior', qc='valid'):
    sequence = _sequence(task, code)
    return Observation(
        sample_id=f'{batch}-{sequence}', sequence=sequence, value=value, qc=qc,
        # The contract requires this provenance for unavailable historical rows.
        # This is a synthetic stand-in, never a real replay or empirical query.
        source='measured_replay' if qc == 'unavailable' else 'synthetic',
        metric='fitness', unit='synthetic', batch_id=batch, assay_protocol='synthetic-only',
    )


def _view(task, program, observations, *, round_index=2, completion=None, history=None):
    return TaskView(
        task=task, round_index=round_index, observations=observations,
        history=history or [], remaining_wells=16 if round_index == 7 else 128,
        workflow=Workflow(programs=[program]), meta=MetaPolicy(enabled=False),
        research_context={'plate_completion': completion} if completion is not None else {},
    )


def _run(tmp_path, view, program, execution_log, *, tag):
    started = time.monotonic()
    receipt = execute_code(Store(tmp_path / tag), view,
                           {'code': program.code, 'inputs': program.inputs})
    elapsed = time.monotonic() - started
    execution_log.append({
        'tag': tag, 'sites': len(view.task.mutable_positions), 'seed': view.task.seed,
        'round': view.round_index, 'observations': len(view.observations),
        'seconds': elapsed, 'status': receipt['status'],
    })
    assert receipt['status'] == 'ok', receipt
    assert receipt['execution_backend'] == 'bwrap_seccomp_generated_v1'
    assert receipt['measurement_authority'] is False
    assert receipt['evidence_kind'] == 'computed_unvalidated'
    assert elapsed < 20, f'Exceeded existing 20-second generated-code wall bound: {elapsed}'
    output = receipt['output']
    completion = view.research_context.get('plate_completion', {})
    count = completion.get('required_new_candidates', view.task.batch_size)
    sequences = [candidate['sequence'] for candidate in output['candidates']]
    assert len(sequences) == len(set(sequences)) == count
    excluded = {row.sequence for row in view.observations}
    excluded.update(completion.get('exclude_sequences', []))
    assert not set(sequences) & excluded
    for candidate in output['candidates']:
        assert candidate['predicted_value'] is None
        validate_candidate(view.task, Candidate.model_validate(candidate))
    return output


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('seed', SEEDS)
def test_same_first16_as_each_published_control_and_retry(
        tmp_path, frontier_program, execution_log, sites, seed):
    task = _task(sites, seed)
    observations = [_observation(task, 'A' * sites, 0)]
    view = _view(task, frontier_program, observations, round_index=0)
    output = _run(tmp_path, view, frontier_program, execution_log, tag='frontier')
    assert output == _run(tmp_path, view, frontier_program, execution_log, tag='retry')
    expected = [row['sequence'] for row in output['candidates']]
    assert output['diagnostic']['model_used'] == 'shared_balanced_single_initialization'
    assert all(sum(a != b for a, b in zip(task.reference_sequence, sequence)) == 1
               for sequence in expected)
    for mode in ('random', 'additive', 'conditional_pairwise'):
        program = MethodProgram.model_validate_json((
            ROOT / 'experiments/frozen-program-selection-v1/policies' / f'{mode}.json'
        ).read_text())
        control = _run(tmp_path, _view(task, program, observations, round_index=0), program,
                       execution_log, tag=mode)
        assert [row['sequence'] for row in control['candidates']] == expected


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('seed', SEEDS)
def test_maximum_observed_context_is_deterministic_legal_and_bounded(
        tmp_path, frontier_program, execution_log, sites, seed):
    task = _task(sites, seed)
    codes = (''.join(code) for code in itertools.product(AA, repeat=sites))
    observations = [_observation(task, code, index / 128,
                                 batch=f'synthetic-batch-{index // 16}')
                    for index, code in enumerate(itertools.islice(codes, 128))]
    excluded = _sequence(task, 'C' * sites)
    view = _view(task, frontier_program, observations, round_index=7,
                 completion={'exclude_sequences': [excluded]})
    output = _run(tmp_path, view, frontier_program, execution_log, tag='maximum')
    assert output == _run(tmp_path, view, frontier_program, execution_log, tag='retry')
    assert output['diagnostic']['valid_training_count'] == 128
    assert output['diagnostic']['local_count'] + output['diagnostic']['uniform_count'] == 16


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('values,qcs,numeric_count', (
    ([None, None, None], ['failed', 'inconclusive', 'unavailable'], 0),
    ([0, None, None], ['valid', 'failed', 'unavailable'], 1),
    ([0, -1, None], ['valid', 'valid', 'unavailable'], 2),
    ([0, 0, None], ['valid', 'valid', 'inconclusive'], 2),
))
def test_numeric_zero_negative_flat_and_missing_are_not_imputed(
        tmp_path, frontier_program, execution_log, sites, values, qcs, numeric_count):
    task = _task(sites)
    codes = ('A' * sites, 'C' + 'A' * (sites - 1), 'D' + 'A' * (sites - 1))
    observations = [_observation(task, code, value, qc=qc)
                    for code, value, qc in zip(codes, values, qcs)]
    view = _view(task, frontier_program, observations)
    output = _run(tmp_path, view, frontier_program, execution_log, tag='numeric-missing')
    diagnostic = output['diagnostic']
    assert diagnostic['valid_training_count'] == numeric_count
    assert diagnostic['missing_values_imputed'] is False
    expected_model = ('uniform_unseen_insufficient_numeric_data' if numeric_count < 2
                      else 'bounded_observed_frontier_additive_ucb')
    assert diagnostic['model_used'] == expected_model


@pytest.mark.parametrize('sites', (3, 4))
def test_repair_count_and_exclusions_use_only_available_contract_fields(
        tmp_path, frontier_program, execution_log, sites):
    task = _task(sites)
    observations = [_observation(task, 'A' * sites, 0),
                    _observation(task, 'C' + 'A' * (sites - 1), 1)]
    excluded = [_sequence(task, 'D' + 'A' * (sites - 1)), _sequence(task, 'C' * sites)]
    view = _view(task, frontier_program, observations,
                 completion={'required_new_candidates': 7, 'exclude_sequences': excluded})
    output = _run(tmp_path, view, frontier_program, execution_log, tag='repair')
    assert output == _run(tmp_path, view, frontier_program, execution_log, tag='retry')


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('latest,expected_stalled', (
    (2, False), (0.5, True), (1, True), (1.001, True), (None, True),
))
def test_latest_batch_stall_trigger_uses_actual_taskview_contract(
        tmp_path, frontier_program, execution_log, sites, latest, expected_stalled):
    task = _task(sites)
    observations = [_observation(task, 'A' * sites, 0),
                    _observation(task, 'C' + 'A' * (sites - 1), 1),
                    _observation(task, 'AC' + 'A' * (sites - 2), latest,
                                 batch='synthetic-latest',
                                 qc='unavailable' if latest is None else 'valid')]
    history = [{'round': 0, 'batch_id': 'synthetic-prior'},
               {'round': 1, 'batch_id': 'synthetic-latest'}]
    view = _view(task, frontier_program, observations, history=history)
    # current_measurements is a provider prompt projection, absent from Python.
    assert 'current_measurements' not in view.model_dump(mode='json')
    output = _run(tmp_path, view, frontier_program, execution_log, tag='stall-trigger')
    diagnostic = output['diagnostic']
    assert diagnostic['no_valid_improvement_trigger'] is expected_stalled, diagnostic
    assert diagnostic['uniform_count'] == (8 if expected_stalled else 4)
    assert diagnostic['local_count'] == (8 if expected_stalled else 12)


@pytest.mark.parametrize('sites', (3, 4))
def test_stall_trigger_disabled_before_round_two(
        tmp_path, frontier_program, execution_log, sites):
    task = _task(sites)
    observations = [_observation(task, 'A' * sites, 1),
                    _observation(task, 'C' + 'A' * (sites - 1), 0, batch='synthetic-latest')]
    view = _view(task, frontier_program, observations, round_index=1,
                 history=[{'round': 0, 'batch_id': 'synthetic-latest'}])
    output = _run(tmp_path, view, frontier_program, execution_log, tag='early-round')
    assert output['diagnostic']['no_valid_improvement_trigger'] is False
    assert output['diagnostic']['uniform_count'] == 4


@pytest.mark.parametrize('sites', (3, 4))
def test_absent_history_does_not_invent_latest_batch(
        tmp_path, frontier_program, execution_log, sites):
    task = _task(sites)
    observations = [_observation(task, 'A' * sites, 1),
                    _observation(task, 'C' + 'A' * (sites - 1), 0, batch='synthetic-latest')]
    view = _view(task, frontier_program, observations, round_index=2)
    output = _run(tmp_path, view, frontier_program, execution_log, tag='no-history')
    assert output['diagnostic']['no_valid_improvement_trigger'] is False
    assert output['diagnostic']['uniform_count'] == 4
