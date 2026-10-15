"""Scripted mechanism tests; no biological or live-model capability claims."""
from copy import copy, deepcopy
import sqlite3

import pytest

from proteinrsi.agents import MetaAgent, MetaResponse, Team
from proteinrsi.contracts import (BudgetSpec, Candidate, EvaluationSpec, GatePolicy,
    MetaPolicy, Observation, Patch, TaskSpec, Workflow, digest)
from proteinrsi.evaluation import MetaCase
from proteinrsi.evaluation_scheduler import schedule_evaluation
from proteinrsi.llm import ProviderPaused
from proteinrsi.loop_evaluation import evaluate_method
from proteinrsi.memory import CapabilityLibrary, sync_shared_memory
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.runtime import Campaign
from proteinrsi.storage import BudgetExceeded, Conflict, SponsoredStore, Store

@pytest.fixture(autouse=True)
def scripted_metrics(monkeypatch):
    # Only the metric worker is scripted. E requests, frozen plans, verdicts,
    # provenance validation and budget accounting use the real implementation.
    ScriptedTeam.pause = False
    from test_custom_metric_integration import _synthetic_output, CODE
    def worker(store, view, args, *, pure=False):
        assert pure and view is None and args['code'] == CODE
        return {'status': 'ok', 'execution_backend': 'landlock_seccomp_generated_v1',
                'code_sha256': digest(CODE), 'output': _synthetic_output(args['inputs'])}
    monkeypatch.setattr('proteinrsi.evaluation_metrics.execute_code', worker)
    monkeypatch.setattr('proteinrsi.replay.sandbox.probe', lambda: {'available': True})


SEQUENCES = sorted(x + 'AAA' for x in 'CDEFGHIKLMNPQRSTVWY')
LABELS = {s: float(i) for i, s in enumerate(SEQUENCES)}


class ScriptedTeam(Team):
    pause = False
    fail_if_meta_mutated = True

    def run(self, view):
        assert not any(k.startswith('validation_') for k in view.research_context)
        binding = self.store.get('configuration', 'evaluation_branch')
        if binding:
            assert view.meta.version == binding['meta']
        if (type(self).pause and binding and binding.get('target') == 'workflow'
                and view.workflow.strategy == 'pairwise'):
            type(self).pause = False
            raise ProviderPaused('synthetic nested context stall', reason='context_stalled')
        key = 'scripted-tool-' + digest({'evidence': view.evidence_version, 'w': view.workflow.version})
        self.store.reserve(key, 'tool_calls', 1, {'operation': 'deterministic-fixture'})
        self.store.settle(key)
        self.store.event('task_harness_invoked', {'workflow': view.workflow.version,
            'meta': view.meta.version, 'evidence_version': view.evidence_version})
        seen = {o.sequence for o in view.observations}
        choices = sorted(set(view.task.candidates) - seen,
                         reverse=view.workflow.strategy == 'pairwise')
        return [Candidate(sequence=s) for s in choices]


def scripted_improver(self, view, last_patch_round):
    binding = self.store.get('configuration', 'evaluation_branch')
    if binding:
        assert binding['target'] == 'meta', 'Fixed-W trial invoked Improver'
    desired = 'pairwise' if view.meta.mode == 'diagnostic' else 'diverse'
    if view.workflow.strategy == desired:
        return MetaResponse(reason='Fixture already applied')
    patch = Patch(target='workflow', base_version=view.workflow.version,
        task_kind=view.task.kind, changes={'strategy': desired},
        hypothesis='Scripted fixture changes ordering, not a scientific discovery')
    return MetaResponse(reason='Synthetic method proposal', patch=patch)


