# SPDX-License-Identifier: MIT
import copy

import pytest

from proteinrsi.contracts import Candidate, TaskKind, TaskSpec, sequence_hash
from proteinrsi.dataflow.protocol import (Protocol, Operation, OperationRegistry, ProtocolExecutor,
                                        register_tool_operations, register_agent_operations)
from proteinrsi.dataflow.resources import SEQUENCES, RANKING, ESTIMATES, ResourceStore, standard_registry
from proteinrsi.dataflow.schema import SchemaRegistry, ContractError, pointer
from proteinrsi.dataflow.tasks import default_profiles
from proteinrsi.dataflow.integration import run_campaign_protocol
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.tools import ToolSpec, ToolGateway


@pytest.mark.parametrize('schema', [
    {'$ref': 'https://example.org/remote-schema'},
    {'$ref': 'file:///etc/passwd'},
    {'$defs': {'x': {'$ref': '#/$defs/x'}}, '$ref': '#/$defs/x'},
    {'type': 'string', 'pattern': '(a+)+$'},
])
def test_untrusted_schema_cannot_fetch_or_execute(schema):
    with pytest.raises(ContractError):
        SchemaRegistry().register('custom.output/v1', schema, custom=True)


def test_schema_version_cannot_be_overwritten():
    registry = SchemaRegistry()
    registry.register('custom.output/v1', {'type': 'object'}, custom=True)
    with pytest.raises(ContractError, match='different definition'):
        registry.register('custom.output/v1', {'type': 'array'}, custom=True)


@pytest.mark.parametrize('path', ['x', '/~9', '/rows/-1', '/rows/01', '/rows/7'])
def test_json_pointer_is_not_eval_or_fuzzy_parser(path):
    with pytest.raises(ContractError):
        pointer({'rows': [5]}, path)


def test_downstream_schema_failure_reuses_successful_tool(resources, view, store):
    gateway = ToolGateway(store)
    calls = []
    gateway.register(ToolSpec(name='native', capability='test', implementation_version='test',
        task_kinds=[TaskKind.VARIANT], input_schema={'type': 'object', 'additionalProperties': False},
        output_schema={'type': 'object', 'properties': {'actual': {'type': 'object'}}, 'required': ['actual']}),
        lambda args: calls.append(args) or {'actual': {'ok': True}})
    operations = OperationRegistry(resources.registry)
    register_tool_operations(operations, gateway, view.task, ['native'])
    resources.registry.register('custom.checked/v1', {'type': 'object', 'properties': {'ok': {'const': True}},
                                                    'required': ['ok']}, custom=True)
    spec = {'hypothesis': 'Data binding repair', 'steps': [{'step_id': 'native', 'operation': 'tool:native',
        'question': 'Read the actual output', 'outputs': {'checked': {'schema_ref': 'custom.checked/v1', 'pointer': '/wrong'}}}],
        'final_outputs': {'result': {'source': 'step:native.checked', 'schema_ref': 'custom.checked/v1'}}}
    executor = ProtocolExecutor(resources, operations)
    with pytest.raises(ContractError):
        executor.execute(Protocol.model_validate(spec), {})
    spec['steps'][0]['outputs']['checked']['pointer'] = '/actual'
    result = executor.execute(Protocol.model_validate(spec), {})
    assert resources.get(result['outputs']['result']['resource_id'])['data'] == {'ok': True}
    assert len(calls) == 1
    assert store.usage()['tool_calls']['committed'] == 1


def test_forward_edge_rejected_before_invocation(resources):
    resources.registry.register('custom.obj/v1', {'type': 'object'}, custom=True)
    ops = OperationRegistry(resources.registry)
    calls = []
    ops.register(Operation('adapter:identity', {'type': 'object'}, 'custom.obj/v1',
                           lambda a,k: calls.append(a) or a, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'forward edge test', 'steps': [{
        'step_id': 'one', 'operation': 'adapter:identity', 'question': 'test',
        'bindings': {'x': {'source': 'step:two.result', 'schema_ref': 'custom.obj/v1'}}}],
        'final_outputs': {'x': {'source': 'step:one.result', 'schema_ref': 'custom.obj/v1'}}})
    with pytest.raises(ContractError, match='forward'):
        ProtocolExecutor(resources, ops).execute(protocol, {})
    assert not calls


