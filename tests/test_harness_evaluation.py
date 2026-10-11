"""Synthetic rankers test the comparison/controller, never protein efficacy."""
from copy import deepcopy

import pytest

from proteinrsi.contracts import BudgetSpec, Candidate, Patch
from proteinrsi.harness_evaluation import evaluate_workflow, abandon_evaluation
from proteinrsi.llm import ProviderPaused
from proteinrsi.storage import Conflict
from test_rsi import make_meta_cases, ScriptedOffspringTeam
from test_custom_metric_integration import synthetic_metric_worker as synthetic_metric_worker


def staged(campaign):
    patch=Patch(target="workflow", base_version=campaign.view().workflow.version,
        changes={"strategy":"pairwise"}, task_kind="variant_design", hypothesis="Synthetic fixture ranker modification")
    campaign.stage_patch(patch)
    return patch


def cases(campaign, *, split="validation", seed_offset=0):
    result=make_meta_cases(campaign,split)
    for i,c in enumerate(result):
        c.task=c.task.model_copy(update={"seed": c.task.seed+i+seed_offset,
            "budget": BudgetSpec(experimental_wells=8,llm_calls=1,tool_calls=1)})
        sequences = c.task.candidates
        indexes = (2*i, 2*i+1) if not seed_offset else (i+1, i+5)
        c.initial = [c.initial[j].model_copy(update={"sequence": sequences[k],
            "value": c.labels[sequences[k]]}) for j,k in enumerate(indexes)]
    return result


def test_fixed_harness_evaluation_never_calls_improver_and_reuses_cache(campaign, monkeypatch):
    p=staged(campaign)
    def no_meta(*a,**k):
        pytest.fail("H0/H1 comparison must not optimize either harness")
    monkeypatch.setattr('proteinrsi.agents.MetaAgent.propose', no_meta)
    data=cases(campaign)
    report=evaluate_workflow(campaign,data,promote=True,team_factory=ScriptedOffspringTeam)
    assert report['promoted'] and report['gate']['decision']=='accepted'
    assert campaign.view().workflow.version==report['challenger']
    assert campaign.state['pending_patch'] is None
    assert campaign.store.usage()['experimental_wells']['committed']==32
    assert not campaign.state['observations']  # Validation labels stay outside Task evidence.
    assert evaluate_workflow(campaign,data,promote=True,team_factory=ScriptedOffspringTeam)==report
    assert campaign.store.usage()['experimental_wells']['committed']==32
    # Confirmation is a distinct frozen episode and never changes active pointers.
    with pytest.raises(ValueError,match='cannot be reused'):
        evaluate_workflow(campaign,cases(campaign,split='test')[:2],patch_id=p.patch_id,team_factory=ScriptedOffspringTeam)
    result=evaluate_workflow(campaign,cases(campaign,split='test',seed_offset=100)[:2],patch_id=p.patch_id,
                             team_factory=ScriptedOffspringTeam)
    assert not result['promoted'] and result['phase']=='confirmation'
    assert len(campaign.store.all('method_switches'))==1
    assert campaign.store.usage()['experimental_wells']['committed']==48
    campaign.methods.rollback('workflow',report['baseline'],operator='test',reason='Synthetic rollback')
    assert campaign.view().workflow.version==report['baseline']
    assert campaign.store.usage()['experimental_wells']['committed']==48


def test_report_only_cannot_repurpose_into_promotion(campaign):
    staged(campaign)
    data=cases(campaign)
    result=evaluate_workflow(campaign,data,team_factory=ScriptedOffspringTeam)
    assert not result['promoted'] and campaign.state['pending_patch']
    with pytest.raises(Conflict,match='intent'):
        evaluate_workflow(campaign,data,promote=True,team_factory=ScriptedOffspringTeam)


def test_rejection_preserves_active_version(campaign):
    staged(campaign)
    class Reversed(ScriptedOffspringTeam):
        def run(self,v):
            seqs=list(v.task.candidates)
            if v.workflow.strategy=='additive':
                seqs.reverse()
            return [Candidate(sequence=s) for s in seqs]
    old=campaign.view().workflow.version
    r=evaluate_workflow(campaign,cases(campaign),promote=True,team_factory=Reversed)
    assert r['gate']['decision']=='rejected' and campaign.view().workflow.version==old
    assert campaign.state['pending_patch'] is None
    assert campaign.memory.retrieve(campaign.view().task.kind,include_unvalidated=True)[0]['evidence_status']=='not_supported'


def test_budget_and_initial_label_checks_happen_before_execution(campaign):
    staged(campaign)
    data=cases(campaign)
    original=campaign.store.usage()
    data[0].task.budget.llm_calls=None
    with pytest.raises(ValueError,match='finite'):
        evaluate_workflow(campaign,data,team_factory=ScriptedOffspringTeam)
    assert campaign.store.usage()==original
    data=cases(campaign)
    data[0].initial[0].value=999
    with pytest.raises(ValueError,match='measured'):
        evaluate_workflow(campaign,data,team_factory=ScriptedOffspringTeam)
    data=cases(campaign)
    data[0].task.budget.llm_calls=10000
    with pytest.raises(ValueError,match='caps exceed'):
        evaluate_workflow(campaign,data,team_factory=ScriptedOffspringTeam)
    assert campaign.store.usage()==original


