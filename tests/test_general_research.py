"""General-task and generated-code mechanism tests: no live provider or protein engine."""
import json

import httpx
import pytest

from proteinrsi.contracts import Candidate, MetaPolicy, TaskSpec, Workflow
from proteinrsi.goal import prepare_research_goal
from proteinrsi.llm import JSONLLM
from proteinrsi.research.code import CODE_TOOL, register_code_tool
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.tasks import validate_candidate
from proteinrsi.tools import ToolCall


def factory(intent, requests):
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(intent)}}]})
    return lambda store: JSONLLM(store, model='fake', base_url='https://example.invalid',
        api_key='test-secret', transport=httpx.MockTransport(respond))


def rank_intent():
    return dict(task_kind='variant_ranking', route='computational', rationale='User supplied ranking inputs',
        reference_sequence='ACDE', candidates=['ACDE', 'AVDE'], mutable_positions=[2],
        max_mutations=1, metric='computed_sequence_quality', unit='proxy', rounds=2, queries=0)


def test_intake_selects_ranking_without_gb1_and_no_label_files(tmp_path):
    requests = []
    result = prepare_research_goal('对 ACDE 和 AVDE 排序，以 ACDE 为母本，仅第2位变化，计算迭代2轮',
        out=tmp_path/'run', data_root=tmp_path/'no-datasets', llm_factory=factory(rank_intent(), requests))
    assert not result['questions'] and result['route'] == 'computational'
    campaign = result['campaign']
    task = campaign.view().task
    assert task.kind.value == 'variant_ranking' and task.execution_mode == 'computational'
    assert task.budget.experimental_wells == 0
    assert CODE_TOOL in campaign.view().workflow.tool_names
    assert campaign.store.usage()['llm_calls']['committed'] == 1
    assert 'available_tools' in json.loads(requests[0]['messages'][1]['content'])
    with pytest.raises(ValueError, match='computational runner'):
        campaign.prepare()


def test_ambiguous_goal_clarifies_then_continues_without_json(tmp_path):
    path = tmp_path/'pending'
    initial = prepare_research_goal('研究 ACDE 和 AVDE', out=path, data_root=tmp_path,
        llm_factory=factory({'rationale': 'Ambiguous', 'questions': ['排序还是设计？']}, []))
    assert initial['campaign'] is None and initial['questions']
    assert Campaign.__name__ and not (path/'task.json').exists()
    ready = prepare_research_goal('以 ACDE 为母本做计算排序，2轮，第2位可变', out=path, data_root=tmp_path,
        continue_from=True, llm_factory=factory(rank_intent(), []))
    assert not ready['questions']
    assert ready['campaign'].store.usage()['llm_calls']['committed'] == 2


def test_missing_reference_is_not_invented(tmp_path):
    intent = rank_intent()
    result = prepare_research_goal('对我的序列排序两轮', out=tmp_path/'run', data_root=tmp_path,
        llm_factory=factory(intent, []))
    assert result['questions'] and result['campaign'] is None


def test_binder_length_and_indel_constraints_are_task_specific():
    task = TaskSpec(name='De novo binder', kind='binder_design', target_sequence='ACDE',
        min_length=3, max_length=8, max_mutations=None, execution_mode='computational',
        budget={'experimental_wells': 0}, controls_per_batch=0)
    validate_candidate(task, Candidate(sequence='ACD'))
    validate_candidate(task, Candidate(sequence='ACDEFGH'))
    with pytest.raises(ValueError):
        validate_candidate(task, Candidate(sequence='AC'))
    variable = TaskSpec(name='Variable length', reference_sequence='ACDE', allow_indels=True,
        mutable_positions=[], max_mutations=None, min_length=2, max_length=8)
    validate_candidate(variable, Candidate(sequence='ACDEFG'))
    fixed = TaskSpec(name='Fixed', reference_sequence='ACDE', mutable_positions=[2])
    with pytest.raises(ValueError):
        validate_candidate(fixed, Candidate(sequence='ACDEFG'))


