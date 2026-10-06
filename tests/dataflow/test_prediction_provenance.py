# SPDX-License-Identifier: MIT
"""Synthetic-only checks of the real tool/typed-resource/frozen-batch path."""
import copy
import json
import os
import subprocess
import sys

import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import Candidate, MetaPolicy, Observation, digest, sequence_hash
from proteinrsi.dataflow.integration import build_operations, run_campaign_protocol
from proteinrsi.dataflow.protocol import Protocol
from proteinrsi.dataflow.resources import NAMESPACE, RANKING, SEQUENCES, ResourceStore, scope_for, standard_registry
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.dataflow.tasks import default_profiles
from proteinrsi.research.analysis import prediction_errors
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.research.prediction import PREDICT_TOOL, attach_predictions, register_prediction_tool
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.tools import ToolCall, ToolGateway


def variant(view, residue):
    sequence = list(view.task.reference_sequence)
    sequence[38] = residue
    return ''.join(sequence)


def observations(view):
    return [Observation(sample_id=f'training-{i}', batch_id='revealed-fixture',
        sequence=variant(view, residue), value=value, metric=view.task.metric,
        unit=view.task.unit, source=view.task.feedback_source, assay_protocol=view.task.assay_protocol)
        for i, (residue, value) in enumerate([('V', 1.0), ('A', 3.0)])]


def prediction_fixture(store, view, sequences=None):
    view = view.model_copy(update={'observations': observations(view)})
    gateway = ToolGateway(store)
    register_prediction_tool(gateway, view)
    sequences = sequences or [variant(view, 'C'), variant(view, 'D')]
    result = gateway.call(ToolCall(name=PREDICT_TOOL,
        arguments={'sequences': sequences, 'features': 'mutation'}), view.task,
        allowed=[PREDICT_TOOL], context_key='explicit-fixture')
    return view, gateway, result


def attach(view, store, result, sequences=None):
    candidates = {s: Candidate(sequence=s) for s in
                  (sequences or [r['sequence'] for r in result['predictions']])}
    attach_predictions(candidates, {s: result['artifact_ref'] for s in candidates},
                       view, [result], store)
    return list(candidates.values())


def protocol_for(team, view):
    resources = ResourceStore(team.store, standard_registry(), scope_for(view))
    operations = build_operations(team, view, resources)
    schema = operations.get('tool:' + PREDICT_TOOL).output_schema_ref
    return Protocol.model_validate({'hypothesis': 'Explicit synthetic prediction provenance',
        'steps': [
            {'step_id': 'design', 'operation': 'agent:propose', 'question': 'Three legal variants',
             'arguments': {'question': 'Three legal variants'}},
            {'step_id': 'predict', 'operation': 'tool:' + PREDICT_TOOL, 'question': 'Fit revealed labels',
             'arguments': {'sequences': [variant(view, 'C'), variant(view, 'D')], 'features': 'mutation'}},
            {'step_id': 'rank', 'operation': 'agent:rank', 'question': 'Attach actual predictions',
             'bindings': {'candidates': {'source': 'step:design.result', 'schema_ref': SEQUENCES, 'delivery': 'ref'},
                          'evidence': {'source': 'step:predict.result', 'schema_ref': schema}}},
            {'step_id': 'select', 'operation': 'agent:select', 'question': 'Review ranked candidates',
             'bindings': {'ranking': {'source': 'step:rank.result', 'schema_ref': RANKING, 'delivery': 'ref'}}},
            {'step_id': 'ordered', 'operation': 'adapter:ranked_sequences', 'question': 'Final candidate resource',
             'bindings': {'ranking': {'source': 'step:select.result', 'schema_ref': RANKING, 'delivery': 'ref'}}}],
        'final_outputs': {'candidates': {'source': 'step:ordered.result', 'schema_ref': SEQUENCES}}})


