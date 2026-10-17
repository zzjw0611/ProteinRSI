"""Synthetic decision-entry resume checks. No datasets, provider APIs or experiments."""
import json
import os

import pytest

from proteinrsi.agents import Team
from proteinrsi.assistant_bridge import AssistantBridgeLLM, AssistantBridgeTimeout
from proteinrsi.contracts import Observation, TaskKind, TaskSpec, Workflow, digest
from proteinrsi.dataflow.resources import scope_for
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.storage import BudgetExceeded, Store
from proteinrsi.tools import ToolSpec


def campaign(tmp_path, **research_changes):
    return Campaign.initialize(str(tmp_path/'campaign'),
        TaskSpec(name='Synthetic decision entry',reference_sequence='AAAA',mutable_positions=[1]),
        workflow=Workflow(skill_names=['protein-metrics'],
            tool_names=['research_metric_extract','research_fit_predict']),
        research_config=ResearchConfig(context_policy='state-v1',protocol_mode='typed', **research_changes))


def catalog(c, view):
    team = Team(c.store,tools=c.team.tools.fork(),register_tools=False)
    team.bind_tools(view)
    return team.tools.catalog(view.task,['research_metric_extract','research_fit_predict'])


@pytest.mark.core
def test_transport_events_keep_decision_scope_and_live_audit(tmp_path):
    c = campaign(tmp_path)
    first = c._decision_view(c.state,'prepare')
    specs = catalog(c, first)
    for kind in ['llm_started','llm_completed','context_prepared','context_read_completed']:
        payload = {'key':'fixture','context_bytes':100,'estimated_input_tokens':20}
        c.store.event(kind,payload)
    resumed = Campaign(Store(c.store.root))
    again = resumed._decision_view(resumed.state,'prepare')
    assert first == again
    assert scope_for(first) == scope_for(again)
    assert catalog(resumed, again) == specs
    assert c.view().research_context['execution_summary'] != first.research_context['execution_summary']
    assert c.store.usage()['experimental_wells']['committed'] == 0


@pytest.mark.parametrize('change',['observation','workflow','meta','task','memory','config','tool','phase'])
def test_semantic_changes_invalidate_decision_entry(tmp_path, change):
    c = campaign(tmp_path)
    initial = c._decision_view(c.state,'prepare')
    c.store.event('llm_started',{'key':'fixture'})
    state = c.state
    if change == 'observation':
        state['observations'].append(Observation(sample_id='synthetic',batch_id='synthetic',sequence='CAAA',
            value=1.0,metric=state['task']['metric'],unit=state['task']['unit'],source='synthetic',assay_protocol=state['task']['assay_protocol']).model_dump(mode='json'))
    elif change == 'workflow':
        state['workflow']['designer_prompt'] += ' Changed fixture input.'
    elif change == 'meta':
        state['meta']['prompt'] += ' Changed fixture input.'
    elif change == 'task':
        state['task']['objective_description'] = 'Changed synthetic task objective'
    elif change == 'memory':
        c.store.put('research_states','fixture',{'working_memory':{'focus':'Changed semantic memory'}})
        c.store.put('research_state','active',{'ref':'fixture'})
    elif change == 'config':
        c.store.put('configuration','local_tools',{'fixture':'changed'})
    elif change == 'tool':
        c.team.tools.register(ToolSpec(name='fixture_tool',capability='analysis.fixture',
            implementation_version='changed-v1',task_kinds=[TaskKind.VARIANT],
            input_schema={'type':'object'},output_schema={'type':'object'}),lambda a:{})
    c.store.put('campaign','state',state)
    newer = c._decision_view(state,'improve' if change=='phase' else 'prepare')
    assert newer.research_context['execution_summary'] != initial.research_context['execution_summary']
    assert len(c.store.all('decision_entries')) == 2


def test_legacy_campaign_does_not_acquire_new_semantics(tmp_path):
    c = campaign(tmp_path)
    with c.store.connect() as con:
        con.execute("DELETE FROM kv WHERE namespace='configuration' AND key='decision_entry_semantics'")
    first = c._decision_view(c.state,'prepare')
    c.store.event('llm_started',{'key':'fixture'})
    second = c._decision_view(c.state,'prepare')
    assert first != second
    assert c.store.all('decision_entries') == {}
    assert c.store.get('configuration','decision_entry_semantics') is None


