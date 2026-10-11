"""Regression coverage for model drafts, durable returns and contract recovery."""
from types import SimpleNamespace

import pytest

from proteinrsi.contracts import Candidate, sequence_hash
from proteinrsi.dataflow.protocol import (
    Operation, OperationRegistry, Protocol, ProtocolExecutor, register_agent_operations,
)
from proteinrsi.dataflow.resources import NAMESPACE, SEQUENCES
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.dataflow.responses import validated_response
from proteinrsi.execution_status import execution_status


def sequence_protocol():
    return Protocol.model_validate({
        'hypothesis': 'Design from available feedback',
        'agent_operations': [{'name': 'agent:design_plate', 'role': 'B',
            'instructions': 'Propose a variant', 'input_schema': {'type': 'object'},
            'output_schema_ref': SEQUENCES}],
        'steps': [{'step_id': 'design', 'operation': 'agent:design_plate', 'question': 'Variant?'}],
        'final_outputs': {'candidates': {'source': 'step:design.result', 'schema_ref': SEQUENCES}}})


def test_custom_design_repairs_draft_and_computes_identity(resources, view, store):
    calls = []
    replies = [
        {'items': [{'candidate_id': 'seq:' + '0' * 64, 'sequence': view.task.reference_sequence}]},
        {'edits': [[{'position': 1, 'from': 'M', 'to': 'A'}]]},
        {'edits': [[{'position': 39, 'from': 'V', 'to': 'I'}]]},
    ]
    def complete(role, instructions, context, schema):
        calls.append(context)
        assert 'items' not in schema['properties']
        assert 'tool_calls' not in schema['properties']
        return replies[len(calls) - 1]
    protocol = sequence_protocol()
    operations = OperationRegistry(resources.registry)
    register_agent_operations(protocol, operations, SimpleNamespace(complete=complete),
                              resources, view.model_dump(mode='json'))
    executor = ProtocolExecutor(resources, operations)
    output = executor.execute(protocol, {})
    entry = resources.get(output['outputs']['candidates']['resource_id'])['data']['items'][0]
    expected = view.task.reference_sequence[:38] + 'I' + view.task.reference_sequence[39:]
    assert entry['sequence'] == expected
    assert entry['candidate_id'] == 'seq:' + sequence_hash(expected)
    assert len(calls) == 3
    assert 'response_repair' in calls[1]
    executor.execute(protocol, {})
    assert len(calls) == 3
    assert store.usage()['experimental_wells']['committed'] == 0
    receipts = [r for k, r in store.all(NAMESPACE).items() if '/response/' in k]
    assert [r['status'] for r in receipts] == ['invalid', 'invalid', 'validated']


def test_semantic_identity_error_is_a_contract_error(resources, view):
    with pytest.raises(ContractError) as error:
        resources.put(SEQUENCES, {'items': [{'candidate_id': 'seq:' + '0' * 64,
            'sequence': view.task.reference_sequence}]}, producer='untrusted')
    assert error.value.code == 'domain_validation'
    assert error.value.path == '/items/0'