def test_no_measurement_authority_via_custom_output(resources):
    with pytest.raises(ContractError, match='task boundary'):
        resources.put('context.task/v1', {'observations': [{'value': 99}]}, producer='agent:A')


def test_missing_required_input_detected_before_tool(resources):
    resources.registry.register('custom.obj/v1', {'type': 'object'}, custom=True)
    ops = OperationRegistry(resources.registry)
    ops.register(Operation('tool:need_input', {'type': 'object', 'required': ['backbone'],
        'properties': {'backbone': {'type': 'string'}}}, 'custom.obj/v1', lambda a,k: {}, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'missing input', 'steps': [{
        'step_id': 'one', 'operation': 'tool:need_input', 'question': 'test'}],
        'final_outputs': {'result': {'source': 'step:one.result', 'schema_ref': 'custom.obj/v1'}}})
    with pytest.raises(ContractError, match='required'):
        ProtocolExecutor(resources, ops).preflight(protocol, {})


def test_task_specific_ranking_has_no_design_operation(resources, view):
    parent = view.task.reference_sequence
    changed = list(parent)
    changed[38] = 'A'
    changed = ''.join(changed)
    task = TaskSpec(name='ranking', kind=TaskKind.RANKING, reference_sequence='', candidates=[parent, changed],
        candidate_access='pool', controls_per_batch=0, execution_mode='computational', feedback_source='computational')
    ranked_view = view.model_copy(update={'task': task})
    seq_ref = resources.sequences([Candidate(sequence=s) for s in task.candidates], producer='user')['resource_id']
    ids = [i['candidate_id'] for i in resources.get(seq_ref)['data']['items']]
    rank_ref = resources.put(RANKING, {'candidate_set_ref': seq_ref, 'ordered_ids': ids[::-1], 'summary': 'test'},
                             producer='agent:C')['resource_id']
    assert [c.sequence for c in default_profiles().accept(ranked_view, resources, {'ranking': rank_ref})] == task.candidates[::-1]


def test_affinity_output_is_not_forced_into_sequence_design(resources, view):
    task = TaskSpec(name='affinity', kind=TaskKind.AFFINITY, reference_sequence='ACDE', target_sequence='FGHI',
        mutable_positions=[], controls_per_batch=0, execution_mode='computational', feedback_source='computational',
        metric='Kd', unit='nM')
    fixed = view.model_copy(update={'task': task})
    estimate = {'subject_refs': ['seq:' + sequence_hash(s) for s in ['ACDE', 'FGHI']],
        'property': 'Kd', 'value': None, 'unit': 'nM', 'method': 'not_available', 'support_refs': [],
        'limitations': 'No calibrated predictor is configured.'}
    ref = resources.put(ESTIMATES, {'estimates': [estimate]}, producer='agent:C')['resource_id']
    assert default_profiles().accept(fixed, resources, {'estimates': ref}) == []
    estimate['value'] = 1.0
    invalid = resources.put(ESTIMATES, {'estimates': [estimate]}, producer='agent:C')['resource_id']
    with pytest.raises(ContractError, match='evidence'):
        default_profiles().accept(fixed, resources, {'estimates': invalid})


def test_generated_agent_step_repairs_only_its_output(resources):
    registry = OperationRegistry(resources.registry)
    protocol = Protocol.model_validate({'hypothesis': 'typed metric',
        'schemas': {'custom.metric/v1': {'type': 'object', 'properties': {'value': {'type': 'number'}},
                                       'required': ['value'], 'additionalProperties': False}},
        'agent_operations': [{'name': 'agent:metric', 'role': 'C', 'instructions': 'Compute fixture only',
                              'input_schema': {'type': 'object'}, 'output_schema_ref': 'custom.metric/v1'}],
        'steps': [{'step_id': 'metric', 'operation': 'agent:metric', 'question': 'fixture'}],
        'final_outputs': {'metric': {'source': 'step:metric.result', 'schema_ref': 'custom.metric/v1'}}})
    class LLM:
        calls = 0
        def complete(self, role, instructions, context, schema):
            self.calls += 1
            return {'value': 'wrong' if self.calls == 1 else 2.0}
    llm = LLM()
    register_agent_operations(protocol, registry, llm, resources, {}, max_repairs=1)
    result = ProtocolExecutor(resources, registry).execute(protocol, {})
    assert resources.get(result['outputs']['metric']['resource_id'])['data'] == {'value': 2.0}
    assert llm.calls == 2