@pytest.mark.core
def test_bridge_context_read_pause_resume_reuses_requests_and_live_budget(tmp_path):
    c = campaign(tmp_path)
    def attach():
        store = Store(c.store.root)
        llm = AssistantBridgeLLM(store,model='transport-fixture',mailbox=tmp_path/'mailbox',
            timeout=0.01,poll_interval=0.001,max_request_bytes=1048576)
        current = Campaign(store,Team(store,llm))
        return current,llm
    schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],
              'additionalProperties':False}
    def call(current,llm):
        view = current._decision_view(current.state,'prepare')
        return llm.complete('A-plan','Transport fixture only',
            {'view':view.model_dump(mode='json'),'tools':catalog(current,view)},schema)
    def latest(store):
        return [r['request'] for r in store.all('llm').values() if r['state']=='pending'][0]
    def reply(request,result):
        fields = ('protocol_version','transport','request_id','request_hash','model')
        output = tmp_path/'mailbox'/'responses'/(request['request_id']+'.json')
        temp = output.with_suffix('.tmp')
        temp.write_text(json.dumps({**{k:request[k] for k in fields},'result':result}))
        os.replace(temp,output)
    current,llm = attach()
    with pytest.raises(AssistantBridgeTimeout):
        call(current,llm)
    first = latest(current.store)
    current,llm = attach()
    with pytest.raises(AssistantBridgeTimeout):
        call(current,llm)
    assert latest(current.store)['request_id'] == first['request_id']
    assert current.store.usage()['llm_calls']['committed'] == 1
    reply(first,{'_context_action':{'kind':'query','filters':{'variants':['AAAA']}}})
    current,llm = attach()
    with pytest.raises(AssistantBridgeTimeout):
        call(current,llm)
    second = latest(current.store)
    assert second['request_id'] != first['request_id']
    assert second['context']['_context']['actions_used'] == 1
    assert second['context']['_context']['last_read']['action']['kind'] == 'query'
    assert current.store.usage()['llm_calls']['committed'] == 2
    reply(second,{'ok':True})
    current,llm = attach()
    assert call(current,llm) == {'ok':True}
    current,llm = attach()
    assert call(current,llm) == {'ok':True}
    assert current.store.usage()['llm_calls']['committed'] == 2
    assert current.store.usage()['experimental_wells']['committed'] == 0
    # Cached presentation never replenishes live controller budgets.
    with pytest.raises(BudgetExceeded):
        current.store.reserve('fixture-over-limit','experimental_wells',100000,digest('fixture'))


def test_snapshot_is_stable_across_python_processes(tmp_path):
    import subprocess
    import sys
    c = campaign(tmp_path)
    program = '''import sys
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.contracts import digest
c=Campaign(Store(sys.argv[1]))
print(digest(c._decision_view(c.state,"prepare")))
'''
    def child(seed):
        env = {**os.environ,'PYTHONHASHSEED':seed}
        return subprocess.check_output([sys.executable,'-c',program,str(c.store.root)],env=env,text=True).strip()
    first = child('1')
    c.store.event('llm_started',{'key':'fixture'})
    assert child('2') == first