def campaign(tmp_path, *, library=None, gate=None, protocol='method-loop-v1', name='root', governance=None,
             budget_scope='shared_campaign', context_policy='legacy', method_policy='validate', small=False):
    independent = budget_scope == 'independent'
    task = TaskSpec(name='mechanism', reference_sequence='AAAA', mutable_positions=[1],
        max_mutations=1, candidates=SEQUENCES, controls_per_batch=0, batch_size=2,
        feedback_source='measured_replay', max_rounds=4,
        budget=BudgetSpec(experimental_wells=128, llm_calls=None if independent else 512,
                          tool_calls=None if independent else 512))
    c = Campaign.initialize(str(tmp_path / name), task, gate=gate or GatePolicy(criterion='llm_adjudicated_v1'),
        memory_library=library, governance_config=governance,
        research_config=ResearchConfig(enabled=False, workflow_validation='independent',
            context_policy=context_policy, method_policy=method_policy,
            evaluation_execution='blocking' if context_policy == 'state-v1' else 'background',
            evaluation_protocol=protocol, auto_evaluation={'rounds': 1, 'budget_scope': budget_scope,
                'llm_calls_per_arm': None if independent else 8,
                'tool_calls_per_arm': None if independent else 8, 'main_reserve_wells': 2,
                'meta_rounds': 3, 'meta_query_budget_per_arm': 10,
                'meta_llm_calls_per_arm': None if independent else 64,
                'meta_tool_calls_per_arm': None if independent else 64,
                **({'rounds': 1, 'batch_size': 2, 'meta_rounds': 2, 'meta_query_budget_per_arm': 8} if small else {})}))
    from test_llm_evaluation_integration import EvaluationHTTP
    from test_custom_metric_integration import custom_plan
    c.transport = EvaluationHTTP(c.store, plan=custom_plan())
    c.team = ScriptedTeam(c.store, c.transport.team(c.store).llm)
    c.team.analyst.llm = None
    c.meta_agent = MetaAgent(None, c.store, c.team.tools)
    return c


def fork_team(c, store):
    llm = copy(c.team.llm)
    llm.store = store
    team = ScriptedTeam(store, llm)
    team.analyst.llm = None
    return team


def case(c, *, split='validation', initial_index=8, rounds=3, query_budget=10):
    t = c.view().task.model_copy(deep=True)
    t.max_rounds = rounds
    compute = None if t.budget.llm_calls is None else 64
    t.budget = BudgetSpec(experimental_wells=query_budget, llm_calls=compute, tool_calls=compute)
    seq = SEQUENCES[initial_index]
    initial = [Observation(sample_id='initial', batch_id='initial', sequence=seq,
        value=LABELS[seq], metric=t.metric, unit=t.unit, assay_protocol=t.assay_protocol,
        source=t.feedback_source)]
    return MetaCase(case_id='test' if split == 'test' else 'validation', group_id='one-start',
        split=split, task=t, initial=initial, labels=LABELS, query_budget=query_budget)


def stage(c, target='meta'):
    current = c.view().meta if target == 'meta' else c.view().workflow
    p = Patch(target=target, base_version=current.version, task_kind=c.view().task.kind,
        changes={'mode': 'diagnostic'} if target == 'meta' else {'strategy': 'pairwise'},
        hypothesis='Synthetic controlled candidate')
    c.stage_patch(p)
    return p


@pytest.mark.core
@pytest.mark.parametrize('policy', ['direct', 'validate'])
def test_publication_policy_is_explicit_and_rolls_back(tmp_path, policy):
    from proteinrsi.evaluation_scheduler import EvaluationScheduler
    c = campaign(tmp_path, budget_scope='independent', context_policy='state-v1', method_policy=policy, small=True)
    p = stage(c, 'workflow')
    base = c.view().workflow.version
    before = deepcopy(c.store.usage())
    with pytest.raises(Conflict, match='awaiting publication'):
        c.prepare()  # Bypassing the boundary checkpoint cannot run a stale method.
    scheduler = EvaluationScheduler(c, None, cases=[case(c, rounds=1, query_budget=2)],
        team_factory=lambda store: fork_team(c, store))
    try:
        scheduler.checkpoint(c)
    finally:
        scheduler.close()
    assert c.view().workflow.version != base and c.state['round_index'] == 0
    candidate = c.store.get('method_candidate_states', p.patch_id)
    assert candidate['status'] == ('adopted_unvalidated' if policy == 'direct' else 'accepted')
    if policy == 'direct':
        assert not c.store.all('evaluation_verdicts') and c.store.usage() == before
        assert not c.memory.retrieve(c.view().task.kind)
    else:
        assert c.store.all('evaluation_verdicts') and c.store.usage() == before
        report = next(iter(c.store.all('harness_evaluations').values()))
        assert all(trace['completed_new_rounds'] == 1 for trace in report['traces'][0].values())
    c.methods.rollback('workflow', base, operator='fixture', reason='Policy rollback preserves evidence')
    assert c.view().workflow.version == base and c.store.usage() == before