def test_task_profile_does_not_make_a_binder_placeholder(view, resources):
    task = TaskSpec(name='binder', kind=TaskKind.BINDER, reference_sequence='', target_sequence='ACDE',
        candidate_access='open', min_length=4, max_length=6, controls_per_batch=0,
        execution_mode='computational', feedback_source='computational')
    binder_view = view.model_copy(update={'task': task})
    ref = resources.sequences([Candidate(sequence='FGHIK')], producer='tool:fixture')['resource_id']
    assert default_profiles().accept(binder_view, resources, {'candidates': ref})[0].sequence == 'FGHIK'


def test_schema_namespace_does_not_overwrite_known_tool(resources):
    protocol = Protocol.model_validate({'hypothesis': 'reject override',
        'schemas': {SEQUENCES: {'type': 'object'}}, 'steps': [
            {'step_id': 'x', 'operation': 'never', 'question': 'test'}],
        'final_outputs': {'x': {'source': 'step:x.result', 'schema_ref': SEQUENCES}}})
    with pytest.raises(ContractError, match='custom'):
        register_agent_operations(protocol, OperationRegistry(resources.registry), None, resources, {})


def test_actual_ranking_agent_protocol_runs_without_B_or_protein_tools(team, view):
    from proteinrsi.agents import AnalystAgent, PrincipalAgent, DesignerAgent
    from proteinrsi.prompting import snapshot_prompts
    task = TaskSpec(name='rank provided', kind=TaskKind.RANKING, reference_sequence='',
        candidates=['ACDEFG', 'LMNPQR'], candidate_access='pool', controls_per_batch=0,
        execution_mode='computational', feedback_source='computational')
    view = view.model_copy(update={'task': task})
    class LLM:
        roles = []
        def complete(self, role, instructions, context, schema):
            self.roles.append(role)
            if role == 'A-plan':
                assert context['task_contract']['required_outputs'] == {'ranking': RANKING}
                return {'hypothesis': 'Rank the user inputs directly.', 'steps': [
                    {'step_id': 'rank', 'operation': 'agent:rank', 'question': 'Compare supplied sequences',
                     'bindings': {'candidates': {'source': 'input:supplied_candidates', 'schema_ref': SEQUENCES, 'delivery': 'ref'}}},
                    {'step_id': 'review', 'operation': 'agent:select', 'question': 'Review priorities',
                     'bindings': {'ranking': {'source': 'step:rank.result', 'schema_ref': RANKING, 'delivery': 'ref'}}}],
                    'final_outputs': {'ranking': {'source': 'step:review.result', 'schema_ref': RANKING}}}
            if role == 'A-review':
                return {'rationale': 'Keep the current valid protocol.', 'replacement': None}
            if role == 'C':
                return {'ranking': [x['candidate_id'] for x in context['candidates']][::-1], 'summary': 'fixture ranking'}
            if role == 'A-selection':
                return {'ranking': [x['candidate_id'] for x in context['ranked_candidates']], 'summary': 'fixture selection'}
            raise AssertionError('Unexpected implicit step: ' + role)
    snapshot_prompts(team.store)
    team.llm = LLM()
    team.designer = DesignerAgent(team.llm, team.store)
    team.analyst = AnalystAgent(team.llm, None, team.store)
    team.principal = PrincipalAgent(team.llm, team.store)
    result = run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed'))
    assert [c.sequence for c in result] == ['LMNPQR', 'ACDEFG']
    assert team.llm.roles == ['A-plan', 'C', 'A-review', 'A-selection']
    assert team.store.usage()['tool_calls']['committed'] == 0
    assert team.store.usage()['experimental_wells']['committed'] == 0
    saved = next(iter(team.store.all('research_runs').values()))
    assert saved['protocol_result']['outputs']['ranking']['schema_ref'] == RANKING


