# SPDX-License-Identifier: MIT
"""A few end-to-end checks for input authority, local repair and shared execution."""
from copy import deepcopy
import json

import httpx
import pytest

from proteinrsi.contracts import Candidate, Observation, canonical, digest
from proteinrsi.dataflow.integration import build_operations
from proteinrsi.dataflow.protocol import Operation, Protocol, ProtocolExecutor, register_agent_operations
from proteinrsi.dataflow.resources import NAMESPACE, SEQUENCES, ResourceStore, standard_registry, scope_for
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.permissions import StudyAuthorization, require_tools
from proteinrsi.replay.sandbox import probe
from proteinrsi.llm import JSONLLM


@pytest.mark.core
def test_hydrated_python_repairs_original_step_and_keeps_upstream(team, view, resources):
    if not probe()['available']:
        pytest.skip('Kernel sandbox unavailable')
    team.store.put('configuration', 'research', {'enable_generated_code': True,
                                                'context_policy': 'state-v1'})
    view.workflow.tool_names = ['research_python']
    view.round_index = 1
    view.history = [{'round': 0, 'analyst_feedback': {'summary': 'Fixture evidence'}}]
    view.observations = [Observation(sample_id='initial', sequence=view.task.reference_sequence,
        value=1.0, metric='fitness', unit='relative', source='synthetic', batch_id='initial',
        assay_protocol='fixture')]
    view.research_context = {'research_state': {'facts': {'observations': 1,
        'evidence_version': view.evidence_version}, 'working_memory': {}}}
    resources = ResourceStore(team.store, standard_registry(), scope_for(view))
    parent = view.task.reference_sequence
    # This exceeds the command-pipe working size and must travel through a local
    # input file, while the repair prompt stays small.
    panel = resources.sequences([Candidate(sequence=sequence, rationale='visible note ' * 12000)
        for sequence in (parent, parent[:38] + 'A' + parent[39:])], producer='fixture')
    data = resources.get(panel['resource_id'])['data']
    calls = []
    from proteinrsi.execution import input_shape
    shape = input_shape(view.for_task().model_dump(mode='json'))
    def handler(request):
        payload = json.loads(request.content)
        context = json.loads(payload['messages'][1]['content'])
        if 'error' not in context:
            assert context['view']['research_context']['research_state'] == view.research_context['research_state']
            assert 'research_state' not in context['view']
            assert context['metadata'] == shape  # Similar field names do not make a TaskView.
            operation = next(row for row in context['operations'] if row['name'] == 'tool:research_python')
            assert operation['execution_contract']['variables']['context']['fields']['history'] == 'list[object]'
            answer = {'ok': True}
        else:
            calls.append(context)
            assert context['resource_shapes']['panel']['items']['count'] == 2
            assert context['context_shape'] == shape
            assert context['context_shape']['meta'] == {'version': view.meta.version}
            assert context['context_shape']['history']['count'] == 1
            assert context['error']['failure']['error_type'] == 'KeyError'
            assert context['execution_contract']['variables']['context']['research_state_path'] == (
                "context['research_context']['research_state']")
            assert len(canonical(context).encode()) < 16000
            answer = {'reason': 'Use the runtime research_state path and original resource items', 'code':
                "assert len(context['observations']) == context['research_context']['research_state']['facts']['observations']\n"
                "result = {'candidate_ids': [x['candidate_id'] for x in inputs['panel']['items']], "
                "'checks': [{'name': 'science', 'passed': False}]}"}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(answer)}}]})
    team.llm = JSONLLM(team.store, model='fixture', base_url='https://example.invalid', api_key='fixture',
                       transport=httpx.MockTransport(handler), max_attempts=1)
    operations = build_operations(team, view, resources)
    assert team.llm.complete('A-plan', 'Inspect runtime contracts', {
        'view': view.model_dump(mode='json'), 'operations': operations.catalog(), 'metadata': shape}, {}) == {'ok': True}
    upstream = []
    operations.register(Operation('adapter:panel', {'type': 'object'}, SEQUENCES,
                                  lambda args, key: upstream.append(key) or data, 'fixture'))
    code_result = operations.get('tool:research_python').output_schema_ref
    protocol = Protocol(hypothesis='Check original objects, locally repair an input-shape error', steps=[
        {'step_id': 'panel', 'operation': 'adapter:panel', 'question': 'Provide the panel'},
        {'step_id': 'check', 'operation': 'tool:research_python', 'question': 'Check the panel',
         'arguments': {'code': "assert len(context['observations']) == context['research_state']['facts']['observations']\n"
                       "result = {'candidate_ids': [x['candidate_id'] for x in inputs['panel']['items']]}"},
         'bindings': {'resource_refs.panel': {'source': 'step:panel.result', 'delivery': 'ref'}}}],
        final_outputs={'check': {'source': 'step:check.result'}})
    executor = ProtocolExecutor(resources, operations)
    result = executor.execute(protocol, {})
    actual = resources.get(result['outputs']['check']['resource_id'], schema_ref=code_result)['data']
    assert actual['status'] == 'ok' and actual['output']['checks'][0]['passed'] is False
    assert executor.execute(protocol, {}) == result
    assert len(upstream) == len(calls) == 1
    journal = team.store.get(NAMESPACE, result['run_ref'])
    assert journal['completed'] == ['panel', 'check'] and not journal.get('repairs')
    assert len(team.store.all('code_programs')) == 2
    assert team.store.usage()['tool_calls']['committed'] == 2
    assert team.store.usage()['llm_calls']['committed'] == 2  # Inspect + repair; repeat costs nothing.
    assert team.store.usage()['experimental_wells']['committed'] == 0
    # Scope failures are runtime failures, not prompts to fabricate replacement data.
    foreign = ResourceStore(team.store, standard_registry(), digest('other-evidence'))
    ref = foreign.sequences([Candidate(sequence=view.task.reference_sequence)], producer='foreign')['resource_id']
    with pytest.raises(ContractError, match='scope'):
        resources.hydrate({'panel': ref})
    with pytest.raises(ContractError, match='both'):
        resources.hydrate({'panel': panel['resource_id']}, {'panel': {}})