class FixtureLLM:
    def __init__(self):
        self.selection = None

    def complete(self, role, instructions, context, schema):
        if role == 'B':
            sequence = context['view']['task']['reference_sequence']
            return {'candidates': [{'sequence': sequence[:38] + r + sequence[39:]} for r in 'CDE']}
        if role == 'C':
            evidence = context['tool_results'][0]
            predicted = {r['sequence'] for r in evidence['predictions']}
            return {'ranking': [c['candidate_id'] for c in context['candidates']],
                'summary': 'Use the executed tool only for its exact sequences',
                'prediction_refs': {c['candidate_id']: evidence['artifact_ref']
                    for c in context['candidates'] if c['sequence'] in predicted}}
        if role == 'A-selection':
            self.selection = context['ranked_candidates']
            return {'ranking': [c['candidate_id'] for c in self.selection][::-1], 'summary': 'Reverse priorities'}
        if role == 'C-feedback':
            return {'summary': 'Synthetic assay received'}
        raise AssertionError(role)


class ExplicitTeam(Team):
    def run(self, view):
        self.bind_tools(view)
        return run_campaign_protocol(self, view, ResearchConfig(protocol_mode='typed', review_after_step=False),
                                     protocol=protocol_for(self, view))


def test_actual_tool_predictions_survive_typed_selection_batch_feedback_and_fresh_cache(tmp_path, view):
    workflow = view.workflow.model_copy(update={'tool_names': [PREDICT_TOOL]})
    campaign = Campaign.initialize(str(tmp_path / 'campaign'), view.task, workflow=workflow,
                                   meta=MetaPolicy(enabled=False))
    state = campaign.state
    state['observations'] = [o.model_dump(mode='json') for o in observations(view)]
    campaign.store.put('campaign', 'state', state)
    llm = FixtureLLM()
    campaign.team = ExplicitTeam(campaign.store, llm, register_tools=False)
    initial_view = campaign.view()
    batch = campaign.prepare()
    artifacts = campaign.store.all('task_predictions')
    assert len(artifacts) == 1
    artifact_key, artifact = next(iter(artifacts.items()))
    expected = {r['sequence']: r['predicted_value'] for r in artifact['predictions']}
    ref = 'task_predictions/' + artifact_key
    for candidates in [llm.selection, [s.candidate.model_dump() for s in batch.samples]]:
        assert [c['predicted_value'] for c in candidates if c['sequence'] in expected] == [
            expected[c['sequence']] for c in candidates if c['sequence'] in expected]
        assert all(c['prediction_ref'] == ref for c in candidates if c['sequence'] in expected)
        assert all(c['predicted_value'] is None and c['prediction_ref'] is None
                   for c in candidates if c['sequence'] not in expected)
    assert [s.candidate.sequence for s in batch.samples] == [variant(view, r) for r in 'EDC']
    run = next(iter(campaign.store.all('research_runs').values()))
    final_ref = run['protocol_result']['outputs']['candidates']['resource_id']
    resources = ResourceStore(campaign.store, standard_registry(), scope_for(initial_view))
    final = resources.get(final_ref)
    assert final['prediction_refs'] == {'seq:' + sequence_hash(s): ref for s in expected}
    assert final['measurement_authority'] is False
    assert all('predicted_value' not in item and 'prediction_ref' not in item for item in final['data']['items'])
    assert [c.predicted_value for c in resources.candidates(final_ref)] == [s.candidate.predicted_value for s in batch.samples]
    frozen = copy.deepcopy(campaign.store.get('batches', batch.batch_id))
    assert campaign.store.all('measurements') == {}
    assert campaign.store.usage()['tool_calls']['committed'] == 1

    # Reopen the database in a fresh interpreter: the typed protocol's receipts
    # reconstruct all annotations without an LLM call, refit, or new tool charge.
    resume = tmp_path / 'resume.json'
    resume.write_text(json.dumps({'view': initial_view.model_dump(mode='json'), 'protocol': run['plan']}))
    script = '''
import json, sys
from proteinrsi.agents import Team
from proteinrsi.contracts import TaskView
from proteinrsi.dataflow.integration import run_campaign_protocol
from proteinrsi.dataflow.protocol import Protocol
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.storage import Store
class NoCalls:
    def complete(self, *args):
        raise AssertionError("Unexpected model call on resume")
store = Store(sys.argv[1])
saved = json.load(open(sys.argv[2]))
view = TaskView.model_validate(saved['view'])
team = Team(store, NoCalls(), register_tools=False)
team.bind_tools(view)
result = run_campaign_protocol(team, view, ResearchConfig(protocol_mode='typed', review_after_step=False),
                               protocol=Protocol.model_validate(saved['protocol']))
print(json.dumps([c.model_dump(mode='json') for c in result]))
'''
    output = subprocess.run([sys.executable, '-c', script, str(campaign.store.root), str(resume)],
                            check=True, text=True, capture_output=True, env=os.environ.copy())
    assert [c['predicted_value'] for c in json.loads(output.stdout)] == [s.candidate.predicted_value for s in batch.samples]
    assert campaign.store.usage()['tool_calls']['committed'] == 1
    campaign.approve(batch.batch_id, operator='synthetic-test')
    feedback = [Observation(sample_id=s.sample_id, batch_id=batch.batch_id, sequence=s.candidate.sequence,
        value=(s.candidate.predicted_value or 0) + 2.0, metric=view.task.metric, unit=view.task.unit,
        source=view.task.feedback_source, assay_protocol=view.task.assay_protocol) for s in batch.samples]
    campaign.ingest(feedback)
    assert campaign.state['history'][-1]['prediction_mae'] == pytest.approx(2.0)
    diagnostic = prediction_errors(campaign.view(), campaign.store)
    assert diagnostic['n'] == 2
    assert diagnostic['mae'] == pytest.approx(2.0)
    assert {r['prediction'] for r in diagnostic['rows']} == set(expected.values())
    assert campaign.store.get('batches', batch.batch_id) == frozen