def test_native_structure_output_binds_to_next_tool_without_llm_copy(resources, store, view):
    gateway = ToolGateway(store)
    artifact = 'artifact:' + 'a' * 64 + '.pdb'
    calls = []
    for name, ins, outs, fn in [
        ('backbone', {'type':'object','additionalProperties':False},
         {'type':'object','properties':{'backbone_ref':{'type':'string'}},'required':['backbone_ref']},
         lambda a: {'backbone_ref': artifact}),
        ('inverse_fold', {'type':'object','properties':{'backbone':{'type':'string'}},'required':['backbone'],'additionalProperties':False},
         {'type':'object','properties':{'candidates':{'type':'array'}},'required':['candidates']},
         lambda a: calls.append(a) or {'candidates':[{'sequence':view.task.reference_sequence}]})]:
        gateway.register(ToolSpec(name=name, capability='fixture', implementation_version='fixture',
            task_kinds=[TaskKind.VARIANT],input_schema=ins,output_schema=outs), fn)
    ops = OperationRegistry(resources.registry)
    register_tool_operations(ops,gateway,view.task,['backbone','inverse_fold'])
    src = ops.get('tool:backbone').output_schema_ref
    dest = ops.get('tool:inverse_fold').output_schema_ref
    p = Protocol.model_validate({'hypothesis':'Bind actual backbone output to sequence generator',
        'steps':[{'step_id':'fold','operation':'tool:backbone','question':'Generate fixture backbone'},
                 {'step_id':'sequence','operation':'tool:inverse_fold','question':'Read fixture artifact',
                  'bindings':{'backbone':{'source':'step:fold.result','schema_ref':src,'pointer':'/backbone_ref'}}}],
        'final_outputs':{'result':{'source':'step:sequence.result','schema_ref':dest}}})
    ProtocolExecutor(resources,ops).execute(p,{})
    assert calls == [{'backbone':artifact}]
    assert store.usage()['tool_calls']['committed'] == 2


def test_no_task_constraint_changes_during_protocol(team, view):
    original = copy.deepcopy(view.task.model_dump(mode='json'))
    resources = ResourceStore(team.store, standard_registry(), 'constraint-test')
    descriptor = resources.sequences([Candidate(sequence=view.task.reference_sequence)],producer='input')
    profiles = default_profiles()
    profiles.accept(view,resources,{'candidates':descriptor['resource_id']})
    assert view.task.model_dump(mode='json') == original


def test_local_replan_preserves_successful_prefix_and_resumes(resources):
    resources.registry.register('custom.value/v1', {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                                                  'required': ['value']}, custom=True)
    ops = OperationRegistry(resources.registry)
    calls = []
    def execute(args, key):
        calls.append(args['value'])
        return args
    ops.register(Operation('adapter:value', {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                                           'required': ['value']}, 'custom.value/v1', execute, 'v1'))
    raw = {'hypothesis': 'Revise the suffix only', 'steps': [
        {'step_id': 'first', 'operation': 'adapter:value', 'question': 'first', 'arguments': {'value': 1}},
        {'step_id': 'second', 'operation': 'adapter:value', 'question': 'second', 'arguments': {'value': 2}}],
        'final_outputs': {'answer': {'source': 'step:second.result', 'schema_ref': 'custom.value/v1'}}}
    original = Protocol.model_validate(raw)
    def review(protocol, completed, refs, run_key):
        assert completed == 1
        assert resources.get(refs['step:first.result'])['data'] == {'value': 1}
        revised = protocol.model_dump(mode='json')
        revised['steps'][1]['arguments']['value'] = 3
        return Protocol.model_validate(revised)
    executor = ProtocolExecutor(resources, ops)
    result = executor.execute(original, {}, after_step=review, max_revisions=1)
    assert calls == [1, 3]
    assert resources.get(result['outputs']['answer']['resource_id'])['data'] == {'value': 3}
    executor.execute(original, {}, after_step=review, max_revisions=1)
    assert calls == [1, 3]


def test_revision_cannot_rewrite_completed_prefix(resources):
    resources.registry.register('custom.object/v1', {'type': 'object'}, custom=True)
    ops = OperationRegistry(resources.registry)
    ops.register(Operation('adapter:object', {'type': 'object'}, 'custom.object/v1', lambda a,k: a, 'v1'))
    raw = {'hypothesis': 'Protect executed prefix', 'steps': [
        {'step_id': 'a', 'operation': 'adapter:object', 'question': 'a'},
        {'step_id': 'b', 'operation': 'adapter:object', 'question': 'b'}],
        'final_outputs': {'answer': {'source': 'step:b.result', 'schema_ref': 'custom.object/v1'}}}
    def bad_review(p, *args):
        altered = p.model_dump(mode='json')
        altered['steps'][0]['question'] = 'Rewrite history'
        return Protocol.model_validate(altered)
    with pytest.raises(ContractError, match='completed'):
        ProtocolExecutor(resources, ops).execute(Protocol.model_validate(raw), {}, after_step=bad_review, max_revisions=1)