@pytest.mark.core
def test_custom_agent_uses_shared_tools_and_empty_history_continues(team, view, resources):
    view.workflow.tool_names = ['research_evidence_query']
    turns = []
    class LLM:
        def complete(self, role, instructions, context, schema):
            turns.append(deepcopy(context))
            if len(turns) == 1:
                return {'tool_calls': [{'name': 'research_evidence_query', 'arguments': {}}]}
            assert context['tool_results'][0]['analysis']['status'] == 'no_matching_observations'
            return {'conclusion': 'No revealed value; continue designing, fitness unknown'}
    team.llm = LLM()
    protocol = Protocol(hypothesis='A custom step can act and finish without a new Planner node',
        schemas={'custom.conclusion/v1': {'type': 'object', 'properties': {'conclusion': {'type': 'string'}},
            'required': ['conclusion'], 'additionalProperties': False}},
        agent_operations=[{'name': 'agent:inspect', 'role': 'B', 'instructions': 'Inspect revealed evidence',
            'input_schema': {'type': 'object'}, 'output_schema_ref': 'custom.conclusion/v1'}],
        steps=[{'step_id': 'inspect', 'operation': 'agent:inspect', 'question': 'What is known?'}],
        final_outputs={'conclusion': {'source': 'step:inspect.result'}})
    operations = build_operations(team, view, resources)
    register_agent_operations(protocol, operations, team.llm, resources, view.model_dump(mode='json'),
                              **operations.agent_runtime)
    executor = ProtocolExecutor(resources, operations)
    result = executor.execute(protocol, {})
    assert executor.execute(protocol, {}) == result
    assert len(turns) == 2 and team.store.usage()['tool_calls']['committed'] == 1
    assert resources.get(result['outputs']['conclusion']['resource_id'])['data']['conclusion'].endswith('unknown')
    assert team.store.usage()['experimental_wells']['committed'] == 0


@pytest.mark.core
def test_frozen_authorization_survives_method_selection_and_rejects_expansion(team, view):
    from proteinrsi.research.analysis import register_analysis_tools
    from proteinrsi.permissions import available_tools
    register_analysis_tools(team.tools, view)
    grant = StudyAuthorization(tool_names=['research_evidence_query'], generated_code=False)
    team.store.put('configuration', 'execution_authorization', grant.model_dump(), immutable=True)
    # Dropping a preference leaves the authorized capability visible.
    view.workflow.tool_names = []
    assert available_tools(team.tools, view, []) == ['research_evidence_query']
    with pytest.raises(PermissionError):
        require_tools(team.store, ['research_python'])
    view.workflow.tool_names = ['undeployed_new_tool']
    with pytest.raises(PermissionError):
        available_tools(team.tools, view, [])