@pytest.mark.core
def test_small_meta_cycle_clones_state_and_feedback_resume_is_idempotent(tmp_path, monkeypatch):
    from proteinrsi.agents import FeedbackAnalysis
    from proteinrsi.loop_evaluation import CaseFeedback
    from proteinrsi.research.state import checkpoint_state
    c = campaign(tmp_path, budget_scope='independent', context_policy='state-v1', small=True)
    with c.store.transaction():
        checkpoint_state(c.store, c.state, memory={'focus': 'Frozen initial hypothesis'}, source_ref='fixture')
    snapshot = deepcopy(c.view().research_context['research_state'])
    calls = []
    def improver(agent, view, last_patch_round):
        calls.append((view.meta.version, view.round_index))
        return scripted_improver(agent, view, last_patch_round)
    monkeypatch.setattr(MetaAgent, 'propose', improver)
    stage(c)
    starts = []
    def factory(store):
        team = fork_team(c, store)
        branch = store.get('configuration', 'evaluation_branch')
        if branch['target'] == 'meta':
            initial = store.get('research_states', store.get('research_state', 'active')['ref'])
            assert initial == snapshot
            starts.append(branch['arm'])
        def feedback(view):
            if branch['target'] != 'meta':
                return FeedbackAnalysis(summary='Nested fixture feedback')
            focus = branch['arm'] + ' private hypothesis'
            assert not view.research_context['research_state']['working_memory']['focus'].startswith(
                ('challenger' if branch['arm'] == 'baseline' else 'baseline') + ' private')
            return FeedbackAnalysis(summary='Fixture feedback', working_memory={'focus': focus})
        team.analyst.feedback = feedback
        return team
    oracle = CaseFeedback(case(c, rounds=2, query_budget=8))
    report = schedule_evaluation(c, oracle, team_factory=factory)
    assert sorted(starts) == ['baseline', 'challenger']
    assert len(calls) == 2  # Both fixed-M arms actually invoke their M between Task rounds.
    assert all(t['completed_new_rounds'] == 2 and t['meta_decisions'] for t in report['traces'][0].values())
    assert c.view().research_context['research_state']['working_memory']['focus'] == 'Frozen initial hypothesis'
    # A crash after committed feedback cannot create another state version or charge.
    c.meta_agent.propose = lambda *args: MetaResponse(reason='Fixture abstention')
    batch = c.prepare()
    c.approve(batch.batch_id, operator='fixture')
    c.ingest(oracle.measure(batch))
    c.analyze_feedback()
    before = (deepcopy(c.store.usage()), c.store.all('research_states'), c.store.get('research_state', 'active'))
    Campaign(c.store, team=c.team).analyze_feedback()
    assert before == (c.store.usage(), c.store.all('research_states'), c.store.get('research_state', 'active'))


@pytest.mark.parametrize('target,allowed', [(None, ('workflow', 'meta')),
    ('workflow', ()), ('meta', ('workflow',))])
def test_authority_matrix(tmp_path, target, allowed):
    c = campaign(tmp_path)
    if target:
        c.store.put('configuration', 'evaluation_branch', {'target': target,
            'workflow': c.view().workflow.version, 'meta': c.view().meta.version}, immutable=True)
    assert c.methods.allowed_targets == allowed
    for t in ('workflow', 'meta'):
        if t in allowed:
            c.methods.require_target(t)
        else:
            with pytest.raises(PermissionError):
                c.methods.require_target(t)
    assert c.methods.can_improve == bool(allowed)


