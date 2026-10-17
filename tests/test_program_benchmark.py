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


def completed_audit_fixture(tmp_path, *, failed_attempts=0, missing=False):
    """Construct saved ledger/receipt shapes without executing a policy or model."""
    from proteinrsi.contracts import digest
    row, task, workflow = runner.prepare_matrix(matrix_fixture(tmp_path))[0]
    store = Store(tmp_path / 'completed-audit')
    store.configure_budget(task.budget.model_dump())
    state = {'task': task.model_dump(mode='json'), 'workflow': workflow.model_dump(mode='json'),
             'round_index': 8, 'status': 'complete', 'pending_batch': None}
    sequences = [''.join(pair) + 'AA' for pair in itertools.product('ACDEFGHIKLMNPQRSTVWY', repeat=2)
                 if pair != ('A', 'A')][:128]
    program = workflow.programs[0]
    def attempt(round_index, suffix, candidates=None):
        context = f'method-programs:synthetic-{round_index}-{suffix}/{program.version}'
        call = {'name': 'research_python', 'purpose': program.purpose,
                'arguments': {'code': program.code, 'inputs': program.inputs}}
        payload = {'spec': {'name': 'research_python'}, 'call': call, 'context': context}
        key = 'tool-' + digest(payload)
        receipt = {'status': 'ok' if candidates else 'failed',
                   'code_sha256': digest(program.code), 'measurement_authority': False,
                   'execution_backend': 'bwrap_seccomp_generated_v1', 'evidence_kind': 'computed_unvalidated'}
        receipt.update({'output': {'candidates': candidates}} if candidates else
                       {'error_type': 'Timeout', 'error': '20-second wall limit'})
        store.reserve(key, 'tool_calls', 1, payload)
        store.settle(key)
        store.put('tool_jobs', key, {**payload, 'state': 'done', 'result': receipt})
        store.put('research_step_outputs', context, {'owner': 'W', 'method_version': workflow.version,
            'workflow': workflow.version, 'program': program.name, 'program_version': program.version,
            'round': round_index, 'result': receipt, 'authority': 'computed_unvalidated'}, immutable=True)
    for round_index in range(8):
        bid = f'synthetic-batch-{round_index}'
        candidates = [{'sequence': s} for s in sequences[round_index * 16:(round_index + 1) * 16]]
        samples = [{'sample_id': f'{bid}-{i}', 'candidate': candidate,
                    'workflow_version': workflow.version, 'arm': 'research'}
                   for i, candidate in enumerate(candidates)]
        batch = {'batch_id': bid, 'round_index': round_index, 'phase': 'research', 'samples': samples}
        store.put('batches', bid, batch)
        observations = [{'sample_id': sample['sample_id'], 'sequence': sample['candidate']['sequence'],
                         'value': 1.0, 'qc': 'valid', 'batch_id': bid, 'metric': 'fitness',
                         'unit': 'a.u.', 'source': 'synthetic', 'assay_protocol': 'synthetic-only'}
                        for sample in samples]
        if missing and round_index == 7:
            observations[-1].update(value=None, qc='unavailable', source='measured_replay')
        store.put('measurements', bid, observations)
        store.reserve(bid, 'experimental_wells', 16, batch)
        store.settle(bid)
        if round_index == 4:
            for i in range(failed_attempts):
                attempt(round_index, f'failure-{i}')
        attempt(round_index, 'success', candidates)
    return store, state, task, workflow, attempt


@pytest.mark.parametrize('failed_attempts,missing', [(0, False), (1, False), (1, True), (8, True)])
def test_completed_audit_accounts_failed_attempts_without_replacing_outcomes(tmp_path, failed_attempts, missing):
    store, state, task, workflow, _ = completed_audit_fixture(
        tmp_path, failed_attempts=failed_attempts, missing=missing)
    before_usage = store.usage()
    before_outputs = store.all('research_step_outputs')
    report = runner.audit_completed_case(store, state, task, workflow)
    assert report['successful_selection_rounds'] == 8
    assert report['charged_tool_attempts'] == 8 + failed_attempts
    assert report['failed_program_attempts'] == failed_attempts
    assert report['distinct_charged_queries'] == 128
    assert report['returned_qc_counts'] == ({'unavailable': 1, 'valid': 127} if missing else {'valid': 128})
    assert store.usage() == before_usage
    assert store.all('research_step_outputs') == before_outputs
    assert runner.audit_completed_case(store, state, task, workflow) == report


