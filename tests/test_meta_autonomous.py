"""Mechanism/contract tests with scripted model and computation fixtures, not science results."""
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import MetaPolicy, MethodProgram, Patch, Workflow, digest
from proteinrsi.improvement import apply_patch
from proteinrsi.llm import JSONLLM, ProviderPaused
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.research.meta import MetaStep
from proteinrsi.storage import BudgetExceeded, SponsoredStore
from proteinrsi.tools import ToolGateway


def configure(campaign, *, code=False, rounds=4):
    campaign.store.put('configuration', 'research', ResearchConfig(
        enabled=False, enable_generated_code=code, meta_tool_rounds=rounds).model_dump())
    view = campaign.view()
    view.meta = MetaPolicy(min_observations=5000, min_remaining_wells=10000)
    return view


class Script:
    model, base_url = 'fixture', 'https://example.invalid'
    cache_settings = {}

    def __init__(self, fn):
        self.fn, self.contexts = fn, []

    def complete(self, role, instructions, context, schema):
        assert role == 'M'
        self.contexts.append(deepcopy(context))
        return self.fn(context)


def test_empty_programs_preserve_existing_method_hashes():
    workflow = Workflow()
    original = workflow.model_dump()
    original.pop('design_tool_rounds')
    original.pop('analysis_tool_rounds')
    assert 'programs' not in original
    assert workflow.version == 'w-' + digest(original)[:16]
    old = {'enabled': True, 'mode': 'plateau', 'min_observations': 6,
           'cooldown_rounds': 1, 'min_remaining_wells': 10,
           'prompt': 'Diagnose workflow failures; propose one falsifiable bounded change.'}
    assert MetaPolicy.model_validate(old).version == 'm-' + digest(old)[:16]


def test_programs_are_versioned_portable_and_strict():
    program = MethodProgram(name='mine', purpose='test fixture', code='result = {}')
    old = MetaPolicy()
    patch = Patch(target='meta', base_version=old.version, changes={'programs': [program.model_dump()]},
                  hypothesis='Test a reusable method program', task_kind='variant_design')
    new = apply_patch(old, patch)
    assert new.version != old.version
    assert MetaPolicy.model_validate(json.loads(new.model_dump_json())).version == new.version
    changed = program.model_copy(update={'code': 'result = {"changed": True}'})
    assert changed.version != program.version
    with pytest.raises(ValidationError):
        MetaPolicy(programs=[program, program])
    with pytest.raises(ValidationError):
        MethodProgram(name='../escape', purpose='test', code='result = {}')
    with pytest.raises(ValueError, match='not both'):
        MetaStep(reason='invalid', patch=patch,
                 tool_call={'name': 'anything', 'arguments': {}})


def test_agent_decides_without_heuristic_admission_and_can_abstain(campaign):
    view = configure(campaign)
    llm = Script(lambda _: {'reason': 'No useful change supported'})
    result = MetaAgent(llm, campaign.store).propose(view, last_patch_round=0)
    assert result.patch is None and len(llm.contexts) == 1
    assert campaign.store.usage()['tool_calls']['committed'] == 0
    assert len([e for e in campaign.store.events() if e['kind'] == 'meta_analysis_started']) == 1
    view.meta.enabled = False
    MetaAgent(llm, campaign.store).propose(view)
    assert len(llm.contexts) == 1


def test_historical_config_keeps_admission_policy(campaign):
    campaign.store.put('configuration', 'research', {'enabled': True})
    view = campaign.view()
    view.meta = MetaPolicy()
    llm = Script(lambda _: pytest.fail('Historical policy should abstain before LLM'))
    assert MetaAgent(llm, campaign.store).propose(view).patch is None


def test_optional_analysis_is_paid_cached_and_visible_to_meta(campaign):
    view = configure(campaign)
    def handler(request):
        context = json.loads(json.loads(request.content)['messages'][1]['content'])
        if not context['analysis_results']:
            value = {'reason': 'Inspect the available evidence', 'tool_call': {
                'name': 'research_evidence_summary', 'arguments': {}}}
        else:
            assert context['analysis_results'][0]['result']['analysis']['n_observations'] == 0
            value = {'reason': 'No evidence yet; keep current method'}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}}]})
    llm = JSONLLM(campaign.store, model='fixture', base_url='https://example.invalid', api_key='fixture',
                  transport=httpx.MockTransport(handler), max_attempts=1)
    agent = MetaAgent(llm, campaign.store)
    first = agent.propose(view)
    assert agent.propose(view) == first
    assert campaign.store.usage()['tool_calls']['committed'] == 1
    assert campaign.store.usage()['llm_calls']['committed'] == 2
    assert campaign.store.usage()['experimental_wells']['committed'] == 0


def test_resume_does_not_repeat_a_tool_or_replace_frozen_context(campaign):
    view = configure(campaign)
    def paused(context):
        if not context['analysis_results']:
            return {'reason': 'Inspect', 'tool_call': {'name': 'research_evidence_summary', 'arguments': {}}}
        raise ProviderPaused('fixture pause')
    with pytest.raises(ProviderPaused):
        MetaAgent(Script(paused), campaign.store).propose(view)
    resumed = Script(lambda c: {'reason': 'Use the completed analysis'})
    MetaAgent(resumed, campaign.store).propose(view)
    assert len(resumed.contexts) == 1
    assert resumed.contexts[0]['analysis_results']
    assert campaign.store.usage()['tool_calls']['committed'] == 1