def test_pause_resumes_exact_completed_arm_without_repeating_queries(campaign):
    staged(campaign)
    data=cases(campaign)
    seen=[]
    paused=[False]
    class Pausing(ScriptedOffspringTeam):
        def run(self,v):
            seen.append((v.task.seed,v.workflow.strategy))
            if v.workflow.strategy=='pairwise' and not paused[0]:
                paused[0]=True
                raise ProviderPaused('fixture')
            return super().run(v)
    with pytest.raises(ProviderPaused):
        evaluate_workflow(campaign,data,promote=True,team_factory=Pausing)
    with pytest.raises(Conflict):
        campaign.prepare()
    report=evaluate_workflow(campaign,data,promote=True,team_factory=Pausing)
    assert report['promoted'] and seen.count((data[0].task.seed,'additive'))==1
    assert campaign.store.usage()['experimental_wells']['committed']==32


def test_abandon_unknown_work_never_refunds_or_republishes(campaign):
    staged(campaign)
    class Broken(ScriptedOffspringTeam):
        def run(self,v):
            raise RuntimeError('fixture unknown completion')
    with pytest.raises(RuntimeError):
        evaluate_workflow(campaign,cases(campaign),promote=True,team_factory=Broken)
    key=next(iter(campaign.store.all('harness_attempts')))
    usage=deepcopy(campaign.store.usage())
    with pytest.raises(Conflict):
        abandon_evaluation(campaign,key,operator='test',reason='test')
    abandon_evaluation(campaign,key,operator='test',reason='reconciled',acknowledge_uncertain=True)
    assert campaign.state['pending_patch'] is None
    assert campaign.store.usage()==usage
    assert campaign.store.get('harness_attempts',key)['state']=='abandoned'


def test_case_context_never_contains_private_labels_or_other_arm_memory(campaign):
    staged(campaign)
    views=[]
    class Inspect(ScriptedOffspringTeam):
        def run(self,v):
            views.append(v.model_dump())
            assert not v.experience and not v.history
            assert set(self.store.all('experience'))==set()
            return super().run(v)
    evaluate_workflow(campaign,cases(campaign),team_factory=Inspect)
    assert len(views)==8
    assert all('labels' not in v and 'labels' not in v['research_context'] for v in views)
    assert all(views[i]['observations']==views[i+1]['observations'] for i in range(0,8,2))


def test_renaming_or_reseeding_selection_is_not_confirmation(campaign):
    patch = staged(campaign)
    data = cases(campaign)
    evaluate_workflow(campaign, data, promote=True, team_factory=ScriptedOffspringTeam)
    held = cases(campaign, split="test")[:2]
    for c in held:
        c.task.seed += 1000
        c.case_id += "-renamed"
        c.task.budget.llm_calls += 1
    with pytest.raises(ValueError, match="cannot be reused"):
        evaluate_workflow(campaign, held, patch_id=patch.patch_id, team_factory=ScriptedOffspringTeam)


def test_identical_starting_evidence_cannot_inflate_group_count(campaign):
    staged(campaign)
    data = cases(campaign)
    data[1].initial = deepcopy(data[0].initial)
    with pytest.raises(ValueError, match="share one evaluation group"):
        evaluate_workflow(campaign, data, team_factory=ScriptedOffspringTeam)


def test_accepted_harness_is_actually_loaded_in_later_task_execution(campaign):
    staged(campaign)
    report = evaluate_workflow(campaign, cases(campaign), promote=True, team_factory=ScriptedOffspringTeam)
    assert campaign.prepare() is not None
    uses = campaign.report()['harness_use']['adopted_workflows']
    assert uses and uses[0]['version'] == report['challenger']
    assert uses[0]['subsequent_invocations']
    assert all(u['workflow'] == report['challenger'] for u in uses[0]['subsequent_invocations'])


# The metric runner fixture is explicitly synthetic; OS sandbox execution is
# covered by the repository's separate fail-closed acceptance suite.


def test_independent_llm_gate_freezes_plan_and_checks_cached_metric_integrity(
        campaign, monkeypatch, synthetic_metric_worker):
    from test_llm_evaluation_integration import make_campaign
    from test_custom_metric_integration import custom_plan
    campaign, transport, _, _, _ = make_campaign(campaign, monkeypatch, plan=custom_plan())
    data = cases(campaign)
    result = evaluate_workflow(campaign, data, promote=True, team_factory=ScriptedOffspringTeam)
    assert result['promoted'] and len(transport.requests['plan']) == len(transport.requests['verdict']) == 1
    assert transport.contexts['plan'][0]['future_validation_outcomes_present'] is False
    assert evaluate_workflow(campaign, data, promote=True, team_factory=ScriptedOffspringTeam) == result
    key, record = next(iter(campaign.store.all('evaluation_metric_results').items()))
    campaign.store.put('evaluation_metric_results', key, {**record, 'tampered': True})
    with pytest.raises(Conflict):
        evaluate_workflow(campaign, data, promote=True, team_factory=ScriptedOffspringTeam)
