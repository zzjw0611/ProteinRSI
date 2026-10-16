# ruff: noqa: E402
"""Engineering protocol tests: synthetic identities/values only, no empirical data."""
import importlib.util
import itertools
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('program_matrix_runner', ROOT/'scripts/run_frozen_program_matrix.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from proteinrsi.contracts import InitialParentMeasurement, MethodProgram, TaskSpec
from proteinrsi.storage import Store

FIXTURE_CODE = '''import itertools, random
assert 'labels' not in context
assert len(context['observations']) == 1 + context['round_index'] * 16
alphabet = 'ACDEFGHIKLMNPQRSTVWY'
sequences = [''.join(pair) + 'AA' for pair in itertools.product(alphabet, repeat=2)]
random.Random(context['task']['seed']).shuffle(sequences)
excluded = {o['sequence'] for o in context['observations']} | {'AAAA'}
result = {'candidates': [{'sequence': s, 'source': 'synthetic-protocol-fixture'}
                         for s in sequences if s not in excluded][:16]}
'''


def matrix_fixture(tmp_path, code=FIXTURE_CODE):
    task = TaskSpec(name='Synthetic engineering fixture', reference_sequence='AAAA',
        mutable_positions=[1, 2], max_mutations=2, candidate_access='open',
        feedback_source='synthetic', initial_observation_policy='provided_parent',
        initial_parent_measurement=InitialParentMeasurement(value=1, source_ref='synthetic fixture'),
        controls_per_batch=0)
    taskpath, program, dataset = (tmp_path / n for n in ('task.json', 'program.json', 'synthetic.csv'))
    taskpath.write_text(task.model_dump_json())
    program.write_text(MethodProgram(name='synthetic-fixture', purpose='Protocol test only', code=code).model_dump_json())
    sequences = [''.join(pair)+'AA' for pair in itertools.product('ACDEFGHIKLMNPQRSTVWY', repeat=2)]
    dataset.write_text('sequence,value,qc\n' + ''.join(f'{s},{1 if s == "AAAA" else i/400},valid\n'
                                                   for i, s in enumerate(sequences)))
    return [{'case_id': 'synthetic-case', 'landscape': 'synthetic', 'arm': 'fixture', 'seed': 17,
             'task_file': str(taskpath), 'task_sha256': runner.file_sha256(taskpath),
             'program_file': str(program), 'program_sha256': runner.file_sha256(program),
             'dataset': str(dataset), 'dataset_sha256': runner.file_sha256(dataset)}]


def test_execution_inert_by_default(tmp_path):
    with pytest.raises(PermissionError):
        runner.run_matrix([], tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_synthetic_real_bwrap_full_loop_and_resume(tmp_path):
    matrix = matrix_fixture(tmp_path)
    result = runner.run_matrix(matrix, tmp_path/'out', execute=True)
    assert result['llm_calls'] == 0 and result['prompt_workflow_evaluated'] is False
    case = result['case_aggregates'][0]
    assert case['metrics']['charged_queries'] == 128
    assert len(case['metrics']['equal_query_curve']) == 8
    assert case['usage']['tool_calls']['committed'] == 8
    saved = next((tmp_path/'out').glob('case-*'))
    store = Store(saved)
    outputs = store.all('research_step_outputs')
    assert len(outputs) == 8
    assert all(v['result']['execution_backend'] == 'bwrap_seccomp_generated_v1' for v in outputs.values())
    batches = store.all('batches')
    assert len(batches) == 8
    assert all(len(b['samples']) == 16 for b in batches.values())
    before = store.usage()
    assert runner.run_matrix(matrix, tmp_path/'out', execute=True) == result
    assert store.usage() == before
    encoded = json.dumps(result)
    assert '"sequence":' not in encoded and '"observations":' not in encoded
    assert 'global_rank' not in encoded and 'oracle' not in encoded


@pytest.mark.parametrize('bad_code', [
    "result = {}", "result = {'candidates': []}",
    "result = {'candidates': [{'sequence':'AAAA'}] * 16}",
    "raise RuntimeError('synthetic failure')", "result = {'candidates': 'bad'}",
    "result = {'candidates': [], 'measurement': 1}",
    "result = {'candidates': [], 'diagnostic': {'bad': []}}",
    "result = {'candidates': [], 'diagnostic': {'bad': 'x' * 513}}",
])
def test_failed_program_spends_no_experimental_wells(tmp_path, bad_code):
    matrix = matrix_fixture(tmp_path, bad_code)
    with pytest.raises((ValueError, RuntimeError)):
        runner.run_matrix(matrix, tmp_path/'out', execute=True)
    store = Store(next((tmp_path/'out').glob('case-*')))
    assert store.usage()['experimental_wells']['committed'] == 0
    assert store.usage()['llm_calls']['committed'] == 0
    assert store.usage()['tool_calls']['committed'] == 1


@pytest.mark.parametrize('field', ['task_sha256', 'program_sha256', 'dataset_sha256'])
def test_frozen_input_hashes_fail_closed(tmp_path, field):
    matrix = matrix_fixture(tmp_path)
    matrix[0][field] = '0'*64
    with pytest.raises(ValueError, match='hash differs'):
        runner.prepare_matrix(matrix)


def test_valid_bounded_diagnostic_metadata(tmp_path):
    code = FIXTURE_CODE + "\nresult['diagnostic'] = {'mode':'synthetic', 'count':16, 'valid':True}\n"
    result = runner.run_matrix(matrix_fixture(tmp_path, code), tmp_path/'out', execute=True)
    assert result['case_aggregates'][0]['metrics']['charged_queries'] == 128