@pytest.mark.parametrize('change', ['evidence', 'workflow', 'task', 'round', 'context', 'metric', 'unit'])
def test_prediction_refs_reject_other_current_scopes(store, view, change):
    view, _, result = prediction_fixture(store, view)
    other = view.model_copy(deep=True)
    if change == 'evidence':
        other.observations[0].value += 1
    elif change == 'workflow':
        other.workflow.ridge_alpha += 1
    elif change == 'task':
        other.task.name += ' different'
    elif change == 'round':
        other.round_index += 1
    elif change == 'context':
        other.research_context = {'another_request': True}
    else:
        setattr(other.task, change, 'different')
    with pytest.raises(ValueError, match='provenance'):
        attach(other, store, result)
    candidate = attach(view, store, result)[0]
    resources = ResourceStore(store, standard_registry(), scope_for(other))
    with pytest.raises(ContractError, match='provenance'):
        resources.sequences([candidate], producer='agent:C')


def test_prediction_refs_reject_fabrication_sequence_and_numeric_overrides(store, view):
    view, _, result = prediction_fixture(store, view)
    candidates = attach(view, store, result)
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    with pytest.raises(ValueError, match='sequence'):
        attach(view, store, result, [variant(view, 'E')])
    with pytest.raises(ValueError, match='explicitly obtained'):
        attach_predictions({c.sequence: c for c in candidates}, {candidates[0].sequence: result['artifact_ref']},
                           view, [], store)
    forged = copy.deepcopy(result)
    forged['predictions'][0]['predicted_value'] += 999
    with pytest.raises(ValueError, match='differs'):
        attach(view, store, forged)
    for candidate in [candidates[0].model_copy(update={'predicted_value': 999}),
                      candidates[0].model_copy(update={'uncertainty': 0.1}),
                      candidates[0].model_copy(update={'evidence_kind': 'calibrated_prediction'})]:
        with pytest.raises(ContractError, match='differs'):
            resources.sequences([candidate], producer='agent:C')
    with pytest.raises(ContractError, match='Unknown'):
        resources.sequences([candidates[0].model_copy(update={'prediction_ref': 'task_predictions/' + '0' * 64})],
                            producer='agent:C')
    plain = resources.sequences([Candidate(sequence=candidates[0].sequence, predicted_value=999)], producer='agent:B')
    assert resources.candidates(plain['resource_id'])[0].predicted_value is None
    raw = resources.get(plain['resource_id'])['data']
    raw['items'][0]['predicted_value'] = 999
    with pytest.raises(ContractError, match='validation'):
        resources.put(SEQUENCES, raw, producer='agent:B')