def test_bound_value_failure_is_journaled_without_running_operation(resources):
    resources.registry.register('custom.input/v1', {'type': 'object'})
    resources.registry.register('custom.answer/v1', {'type': 'object'})
    ref = resources.put('custom.input/v1', {'n': 'not-an-integer'}, producer='test')['resource_id']
    ops = OperationRegistry(resources.registry)
    calls = []
    ops.register(Operation('adapter:integer', {'type': 'object', 'properties': {'n': {'type': 'integer'}},
                                             'required': ['n']},
                           'custom.answer/v1', lambda args, key: calls.append(args) or args, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'Do not coerce inputs', 'steps': [
        {'step_id': 'a', 'operation': 'adapter:integer', 'question': 'test', 'bindings': {
            'n': {'source': 'input:data', 'schema_ref': 'custom.input/v1', 'pointer': '/n'}}}],
        'final_outputs': {'answer': {'source': 'step:a.result', 'schema_ref': 'custom.answer/v1'}}})
    with pytest.raises(ContractError):
        ProtocolExecutor(resources, ops).execute(protocol, {'data': ref})
    journals = [v for k, v in resources.store.all('research_step_outputs').items() if k.startswith('protocol-run:')]
    assert len(journals) == 1 and journals[0]['status'] == 'blocked'
    assert calls == []


def test_final_validation_failure_retains_tool_receipt(resources):
    resources.registry.register('custom.answer/v1', {'type': 'object'})
    ops = OperationRegistry(resources.registry)
    calls = []
    ops.register(Operation('adapter:answer', {'type': 'object'}, 'custom.answer/v1',
                           lambda args, key: calls.append(key) or {'answer': 1}, 'v1'))
    protocol = Protocol.model_validate({'hypothesis': 'Keep result on final mismatch', 'steps': [
        {'step_id': 'a', 'operation': 'adapter:answer', 'question': 'test'}],
        'final_outputs': {'answer': {'source': 'step:a.result', 'schema_ref': 'custom.answer/v1'}}})
    def reject(outputs):
        raise ContractError('Task-specific final check failed')
    executor = ProtocolExecutor(resources, ops)
    with pytest.raises(ContractError):
        executor.execute(protocol, {}, validate_final=reject)
    journals = [v for k, v in resources.store.all('research_step_outputs').items() if k.startswith('protocol-run:')]
    assert journals[0]['status'] == 'blocked'
    executor.execute(protocol, {})
    assert len(calls) == 1


def test_registered_operation_inputs_cannot_change_through_catalogue(resources):
    resources.registry.register('custom.answer/v1', {'type': 'object'})
    schema = {'type': 'object', 'additionalProperties': False}
    ops = OperationRegistry(resources.registry)
    ops.register(Operation('adapter:a', schema, 'custom.answer/v1', lambda a, k: a, 'v1'))
    schema['type'] = 'array'
    ops.catalog()[0]['input_schema']['type'] = 'string'
    assert ops.get('adapter:a').input_schema['type'] == 'object'


def test_structure_refs_require_registered_assets(resources, store):
    from proteinrsi.dataflow.resources import STRUCTURES
    ref = 'artifact:' + 'a' * 64 + '.pdb'
    data = {'structures': [{'artifact_ref': ref, 'entity_refs': ['entity:binder'],
            'chain_map': {'B': 'entity:binder'}, 'structure_role': 'backbone'}]}
    with pytest.raises(ContractError, match='unregistered'):
        resources.put(STRUCTURES, data, producer='agent:C')
    store.put('artifacts', ref.split(':')[1], {'ref': ref, 'kind': 'pdb', 'sha256': 'a' * 64})
    result = resources.put(STRUCTURES, data, producer='tool:fixture')
    assert resources.get(result['resource_id'])['data'] == data
    data['structures'][0]['chain_map'] = {'B': 'entity:other'}
    with pytest.raises(ValueError):
        resources.put(STRUCTURES, data, producer='tool:fixture')