@pytest.fixture
def code_campaign(tmp_path):
    from proteinrsi.replay.sandbox import probe
    if not probe()['available']:
        pytest.skip('Generated-code sandbox unavailable')
    task = TaskSpec(name='Synthetic code mechanism test', reference_sequence='ACDE',
        mutable_positions=[2], candidates=['ACDE', 'AVDE'], kind='variant_ranking',
        controls_per_batch=0, max_rounds=2, execution_mode='computational', budget={'experimental_wells': 0})
    campaign = Campaign.initialize(str(tmp_path/'campaign'), task, meta=MetaPolicy(enabled=False),
        workflow=Workflow(tool_names=[CODE_TOOL]), research_config=ResearchConfig(enable_generated_code=True))
    register_code_tool(campaign.team.tools, campaign.view())
    return campaign


def call_code(campaign, code, **extra):
    return campaign.team.tools.call(ToolCall(name=CODE_TOOL, arguments={'code': code, **extra}),
        campaign.view().task, allowed=[CODE_TOOL], context_key='synthetic-code-test')


def test_python_computes_metrics_and_repairs_errors(code_campaign):
    first = call_code(code_campaign, "result = {'metric': unknown_name}")
    assert first['status'] == 'failed' and first['error_type'] == 'NameError'
    code = "import numpy as np\nprint('computed')\nresult = {'metric': float(np.mean(inputs['values']))}"
    second = call_code(code_campaign, code, inputs={'values': [2, 4]})
    assert second['status'] == 'ok', second
    assert second['output']['metric'] == 3
    assert second['measurement_authority'] is False
    assert second['stdout'] == 'computed\n'
    assert call_code(code_campaign, code, inputs={'values': [2, 4]}) == second
    assert code_campaign.store.usage()['tool_calls']['committed'] == 2
    assert code_campaign.store.usage()['experimental_wells']['committed'] == 0


def test_generated_code_cannot_read_labels_db_env_or_network(code_campaign, tmp_path, monkeypatch):
    hidden = tmp_path/'labels.csv'
    hidden.write_text('secret-labels')
    monkeypatch.setenv('PROTEINRSI_API_KEY', 'never-send-this')
    source = f'''import os, socket
blocked = []
for path in [{str(hidden)!r}, {str(code_campaign.store.path)!r}, '/etc/passwd']:
    try:
        open(path).read()
    except PermissionError:
        blocked.append(path)
try:
    socket.socket()
except PermissionError:
    blocked.append('network')
result = {{'blocked': blocked, 'key': os.environ.get('PROTEINRSI_API_KEY')}}
'''
    result = call_code(code_campaign, source)
    assert result['status'] == 'ok', result
    assert len(result['output']['blocked']) == 4
    assert result['output']['key'] is None


def test_code_artifacts_are_registered_and_traceable(code_campaign):
    result = call_code(code_campaign, "import json\na = write_artifact('metrics.json', json.dumps({'score': 3}), 'json')\nresult = {'artifacts': [a]}")
    assert result['status'] == 'ok', result
    from proteinrsi.localtools.artifacts import ArtifactStore
    assert json.loads(ArtifactStore(code_campaign.store).resolve(result['artifacts'][0]['ref']).read_text()) == {'score': 3}


def test_computational_iterations_share_history_without_experimental_queries(code_campaign):
    from proteinrsi.computational import run_computational
    class Scripted:
        def run(self, view):
            assert len(view.history) == view.round_index
            return [Candidate(sequence='AVDE'), Candidate(sequence='ACDE')]
    code_campaign.team = Scripted()
    report = run_computational(code_campaign, guarded=False)
    assert report['completed_rounds'] == 2 and report['status'] == 'complete'
    assert report['best_measured_value'] is None
    assert report['returned_observations'] == 0
    assert len(report['final_computational_candidates']) == 2
    assert report['budget']['experimental_wells']['committed'] == 0
    assert not code_campaign.store.all('batches')