def test_task_profile_checks_artifact_metadata_and_hash_on_every_read(store, view):
    view, _, result = prediction_fixture(store, view)
    candidates = attach(view, store, result)
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    ref = resources.sequences(candidates, producer='agent:C')['resource_id']
    assert len(default_profiles().accept(view, resources, {'candidates': ref})) == 2
    wrong = copy.deepcopy(store.get('task_predictions', result['artifact_ref'].split('/')[1]))
    wrong['workflow'] = 'fabricated-workflow'
    key = digest(wrong)
    store.put('task_predictions', key, wrong, immutable=True)
    raw = resources.get(ref)['data']
    forged = resources.put(SEQUENCES, raw, producer='agent:C', prediction_refs={
        i['candidate_id']: 'task_predictions/' + key for i in raw['items']})['resource_id']
    with pytest.raises(ContractError, match='provenance'):
        default_profiles().accept(view, resources, {'candidates': forged})
    # A stale cached resource must re-check the content hash of its evidence.
    store.put('task_predictions', result['artifact_ref'].split('/')[1], wrong)
    reopened = ResourceStore(Store(store.root), standard_registry(), scope_for(view))
    with pytest.raises(ContractError, match='modified'):
        reopened.candidates(ref)


def test_insufficient_evidence_keeps_null_and_old_plain_resource_contract(store, view):
    gateway = ToolGateway(store)
    register_prediction_tool(gateway, view)
    result = gateway.call(ToolCall(name=PREDICT_TOOL,
        arguments={'sequences': [variant(view, 'C')], 'features': 'mutation'}), view.task,
        allowed=[PREDICT_TOOL], context_key='no-measurements')
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    candidates = attach(view, store, result)
    ref = resources.sequences(candidates, producer='agent:C')['resource_id']
    restored = resources.candidates(ref)[0]
    assert restored.prediction_ref == result['artifact_ref']
    assert restored.predicted_value is None and restored.evidence_kind == 'none'
    # No schema change or metadata is required for pre-existing unannotated data.
    plain = resources.sequences([Candidate(sequence=variant(view, 'D'))], producer='legacy')['resource_id']
    saved = store.get(NAMESPACE, plain)
    assert 'prediction_refs' not in saved
    assert saved['schema_ref'] == 'protein.sequence_set/v1'
    assert resources.candidates(plain)[0].prediction_ref is None


def test_prediction_metadata_survives_guarded_broker_without_new_write_authority(store, view):
    from types import SimpleNamespace
    from proteinrsi.replay.broker import dispatch
    view, gateway, result = prediction_fixture(store, view)
    team = SimpleNamespace(store=store, llm=None)

    class Proxy:
        def get(self, namespace, key, default=None):
            return dispatch(team, gateway, view, [], {'rpc': 'get', 'namespace': namespace,
                            'key': key, 'default': default})

        def put(self, namespace, key, value, *, immutable=False):
            return dispatch(team, gateway, view, [], {'rpc': 'put', 'namespace': namespace,
                            'key': key, 'value': value, 'immutable': immutable})

    proxy = Proxy()
    resources = ResourceStore(proxy, standard_registry(), scope_for(view))
    candidates = attach(view, proxy, result)
    ref = resources.sequences(candidates, producer='agent:C')['resource_id']
    assert [c.predicted_value for c in resources.candidates(ref)] == [c.predicted_value for c in candidates]
    with pytest.raises(PermissionError, match='Protected namespace'):
        proxy.put('task_predictions', 'forged', {'predictions': [{'predicted_value': 999}]})