@pytest.mark.core
@pytest.mark.parametrize('guarded',[False,True])
def test_real_meta_bridge_pause_preserves_opportunity_and_resumes_once(tmp_path, monkeypatch, guarded):
    from proteinrsi.agents import FeedbackAnalysis, MetaAgent
    from proteinrsi.llm import ProviderPaused
    if guarded:
        from proteinrsi.replay.sandbox import probe
        monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND','bwrap')
        security = probe()
        if not security['available']:
            if os.environ.get('PROTEINRSI_REQUIRE_BWRAP') == '1':
                pytest.fail(str(security))
            pytest.skip('Actual bwrap required for guarded M continuation')
    c = campaign(tmp_path, improvement_contract='capability-v1',method_policy='direct',auto_evaluation=None)
    state = c.state
    state['round_index'] = 1
    state['history'] = [{'round':0,'analyst_feedback':FeedbackAnalysis(summary='Synthetic fixture').model_dump(mode='json')}]
    state['observations'] = [Observation(sample_id='fixture-'+str(i),batch_id='synthetic',sequence=s,
        value=float(i),metric=state['task']['metric'],unit=state['task']['unit'],source='synthetic',
        assay_protocol=state['task']['assay_protocol']).model_dump(mode='json')
        for i,s in enumerate(['AAAA','CAAA','DAAA','EAAA','FAAA','GAAA'])]
    c.store.put('campaign','state',state)
    def attach():
        store = Store(c.store.root)
        llm = AssistantBridgeLLM(store,model='transport-fixture',mailbox=tmp_path/'mailbox',
            timeout=0.01,poll_interval=0.001,max_request_bytes=1048576)
        team = Team(store,llm)
        if guarded:
            from proteinrsi.replay.broker import GuardedTeam, GuardedMetaAgent
            team = GuardedTeam.from_team(team)
            return Campaign(store,team,GuardedMetaAgent(team))
        return Campaign(store,team,MetaAgent(llm,store,team.tools))
    requests = []
    for _ in range(2):
        current = attach()
        with pytest.raises(ProviderPaused):
            current._optional_improvement()
        assert current.state['considered_round'] != 1
        assert not current.methods.paused(current.state)
        pending = [r['request'] for r in current.store.all('llm').values() if r['state']=='pending']
        assert len(pending) == 1 and pending[0]['role'] == 'M'
        requests.append(pending[0])
        assert current.store.usage()['llm_calls']['committed'] == 1
    assert requests[0] == requests[1]
    request = requests[0]
    fields = ('protocol_version','transport','request_id','request_hash','model')
    output = tmp_path/'mailbox'/'responses'/(request['request_id']+'.json')
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps({**{k:request[k] for k in fields},
        'result':{'reason':'Synthetic no-change transport fixture','patch':None}}))
    os.replace(temporary,output)
    current = attach()
    current._optional_improvement()
    assert current.state['considered_round'] == 1
    assert not current.methods.paused(current.state)
    assert current.state['pending_patch'] is None and current.state['pending_meta'] is None
    again = attach()
    again._optional_improvement()
    assert again.store.usage()['llm_calls']['committed'] == 1
    assert again.store.usage()['experimental_wells']['committed'] == 0
    assert sum(e['kind']=='meta_decision' for e in again.store.events()) == 1
    assert not any(e['kind']=='method_improvement_paused' for e in again.store.events())


def test_legacy_meta_provider_pause_keeps_historical_behavior(tmp_path, monkeypatch):
    from proteinrsi.llm import ProviderPaused
    c = campaign(tmp_path, improvement_contract='capability-v1')
    with c.store.connect() as con:
        con.execute("DELETE FROM kv WHERE namespace='configuration' AND key='decision_entry_semantics'")
    def pause():
        raise ProviderPaused('Synthetic historical provider pause')
    monkeypatch.setattr(c,'consider_improvement',pause)
    c._optional_improvement()
    assert c.methods.paused(c.state)
    assert c.state['method_governance']['pause_reason'] == 'improver_provider_paused'