@pytest.mark.parametrize('targets', [['workflow', 'meta'], ['meta']])
@pytest.mark.core
def test_meta_comparison_keeps_operator_limits_and_permissions(tmp_path, monkeypatch, targets):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path, governance={'allowed_patch_targets': targets,
        'max_changed_fields': 1, 'max_consecutive_failures': 1, 'max_deferred_rounds': 1})
    stage(c)
    inspected = []

    def factory(store):
        team = fork_team(c, store)
        child = Campaign(store, team=team)
        binding = store.get('configuration', 'evaluation_branch')
        policy = child.methods.policy
        assert policy.max_changed_fields == policy.max_consecutive_failures == policy.max_deferred_rounds == 1
        expected = ('workflow',) if binding['target'] == 'meta' and 'workflow' in targets else ()
        assert child.methods.allowed_targets == expected
        patch = Patch(target='workflow', base_version=child.view().workflow.version,
            task_kind=child.view().task.kind, changes={'strategy': 'pairwise', 'exploration': .5},
            hypothesis='Deliberately exceeds the frozen one-field limit')
        error = ValueError if expected else PermissionError
        with pytest.raises(error):
            child.stage_patch(patch)
        inspected.append(binding['target'])
        return team

    report = evaluate_method(c, [case(c)], target='meta', promote=True, team_factory=factory)
    assert inspected.count('meta') == 2
    if 'workflow' in targets:
        assert 'workflow' in inspected
    else:
        assert not any(t['method_switches'] for t in report['traces'][0].values())
    resources = c.store.get('evaluation_resources', c.store.get('evaluation_specs', report['spec_ref'])['resource_snapshot_ref'])
    assert resources['governance'] == c.methods.policy.model_dump(mode='json')


@pytest.mark.core
@pytest.mark.parametrize('budget_scope', ['shared_campaign', 'independent'])
def test_meta_comparison_nested_w_publication_stays_local(tmp_path, monkeypatch, budget_scope):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path, budget_scope=budget_scope)
    p = stage(c)
    initial_w, initial_m = c.view().workflow.version, c.view().meta.version
    report = evaluate_method(c, [case(c)], target='meta', promote=True, team_factory=lambda s: fork_team(c, s))
    assert report['promoted']
    assert c.view().meta.version != initial_m
    assert c.view().workflow.version == initial_w
    base, child = report['traces'][0]['baseline'], report['traces'][0]['challenger']
    assert base['meta'] == initial_m and child['meta'] == c.view().meta.version
    assert child['descendant'] != initial_w
    assert child['method_switches'] and all(s['target'] == 'workflow' for s in child['method_switches'])
    assert all(t['completed_new_rounds'] == 3 for t in (base, child))
    assert all(t['nested_evaluations'] for t in (base, child))
    assert all(h.get('analyst_feedback') for t in (base, child) for h in t['history'])
    assert c.store.usage()['experimental_wells']['committed'] == (0 if budget_scope == 'independent' else 20)
    assert report['budget_after']['experimental_wells']['committed'] == 20
    assert sum(t['usage']['experimental_wells']['committed'] for t in (base, child)) == 20
    assert sum(t['total_submitted_wells'] for t in (base, child)) == 12
    assert report['replay_experiment_cost']['amount'] == 0
    assert report['budget_after']['tool_calls']['committed'] >= 10
    if budget_scope == 'independent':
        assert report['main_budget_before'] == report['main_budget_after']
        assert report['budget_after']['llm_calls']['limit'] is None
        assert report['budget_after']['tool_calls']['limit'] is None
        assert all(t['usage']['experimental_wells']['committed'] <= t['query_cap'] for t in (base, child))
    assert len(c.transport.requests['verdict']) == 3
    assert not any(v.get('allocated_unspent') for v in c.store.usage().values())
    assert not c.state['observations']
    spec = c.store.get('evaluation_specs', report['spec_ref'])
    assert spec['target'] == 'meta' and spec['initial_workflow_ref']
    assert not c.state['pending_meta']
    assert c.store.all('validation_harness_evaluations')
    assert all(row['target'] == 'meta' for row in c.methods.history()['versions'])
    before = deepcopy(c.store.usage())
    assert evaluate_method(c, [case(c)], target='meta', promote=True, team_factory=lambda s: fork_team(c, s)) == report
    assert c.store.usage() == before
    batch = c.prepare()
    assert batch.meta_version == report['challenger']
    c.cancel_prepared(batch.batch_id, operator='fixture')
    c.methods.rollback('meta', initial_m, operator='fixture', reason='pointer-only test')
    batch = c.prepare()
    assert batch.meta_version == initial_m
    assert c.view().workflow.version == initial_w
    assert c.store.get('patches', p.patch_id)