def test_repair_runtime_error_keeps_original_program_failure(team, view):
    from proteinrsi.execution import execute_tool
    from proteinrsi.research.code import register_code_tool
    from proteinrsi.tools import ToolCall
    if not probe()['available']:
        pytest.skip('Kernel sandbox unavailable')
    team.store.put('configuration', 'research', {'enable_generated_code': True})
    register_code_tool(team.tools, view)
    class BrokenRepairRuntime:
        def complete(self, *args):
            raise RuntimeError('Fixture repair infrastructure failure')
    call = ToolCall(name='research_python', arguments={'code': "result = {'n': missing_variable}"})
    with pytest.raises(RuntimeError, match='repair infrastructure'):
        execute_tool(team.tools, call, view.task, allowed=['research_python'], context_key='fixture',
                     llm=BrokenRepairRuntime(), view=view)
    event = next(event for event in team.store.events() if event['kind'] == 'execution_repair_failed')
    journal = team.store.get(NAMESPACE, event['payload']['call_ref'])
    assert journal['error']['failure']['error_type'] == 'NameError'
    assert len(journal['attempts']) == 1
    assert event['payload']['repair_error_type'] == 'RuntimeError'
    assert team.store.usage()['tool_calls']['committed'] == 1
    assert team.store.usage()['experimental_wells']['committed'] == 0


@pytest.mark.core
def test_guarded_two_round_loop_repairs_code_without_new_planner_nodes(tmp_path):
    from proteinrsi.agents import Team
    from proteinrsi.contracts import MetaPolicy, TaskSpec, Workflow
    from proteinrsi.replay.controller import run_replay
    from proteinrsi.research.contracts import ResearchConfig
    from proteinrsi.runtime import Campaign
    if not probe()['available']:
        pytest.skip('Kernel sandbox unavailable')
    labels = tmp_path/'private-labels.csv'
    labels.write_text('sequence,value,qc\nAVDE,1,valid\nAWDE,2,valid\nAFDE,3,valid\nAGDE,4,valid\n')
    task = TaskSpec(name='Execution mechanism fixture', reference_sequence='ACDE',
        mutable_positions=[2], candidate_access='open', controls_per_batch=0,
        feedback_source='synthetic', max_rounds=2, batch_size=2,
        budget={'experimental_wells': 4, 'llm_calls': None, 'tool_calls': None})
    campaign = Campaign.initialize(str(tmp_path/'run'), task, workflow=Workflow(tool_names=['research_python']),
        meta=MetaPolicy(enabled=False), research_config=ResearchConfig(protocol_mode='typed',
            enable_generated_code=True, review_after_step=False))
    code = ("parent = context['task']['reference_sequence']\n"
            "amino = ['V', 'W'] if context['round_index'] == 0 else ['F', 'G']\n"
            "result = {'candidates': [{'sequence': parent[:1] + a + parent[2:]} for a in amino], "
            "'observations_seen': len(context['observations']), 'meta_fields': list(context['meta'])}")
    roles = []
    def handler(request):
        context = json.loads(json.loads(request.content)['messages'][1]['content'])
        if 'operations' in context:
            roles.append('A-plan')
            generated = code if not context['view']['round_index'] else (
                "assert context['research_state']['facts']['observations'] == 2\n" + code)
            answer = {'hypothesis': 'Generate a legal fixture batch with code', 'steps': [
                {'step_id': 'generate', 'operation': 'tool:research_python', 'question': 'Generate candidates',
                 'arguments': {'code': generated}},
                {'step_id': 'normalize', 'operation': 'adapter:normalize_candidates', 'question': 'Reuse result',
                 'bindings': {'result': {'source': 'step:generate.payload'}}}],
                'final_outputs': {'candidates': {'source': 'step:normalize.result'}}}
        elif context.get('tool') == 'research_python':
            roles.append('B-repair')
            assert context['error']['failure']['error_type'] == 'KeyError'
            assert context['context_shape']['history']['type'] == 'array'
            assert context['context_shape']['history']['count'] == 1
            assert context['context_shape']['observations']['count'] == 2
            answer = {'code': code, 'reason': 'Use original TaskView inputs, preserving the scientific program'}
        else:
            roles.append('C-feedback')
            answer = {'summary': 'Review only the newly revealed fixture values'}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(answer)}}]})
    campaign.team = Team(campaign.store, JSONLLM(campaign.store, model='fixture',
        base_url='https://example.invalid', api_key='fixture', transport=httpx.MockTransport(handler),
        max_attempts=1))
    report = run_replay(campaign, labels, guarded=True)
    assert report['completed_rounds'] == 2 and report['budget']['experimental_wells']['committed'] == 4
    assert roles.count('A-plan') == 2 and roles.count('B-repair') == 1 and 'A-review' not in roles
    runs = campaign.store.all('research_runs').values()
    assert all(run['completed'] == ['generate', 'normalize'] for run in runs)
    outputs = [record['result']['output'] for record in campaign.store.all('tool_jobs').values()
               if record['state'] == 'done' and record['result'].get('status') == 'ok']
    assert sorted(result['observations_seen'] for result in outputs) == [0, 2]
    assert all(result['meta_fields'] == ['version'] for result in outputs)