def test_affinity_numeric_requires_matching_subject_and_actual_tool_receipt(resources, view):
    from proteinrsi.contracts import digest
    from proteinrsi.dataflow.resources import METRICS
    task = TaskSpec(name='affinity', kind=TaskKind.AFFINITY, reference_sequence='ACDE', target_sequence='FGHI',
        controls_per_batch=0, execution_mode='computational', feedback_source='computational', metric='Kd', unit='nM')
    view = view.model_copy(update={'task': task})
    subjects = ['seq:' + sequence_hash(s) for s in ['ACDE', 'FGHI']]
    metric = {'subject_ref': 'complex:' + digest(sorted(subjects)), 'name': 'Kd', 'value': 3.0,
              'unit': 'nM', 'method': 'fixture-only', 'evidence': 'predicted'}
    support = resources.put(METRICS, {'rows': [metric]}, producer='tool:fixture')['resource_id']
    estimate = {'subject_refs': subjects, 'property': 'Kd', 'value': 3.0, 'unit': 'nM', 'method': 'fixture-only',
                'support_refs': [support], 'limitations': 'Synthetic software test, not a real prediction.'}
    ref = resources.put(ESTIMATES, {'estimates': [estimate]}, producer='agent:C')['resource_id']
    assert default_profiles().accept(view, resources, {'estimates': ref}) == []
    metric['subject_ref'] = 'complex:wrong'
    wrong = resources.put(METRICS, {'rows': [metric]}, producer='tool:fixture')['resource_id']
    estimate['support_refs'] = [wrong]
    ref = resources.put(ESTIMATES, {'estimates': [estimate]}, producer='agent:C')['resource_id']
    with pytest.raises(ContractError, match='matching'):
        default_profiles().accept(view, resources, {'estimates': ref})


def test_campaign_protocol_direct_design_is_optional_model_route(team, view):
    from proteinrsi.agents import AnalystAgent, PrincipalAgent, DesignerAgent
    class LLM:
        model, base_url = 'fixture', 'https://fixture.invalid'
        roles = []
        def complete(self, role, instructions, context, schema):
            self.roles.append(role)
            if role == 'A-plan':
                return {'hypothesis': 'Design directly from the supplied constraints.', 'steps': [
                    {'step_id': 'propose', 'operation': 'agent:propose', 'question': 'direct edit',
                     'arguments': {'question': 'Propose a legal single substitution.'}},
                    {'step_id': 'rank', 'operation': 'agent:rank', 'question': 'prioritize', 'bindings': {
                        'candidates': {'source': 'step:propose.result', 'schema_ref': SEQUENCES, 'delivery': 'ref'}}},
                    {'step_id': 'final', 'operation': 'adapter:ranked_sequences', 'question': 'Keep the priority order',
                     'bindings': {'ranking': {'source': 'step:rank.result', 'schema_ref': RANKING, 'delivery': 'ref'}}}],
                    'final_outputs': {'candidates': {'source': 'step:final.result', 'schema_ref': SEQUENCES}}}
            if role == 'B':
                return {'edits': [[{'position': 39, 'from': 'V', 'to': 'A'}]]}
            if role == 'C':
                return {'ranking': [c['candidate_id'] for c in context['candidates']], 'summary': 'fixture'}
            raise AssertionError(role)
    team.llm = LLM()
    team.designer = DesignerAgent(team.llm, team.store)
    team.analyst = AnalystAgent(team.llm, None, team.store)
    team.principal = PrincipalAgent(team.llm, team.store)
    result = run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed', review_after_step=False))
    assert result[0].sequence[38] == 'A'
    assert team.llm.roles == ['A-plan', 'B', 'C']
    assert team.store.usage()['tool_calls']['committed'] == 0


