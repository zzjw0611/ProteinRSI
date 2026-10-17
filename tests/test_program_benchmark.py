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
assert len(context['observations']) == 1 + context['round_index'] * context['task']['batch_size']
alphabet = 'ACDEFGHIKLMNPQRSTVWY'
sequences = [''.join(pair) + 'AA' for pair in itertools.product(alphabet, repeat=2)]
random.Random(context['task']['seed']).shuffle(sequences)
excluded = {o['sequence'] for o in context['observations']} | {'AAAA'}
result = {'candidates': [{'sequence': s, 'source': 'synthetic-protocol-fixture'}
                         for s in sequences if s not in excluded][:context['task']['batch_size']]}
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


@pytest.mark.parametrize('rounds,size', [(4, 16), (8, 16), (3, 7)])
def test_synthetic_real_bwrap_full_loop_and_resume(tmp_path, rounds, size):
    matrix = matrix_fixture(tmp_path)
    limits = dict(max_rounds=rounds, batch_size=size, query_budget=rounds * size)
    result = runner.run_matrix(matrix, tmp_path/'out', execute=True, **limits)
    assert result['llm_calls'] == 0 and result['prompt_workflow_evaluated'] is False
    case = result['case_aggregates'][0]
    assert case['metrics']['charged_queries'] == rounds * size
    assert len(case['metrics']['equal_query_curve']) == rounds
    assert case['usage']['tool_calls']['committed'] == rounds
    saved = next((tmp_path/'out').glob('case-*'))
    store = Store(saved)
    outputs = store.all('research_step_outputs')
    assert len(outputs) == rounds
    assert all(v['result']['execution_backend'] == 'bwrap_seccomp_generated_v1' for v in outputs.values())
    batches = store.all('batches')
    assert len(batches) == rounds
    assert all(len(b['samples']) == size for b in batches.values())
    before = store.usage()
    assert runner.run_matrix(matrix, tmp_path/'out', execute=True, **limits) == result
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


def completed_audit_fixture(tmp_path, *, failed_attempts=0, missing=False, rounds=8):
    """Construct saved ledger/receipt shapes without executing a policy or model."""
    from proteinrsi.contracts import digest
    row, task, workflow = runner.prepare_matrix(matrix_fixture(tmp_path), max_rounds=rounds, query_budget=rounds * 16)[0]
    store = Store(tmp_path / 'completed-audit')
    store.configure_budget(task.budget.model_dump())
    state = {'task': task.model_dump(mode='json'), 'workflow': workflow.model_dump(mode='json'),
             'round_index': rounds, 'status': 'complete', 'pending_batch': None}
    sequences = [''.join(pair) + 'AA' for pair in itertools.product('ACDEFGHIKLMNPQRSTVWY', repeat=2)
                 if pair != ('A', 'A')][:rounds * 16]
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
    for round_index in range(rounds):
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
        if missing and round_index == rounds - 1:
            observations[-1].update(value=None, qc='unavailable', source='measured_replay')
        store.put('measurements', bid, observations)
        store.reserve(bid, 'experimental_wells', 16, batch)
        store.settle(bid)
        if round_index == 2:
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


