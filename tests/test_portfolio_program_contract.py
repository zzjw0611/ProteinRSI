"""Synthetic execution contracts for the global/local portfolio program.

Run this extended module with pytest -m ''. Actual bwrap enforcement is required.
"""
import ast
import itertools
from pathlib import Path
import time

import numpy as np
import pytest

from proteinrsi.contracts import (
    BudgetSpec, Candidate, MetaPolicy, MethodProgram, Observation, TaskSpec, TaskView, Workflow,
)
from proteinrsi.replay import sandbox
from proteinrsi.research.code import execute_code, python_input_contract
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_candidate


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'experiments/mechanism-portfolio-v2/policies/global_local_portfolio_v2.json'
CONTROL = ROOT / 'experiments/frozen-program-selection-v1/policies/additive.json'
AA = 'ACDEFGHIKLMNPQRSTVWY'


@pytest.fixture(scope='module')
def programs():
    portfolio = MethodProgram.model_validate_json(POLICY.read_text())
    assert POLICY.with_suffix('.py').read_bytes() == portfolio.code.encode('utf-8')
    return portfolio, MethodProgram.model_validate_json(CONTROL.read_text())


@pytest.fixture(scope='module', autouse=True)
def actual_bwrap():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
        state = sandbox.probe()
        assert state['available'] and state['backend'] == 'bwrap', state
        assert state['enforcement'] == [
            {'confined': True, 'generated': False, 'denials': 9},
            {'confined': True, 'generated': True, 'denials': 9},
        ]
        yield


def _task(sites, seed=11):
    return TaskSpec(
        name='Synthetic portfolio execution contract', reference_sequence='A' * (2 * sites + 1),
        mutable_positions=list(range(2, 2 * sites + 1, 2)), max_mutations=sites,
        candidate_access='open', feedback_source='synthetic', batch_size=16,
        controls_per_batch=0, max_rounds=8, seed=seed, batch_fill_policy='full_plate',
        budget=BudgetSpec(experimental_wells=128, llm_calls=0, tool_calls=16),
    )


def _sequence(task, code):
    sequence = list(task.reference_sequence)
    for position, residue in zip(task.mutable_positions, code):
        sequence[position - 1] = residue
    return ''.join(sequence)


def _short(task, sequence):
    return ''.join(sequence[position - 1] for position in task.mutable_positions)


def _observation(task, code, value, qc='valid', batch='synthetic-prior'):
    return Observation(
        sample_id=f'{batch}-{code}', sequence=_sequence(task, code), value=value, qc=qc,
        # An unavailable synthetic row must use this schema provenance label.
        source='measured_replay' if qc == 'unavailable' else 'synthetic',
        metric='fitness', unit='synthetic', batch_id=batch, assay_protocol='synthetic-only',
    )


def _view(task, program, observations, *, round_index=2, completion=None):
    return TaskView(
        task=task, round_index=round_index, observations=observations, history=[],
        remaining_wells=16 if round_index == 7 else 128,
        workflow=Workflow(programs=[program]), meta=MetaPolicy(enabled=False),
        research_context={} if completion is None else {'plate_completion': completion},
    )


def _run(tmp_path, view, program, tag):
    started = time.monotonic()
    receipt = execute_code(Store(tmp_path / tag), view,
                           {'code': program.code, 'inputs': program.inputs})
    elapsed = time.monotonic() - started
    assert receipt['status'] == 'ok', receipt
    assert receipt['execution_backend'] == 'bwrap_seccomp_generated_v1'
    assert receipt['evidence_kind'] == 'computed_unvalidated'
    assert receipt['measurement_authority'] is False
    assert elapsed < 20, f'Generated execution exceeded 20 seconds: {elapsed}'
    output = receipt['output']
    completion = view.research_context.get('plate_completion', {})
    sequences = [row['sequence'] for row in output['candidates']]
    assert len(sequences) == len(set(sequences)) == completion.get(
        'required_new_candidates', view.task.batch_size)
    excluded = {row.sequence for row in view.observations}
    excluded.update(completion.get('exclude_sequences', []))
    assert not set(sequences) & excluded
    for row in output['candidates']:
        assert row['predicted_value'] is None
        validate_candidate(view.task, Candidate.model_validate(row))
        assert all(row['sequence'][index] == residue
                   for index, residue in enumerate(view.task.reference_sequence)
                   if index + 1 not in view.task.mutable_positions)
    return output