def test_guarded_ranking_uses_generated_program_without_design_and_revises(code_campaign):
    from proteinrsi.agents import Team
    from proteinrsi.computational import run_computational
    from proteinrsi.research.contracts import ResearchPlan, ResearchStep
    class ProgramLLM:
        model = 'scripted-test'
        base_url = 'https://example.invalid'
        cache_settings = {}
        def __init__(self, store): self.store = store
        def complete(self, role, instructions, context, schema):
            if role == 'A-plan':
                steps = [ResearchStep(step_id='custom', operation='tool', question='Compute a metric',
                    expected_output='metric', tool_call=ToolCall(name=CODE_TOOL,
                        arguments={'code': "result = {'metric': missing_name}"})),
                    ResearchStep(step_id='rank', operation='rank', question='Rank supplied inputs', expected_output='ranking'),
                    ResearchStep(step_id='final', operation='finalize', question='Return ordering', expected_output='priorities')]
                return ResearchPlan(hypothesis='Artificial code repair and ranking test', steps=steps).model_dump(mode='json')
            if role == 'A-review':
                latest = context['latest_result']
                if latest.get('status') == 'failed':
                    return {'rationale': 'Repair the observed NameError', 'pending_steps': [
                        {'step_id': 'repaired', 'operation': 'tool', 'question': 'Compute metric correctly',
                         'expected_output': 'metric', 'tool_call': {'name': CODE_TOOL,
                             'arguments': {'code': "result = {'metric': len(context['task']['reference_sequence'])}"}}},
                        {'step_id': 'rank', 'operation': 'rank', 'question': 'Rank supplied inputs', 'expected_output': 'ranking'},
                        {'step_id': 'final', 'operation': 'finalize', 'question': 'Return ordering', 'expected_output': 'priorities'}]}
                return {'rationale': 'Continue after inspecting actual result', 'pending_steps': None}
            if role in {'C', 'A-selection'}:
                return {'summary': 'Synthetic ranking fixture', 'ranking': ['AVDE', 'ACDE']}
            raise AssertionError('Unexpected role or mandatory design: '+role)
    llm = ProgramLLM(code_campaign.store)
    code_campaign.team = Team(code_campaign.store, llm)
    report = run_computational(code_campaign, guarded=True)
    assert report['completed_rounds'] == 2 and report['returned_observations'] == 0
    runs = list(code_campaign.store.all('research_runs').values())
    assert len(runs) == 2
    assert all(r['revisions'] for r in runs)
    assert not any(s['operation'] == 'design' for r in runs for s in r['completed'])
    assert report['budget']['tool_calls']['committed'] == 4
    assert report['budget']['experimental_wells']['committed'] == 0


def test_binder_intake_uses_fasta_target_without_placeholder_scaffold(tmp_path):
    fasta = tmp_path/'target.fasta'
    fasta.write_text('>target\nACDEFG\n')
    intent = dict(task_kind='binder_design', route='computational', rationale='De novo binder goal',
        target_sequence='ACDEFG', min_length=20, max_length=60, max_mutations=None,
        metric='computed_interface_quality', unit='proxy', rounds=2, queries=0)
    prepared = prepare_research_goal('为输入靶点设计20到60残基binder，只进行计算，2轮迭代',
        out=tmp_path/'binder', data_root=tmp_path, inputs=[fasta], llm_factory=factory(intent, []))
    assert not prepared['questions']
    task = prepared['campaign'].view().task
    assert task.reference_sequence == '' and task.target_sequence == 'ACDEFG'
    assert task.min_length == 20 and task.max_length == 60
    assert prepared['campaign'].store.all('artifacts')


