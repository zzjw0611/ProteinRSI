import pytest

from proteinrsi.agents import Plan
from proteinrsi.dataflow.design import request_design, proposal_contract
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.llm import ProviderPaused


def test_repair_retains_valid_candidates_and_supplies_original_error(store, view):
    reference = view.task.reference_sequence
    good = reference[:38] + 'A' + reference[39:]
    invalid = reference[:52] + '\u0422' + reference[53:]
    class LLM:
        calls = 0
        def complete(self, role, instructions, context, schema):
            self.calls += 1
            if self.calls == 1:
                return {'candidates': [{'sequence': good}, {'sequence': invalid}]}
            error = context['validation_errors'][0]
            assert error['original']['sequence'] == invalid
            assert error['sequence_checks']['noncanonical'][0]['codepoint'] == 'U+0422'
            assert context['repair_state']['retained_count'] == 1
            assert context['repair_state']['retained_candidate_set']['count'] == 1
            return {'edits': [[{'position': 40, 'from': 'D', 'to': 'W'}]]}
    llm = LLM()
    result = request_design(llm, store, 'design', view, Plan(rationale='Two designs'), [], [])
    assert {c.sequence for c in result.candidates} == {good, reference[:39] + 'W' + reference[40:]}
    assert invalid not in {c.sequence for c in result.candidates}
    assert request_design(llm, store, 'design', view, Plan(rationale='Two designs'), [], []) == result
    assert llm.calls == 2


def test_provider_pause_does_not_advance_format_attempt_or_lose_candidates(store, view):
    class LLM:
        requests = []
        def complete(self, role, instructions, context, schema):
            self.requests.append(context)
            if len(self.requests) == 1:
                return {'edits': [[{'position': 39, 'from': 'V', 'to': 'A'}], [{'wrong': 1}]]}
            if len(self.requests) == 2:
                raise ProviderPaused('retry limit')
            return {'edits': [[{'position': 40, 'from': 'D', 'to': 'W'}]]}
    llm = LLM()
    args = (llm, store, 'design', view, Plan(rationale='Two designs'), [], [])
    with pytest.raises(ProviderPaused):
        request_design(*args)
    result = request_design(*args)
    assert llm.requests[1] == llm.requests[2]
    assert len(result.candidates) == 2
    assert llm.requests[2]['format_attempt'] == 1


def test_infeasible_plan_returns_to_protocol_instead_of_fabricating_candidates(store, view):
    class LLM:
        def complete(self, *args):
            return {'replan_reason': 'The requested pair allocation cannot fit the remaining slots'}
    with pytest.raises(ContractError) as exc:
        request_design(LLM(), store, 'design', view, Plan(rationale='paired panel'), [], [])
    assert exc.value.code == 'plan_infeasible'


def test_design_contract_is_task_dependent_not_gb1_specific(view):
    from proteinrsi.contracts import TaskSpec
    assert proposal_contract(view)['preferred_representation'] == 'edits_or_candidate_refs'
    binder = view.model_copy(update={'task': TaskSpec(name='binder', kind='binder_design',
        target_sequence='ACDE', min_length=8, max_length=20, controls_per_batch=0)})
    assert proposal_contract(binder)['preferred_representation'] == 'sequences_or_candidate_refs'
    assert '39' not in str(proposal_contract(binder)['sequence_construction'])


def test_binder_direct_sequences_remain_supported(store, view):
    from proteinrsi.contracts import TaskSpec
    binder = view.model_copy(update={'task': TaskSpec(name='binder', kind='binder_design',
        target_sequence='ACDE', min_length=8, max_length=20, controls_per_batch=0)})
    class LLM:
        def complete(self, *args):
            return {'candidates': [{'sequence': 'ACDEFGHIK'}]}
    result = request_design(LLM(), store, 'binder design', binder, Plan(rationale='test'), [], [])
    assert result.candidates[0].sequence == 'ACDEFGHIK'


@pytest.mark.parametrize('fault', ['none', 'failed_check', 'wrong_set', 'agent_assertion', 'missing_check'])
def test_scientific_panel_checks_bind_to_executed_result(team, resources, view, fault):
    from proteinrsi.dataflow.integration import build_operations
    from proteinrsi.contracts import sequence_hash
    sequence = view.task.reference_sequence
    ref = resources.sequences([{'sequence': sequence}], producer='test')['resource_id']
    resources.registry.register('tool.panel_check/v1', {'type': 'object'})
    output = {'status': 'ok', 'output': {
        'candidate_ids': ['seq:'+sequence_hash(sequence)],
        'checks': [{'name': 'paired_backgrounds', 'passed': fault != 'failed_check'}]}}
    if fault == 'wrong_set':
        output['output']['candidate_ids'] = ['seq:'+'0'*64]
    if fault == 'missing_check':
        output['output']['checks'][0]['name'] = 'count_only'
    check = resources.put('tool.panel_check/v1', output,
        producer='agent:C' if fault == 'agent_assertion' else 'tool:research_python@fixture')['resource_id']
    operation = build_operations(team, view, resources).get('adapter:accept_checked_candidates')
    args = {'candidates': ref, 'check_result': check, 'required_checks': ['paired_backgrounds']}
    if fault == 'none':
        assert operation.invoke(args, 'test')['items'][0]['sequence'] == sequence
    else:
        with pytest.raises(ContractError):
            operation.invoke(args, 'test')


def test_protocol_provider_resume_reuses_successful_upstream_step(resources, store):
    from proteinrsi.dataflow.protocol import Protocol, ProtocolExecutor, Operation, OperationRegistry
    resources.registry.register('fixture.result/v1', {'type': 'object'})
    operations = OperationRegistry(resources.registry)
    calls = []
    def upstream(args, key):
        calls.append('upstream')
        return {'ok': True}
    def downstream(args, key):
        calls.append('downstream')
        if calls.count('downstream') == 1:
            raise ProviderPaused('retry limit')
        return {'ok': True}
    for name, invoke in [('first', upstream), ('second', downstream)]:
        operations.register(Operation(name, {'type': 'object'}, 'fixture.result/v1', invoke, 'test'))
    protocol = Protocol(hypothesis='Resume a downstream request', steps=[
        {'step_id': 'first', 'operation': 'first', 'question': 'upstream'},
        {'step_id': 'second', 'operation': 'second', 'question': 'downstream'}],
        final_outputs={'result': {'source': 'step:second.result', 'schema_ref': 'fixture.result/v1'}})
    executor = ProtocolExecutor(resources, operations)
    with pytest.raises(ProviderPaused):
        executor.execute(protocol, {})
    executor.execute(protocol, {})
    assert calls == ['upstream', 'downstream', 'downstream']