def test_campaign_native_output_is_normalized_and_ranked_without_B(team, view):
    from proteinrsi.agents import AnalystAgent
    calls = []
    team.tools.register(ToolSpec(name='fixture_generate', capability='sequence.generate', implementation_version='test',
        task_kinds=[TaskKind.VARIANT], input_schema={'type': 'object', 'additionalProperties': False},
        output_schema={'type': 'object', 'properties': {'candidates': {'type': 'array'}}, 'required': ['candidates']}),
        lambda args: calls.append(args) or {'candidates': [{'sequence': view.task.reference_sequence}]})
    view = view.model_copy(update={'workflow': view.workflow.model_copy(update={'tool_names': ['fixture_generate']})})
    class LLM:
        def complete(self, role, instructions, context, schema):
            if role == 'A-plan':
                native = next(o for o in context['operations'] if o['name'] == 'tool:fixture_generate')
                return {'hypothesis': 'Bind generated data directly, no transcription.', 'steps': [
                    {'step_id': 'generate', 'operation': 'tool:fixture_generate', 'question': 'fixture'},
                    {'step_id': 'normalize', 'operation': 'adapter:normalize_candidates', 'question': 'convert once',
                     'bindings': {'result': {'source': 'step:generate.result', 'schema_ref': native['output_schema_ref']}}},
                    {'step_id': 'rank', 'operation': 'agent:rank', 'question': 'prioritize', 'bindings': {
                        'candidates': {'source': 'step:normalize.result', 'schema_ref': SEQUENCES, 'delivery': 'ref'}}},
                    {'step_id': 'final', 'operation': 'adapter:ranked_sequences', 'question': 'ordered result', 'bindings': {
                        'ranking': {'source': 'step:rank.result', 'schema_ref': RANKING, 'delivery': 'ref'}}}],
                    'final_outputs': {'candidates': {'source': 'step:final.result', 'schema_ref': SEQUENCES}}}
            if role == 'C':
                return {'ranking': [c['candidate_id'] for c in context['candidates']], 'summary': 'fixture'}
            raise AssertionError('Unexpected B/model request: ' + role)
    team.llm = LLM()
    team.analyst = AnalystAgent(team.llm, None, team.store)
    result = run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed', review_after_step=False))
    assert result[0].sequence == view.task.reference_sequence
    assert len(calls) == 1
    assert team.store.usage()['tool_calls']['committed'] == 1


def test_campaign_protocol_affinity_has_resource_output_without_candidates(team, view):
    task = TaskSpec(name='affinity', kind=TaskKind.AFFINITY, reference_sequence='ACDE', target_sequence='FGHI',
        controls_per_batch=0, execution_mode='computational', feedback_source='computational', metric='Kd', unit='nM')
    view = view.model_copy(update={'task': task})
    class LLM:
        def complete(self, role, instructions, context, schema):
            if role == 'A-plan':
                return {'hypothesis': 'Report insufficient quantitative evidence honestly.',
                    'agent_operations': [{'name': 'agent:interpret', 'role': 'C', 'instructions': 'Assess the supplied fixed inputs.',
                                          'input_schema': {'type': 'object'}, 'output_schema_ref': ESTIMATES}],
                    'steps': [{'step_id': 'estimate', 'operation': 'agent:interpret', 'question': 'Is a numerical prediction supported?'}],
                    'final_outputs': {'estimates': {'source': 'step:estimate.result', 'schema_ref': ESTIMATES}}}
            if role == 'C':
                return {'estimates': [{'subject_refs': ['seq:' + sequence_hash(s) for s in ['ACDE', 'FGHI']],
                    'property': 'Kd', 'value': None, 'unit': 'nM', 'method': 'not_available',
                    'support_refs': [], 'limitations': 'No calibrated predictor is configured.'}]}
            raise AssertionError(role)
    team.llm = LLM()
    assert run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed')) == []
    record = next(iter(team.store.all('research_runs').values()))
    assert record['protocol_result']['outputs']['estimates']['schema_ref'] == ESTIMATES
    assert team.store.usage()['experimental_wells']['committed'] == 0


def test_campaign_protocol_repairs_preflight_before_execution(team, view):
    from proteinrsi.agents import DesignerAgent
    class LLM:
        plans = 0
        def complete(self, role, instructions, context, schema):
            if role == 'A-plan':
                self.plans += 1
                name = 'uninstalled_tool' if self.plans == 1 else 'agent:propose'
                return {'hypothesis': 'Only configured operations may execute.',
                    'steps': [{'step_id': 'design', 'operation': name, 'question': 'fixture',
                               'arguments': {'question': 'Propose one legal variant.'}}],
                    'final_outputs': {'candidates': {'source': 'step:design.result', 'schema_ref': SEQUENCES}}}
            if role == 'B':
                return {'edits': [[{'position': 39, 'from': 'V', 'to': 'A'}]]}
            raise AssertionError(role)
    team.llm = LLM()
    team.designer = DesignerAgent(team.llm, team.store)
    result = run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed', review_after_step=False))
    assert result[0].sequence[38] == 'A' and team.llm.plans == 2
    assert team.store.usage()['tool_calls']['committed'] == 0