@pytest.mark.core
def test_nested_context_pause_resumes_without_double_spend(tmp_path, monkeypatch):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path, budget_scope='independent')
    stage(c)
    data = [case(c)]
    ScriptedTeam.pause = True
    with pytest.raises(ProviderPaused, match='synthetic nested context stall') as paused:
        evaluate_method(c, data, target='meta', promote=True, team_factory=lambda s: fork_team(c, s))
    assert paused.value.reason == 'context_stalled'
    assert any(a.get('pause_reason') == 'context_stalled'
               for a in c.store.all('harness_attempts').values())
    assert c.state['pending_meta']
    from proteinrsi.evaluation_fork import evaluation_budget_store
    eid = next(iter(c.store.all('harness_attempts')))
    spec = EvaluationSpec.model_validate(c.store.get('evaluation_drafts', eid)['spec'])
    ledger = evaluation_budget_store(c.store, eid, spec)
    spent = ledger.usage()['experimental_wells']['committed']
    assert spent > 0
    assert c.store.usage()['experimental_wells']['committed'] == 0
    assert any(a['state'] == 'paused_provider' for a in c.store.all('harness_attempts').values())
    report = evaluate_method(c, data, target='meta', promote=True, team_factory=lambda s: fork_team(c, s))
    assert report['promoted'] and ledger.usage()['experimental_wells']['committed'] == 20
    assert c.store.usage()['experimental_wells']['committed'] == 0


def test_forbidden_self_proposal_is_not_adopted_in_m_arm(tmp_path, monkeypatch):
    def illegal(self, view, last):
        return MetaResponse(reason='Deliberately invalid fixture', patch=Patch(
            target='meta', base_version=view.meta.version, changes={'prompt': 'Forbidden'},
            task_kind=view.task.kind, hypothesis='Invalid self modification'))
    monkeypatch.setattr(MetaAgent, 'propose', illegal)
    c = campaign(tmp_path)
    c.transport.decision = 'inconclusive'
    stage(c)
    report = evaluate_method(c, [case(c)], target='meta', promote=True, team_factory=lambda s: fork_team(c, s))
    assert not report['promoted']
    assert all(not t['method_switches'] for t in report['traces'][0].values())


def test_scheduler_handles_m_and_keeps_root_on_prestart_shortage(tmp_path, monkeypatch):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path)
    stage(c)
    class Oracle:
        _labels = {s: (v, 'valid') for s, v in LABELS.items()}
    result = schedule_evaluation(c, Oracle(), team_factory=lambda s: fork_team(c, s))
    assert result['target'] == 'meta' and result['scope'] == 'same_snapshot'
    assert result['promoted']
    other = campaign(tmp_path, name='short')
    stage(other)
    other.store.reserve('used', 'experimental_wells', 124, {'fixture': 1})
    other.store.settle('used')
    assert schedule_evaluation(other, Oracle(), team_factory=lambda s: fork_team(other, s)) is None
    assert other.state['pending_meta'] and not other.store.all('harness_attempts')
    assert other.prepare().meta_version == other.view().meta.version


def test_nested_allocations_are_idempotent_at_every_ancestor(tmp_path):
    root = Store(tmp_path/'root')
    caps = {'experimental_wells': 20, 'llm_calls': 8, 'tool_calls': 8}
    root.configure_budget(caps)
    root.reserve_allocations('outer', {'m': caps})
    middle = SponsoredStore(tmp_path/'middle', root, 'm', allocation_id='m')
    middle.configure_budget(caps)
    child_caps = {'experimental_wells': 4, 'llm_calls': 2, 'tool_calls': 2}
    middle.reserve_allocations('inner', {'w': child_caps}, keep_free={'experimental_wells': 2})
    leaf = SponsoredStore(tmp_path/'leaf', middle, 'w', allocation_id='w')
    leaf.configure_budget(child_caps)
    leaf.reserve('one', 'experimental_wells', 2, {'fixture': 1})
    with pytest.raises(Conflict):
        root.release_allocations('outer')
    leaf.settle('one')
    for _ in range(2):
        leaf.reserve('one', 'experimental_wells', 2, {'fixture': 1})
        leaf.settle('one')
    for store in (root, middle, leaf):
        assert store.usage()['experimental_wells']['committed'] == 2
    with pytest.raises(BudgetExceeded):
        leaf.reserve('too-big', 'experimental_wells', 4, {'fixture': 2})
    assert root.usage()['experimental_wells']['committed'] == 2
    middle.release_allocations('inner')
    root.release_allocations('outer')
    assert root.remaining('experimental_wells') == 18