@pytest.mark.parametrize('rounds', [4, 8])
def test_supported_resume_after_saved_timeout_receipt(tmp_path, monkeypatch, rounds):
    """Inject one timeout receipt; successful selections still execute real bwrap."""
    from proteinrsi.contracts import digest
    from proteinrsi.research import code as code_module
    original = code_module.execute_code
    failed_once = []
    def timeout_once(store, view, arguments, **kwargs):
        if view.round_index == 2 and not failed_once:
            failed_once.append(True)
            return {'status': 'failed', 'error_type': 'Timeout', 'error': 'synthetic timeout receipt',
                    'execution_backend': 'bwrap_seccomp_generated_v1', 'measurement_authority': False,
                    'code_sha256': digest(arguments['code']), 'evidence_kind': 'computed_unvalidated'}
        return original(store, view, arguments, **kwargs)
    monkeypatch.setattr(code_module, 'execute_code', timeout_once)
    matrix = matrix_fixture(tmp_path)
    limits = dict(max_rounds=rounds, batch_size=16, query_budget=rounds * 16)
    with pytest.raises((ValueError, RuntimeError), match='Frozen selection program failed'):
        runner.run_matrix(matrix, tmp_path / 'out', execute=True, **limits)
    store = Store(next((tmp_path / 'out').glob('case-*')))
    failed_receipts = {key: value for key, value in store.all('research_step_outputs').items()
                       if value['result']['status'] == 'failed'}
    assert len(failed_receipts) == 1
    result = runner.run_matrix(matrix, tmp_path / 'out', execute=True, **limits)
    report = result['case_aggregates'][0]['execution_audit']
    assert report['charged_tool_attempts'] == rounds + 1 and report['failed_program_attempts'] == 1
    assert report['successful_selection_rounds'] == rounds and report['distinct_charged_queries'] == rounds * 16
    assert report['returned_qc_counts'] == {'valid': rounds * 16}
    assert all(store.all('research_step_outputs')[key] == value for key, value in failed_receipts.items())
    before = store.usage()
    assert runner.run_matrix(matrix, tmp_path / 'out', execute=True, **limits) == result
    assert store.usage() == before


@pytest.mark.parametrize('budgets', [
    {'batch_size': 16, 'max_rounds': 4, 'query_budget': 128},
    {'batch_size': 1, 'query_budget': 8},
    {'batch_size': 385, 'query_budget': 3080}, {'batch_size': 0}, {'max_rounds': -1}, {'query_budget': True},
    {'batch_size': 16.0}, {'query_budget': '128'},
])
def test_wrong_budgets_fail_before_output(tmp_path, budgets):
    with pytest.raises(ValueError):
        runner.run_matrix(matrix_fixture(tmp_path), tmp_path / 'out', execute=True, **budgets)
    assert not (tmp_path / 'out').exists()


def test_changed_frozen_budget_or_runtime_never_migrates_old_run(tmp_path):
    matrix = matrix_fixture(tmp_path)
    destination = tmp_path / 'out'
    runner.run_matrix(matrix, destination, execute=True)
    manifest = destination / 'frozen-manifest.json'
    # Snapshot every byte after the completed default-CLI run, including its ledger.
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='different frozen matrix or runtime'):
        runner.run_matrix(matrix, destination, execute=True, max_rounds=4, query_budget=64)
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before
    frozen = json.loads(manifest.read_text())
    frozen['runner_sha256'] = '0' * 64
    manifest.write_text(json.dumps(frozen))
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='different frozen matrix or runtime'):
        runner.run_matrix(matrix, destination, execute=True)
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('rounds', [4, 8])
def test_supplied_common_initialization_and_seed_round_context(tmp_path, rounds):
    # Engineering-only policy supplied inline. No repository scientific policy changes.
    code = FIXTURE_CODE.replace("random.Random(context['task']['seed'])",
        "random.Random(context['task']['seed'] + context['round_index'])")
    code += "\nassert context['task']['max_rounds'] == inputs['rounds']\n"
    code += "assert context['task']['budget']['experimental_wells'] == inputs['rounds'] * 16\n"
    firsts = []
    for arm in ('fixture-a', 'fixture-b'):
        folder = tmp_path / arm
        folder.mkdir()
        matrix = matrix_fixture(folder, code)
        program_path = Path(matrix[0]['program_file'])
        program = MethodProgram.model_validate_json(program_path.read_text())
        program = program.model_copy(update={'name': arm, 'inputs': {'rounds': rounds}})
        program_path.write_text(program.model_dump_json())
        matrix[0]['program_sha256'] = runner.file_sha256(program_path)
        runner.run_matrix(matrix, folder / 'out', execute=True, max_rounds=rounds, query_budget=rounds * 16)
        store = Store(next((folder / 'out').glob('case-*')))
        batch = next(b for b in store.all('batches').values() if b['round_index'] == 0)
        firsts.append([s['candidate']['sequence'] for s in batch['samples']])
        initial = store.get('configuration', 'provided_initial_evidence')['observations']
        assert len(initial) == 1 and initial[0]['sequence'] == 'AAAA'
    assert firsts[0] == firsts[1]


