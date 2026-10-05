# SPDX-License-Identifier: MIT
import json

import pytest
from pydantic import ValidationError

from proteinrsi.agents import AnalystAgent, Design, DesignerAgent, Plan
from proteinrsi.contracts import Candidate, digest
from proteinrsi.dataflow.design import MutationEdit, prepare_handoff, request_design
from proteinrsi.dataflow.resources import ResourceStore, standard_registry
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.tasks import apply_mutations


def variant(view, residue='A'):
    return apply_mutations(view.task.reference_sequence, [{'position': 39, 'from': 'V', 'to': residue}])


@pytest.mark.parametrize('raw', [
    {'label': 'E001', 'position/from/to': '40/D/W,41/G/A,54/V/A'},
    {'position': '40', 'from': 'D', 'to': 'W'},
    {'position': True, 'from': 'D', 'to': 'W'},
    {'position': 40, 'from': 'D', 'to': 'D'},
    {'position': 40, 'from': 'D', 'to': 'XX'},
    {'position': 0, 'from': 'D', 'to': 'W'},
])
def test_strict_mutations_reject_noncanonical_formats(raw):
    with pytest.raises(ValidationError):
        MutationEdit.model_validate(raw)


def test_mutation_schema_is_same_as_runtime(view):
    assert set(MutationEdit.model_json_schema()['required']) == {'position', 'from', 'to'}
    edit = MutationEdit.model_validate({'position': 39, 'from': 'V', 'to': 'A'})
    assert apply_mutations(view.task.reference_sequence, [edit]) == variant(view)


def test_group_duplicate_sites_rejected():
    with pytest.raises(ValidationError):
        Design(edits=[[{'position': 39, 'from': 'V', 'to': 'A'}] * 2])


def test_upstream_candidate_data_not_reprinted(resources, view):
    values = [{'sequence': variant(view)}]
    compact, allowed = prepare_handoff(resources, [{'status': 'ok', 'candidates': values,
                                                    'output': {'candidates': values, 'summary': 'actual'}}])
    assert 'candidates' not in compact[0]
    assert 'candidates' not in compact[0]['output']
    assert len(allowed) == 1
    ref = next(iter(allowed))
    assert resources.candidates(ref)[0].sequence == variant(view)


def test_latest_real_failure_repairs_using_resource_without_tool_rerun(store, view):
    class LLM:
        calls = []
        def complete(self, role, instructions, context, schema):
            self.calls.append(context)
            if len(self.calls) == 1:
                return {'edits': [[{'label': 'E001', 'position/from/to': '40/D/W,41/G/A,54/V/A'}]]}
            assert context['validation_errors']
            return {'candidate_refs': [context['available_candidate_sets'][0]['resource_id']]}
    llm = LLM()
    existing = [{'status': 'ok', 'candidates': [{'sequence': variant(view)}]}]
    output = request_design(llm, store, 'Design', view, Plan(rationale='Reuse code output'), existing, [])
    assert [c.sequence for c in output.candidates] == [variant(view)]
    assert not output.edits
    assert len(llm.calls) == 2
    assert store.usage()['tool_calls']['committed'] == 0
    assert store.usage()['experimental_wells']['committed'] == 0
    request_design(llm, store, 'Design', view, Plan(rationale='Reuse code output'), existing, [])
    assert len(llm.calls) == 2


def test_format_repair_is_bounded_across_reentry(store, view):
    class Bad:
        calls = 0
        def complete(self, *args):
            self.calls += 1
            return {'edits': [[{'wrong': 1}]]}
    llm = Bad()
    for _ in range(2):
        with pytest.raises(ContractError, match='exhausted'):
            request_design(llm, store, 'Design', view, Plan(rationale='test'), [], [])
    assert llm.calls == 3


def test_no_schema_repair_can_change_fixed_residues(store, view):
    wrong = 'A' + view.task.reference_sequence[1:]
    class LLM:
        def complete(self, *args):
            return {'candidates': [{'sequence': wrong}]}
    with pytest.raises(ContractError, match='exhausted'):
        request_design(LLM(), store, 'Design', view, Plan(rationale='test'), [], [])


def test_unoffered_reference_rejected_even_when_stored(resources, view):
    private = resources.sequences([{'sequence': variant(view)}], producer='other-decision')['resource_id']
    class LLM:
        def complete(self, *args):
            return {'candidate_refs': [private]}
    with pytest.raises(ContractError, match='exhausted'):
        request_design(LLM(), resources.store, 'Design', view, Plan(rationale='test'), [], [])


def test_resource_hash_and_scope_are_checked(resources, view):
    descriptor = resources.sequences([{'sequence': variant(view)}], producer='test')
    wrong_scope = ResourceStore(resources.store, standard_registry(), digest('another-trial'))
    with pytest.raises(ContractError, match='scope'):
        wrong_scope.get(descriptor['resource_id'])
    with pytest.raises(ContractError):
        resources.get('resource:' + '0' * 64)


def test_failed_output_does_not_create_candidate_resource(resources, view):
    _, allowed = prepare_handoff(resources, [{'status': 'failed', 'candidates': [{'sequence': variant(view)}]}])
    assert allowed == {}


def test_no_implicit_models_in_real_llm_route(store, view):
    class Bomb:
        def __getattr__(self, name):
            raise AssertionError('Implicit model access: ' + name)
    class LLM:
        def complete(self, role, instructions, context, schema):
            return {'ranking': [c['candidate_id'] for c in context['candidates']], 'summary': 'direct reasoning'}
    candidates = [Candidate(sequence=variant(view))]
    ranked = AnalystAgent(LLM(), Bomb(), store).rank(view, candidates, [])
    assert ranked[0].predicted_value is None