@pytest.mark.parametrize('tamper', ['reserved', 'wrong_rounds', 'duplicate_query', 'extra_success',
                                  'missing_receipt', 'missing_charge', 'wrong_code', 'unknown_failure',
                                  'unfinished_job', 'over_cap'])
def test_completed_audit_rejects_unexplained_accounting(tmp_path, tamper):
    store, state, task, workflow, attempt = completed_audit_fixture(tmp_path)
    if tamper == 'reserved':
        store.reserve('synthetic-reserved', 'tool_calls', 1, {})
    elif tamper == 'wrong_rounds':
        state['round_index'] = 7
    elif tamper == 'duplicate_query':
        batches = store.all('batches')
        batch = batches['synthetic-batch-7']
        batch['samples'][0]['candidate'] = batches['synthetic-batch-0']['samples'][0]['candidate']
        store.put('batches', 'synthetic-batch-7', batch)
    elif tamper == 'extra_success':
        batch = store.all('batches')['synthetic-batch-4']
        attempt(4, 'extra-success', [s['candidate'] for s in batch['samples']])
    elif tamper == 'missing_charge':
        with store.connect() as con:
            con.execute("DELETE FROM charges WHERE key=(SELECT key FROM charges WHERE resource='tool_calls' LIMIT 1)")
    elif tamper == 'over_cap':
        with store.connect() as con:
            con.execute("UPDATE charges SET amount=10 WHERE key=(SELECT key FROM charges WHERE resource='tool_calls' LIMIT 1)")
    else:
        key, job = next(iter(store.all('tool_jobs').items()))
        if tamper == 'missing_receipt':
            with store.connect() as con:
                con.execute("DELETE FROM kv WHERE namespace='research_step_outputs' AND key=?", (job['context'],))
        elif tamper == 'wrong_code':
            job['result']['code_sha256'] = '0' * 64
            store.put('tool_jobs', key, job)
        elif tamper == 'unknown_failure':
            job['result'] = {**job['result'], 'status': 'unknown'}
            store.put('tool_jobs', key, job)
        elif tamper == 'unfinished_job':
            job['state'] = 'started'
            store.put('tool_jobs', key, job)
    with pytest.raises(ValueError):
        runner.audit_completed_case(store, state, task, workflow)


def test_supported_resume_after_saved_timeout_receipt(tmp_path, monkeypatch):
    """Inject one timeout receipt; successful selections still execute real bwrap."""
    from proteinrsi.contracts import digest
    from proteinrsi.research import code as code_module
    original = code_module.execute_code
    failed_once = []
    def timeout_once(store, view, arguments, **kwargs):
        if view.round_index == 4 and not failed_once:
            failed_once.append(True)
            return {'status': 'failed', 'error_type': 'Timeout', 'error': 'synthetic timeout receipt',
                    'execution_backend': 'bwrap_seccomp_generated_v1', 'measurement_authority': False,
                    'code_sha256': digest(arguments['code']), 'evidence_kind': 'computed_unvalidated'}
        return original(store, view, arguments, **kwargs)
    monkeypatch.setattr(code_module, 'execute_code', timeout_once)
    matrix = matrix_fixture(tmp_path)
    with pytest.raises((ValueError, RuntimeError), match='Frozen selection program failed'):
        runner.run_matrix(matrix, tmp_path / 'out', execute=True)
    store = Store(next((tmp_path / 'out').glob('case-*')))
    failed_receipts = {key: value for key, value in store.all('research_step_outputs').items()
                       if value['result']['status'] == 'failed'}
    assert len(failed_receipts) == 1
    result = runner.run_matrix(matrix, tmp_path / 'out', execute=True)
    report = result['case_aggregates'][0]['execution_audit']
    assert report['charged_tool_attempts'] == 9 and report['failed_program_attempts'] == 1
    assert report['successful_selection_rounds'] == 8 and report['distinct_charged_queries'] == 128
    assert report['returned_qc_counts'] == {'valid': 128}
    assert all(store.all('research_step_outputs')[key] == value for key, value in failed_receipts.items())
    before = store.usage()
    assert runner.run_matrix(matrix, tmp_path / 'out', execute=True) == result
    assert store.usage() == before
