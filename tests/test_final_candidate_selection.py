"""Synthetic typed-design adoption tests; no provider, oracle or assay calls."""
from itertools import product

import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import BudgetSpec, MetaPolicy, TaskSpec, Workflow
from proteinrsi.dataflow.integration import run_campaign_protocol
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.llm import ProviderPaused
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.tools import ToolSpec


def _sequences():
    return [''.join(row) + 'V' for row in product('ACDEFGHIKLMNPQRSTVWY', repeat=3)
            if ''.join(row) != 'AAA']


class SelectionLLM:
    model, base_url, cache_settings = 'synthetic-selection', 'https://fixture.invalid', {}

    def __init__(self, mode, provisional=()):
        self.mode, self.provisional = mode, list(provisional)
        self.calls, self.paused = [], False

    def complete(self, role, instructions, context, schema):
        self.calls.append((role, context))
        if role == 'A-plan':
            return {'hypothesis': 'Synthetic final-adoption contract', 'steps': [
                {'step_id': 'design', 'operation': 'agent:propose', 'question': 'Select a panel',
                 'arguments': {'question': 'Adopt only the final selected resources'}}],
                'final_outputs': {'candidates': {'source': 'step:design.result',
                                                'schema_ref': 'protein.sequence_set/v1'}}}
        if role == 'A-review':
            return {'rationale': 'Preserve the explicit synthetic failure', 'replacement': None}
        assert role == 'B'
        refs = [row['resource_id'] for row in context['available_candidate_sets']]
        if not refs:
            reply = {'tool_calls': [{'name': 'fixture_panel', 'arguments': {'panel': 0}}]}
            if self.mode == 'multiple_refs':
                reply['tool_calls'].append({'name': 'fixture_panel', 'arguments': {'panel': 1}})
            if self.provisional:
                reply['candidates'] = [{'sequence': sequence} for sequence in self.provisional]
            return reply
        if self.mode == 'empty':
            return {}
        if self.mode == 'exhaustion' or (len(refs) == 1 and self.mode != 'provisional'):
            return {'tool_calls': [{'name': 'fixture_panel', 'arguments': {'panel': 1}}]}
        if self.mode == 'pause' and not self.paused:
            self.paused = True
            raise ProviderPaused('Synthetic pause before final adoption')
        return {'candidate_refs': list(reversed(refs)) if self.mode == 'multiple_refs' else [refs[-1]]}


def _campaign(tmp_path, panels, *, mode='selected', count=2, guarded=False, turns=4,
              provisional=()):
    task = TaskSpec(name='Synthetic final-adoption fixture', reference_sequence='AAAV',
        mutable_positions=[1, 2, 3], max_mutations=3, candidate_access='open',
        controls_per_batch=0, batch_fill_policy='full_plate', batch_size=count, max_rounds=1,
        initial_observation_policy='none', feedback_source='measured_replay',
        budget=BudgetSpec(experimental_wells=count, llm_calls=100, tool_calls=100))
    config = ResearchConfig(protocol_mode='typed', review_after_step=False)
    campaign = Campaign.initialize(str(tmp_path/'campaign'), task,
        workflow=Workflow(tool_names=['fixture_panel'], design_tool_rounds=turns, analysis_tool_rounds=0),
        meta=MetaPolicy(enabled=False), research_config=config)
    llm = SelectionLLM(mode, provisional)
    campaign.team = Team(campaign.store, llm)
    calls = []
    def produce(args):
        calls.append(args['panel'])
        return {'status': 'ok', 'candidates': [{'sequence': sequence} for sequence in panels[args['panel']]]}
    campaign.team.tools.register(ToolSpec(name='fixture_panel', capability='sequence.generate',
        implementation_version='synthetic-v1', task_kinds=[task.kind],
        input_schema={'type': 'object', 'properties': {'panel': {'type': 'integer'}},
                      'required': ['panel'], 'additionalProperties': False},
        output_schema={'type': 'object', 'required': ['candidates']}), produce)
    if guarded:
        from proteinrsi.replay.broker import GuardedTeam
        campaign.team = GuardedTeam.from_team(campaign.team)
    return campaign, llm, calls, config


def test_rejected_intermediate_panel_is_not_published_or_ordered_first(tmp_path):
    values = _sequences()
    old, selected = values[:3], [values[3], values[1], values[4]]
    campaign, llm, calls, config = _campaign(tmp_path, [old, selected], count=3)
    result = run_campaign_protocol(campaign.team, campaign.view(), config)
    assert [candidate.sequence for candidate in result] == selected
    assert calls == [0, 1]
    snapshots = campaign.store.all('research_runs')
    assert len(snapshots) == 1
    record = next(iter(snapshots.values()))
    assert [row['sequence'] for row in record['state']['final']] == selected
    assert campaign.store.usage()['experimental_wells']['committed'] == 0
    # Same-version reentry preserves exact selection and cached tool charges.
    before = len(llm.calls), campaign.store.usage()
    again = run_campaign_protocol(campaign.team, campaign.view(), config)
    assert again == result and (len(llm.calls), campaign.store.usage()) == before
    assert calls == [0, 1]