def test_returned_operation_survives_publication_failure(resources, store, monkeypatch):
    resources.registry.register('custom.result/v1', {'type': 'object'}, custom=True)
    calls = []
    ops = OperationRegistry(resources.registry)
    ops.register(Operation('tool:fixture', {'type': 'object'}, 'custom.result/v1',
                           lambda a, k: calls.append(k) or {'value': 42}, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'durable return',
        'steps': [{'step_id': 'compute', 'operation': 'tool:fixture', 'question': 'compute'}],
        'final_outputs': {'result': {'source': 'step:compute.result', 'schema_ref': 'custom.result/v1'}}})
    original = resources.put
    monkeypatch.setattr(resources, 'put', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk fault')))
    executor = ProtocolExecutor(resources, ops)
    with pytest.raises(OSError):
        executor.execute(protocol, {})
    assert any(r.get('status') == 'returned' for r in store.all(NAMESPACE).values())
    monkeypatch.setattr(resources, 'put', original)
    result = executor.execute(protocol, {})
    assert len(calls) == 1
    assert resources.get(result['outputs']['result']['resource_id'])['data']['value'] == 42


def test_valid_response_not_resampled(store):
    calls = []
    llm = SimpleNamespace(complete=lambda *a: calls.append(a) or {'sequence': 'ACDE'})
    for _ in range(2):
        result = validated_response(store, llm, key='decision', role='M', instructions='fixed',
            context={}, schema=Candidate.model_json_schema(), validate=Candidate.model_validate)
        assert result.sequence == 'ACDE'
    assert len(calls) == 1


def test_health_does_not_confuse_ready_with_running(store):
    store.event('run_started', {})
    store.event('run_failed', {'error_type': 'ContractError'})
    with store.connect() as con:
        assert execution_status(con, 'ready')['state'] == 'blocked'
    store.event('run_started', {'resume': True})
    with store.connect() as con:
        assert execution_status(con, 'ready')['state'] == 'in_progress_or_interrupted'


def test_custom_binder_design_uses_task_contract_not_gb1(resources, view):
    from proteinrsi.contracts import TaskKind, TaskSpec
    task = TaskSpec(name='binder', kind=TaskKind.BINDER, reference_sequence='',
        target_sequence='ACDE', candidate_access='open', min_length=4, max_length=6,
        controls_per_batch=0, execution_mode='computational', feedback_source='computational')
    binder = view.model_copy(update={'task': task})
    llm = SimpleNamespace(complete=lambda *a: {'candidates': [{'sequence': 'FGHIK'}]})
    ops = OperationRegistry(resources.registry)
    protocol = sequence_protocol()
    register_agent_operations(protocol, ops, llm, resources, binder.model_dump(mode='json'))
    result = ProtocolExecutor(resources, ops).execute(protocol, {})
    assert resources.candidates(result['outputs']['candidates']['resource_id'])[0].sequence == 'FGHIK'


def test_ranking_unknown_identity_rejected_at_publication(resources, view):
    from proteinrsi.dataflow.resources import RANKING
    source = resources.sequences([Candidate(sequence=view.task.reference_sequence)], producer='input')
    with pytest.raises(ContractError, match='outside its source'):
        resources.put(RANKING, {'candidate_set_ref': source['resource_id'],
            'ordered_ids': ['seq:' + '0' * 64]}, producer='model')


def test_model_repair_resumes_after_provider_pause_without_resampling(store):
    from proteinrsi.llm import ProviderPaused
    calls = []
    def complete(*args):
        calls.append(args)
        if len(calls) == 1:
            return {'unexpected': 'invalid'}
        if len(calls) == 2:
            raise ProviderPaused('fixture pause')
        return {'sequence': 'ACDE'}
    llm = SimpleNamespace(complete=complete)
    kwargs = dict(key='paused', role='B', instructions='fixed', context={'evidence': 'frozen'},
                  schema=Candidate.model_json_schema(), validate=Candidate.model_validate)
    with pytest.raises(ProviderPaused):
        validated_response(store, llm, **kwargs)
    assert validated_response(store, llm, **kwargs).sequence == 'ACDE'
    assert len(calls) == 3
    assert calls[1] == calls[2]


def test_unknown_tool_completion_is_not_retried(resources, store):
    resources.registry.register('custom.result/v1', {'type': 'object'}, custom=True)
    calls = []
    ops = OperationRegistry(resources.registry)
    def invoke(*args):
        calls.append(args)
        raise KeyboardInterrupt('process stopped before durable return')
    ops.register(Operation('tool:uncertain', {'type': 'object'}, 'custom.result/v1', invoke, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'uncertain execution',
        'steps': [{'step_id': 'work', 'operation': 'tool:uncertain', 'question': 'compute'}],
        'final_outputs': {'result': {'source': 'step:work.result', 'schema_ref': 'custom.result/v1'}}})
    executor = ProtocolExecutor(resources, ops)
    with pytest.raises(KeyboardInterrupt):
        executor.execute(protocol, {})
    with pytest.raises(ContractError, match='uncertain completion'):
        executor.execute(protocol, {})
    assert len(calls) == 1