@pytest.mark.parametrize('rounds', [4, 8])
def test_configured_audit_counts_missing_and_timeout_without_mutation(tmp_path, rounds):
    store, state, task, workflow, _ = completed_audit_fixture(
        tmp_path, rounds=rounds, failed_attempts=1, missing=True)
    before = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    report = runner.audit_completed_case(store, state, task, workflow)
    assert report['distinct_charged_queries'] == rounds * 16
    assert report['charged_tool_attempts'] == rounds + 1
    assert report['failed_attempt_types'] == {'Timeout': 1}
    assert report['returned_qc_counts'] == {'unavailable': 1, 'valid': rounds * 16 - 1}
    assert {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('rounds', [4, 8])
def test_absent_measurement_keeps_purchased_identity_and_budget(tmp_path, rounds):
    store, state, task, workflow, _ = completed_audit_fixture(tmp_path, rounds=rounds)
    bid = f'synthetic-batch-{rounds - 1}'
    observations = store.all('measurements')[bid]
    store.put('measurements', bid, observations[:-1])
    before = store.usage()
    report = runner.audit_completed_case(store, state, task, workflow)
    assert report['returned_qc_counts'] == {'not_returned': 1, 'valid': rounds * 16 - 1}
    assert report['distinct_charged_queries'] == rounds * 16
    assert store.usage() == before


@pytest.mark.parametrize('field', ['python', 'installed_dependency_versions'])
def test_interpreter_and_dependency_drift_refuse_resume(tmp_path, monkeypatch, field):
    from copy import deepcopy
    from proteinrsi import evaluation_metrics
    matrix = matrix_fixture(tmp_path)
    destination = tmp_path / 'out'
    limits = dict(max_rounds=1, batch_size=2, query_budget=2)
    result = runner.run_matrix(matrix, destination, execute=True, **limits)
    store = Store(next(destination.glob('case-*')))
    before_usage, before_charges = store.usage(), runner._saved_charges(store)
    namespaces = ('batches', 'measurements', 'research_step_outputs', 'tool_jobs')
    before_records = {name: store.all(name) for name in namespaces}
    assert runner.run_matrix(matrix, destination, execute=True, **limits) == result
    assert store.usage() == before_usage and runner._saved_charges(store) == before_charges
    assert {name: store.all(name) for name in namespaces} == before_records
    # Valid resume may update reversible execution-control bookkeeping. Rejected
    # drift must fail before opening a campaign, leaving every saved byte intact.
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    original = evaluation_metrics.runtime_identity
    identity = deepcopy(original())
    identity[field] = ('different-interpreter' if field == 'python'
                       else identity[field] + [['synthetic-dependency-drift', '0']])
    monkeypatch.setattr(evaluation_metrics, 'runtime_identity', lambda: identity)
    with pytest.raises(ValueError, match='different frozen matrix or runtime'):
        runner.run_matrix(matrix, destination, execute=True, **limits)
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before


def test_pre_runtime_identity_manifest_is_never_migrated(tmp_path):
    matrix = matrix_fixture(tmp_path)
    destination = tmp_path / 'out'
    limits = dict(max_rounds=1, batch_size=2, query_budget=2)
    runner.run_matrix(matrix, destination, execute=True, **limits)
    manifest = destination / 'frozen-manifest.json'
    frozen = json.loads(manifest.read_text())
    del frozen['runtime']
    manifest.write_text(json.dumps(frozen))
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='different frozen matrix or runtime'):
        runner.run_matrix(matrix, destination, execute=True, **limits)
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before