def test_no_unapproved_tool_or_implicit_code_permission(campaign):
    view = configure(campaign)
    llm = Script(lambda _: {'reason': 'Invalid request', 'tool_call': {
        'name': 'research_python', 'arguments': {'code': 'result = {}'}}})
    with pytest.raises(PermissionError):
        MetaAgent(llm, campaign.store).propose(view)
    assert campaign.store.usage()['tool_calls']['committed'] == 0
    view.meta.programs = [MethodProgram(name='x', purpose='test', code='result = {}')]
    with pytest.raises(PermissionError, match='operator-enabled'):
        MetaAgent(Script(lambda _: {'reason': 'unused'}), campaign.store).propose(view)


def test_tool_limit_is_external_not_meta_editable(campaign):
    view = configure(campaign, rounds=0)
    llm = Script(lambda _: {'reason': 'Over limit', 'tool_call': {
        'name': 'research_evidence_summary', 'arguments': {}}})
    with pytest.raises(ValidationError):
        MetaAgent(llm, campaign.store).propose(view)
    bad = Patch(target='meta', base_version=view.meta.version,
                changes={'meta_tool_rounds': 1000}, hypothesis='Bypass the external call cap', task_kind=view.task.kind)
    with pytest.raises(PermissionError):
        apply_patch(view.meta, bad)


def test_failed_code_can_be_inspected_and_repaired(campaign, monkeypatch):
    import proteinrsi.research.code as code
    view = configure(campaign, code=True)
    calls = []
    def execute(store, context, arguments):
        calls.append(arguments['code'])
        return {'status': 'failed' if len(calls) == 1 else 'ok',
                'code_sha256': digest(arguments['code']), 'evidence_kind': 'computed_unvalidated',
                'error_type': 'FixtureError' if len(calls) == 1 else None}
    monkeypatch.setattr(code, 'execute_code', execute)
    def think(context):
        n = len(context['analysis_results'])
        if n < 2:
            return {'reason': 'Analyze or repair', 'tool_call': {'name': 'research_python',
                    'arguments': {'code': f'result = {{"step": {n}}}'}}}
        assert context['analysis_results'][0]['result']['status'] == 'failed'
        assert context['analysis_results'][1]['result']['status'] == 'ok'
        return {'reason': 'Keep current method'}
    MetaAgent(Script(think), campaign.store).propose(view)
    assert len(calls) == 2 and campaign.store.usage()['tool_calls']['committed'] == 2


def test_successor_meta_reuses_own_program_and_proposes_descendant(campaign, monkeypatch):
    import proteinrsi.research.code as code
    view = configure(campaign, code=True)
    seen = []
    def execute(store, context, arguments):
        seen.append((context.meta.version, arguments['code']))
        return {'status': 'ok', 'code_sha256': digest(arguments['code']),
                'evidence_kind': 'computed_unvalidated', 'output': {'diagnostic': 'fixture'}}
    monkeypatch.setattr(code, 'execute_code', execute)
    program = MethodProgram(name='learned', purpose='A fixture-defined learned analysis', code='result = {}')
    def think(context):
        current = context['view']
        if not context['program_results']:
            patch = Patch(target='meta', base_version=view.meta.version,
                changes={'programs': [program.model_dump()], 'prompt': 'Use my revised analysis'},
                hypothesis='Test the fixture improver update', task_kind=view.task.kind)
        else:
            assert context['program_results'][0]['result']['output']['diagnostic'] == 'fixture'
            patch = Patch(target='workflow', base_version=view.workflow.version,
                changes={'designer_prompt': 'Use the newly available evidence'},
                hypothesis='Test the descendant workflow', task_kind=view.task.kind)
            assert current['meta']['prompt'] == 'Use my revised analysis'
        return {'reason': 'Fixture proposal', 'patch': patch.model_dump(mode='json')}
    llm = Script(think)
    agent = MetaAgent(llm, campaign.store)
    first = agent.propose(view)
    assert not seen  # Proposing source must not execute it before adoption.
    successor = apply_patch(view.meta, first.patch)
    next_view = view.model_copy(deep=True, update={'meta': successor, 'round_index': 1})
    second = agent.propose(next_view)
    assert second.patch.target == 'workflow'
    assert seen == [(successor.version, program.code)]
    assert view.meta.programs == []
    assert agent.propose(next_view) == second and len(seen) == 1
    # A clean validation branch needs only the source-carrying policy, not private parent code files.
    branch = SponsoredStore(campaign.store.root/'branch', campaign.store, 'branch')
    branch.configure_budget(view.task.budget.model_dump())
    branch.put('configuration', 'research', campaign.store.get('configuration', 'research'))
    MetaAgent(llm, branch).propose(next_view)
    assert len(seen) == 2
    assert campaign.store.usage()['tool_calls']['committed'] == 2