@pytest.mark.parametrize('change', ['value', 'missing_value', 'reference', 'workflow', 'evidence'])
def test_frozen_diagnostics_recheck_original_prediction_provenance(store, view, change):
    view, _, result = prediction_fixture(store, view)
    candidate = attach(view, store, result)[0]
    batch = {'evidence_version': view.evidence_version, 'samples': [{'sample_id': 'measured',
        'workflow_version': view.workflow.version, 'arm': 'baseline', 'candidate': candidate.model_dump()}]}
    observed = Observation(sample_id='measured', batch_id='frozen', sequence=candidate.sequence,
        value=5, metric=view.task.metric, unit=view.task.unit, source=view.task.feedback_source,
        assay_protocol=view.task.assay_protocol)
    # Today's evidence/workflow can differ from the prediction's original context.
    current = view.model_copy(deep=True)
    current.observations.append(observed)
    current.workflow.ridge_alpha += 1
    store.put('batches', 'frozen', batch)
    assert prediction_errors(current, store)['n'] == 1
    if change == 'value':
        batch['samples'][0]['candidate']['predicted_value'] += 1
    elif change == 'missing_value':
        batch['samples'][0]['candidate']['predicted_value'] = None
    elif change == 'reference':
        batch['samples'][0]['candidate']['prediction_ref'] = 'task_predictions/' + '0' * 64
    elif change == 'workflow':
        batch['samples'][0]['workflow_version'] = current.workflow.version
    else:
        batch['evidence_version'] = current.evidence_version
    store.put('batches', 'frozen', batch)
    with pytest.raises(ValueError):
        prediction_errors(current, store)


def test_running_prediction_tool_does_not_attach_without_analyst_choice(store, view):
    class WithoutReferences(FixtureLLM):
        def complete(self, role, instructions, context, schema):
            result = super().complete(role, instructions, context, schema)
            if role == 'C':
                result.pop('prediction_refs')
            return result

    current = view.model_copy(update={'observations': observations(view),
        'workflow': view.workflow.model_copy(update={'tool_names': [PREDICT_TOOL]})})
    team = ExplicitTeam(store, WithoutReferences(), register_tools=False)
    result = team.run(current)
    assert store.usage()['tool_calls']['committed'] == 1
    assert all(c.predicted_value is None and c.prediction_ref is None for c in result)


def test_resource_metadata_rejects_wrong_sequence_and_ambiguous_parents(store, view):
    view, gateway, first = prediction_fixture(store, view)
    second = gateway.call(ToolCall(name=PREDICT_TOOL,
        arguments={'sequences': [variant(view, 'C')], 'features': 'mutation'}), view.task,
        allowed=[PREDICT_TOOL], context_key='explicit-second')
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    a, b = attach(view, store, first)[0], attach(view, store, second)[0]
    a_ref = resources.sequences([a], producer='agent:C')['resource_id']
    b_ref = resources.sequences([b], producer='agent:C')['resource_id']
    wrong = resources.sequences([Candidate(sequence=variant(view, 'E'))], producer='agent:B')['resource_id']
    with pytest.raises(ContractError, match='sequence'):
        resources.put(SEQUENCES, resources.get(wrong)['data'], producer='agent:C',
                      prediction_refs={'seq:' + sequence_hash(variant(view, 'E')): first['artifact_ref']})
    with pytest.raises(ContractError, match='Conflicting'):
        resources.sequences([a, b], producer='agent:C')
    with pytest.raises(ContractError, match='Conflicting'):
        resources.put(SEQUENCES, resources.get(a_ref)['data'], producer='agent:C', parents=[a_ref, b_ref])