@pytest.mark.core
def test_capability_library_is_opt_in_frozen_and_controller_derived(tmp_path, monkeypatch):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    library = tmp_path/'memory'
    c = campaign(tmp_path, library=library)
    assert not c.memory.freeze_records()
    p = stage(c, 'workflow')
    sync_shared_memory(c)  # pending hypotheses are not given supported status
    records = CapabilityLibrary(library).retrieve(c.view().task.kind)
    assert len(records) == 1 and next(iter(records.values()))['evidence_status'] == 'unverified'
    evaluate_method(c, [case(c, rounds=1, query_budget=2)], target='workflow',
                    promote=True, team_factory=lambda s: fork_team(c, s))
    records = CapabilityLibrary(library).retrieve(c.view().task.kind)
    assert next(iter(records.values()))['status'] == 'local_support'
    assert p.patch_id in c.memory.freeze_records()
    assert not any(k.startswith('capability-') for k in c.memory.freeze_records())
    new = campaign(tmp_path, library=library, name='new')
    frozen = new.memory.freeze_records()
    assert len(frozen) == 1 and next(iter(frozen.values()))['scope']['campaign_id'] == c.state['campaign_id']
    exported = next(iter(records.values()))
    assert 'observed_rows' not in str(exported) and 'labels' not in exported
    again = campaign(tmp_path, library=library, name='again')
    stage(again)
    sync_shared_memory(again)
    assert new.memory.freeze_records() == frozen
    snapshot = new.store.get('configuration', 'capability_memory_snapshot')['ref']
    new.store.put('capability_memory_snapshots', snapshot, {'records': {}})
    with pytest.raises(Conflict):
        new.memory.freeze_records()


def test_shared_library_rejects_forged_support_and_branches(tmp_path):
    c = campaign(tmp_path)
    p = stage(c)
    library = CapabilityLibrary(tmp_path/'memory')
    state = c.store.get('method_candidate_states', p.patch_id)
    c.store.put('method_candidate_states', p.patch_id, {**state, 'status': 'accepted'})
    with pytest.raises(Conflict):
        library.record(c, p.patch_id)
    c.store.put('configuration', 'evaluation_branch', {'target': 'meta',
        'workflow': c.view().workflow.version, 'meta': c.view().meta.version})
    with pytest.raises(PermissionError):
        library.record(c, p.patch_id)


def test_export_definition_is_init_compatible_and_cannot_export_staged(tmp_path):
    c = campaign(tmp_path)
    definition = c.methods.export('workflow')
    w = Workflow.model_validate(definition['definition'])
    assert w.version == c.view().workflow.version
    exported_m = MetaPolicy.model_validate(c.methods.export('meta')['definition'])
    assert exported_m.version == c.view().meta.version
    p = stage(c)
    child_ref = c.store.get('method_candidates', p.patch_id)['candidate_snapshot_ref']
    child_version = c.store.get('method_snapshots', child_ref)['version']
    with pytest.raises(Conflict):
        c.methods.export('meta', child_version)
    assert c.methods.history()['versions'][0]['status'] == 'staged'
    assert definition['provenance']['activation']['origin'] == 'initial'