def test_workflow_program_runs_before_task_and_outputs_are_visible(campaign, monkeypatch):
    import proteinrsi.research.code as code
    view = configure(campaign, code=True)
    view.workflow.programs = [MethodProgram(name='learned', purpose='fixture', code='result = {}')]
    monkeypatch.setattr(code, 'execute_code', lambda s, v, a: {
        'status': 'ok', 'code_sha256': digest(a['code']), 'evidence_kind': 'computed_unvalidated',
        'output': {'computed': 1}})
    team = Team(campaign.store, register_tools=False)
    def run(context):
        assert context.research_context['workflow_program_outputs'][0]['result']['output']['computed'] == 1
        return []
    monkeypatch.setattr(team, '_run_fixed', run)
    assert team.run(view) == []
    assert team.run(view) == []
    assert campaign.store.usage()['tool_calls']['committed'] == 1
    assert 'workflow_program_outputs' not in view.research_context


def test_programs_cannot_bypass_tool_budget(campaign, monkeypatch):
    from proteinrsi.research.methods import run_method_programs
    import proteinrsi.research.code as code
    view = configure(campaign, code=True)
    program = MethodProgram(name='x', purpose='fixture', code='result = {}')
    view.meta.programs = [program]
    gateway = ToolGateway(campaign.store)
    code.register_code_tool(gateway, view)
    remaining = campaign.store.remaining('tool_calls')
    campaign.store.reserve('consume', 'tool_calls', remaining, {})
    campaign.store.settle('consume')
    with pytest.raises(BudgetExceeded):
        run_method_programs(campaign.store, gateway, view, view.meta, owner='M')


def test_fixed_meta_ablation_rejects_self_patch(tmp_path, fixture_data):
    from proteinrsi.runtime import Campaign
    task, _ = fixture_data
    campaign = Campaign.initialize(str(tmp_path/'ablation'), task,
        governance_config={'allowed_patch_targets': ['workflow']})
    view = campaign.view()
    patch = Patch(target='meta', base_version=view.meta.version,
                  changes={'prompt': 'Change myself'}, hypothesis='Fixture self modification', task_kind=task.kind)
    with pytest.raises(PermissionError, match='disabled'):
        campaign.stage_patch(patch)
    assert campaign.state['pending_meta'] is None
    assert view.research_context['allowed_patch_targets'] == ['workflow']


def test_rsi_report_separates_adoption_use_and_efficacy(campaign):
    from proteinrsi.rsi_evidence import summarize_rsi
    store = campaign.store
    # Fixture events, not a real accepted run.
    store.event('method_version_switched', {'target': 'meta', 'from_version': 'm0',
        'to_version': 'm1', 'round': 1, 'action': 'accepted', 'evaluation_ref': 'fixture'})
    store.event('validation_event', {'branch': 'trial', 'kind': 'meta_analysis_started',
        'payload': {'meta': 'm1', 'workflow': 'w0', 'round': 1, 'output_id': 'branch'}})
    assert not summarize_rsi(store)['adopted_meta_versions'][0]['actually_reused']
    store.event('meta_analysis_started', {'meta': 'm1', 'workflow': 'w0', 'round': 2, 'output_id': 'real'})
    store.event('meta_analysis_completed', {'meta': 'm1', 'target': 'workflow', 'patch_id': 'p1',
        'proposed_version': 'w1', 'output_id': 'result'})
    report = summarize_rsi(store)
    assert report['recursive_use_observed']
    assert report['efficacy_claim'] == 'not_established_by_this_audit'


def test_guarded_meta_computation_cannot_read_oracle_or_keys(campaign, tmp_path, monkeypatch):
    from proteinrsi.replay.sandbox import probe
    from proteinrsi.replay.broker import GuardedMetaAgent
    if not probe()['available']:
        pytest.skip('Host lacks Landlock/seccomp; isolation must fail closed')
    view = configure(campaign, code=True)
    hidden = tmp_path/'private-labels.txt'
    hidden.write_text('FIXTURE_HIDDEN_LABELS')
    monkeypatch.setenv('PROTEINRSI_API_KEY', 'FIXTURE_PRIVATE_KEY')
    source = f'''import os
from pathlib import Path
try:
    Path({str(hidden)!r}).read_text()
    denied = False
except PermissionError:
    denied = True
result = {{'denied': denied, 'key': os.environ.get('PROTEINRSI_API_KEY'),
          'n': len(context['observations'])}}
'''
    def think(context):
        if not context['analysis_results']:
            return {'reason': 'Fixture sandbox probe', 'tool_call': {
                'name': 'research_python', 'arguments': {'code': source}}}
        result = context['analysis_results'][0]['result']
        assert result['status'] == 'ok'
        assert result['output'] == {'denied': True, 'key': None, 'n': 0}
        return {'reason': 'No method change'}
    team = Team(campaign.store, Script(think), register_tools=False)
    result = GuardedMetaAgent(team).propose(view)
    assert result.patch is None
    assert campaign.store.usage()['tool_calls']['committed'] == 1
    assert campaign.store.usage()['experimental_wells']['committed'] == 0