@pytest.mark.core
def test_actual_guarded_typed_replay_restarts_resource_selection_and_context_read(tmp_path, monkeypatch):
    """Production guarded/controller path, synthetic parent only, stops before designs."""
    import subprocess
    import sys
    from proteinrsi.contracts import MetaPolicy
    from proteinrsi.replay.sandbox import probe
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND','bwrap')
    state = probe()
    if not state['available']:
        if os.environ.get('PROTEINRSI_REQUIRE_BWRAP') == '1':
            pytest.fail(str(state))
        pytest.skip('Actual bwrap required for guarded resume integration')
    task = TaskSpec(name='Synthetic guarded transport fixture',reference_sequence='AAAA',mutable_positions=[1],
        feedback_source='synthetic',candidate_access='open',initial_observation_policy='provided_parent',
        initial_parent_measurement={'value':0.0,'source_ref':'synthetic transport fixture'},
        controls_per_batch=0,max_rounds=1,batch_size=2,batch_fill_policy='full_plate',
        budget={'experimental_wells':2,'llm_calls':12,'tool_calls':0})
    c = Campaign.initialize(str(tmp_path/'campaign'),task,
        workflow=Workflow(skill_names=['protein-metrics','protein-design-workflows'],
            tool_names=['research_metric_extract','research_fit_predict']),
        meta=MetaPolicy(enabled=False),auto_meta_evaluation=False,
        research_config=ResearchConfig(context_policy='state-v1',protocol_mode='typed',resource_selection='llm'))
    dataset = tmp_path/'synthetic-controller.csv'
    dataset.write_text('sequence,value,qc\nAAAA,0.0,valid\n')
    program = '''import sys
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.assistant_bridge import AssistantBridgeLLM
from proteinrsi.agents import Team,MetaAgent
from proteinrsi.replay.controller import run_replay
from proteinrsi.llm import ProviderPaused
store=Store(sys.argv[1])
llm=AssistantBridgeLLM(store,model="guarded-transport-fixture",mailbox=sys.argv[2],timeout=.01,poll_interval=.001,max_request_bytes=1048576)
team=Team(store,llm)
c=Campaign(store,team,MetaAgent(llm,store,team.tools))
try:
 run_replay(c,sys.argv[3],guarded=True)
except ProviderPaused:
 print("PAUSED")
else:
 raise AssertionError("Fixture must stop before any experimental query")
'''
    mailbox = tmp_path/'mailbox'
    def advance():
        output = subprocess.check_output([sys.executable,'-c',program,str(c.store.root),str(mailbox),str(dataset)],
            env=os.environ,text=True,timeout=30)
        assert output.strip() == 'PAUSED'
        store = Store(c.store.root)
        assert store.usage()['experimental_wells']['committed'] == 0
        records = [r['request'] for r in store.all('llm').values() if r['state']=='pending']
        assert len(records) == 1
        return store,records[0]
    def reply(request,result):
        fields = ('protocol_version','transport','request_id','request_hash','model')
        output = mailbox/'responses'/(request['request_id']+'.json')
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps({**{k:request[k] for k in fields},'result':result}))
        os.replace(temporary,output)
    store,request = advance()
    configuration = store.all('configuration')
    for _ in range(3):
        # Knowledge choice and Biomni-style resource-index retrieval are separate
        # real operations. Empty synthetic choices select no scientific methods.
        if request['role'] != 'A-resources':
            break
        alternatives = request['schema'].get('anyOf',[request['schema']])
        props = next(s['properties'] for s in alternatives if 'properties' in s and
            ('template_ids' in s['properties'] or 'tools' in s['properties']))
        result = ({'template_ids':[],'extra_metric_ids':[],'rationale':'Synthetic transport fixture'}
                  if 'template_ids' in props else
                  {'tools':[],'data_lake':[],'libraries':[],'know_how':[],'experience':[]})
        reply(request,result)
        store,request = advance()
        assert store.all('configuration') == configuration
    assert request['role'] == 'A-plan'
    initial_id = request['request_id']
    used = store.usage()['llm_calls']['committed']
    store,same = advance()
    assert same['request_id'] == initial_id
    assert store.usage()['llm_calls']['committed'] == used
    reply(same,{'_context_action':{'kind':'query','filters':{'variants':['AAAA']}}})
    store,following = advance()
    assert following['role'] == 'A-plan'
    assert following['context']['_context']['actions_used'] == 1
    assert following['context']['_context']['last_read']['action']['kind'] == 'query'
    assert store.all('configuration') == configuration
    assert store.usage()['llm_calls']['committed'] == used+1
    store,same_read = advance()
    assert same_read['request_id'] == following['request_id']
    assert store.usage()['llm_calls']['committed'] == used+1
    assert len(store.all('decision_entries')) == 1
    assert not store.all('batches')