def test_repeat_policy_is_task_defined(campaign, oracle):
    from conftest import finish_round
    finish_round(campaign, oracle)
    state = campaign.state
    state['task']['repeat_policy'] = 'allow'
    campaign.store.put('campaign', 'state', state)
    measured = state['observations'][0]['sequence']
    from proteinrsi.agents import Team
    class RepeatTeam(Team):
        def run(self, view): return [Candidate(sequence=measured)]
    campaign.team = RepeatTeam(campaign.store)
    batch = campaign.prepare()
    assert any(s.candidate.sequence == measured and s.arm == 'baseline' for s in batch.samples)


def test_ranking_needs_no_artificial_parent_and_can_compare_different_lengths(tmp_path):
    intent = dict(task_kind='variant_ranking', route='computational', rationale='Rank the supplied proteins',
        candidates=['ACD', 'ACDEFG'], rounds=1, queries=0, metric='computed_quality', unit='proxy')
    prepared = prepare_research_goal('比较 ACD 和 ACDEFG，计算排序1轮，不需要母本', out=tmp_path/'rank',
        data_root=tmp_path, llm_factory=factory(intent, []))
    assert not prepared['questions']
    task = prepared['campaign'].view().task
    assert task.reference_sequence == ''
    for seq in intent['candidates']:
        validate_candidate(task, Candidate(sequence=seq))
    with pytest.raises(ValueError):
        validate_candidate(task, Candidate(sequence='ACDEF'))


def test_de_novo_binder_adapters_use_actual_backbone_without_fake_reference(tmp_path, monkeypatch):
    from proteinrsi.localtools import functions as fn
    from proteinrsi.localtools.artifacts import ArtifactStore
    from test_local_tools import pdb_fixture
    task = TaskSpec(name='Synthetic binder adapter test', kind='binder_design', target_sequence='ACDE',
        min_length=3, max_length=8, max_mutations=None, controls_per_batch=0,
        execution_mode='computational', budget={'experimental_wells': 0})
    campaign = Campaign.initialize(str(tmp_path/'binder'), task)
    registry = ArtifactStore(campaign.store)
    target = registry.put(pdb_fixture(tmp_path/'target.pdb', {'A': 'ACDE'}), 'pdb')['ref']
    requests = []
    class FakeJob:
        def __init__(self, store, engine, config):
            self.store, self.engine = store, engine
            self.job_id = 'fake-'+engine
            self.work = tmp_path/self.job_id
            self.work.mkdir()
            store.put('local_jobs', self.job_id, {'state': 'executed_pending_validation'})
        def run(self, params):
            requests.append((self.engine, params))
            if self.engine == 'rfdiffusion':
                pdb_fixture(self.work/'backbone.pdb', {'A': 'ACDE', 'B': 'GGGG'})
                return {'files': [{'path': 'backbone.pdb', 'kind': 'pdb'}]}
            return {'candidates': [{'sequence': 'AVDE'}], 'files': []}
    monkeypatch.setattr(fn, 'LocalJob', FakeJob)
    backbone = fn.rfdiffusion_binder({'length': 4, 'target_ref': target, 'target_chain': 'A',
        'hotspots': [], 'num_designs': 1, 'seed': 1}, task, campaign.store, None)
    output = fn.proteinmpnn_design({'backbone_ref': backbone['backbones'][0]['backbone_ref'],
        'design_chain': 'B', 'target_chain': 'A', 'num_sequences': 1, 'temperature': .1, 'seed': 1},
        task, campaign.store, None)
    assert output['candidates'][0]['sequence'] == 'AVDE'
    assert requests[-1][1]['fixed_positions'] == []
    assert task.reference_sequence == ''
    with pytest.raises(ValueError, match='bounds'):
        fn.rfdiffusion_binder({'length': 9}, task, campaign.store, None)


def test_generated_code_has_no_unbounded_scratch_writes(code_campaign):
    output = call_code(code_campaign, "open('unbounded-file', 'x').write('data')\nresult = {}")
    assert output['status'] == 'failed'
    assert output['error_type'] == 'PermissionError'