def _counts(output):
    diagnostic = output['diagnostic']
    return tuple(diagnostic[key] for key in (
        'global_model_count', 'local_ucb_count', 'uniform_count'))


def test_portfolio_uses_available_input_contract(programs):
    tree = ast.parse(programs[0].code)
    contract = python_input_contract()['variables']['context']
    for variable, fields in (('context', 'fields'), ('task', 'task_fields')):
        used = {node.slice.value for node in ast.walk(tree)
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == variable and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)}
        assert used <= set(contract[fields])


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('seed', (11, 23, 37))
def test_first16_match_additive_and_are_balanced(tmp_path, programs, sites, seed):
    portfolio, additive = programs
    task = _task(sites, seed)
    observations = [_observation(task, 'A' * sites, 0)]
    view = _view(task, portfolio, observations, round_index=0)
    output = _run(tmp_path, view, portfolio, 'initial')
    assert output == _run(tmp_path, view, portfolio, 'retry')
    control = _run(tmp_path, _view(task, additive, observations, round_index=0),
                   additive, 'additive')
    sequences = [row['sequence'] for row in output['candidates']]
    assert sequences == [row['sequence'] for row in control['candidates']]
    assert output['diagnostic']['model_used'] == 'shared_balanced_single_initialization'
    assert all(sum(a != b for a, b in zip(task.reference_sequence, sequence)) == 1
               for sequence in sequences)
    counts = [sum(sequence[position - 1] != 'A' for sequence in sequences)
              for position in task.mutable_positions]
    assert max(counts) - min(counts) <= 1


def _assert_portfolio_acquisition(view, output, control):
    """Reconstruct reserved draws and sequential local acquisition independently."""
    task = view.task
    sites = len(task.mutable_positions)
    seen = {_short(task, row.sequence) for row in view.observations}
    seen.update(_short(task, sequence) for sequence in
                view.research_context.get('plate_completion', {}).get('exclude_sequences', []))
    valid = {_short(task, row.sequence): row.value for row in view.observations
             if row.qc == 'valid' and row.value is not None and np.isfinite(row.value)}
    unseen = [''.join(code) for code in itertools.product(AA, repeat=sites)
              if ''.join(code) not in seen]
    actual = [_short(task, row['sequence']) for row in output['candidates']]
    additive = [_short(task, row['sequence']) for row in control['candidates']]
    rng = np.random.default_rng(task.seed + 1009 * view.round_index)
    reserve = rng.choice(len(unseen), 4, replace=False)
    ties = rng.random(len(unseen))
    assert actual[:6] == additive[:6]
    assert actual[-4:] == additive[-4:] == [unseen[int(index)] for index in reserve]
    global_set, local_set, uniform = set(actual[:6]), set(actual[6:12]), set(actual[-4:])
    assert tuple(map(len, (global_set, local_set, uniform))) == (6, 6, 4)
    assert not (global_set & local_set or global_set & uniform or local_set & uniform)
    centers = sorted(valid, key=lambda code: (-valid[code], code))[:4]
    frontier = {center[:site] + residue + center[site + 1:]
                for center in centers for site in range(sites) for residue in AA} - seen
    assert local_set <= frontier
    train = np.array([[AA.index(a) for a in code] for code in valid], dtype=np.int16)
    values = np.asarray(list(valid.values()), dtype=float)
    centered = values - values.mean()
    if values.std() > 0:
        centered /= values.std()
    kernel = np.sum(train[:, None, :] == train[None, :, :], axis=2).astype(float)
    regularized = kernel + np.identity(len(valid))
    dual = np.linalg.solve(regularized, centered)
    eligible = [(index, code) for index, code in enumerate(unseen)
                if code in frontier and code not in global_set | uniform]
    local = np.array([[AA.index(a) for a in code] for _, code in eligible], dtype=np.int16)
    cross = np.sum(local[:, None, :] == train[None, :, :], axis=2).astype(float)
    variance = np.maximum(0, sites - np.sum(
        cross * np.linalg.solve(regularized, cross.T).T, axis=1))
    acquisition = cross @ dual + 0.5 * np.sqrt(variance)
    expected = actual[:6].copy()
    for _ in range(6):
        selected = np.array([[AA.index(a) for a in code] for code in expected])
        similarity = np.sum(local[:, None, :] == selected[None, :, :], axis=2).max(axis=1)
        ranking = np.lexsort((np.array([ties[index] for index, _ in eligible]),
                              -(acquisition - 0.15 * similarity / sites)))
        expected.append(next(eligible[int(index)][1] for index in ranking
                             if eligible[int(index)][1] not in expected))
    assert actual[:12] == expected


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('seed', (11, 23, 37))
def test_128_observations_are_deterministic_bounded_and_split(tmp_path, programs, sites, seed):
    portfolio, additive = programs
    task = _task(sites, seed)
    codes = itertools.islice(itertools.product(AA, repeat=sites), 128)
    observations = [_observation(task, ''.join(code), index / 128,
                                 batch=f'synthetic-batch-{index // 16}')
                    for index, code in enumerate(codes)]
    completion = {'exclude_sequences': [_sequence(task, 'C' * sites)]}
    view = _view(task, portfolio, observations, round_index=7, completion=completion)
    output = _run(tmp_path, view, portfolio, 'stress')
    assert output == _run(tmp_path, view, portfolio, 'retry')
    assert output['diagnostic']['valid_training_count'] == 128
    assert _counts(output) == (6, 6, 4)
    control = _run(tmp_path, _view(task, additive, observations, round_index=7,
                                  completion=completion), additive, 'additive')
    _assert_portfolio_acquisition(view, output, control)


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('values,qcs,numeric_count', (
    ([None, None, None], ['failed', 'inconclusive', 'unavailable'], 0),
    ([0, None, None], ['valid', 'failed', 'unavailable'], 1),
    ([0, -1, None], ['valid', 'valid', 'unavailable'], 2),
    ([0, 0, None], ['valid', 'valid', 'inconclusive'], 2),
))
def test_zero_negative_flat_and_missing_values(tmp_path, programs, sites, values, qcs, numeric_count):
    portfolio = programs[0]
    task = _task(sites)
    codes = ('A' * sites, 'C' + 'A' * (sites - 1), 'D' + 'A' * (sites - 1))
    observations = [_observation(task, code, value, qc)
                    for code, value, qc in zip(codes, values, qcs)]
    output = _run(tmp_path, _view(task, portfolio, observations), portfolio, 'numeric-missing')
    diagnostic = output['diagnostic']
    assert diagnostic['valid_training_count'] == numeric_count
    assert diagnostic['missing_values_imputed'] is False
    assert _counts(output) == ((0, 0, 16) if numeric_count < 2 else (6, 6, 4))
    assert diagnostic['model_used'] == (
        'uniform_unseen_insufficient_numeric_data' if numeric_count < 2
        else 'fixed_global_additive_local_ucb_portfolio')