def test_original_designer_entry_uses_typed_repair(store, view):
    class LLM:
        def complete(self, role, instructions, context, schema):
            assert 'candidate_refs' in schema['properties']
            return {'edits': [[{'position': 39, 'from': 'V', 'to': 'A'}]]}
    design = DesignerAgent(LLM(), store).propose(view, Plan(rationale='direct edit'), [], [])
    assert design.candidates[0].sequence == variant(view)
    assert design.edits == []


def test_large_set_is_adopted_by_one_id(store, view):
    sequence = view.task.reference_sequence
    amino = 'ACDEFGHIKLMNPQRSTVWY'
    candidates = []
    for a in amino:
        for b in amino:
            s = list(sequence)
            s[38], s[39] = a, b
            candidates.append({'sequence': ''.join(s)})
    candidates = candidates[:384]
    class LLM:
        def complete(self, role, instructions, context, schema):
            sets = context['available_candidate_sets']
            assert sets[0]['count'] == 384
            assert len(sets[0]['preview']) == 3
            assert len(json.dumps(context)) < 18000
            return {'candidate_refs': [sets[0]['resource_id']]}
    result = request_design(LLM(), store, 'reuse', view, Plan(rationale='large output'),
                            [{'status': 'ok', 'candidates': candidates}], [])
    assert len(result.candidates) == 384


def test_repair_calls_use_real_client_budget_and_cache(store, view):
    import httpx
    from proteinrsi.llm import JSONLLM
    calls = []
    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body['messages'][1]['content'])
        calls.append(context)
        if len(calls) == 1:
            result = {'edits': [[{'position/from/to': '39/V/A'}]]}
        else:
            result = {'candidate_refs': [context['available_candidate_sets'][0]['resource_id']]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}],
                                        'usage': {'total_tokens': 10}})
    client = JSONLLM(store, model='fixture', base_url='https://fixture.example/v1',
                     api_key='NOT_A_REAL_KEY', transport=httpx.MockTransport(handler))
    actual = [{'status': 'ok', 'candidates': [{'sequence': variant(view)}]}]
    result = request_design(client, store, 'test', view, Plan(rationale='test'), actual, [])
    assert result.candidates[0].sequence == variant(view)
    assert store.usage()['llm_calls']['committed'] == 2
    assert store.usage()['experimental_wells']['committed'] == 0


def test_current_runner_design_round_preserves_one_successful_tool(team, view):
    from proteinrsi.research.runner import ResearchRunner
    from proteinrsi.research.contracts import ResearchStep, ResearchConfig
    from proteinrsi.tools import ToolSpec
    calls = []
    name = 'fixture_generator'
    team.tools.register(ToolSpec(name=name, capability='sequence.generate', implementation_version='fixture',
        task_kinds=[view.task.kind], input_schema={'type': 'object', 'additionalProperties': False},
        output_schema={'type': 'object', 'properties': {'candidates': {'type': 'array'}}, 'required': ['candidates']}),
        lambda a: calls.append(a) or {'candidates': [{'sequence': variant(view)}]})
    class LLM:
        count = 0
        def complete(self, role, instructions, context, schema):
            self.count += 1
            if self.count == 1:
                return {'tool_calls': [{'name': name, 'arguments': {}, 'purpose': 'fixture generation'}]}
            if self.count == 2:
                return {'edits': [[{'label': 'E001', 'position/from/to': '40/D/W,41/G/A,54/V/A'}]]}
            return {'candidate_refs': [context['available_candidate_sets'][0]['resource_id']]}
    team.llm = LLM()
    team.designer = DesignerAgent(team.llm, team.store)
    runner = ResearchRunner(team, ResearchConfig())
    state = {'candidates': [], 'ranked': [], 'final': [], 'tool_results': []}
    record = {'run_id': 'fixture-run', 'plan': {}, 'completed': [], 'revisions': [],
              'resources': {'resources': [{'kind': 'tools', 'name': name}]}}
    step = ResearchStep(step_id='design', operation='design', question='Reuse a successful generator',
                        expected_output='Candidate resource')
    result = runner._execute(step, view, state, record, team.tools,
                              team.tools.catalog(view.task, [name]), [name])
    assert result['candidate_count'] == 1
    assert len(calls) == 1
    assert team.llm.count == 3
    assert team.store.usage()['tool_calls']['committed'] == 1


def test_resource_handoff_uses_existing_restricted_broker_namespaces(store, view):
    from types import SimpleNamespace
    from proteinrsi.replay.broker import dispatch
    from proteinrsi.tools import ToolGateway
    from proteinrsi.dataflow.resources import scope_for
    team = SimpleNamespace(store=store, llm=None)
    gateway = ToolGateway(store)
    class Proxy:
        def get(self, namespace, key, default=None):
            return dispatch(team, gateway, view, [], {'rpc': 'get', 'namespace': namespace,
                            'key': key, 'default': default})
        def put(self, namespace, key, value, *, immutable=False):
            return dispatch(team, gateway, view, [], {'rpc': 'put', 'namespace': namespace,
                            'key': key, 'value': value, 'immutable': immutable})
    proxy = Proxy()
    resources = ResourceStore(proxy, standard_registry(), scope_for(view))
    ref = resources.sequences([{'sequence': variant(view)}], producer='handoff-test')['resource_id']
    assert resources.candidates(ref)[0].sequence == variant(view)
    with pytest.raises(PermissionError):
        proxy.get('measurements', 'unrevealed')
    with pytest.raises(PermissionError):
        proxy.put('campaign', 'state', {'budget': 999999})