def test_legacy_spec_identity_and_meta_protocol_boundary(tmp_path):
    c = campaign(tmp_path, protocol='task-loop-v1')
    stage(c)
    with pytest.raises(ValueError, match='new method-loop'):
        evaluate_method(c, [case(c)], target='meta', team_factory=lambda s: fork_team(c, s))
    spec = EvaluationSpec(purpose='selection', scope='same_snapshot', patch_id='p',
        baseline_version='w0', candidate_version='w1', manifest_hash='x',
        resource_snapshot_ref='r', memory_snapshot_ref='m',
        cases=[{'case_id':'c', 'group_id':'g', 'start_ref':'s', 'rounds':1,
                'resource_limits':{'experimental_wells':2, 'llm_calls':2, 'tool_calls':2}}],
        gate={}, evaluator_limits={}, main_reserve_wells=0)
    old = spec.model_dump(mode='json')
    assert 'target' not in old and 'initial_workflow_ref' not in old
    assert EvaluationSpec.model_validate(old).spec_ref == spec.spec_ref


def test_confirmation_does_not_publish_or_enter_shared_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path, library=tmp_path/'memory')
    p = stage(c)
    selected = evaluate_method(c, [case(c)], target='meta', promote=True,
                               team_factory=lambda s: fork_team(c, s))
    library = CapabilityLibrary(tmp_path/'memory')
    before = library.store.all('capability_records')
    seen = {row['sequence'] for t in selected['traces'][0].values() for row in t['observed_rows']}
    seen |= {s for t in selected['traces'][0].values() for s in t['validation_observed_sequences']}
    remaining = [s for s in SEQUENCES if s not in seen]
    assert len(remaining) >= 3
    data = case(c, split='test', initial_index=SEQUENCES.index(remaining[0]), rounds=1, query_budget=2)
    data.task.candidates = remaining
    data.labels = {s: LABELS[s] for s in remaining}
    w, m = c.view().workflow.version, c.view().meta.version
    report = evaluate_method(c, [data], target='meta', patch_id=p.patch_id,
                             team_factory=lambda s: fork_team(c, s))
    assert report['phase'] == 'confirmation' and not report['promoted']
    assert library.store.all('capability_records') == before
    assert (c.view().workflow.version, c.view().meta.version) == (w, m)
    with pytest.raises(ValueError, match='Confirmation'):
        evaluate_method(c, [data], target='meta', patch_id=p.patch_id, promote=True)


def test_export_cli_writes_definition_and_provenance_without_overwrite(tmp_path):
    from types import SimpleNamespace
    from proteinrsi.method_cli import run
    c = campaign(tmp_path)
    out = tmp_path/'method.json'
    args = SimpleNamespace(campaign=str(c.store.root), action='export', target='workflow',
                           version=None, out=str(out))
    result = run(args)
    import json
    assert Workflow.model_validate(json.loads(out.read_text())).version == result['version']
    assert out.with_name('method.json.provenance.json').exists()
    assert run(args) == result
    out.write_text('{}')
    with pytest.raises(FileExistsError):
        run(args)


@pytest.mark.core
def test_history_and_trace_distinguish_adopted_from_used(tmp_path):
    from proteinrsi.trace_export import snapshot, summary
    c = campaign(tmp_path)
    stage(c, 'workflow')
    evaluate_method(c, [case(c, rounds=1, query_budget=2)], target='workflow',
                    promote=True, team_factory=lambda s: fork_team(c, s))
    assert c.methods.history()['versions'][0]['used_after_publication'] is False
    with snapshot(c.store.root) as (con, path):
        assert summary(con, path)['improvements'][0]['used_after_publication'] is False
    c.prepare()
    assert c.methods.history()['versions'][0]['used_after_publication'] is True
    with snapshot(c.store.root) as (con, path):
        assert summary(con, path)['improvements'][0]['used_after_publication'] is True
        assert summary(con, path)['replay_experiment_cost']['amount'] == 0


def test_cannot_expand_branch_authority_through_evaluation_api(tmp_path):
    c = campaign(tmp_path)
    c.store.put('configuration', 'evaluation_branch', {'target': 'workflow',
        'workflow': c.view().workflow.version, 'meta': c.view().meta.version})
    for target in ('workflow', 'meta'):
        with pytest.raises(PermissionError):
            evaluate_method(c, [case(c)], target=target)
        with pytest.raises(PermissionError):
            evaluate_method(c, [case(c)], target=target, patch_id='arbitrary-confirmation')