@pytest.mark.parametrize('sites', (2, 3))
def test_exhausted_local_frontier_falls_back_to_additive(tmp_path, programs, sites):
    portfolio, additive = programs
    task = _task(sites)
    centers = [residue + 'A' * (sites - 1) for residue in 'ACDE']
    frontier = {center[:site] + residue + center[site + 1:]
                for center in centers for site in range(sites) for residue in AA}
    observations = [_observation(task, code, float(4 - index))
                    for index, code in enumerate(centers)]
    observations.extend(_observation(task, code, None, 'failed')
                        for code in sorted(frontier - set(centers)))
    output = _run(tmp_path, _view(task, portfolio, observations), portfolio, 'exhausted')
    control = _run(tmp_path, _view(task, additive, observations), additive, 'additive')
    assert _counts(output) == (12, 0, 4)
    assert [row['sequence'] for row in output['candidates']] == [
        row['sequence'] for row in control['candidates']]


@pytest.mark.parametrize('sites', (3, 4))
@pytest.mark.parametrize('count', (1, 7))
def test_completion_count_and_exclusions(tmp_path, programs, sites, count):
    portfolio = programs[0]
    task = _task(sites)
    observations = [_observation(task, 'A' * sites, 0),
                    _observation(task, 'C' + 'A' * (sites - 1), 1)]
    completion = {'required_new_candidates': count, 'exclude_sequences': [
        _sequence(task, 'D' + 'A' * (sites - 1)), _sequence(task, 'C' * sites)]}
    view = _view(task, portfolio, observations, completion=completion)
    output = _run(tmp_path, view, portfolio, 'completion')
    assert output == _run(tmp_path, view, portfolio, 'retry')
    uniform = max(1, count // 4)
    global_count = (count - uniform) // 2
    assert _counts(output) == (global_count, count - uniform - global_count, uniform)