def test_new_goal_runs_typed_ranking_feedback_loop_in_guarded_worker(tmp_path):
    from proteinrsi.agents import Team
    from proteinrsi.computational import run_computational
    from proteinrsi.replay.sandbox import probe
    if not probe()['available']:
        pytest.skip('Host isolation unavailable')
    prepared = prepare_research_goal('对 ACDE 和 AVDE 排序，以 ACDE 为母本，仅第2位变化，计算迭代2轮',
        out=tmp_path/'typed-ranking', data_root=tmp_path, llm_factory=factory(rank_intent(), []))
    c = prepared['campaign']
    assert c.store.get('configuration', 'research')['protocol_mode'] == 'typed'
    assert c.store.get('configuration', 'research')['context_policy'] == 'evidence-v1'
    seen = []
    knowledge_attempts = []
    def respond(request):
        payload = json.loads(request.content)
        instructions = payload['messages'][0]['content']
        ctx = json.loads(payload['messages'][1]['content'])
        if 'catalogue' in ctx:
            knowledge_attempts.append(ctx['format_attempt'])
            assert ctx['catalogue']['templates']
            result = {'template_ids': [], 'extra_metric_ids': [],
                      'rationale': 'Ranking-only exploration of supplied candidates'}
        elif 'Select resources relevant' in instructions:
            result = {}
        elif '# A — resource protocol planner' in instructions:
            assert ctx['resources']['method_knowledge']['template_ids'] == []
            round_index = ctx['view']['round_index']
            seen.append(round_index)
            if round_index == 1:
                assert ctx['view']['history'][0]['protocol_results']
                assert ctx['view']['observations'] == []
            result = {'hypothesis': 'Rank supplied candidates without a design stage', 'steps': [
                {'step_id': 'rank', 'operation': 'agent:rank', 'question': 'Rank visible inputs',
                 'bindings': {'candidates': {'source': 'input:supplied_candidates',
                    'schema_ref': 'protein.sequence_set/v1', 'delivery': 'ref'}}}],
                'final_outputs': {'ranking': {'source': 'step:rank.result',
                                             'schema_ref': 'protein.ranking/v1'}}}
        elif 'candidates' in ctx:
            result = {'ranking': [x['candidate_id'] for x in reversed(ctx['candidates'])],
                      'summary': 'Artificial preference, no numerical phenotype claim'}
        else:
            raise AssertionError('Unexpected design/tool operation: ' + instructions[:80])
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    c.team = Team(c.store, JSONLLM(c.store, model='fake', base_url='https://example.invalid',
        api_key='fake', transport=httpx.MockTransport(respond)))
    report = run_computational(c, guarded=True)
    assert seen == [0, 1] and report['completed_rounds'] == 2
    # Unchanged task/catalogue reuses the provider response in the second round.
    assert knowledge_attempts == [0]
    assert report['budget']['experimental_wells']['committed'] == 0
    assert report['budget']['tool_calls']['committed'] == 0
    assert c.state['observations'] == []
    assert all(r['runner'] == 'resource-protocol-v1' for r in c.store.all('research_runs').values())


def test_legacy_intake_mode_is_explicit_and_survives_clarification(tmp_path):
    out = tmp_path/'legacy'
    prepare_research_goal('比较 ACDE 和 AVDE', out=out, data_root=tmp_path, protocol_mode='legacy',
        llm_factory=factory({'rationale': 'Need objective', 'questions': ['需要排序吗？']}, []))
    prepared = prepare_research_goal('以 ACDE 为母本排序2轮，只改第2位，计算反馈', out=out,
        data_root=tmp_path, continue_from=True, llm_factory=factory(rank_intent(), []))
    assert prepared['campaign'].store.get('configuration', 'research')['protocol_mode'] == 'legacy'