def test_settle_after_local_crash_is_idempotent_across_sponsors(tmp_path, monkeypatch):
    root = Store(tmp_path/'root')
    caps = {'experimental_wells': 8}
    root.configure_budget(caps)
    root.reserve_allocations('pair', {'outer': caps})
    parent = SponsoredStore(tmp_path/'parent', root, 'outer', allocation_id='outer')
    parent.configure_budget(caps)
    parent.reserve_allocations('inner', {'leaf': {'experimental_wells': 4}})
    leaf = SponsoredStore(tmp_path/'leaf', parent, 'leaf', allocation_id='leaf')
    leaf.configure_budget({'experimental_wells': 4})
    leaf.reserve('one', 'experimental_wells', 2, {'request': 1})
    settle = parent.settle
    def crash(key, *, release=False):
        settle(key, release=release)
        raise RuntimeError('crash after ancestor commits, before local receipt settles')
    monkeypatch.setattr(parent, 'settle', crash)
    with pytest.raises(RuntimeError):
        leaf.settle('one')
    monkeypatch.setattr(parent, 'settle', settle)
    leaf.reserve('one', 'experimental_wells', 2, {'request': 1})
    leaf.settle('one')
    assert all(s.usage()['experimental_wells']['committed'] == 2 for s in (root, parent, leaf))
    parent.release_allocations('inner')
    root.release_allocations('pair')
    assert root.remaining('experimental_wells') == 6


def test_empty_library_is_frozen_even_after_new_entries(tmp_path):
    path = tmp_path/'library'
    a = campaign(tmp_path, library=path, name='a')
    b = campaign(tmp_path, library=path, name='b')
    stage(a)
    sync_shared_memory(a)
    assert CapabilityLibrary(path).retrieve(a.view().task.kind)
    assert b.memory.freeze_records() == {}


def test_branch_memory_cannot_read_sibling_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(MetaAgent, 'propose', scripted_improver)
    c = campaign(tmp_path)
    stage(c)
    seen = []
    def factory(store):
        team = fork_team(c, store)
        original = team.run
        def run(view):
            binding = store.get('configuration', 'evaluation_branch')
            if binding and binding['target'] == 'meta':
                arm = binding['arm']
                other = 'challenger' if arm == 'baseline' else 'baseline'
                assert store.get('research_notes', other) is None
                assert all(other + '-private' not in str(row) for row in view.experience)
                store.put('research_notes', arm, {'text': arm + '-private'})
                seen.append(arm)
            return original(view)
        team.run = run
        return team
    evaluate_method(c, [case(c)], target='meta', promote=True, team_factory=factory)
    assert {'baseline', 'challenger'} <= set(seen)
    assert not c.store.all('research_notes')


def test_new_protocol_cannot_skip_independent_w_verification():
    with pytest.raises(ValueError, match="independent W validation"):
        ResearchConfig(evaluation_protocol="method-loop-v1", workflow_validation="online")


@pytest.mark.parametrize("error_type", [OSError, sqlite3.OperationalError])
def test_unavailable_optional_library_does_not_block_task(tmp_path, monkeypatch, error_type):
    c = campaign(tmp_path)
    c.store.put("controller_settings", "capability_library", {"path": str(tmp_path / "library")})
    def unavailable(*args, **kwargs):
        raise error_type("synthetic inaccessible library")
    monkeypatch.setattr("proteinrsi.memory.CapabilityLibrary", unavailable)
    sync_shared_memory(c)
    assert c.store.get("capability_export_errors", "library")["error_type"] == error_type.__name__
    assert c.state["status"] == "ready"


def test_trace_summary_is_bounded_without_dropping_original_evidence(tmp_path):
    from proteinrsi.trajectory import read_trace
    s = Store(tmp_path)
    for i in range(105):
        s.put("method_candidates", str(i), {"patch": {"target": "workflow", "hypothesis": "x" * 1000,
            "changes": {"strategy": "pairwise"}}}, immutable=True)
    trace = read_trace(tmp_path)
    assert len(trace["improvements"]) == 100
    assert trace["improvement_count"] == 105 and trace["improvements_truncated"]
    assert len(trace["records"]["method_candidates"]) == 105
    assert len(trace["improvements"][0]["why"]) == 500