def test_python_payload_ports_preserve_business_fields_and_cached_receipts(team, view, resources):
    if not probe()['available']:
        pytest.skip('Kernel sandbox unavailable')
    from proteinrsi.dataflow.design import _candidate_payload
    team.store.put('configuration', 'research', {'enable_generated_code': True})
    view.workflow.tool_names = ['research_python']
    resources = ResourceStore(team.store, standard_registry(), scope_for(view))
    operations = build_operations(team, view, resources)
    payload_schema = 'custom.panel/v1'
    operations.schemas.register(payload_schema, {'type': 'object', 'properties': {
        'candidates': {'type': 'array'}, 'groups': {'type': 'array'},
        'allocation': {'type': 'object'}, 'excluded_sequences': {'type': 'array'},
        'output': {'type': 'string'}},
        'required': ['candidates', 'groups', 'allocation', 'excluded_sequences', 'output']}, custom=True)
    protocol = Protocol(hypothesis='Pass generated business data without copying or guessing paths', steps=[
        {'step_id': 'build', 'operation': 'tool:research_python', 'question': 'Build the panel',
         'arguments': {'code': "result = {'candidates': [{'sequence': context['task']['reference_sequence']}], "
                       "'groups': ['explore'], 'allocation': {'explore': 1}, "
                       "'excluded_sequences': [], 'output': 'legitimate business field'}"},
         'outputs': {'payload': {'schema_ref': payload_schema, 'pointer': '/output'}}},
        {'step_id': 'check', 'operation': 'tool:research_python', 'question': 'Check original fields',
         'bindings': {'resource_refs.construction': {'source': 'step:build.payload', 'delivery': 'ref'},
                      'resource_refs.receipt': {'source': 'step:build.result', 'delivery': 'ref'}},
         'arguments': {'code': "built = inputs['construction']\n"
                       "assert built == inputs['receipt']['output']\n"
                       "assert built['output'] == 'legitimate business field'\n"
                       "assert built['excluded_sequences'] == []\n"
                       "result = {'groups': built['groups'], 'allocation': built['allocation']}"}}],
        final_outputs={'receipt': {'source': 'step:build.result'},
                       'panel': {'source': 'step:build.payload'}, 'check': {'source': 'step:check.payload'}})
    executor = ProtocolExecutor(resources, operations)
    operation = next(row for row in operations.catalog() if row['name'] == 'tool:research_python')
    assert operation['output_views']['payload']['pointer'] == '/output'
    incompatible = protocol.model_copy(deep=True)
    incompatible.steps[0].outputs['payload'].pointer = '/groups'
    with pytest.raises(ContractError, match='fixed data path'):
        executor.preflight(incompatible, {})
    assert team.store.usage()['tool_calls']['committed'] == 0
    result = executor.execute(protocol, {})
    assert executor.execute(protocol, {}) == result
    raw = resources.get(result['outputs']['receipt']['resource_id'])['data']
    panel_ref = result['outputs']['panel']['resource_id']
    panel = resources.get(panel_ref)['data']
    assert raw['output'] == panel and 'candidates' not in raw
    assert resources.get(result['outputs']['check']['resource_id'])['data'] == {
        'groups': ['explore'], 'allocation': {'explore': 1}}
    assert _candidate_payload(raw) == _candidate_payload({'candidates': panel['candidates']})
    assert _candidate_payload({'status': 'failed', 'output': panel}) is None
    shape = resources.describe(panel_ref)['shape']
    assert shape['properties']['allocation']['properties']['explore'] == {'type': 'integer'}
    assert shape['properties']['groups']['first_item_shape'] == {'type': 'string'}
    assert 'legitimate business field' not in canonical(shape)
    assert shape['complete']
    assert team.store.usage()['tool_calls']['committed'] == 2
    assert team.store.usage()['llm_calls']['committed'] == 0
    assert team.store.usage()['experimental_wells']['committed'] == 0