def test_provisional_direct_candidates_are_not_implicitly_adopted(tmp_path):
    values = _sequences()
    campaign, _, calls, config = _campaign(tmp_path, [values[2:4]], mode='provisional',
                                          provisional=values[:2])
    result = run_campaign_protocol(campaign.team, campaign.view(), config)
    assert [candidate.sequence for candidate in result] == values[2:4]
    assert calls == [0]


def test_explicit_multiple_refs_preserve_requested_order_and_deduplicate(tmp_path):
    values = _sequences()
    campaign, _, calls, config = _campaign(tmp_path, [values[:3], values[2:5]], mode='multiple_refs')
    result = run_campaign_protocol(campaign.team, campaign.view(), config)
    assert [candidate.sequence for candidate in result] == values[2:5] + values[:2]
    assert calls == [0, 1]


def test_empty_final_selection_does_not_fall_back_to_any_tool_panel(tmp_path):
    campaign, _, calls, config = _campaign(tmp_path, [_sequences()[:2]], mode='empty')
    with pytest.raises(ContractError, match='candidate'):
        run_campaign_protocol(campaign.team, campaign.view(), config)
    assert calls == [0]
    assert not campaign.store.all('batches')
    assert campaign.store.usage()['experimental_wells']['committed'] == 0


def test_final_selection_provider_resume_does_not_rerun_successful_tools(tmp_path):
    values = _sequences()
    campaign, _, calls, config = _campaign(tmp_path, [values[:2], values[2:4]], mode='pause')
    with pytest.raises(ProviderPaused):
        run_campaign_protocol(campaign.team, campaign.view(), config)
    result = run_campaign_protocol(campaign.team, campaign.view(), config)
    assert [candidate.sequence for candidate in result] == values[2:4]
    assert calls == [0, 1]
    assert campaign.store.usage()['tool_calls']['committed'] == 2


@pytest.mark.parametrize("turns", [1, 2])
def test_tool_turn_exhaustion_never_submits_intermediate_candidates(tmp_path, turns):
    campaign, _, calls, config = _campaign(tmp_path, [_sequences()[:2]], mode='exhaustion', turns=turns)
    with pytest.raises(ContractError, match='tool-turn limit'):
        run_campaign_protocol(campaign.team, campaign.view(), config)
    assert calls == ([] if turns == 1 else [0])
    assert not campaign.store.all('batches')


@pytest.mark.parametrize('guarded', [False, True])
def test_full_100_well_plate_uses_only_final_selected_panel(tmp_path, guarded):
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()['available']:
            pytest.skip('Actual guarded backend unavailable; never use an unsafe fallback')
    values = _sequences()
    rejected, selected = values[:100], values[100:200]
    campaign, _, calls, _ = _campaign(tmp_path, [rejected, selected], count=100, guarded=guarded)
    batch = campaign.prepare()
    assert [sample.candidate.sequence for sample in batch.samples] == selected
    assert calls == [0, 1]
    usage = campaign.store.usage()
    assert usage['experimental_wells']['committed'] == 0
    assert usage['experimental_wells']['reserved'] == 100
    assert campaign.state['round_index'] == 0
    assert not campaign.store.all('measurements')
    assert campaign.prepare() == batch
    assert calls == [0, 1]


def test_fresh_run_does_not_reuse_legacy_proposal_step_receipt(tmp_path):
    from copy import deepcopy
    from proteinrsi.contracts import digest
    from proteinrsi.dataflow.integration import build_operations
    from proteinrsi.dataflow.protocol import Protocol, ProtocolExecutor
    from proteinrsi.dataflow.resources import ResourceStore, standard_registry, scope_for, SEQUENCES
    values = _sequences()
    campaign, _, calls, _ = _campaign(tmp_path, [values[:2], values[2:4]])
    view = campaign.view()
    resources = ResourceStore(campaign.store, standard_registry(), scope_for(view))
    operations = build_operations(campaign.team, view, resources)
    assert operations.get('agent:propose').implementation == 'typed-designer-v3-final-selection'
    arguments = {'question': 'Select the final panel'}
    old_key = 'protocol-step:' + digest({'scope': resources.scope,
        'operation': 'agent:propose', 'implementation': 'typed-designer-v2',
        'arguments': arguments, 'parents': [], 'output_schema': resources.registry.fingerprint(SEQUENCES)})
    rejected_ref = resources.sequences([{'sequence': row} for row in values[:2]],
                                        producer='synthetic-old-proposal')['resource_id']
    old_receipt = {'status': 'done', 'resource_id': rejected_ref}
    campaign.store.put('research_step_outputs', old_key, deepcopy(old_receipt))
    protocol = Protocol(hypothesis='Fresh corrected execution, preserving historical receipt',
        steps=[{'step_id': 'design', 'operation': 'agent:propose', 'question': 'Select',
                'arguments': arguments}],
        final_outputs={'candidates': {'source': 'step:design.result', 'schema_ref': SEQUENCES}})
    result = ProtocolExecutor(resources, operations).execute(protocol, {})
    selected = resources.candidates(result['outputs']['candidates']['resource_id'])
    assert [candidate.sequence for candidate in selected] == values[2:4]
    assert campaign.store.get('research_step_outputs', old_key) == old_receipt
    assert calls == [0, 1]
