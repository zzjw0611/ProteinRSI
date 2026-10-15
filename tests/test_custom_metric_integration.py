"""Synthetic wiring/provenance tests; no live provider or scientific experiments.

The isolated worker is replaced by a fixed test implementation, never eval/exec.
The actual sandbox implementation has separate execution/security tests.
"""
from copy import deepcopy
import json

import pytest

from proteinrsi.contracts import digest
from proteinrsi.replay.sandbox import generated_profile
from proteinrsi.evaluation import evaluate_llm_trial, evaluate_meta, _offline_metric_envelope
from proteinrsi.reporting_metrics import evaluation_report_configuration
from proteinrsi.storage import Conflict
from proteinrsi.trajectory import read_trace
from test_llm_evaluation_integration import make_campaign, measurements, PLAN
from test_rsi import ScriptedOffspringTeam, make_meta_cases


CODE = """result = {'items': []}
for arm in ('baseline', 'challenger'):
    rows = list(inputs['panels'][arm])
    for case in inputs['paired']:
        rows.extend(case['arms'][arm])
    values = [row['value'] for row in rows if row['qc'] == 'valid']
    result['items'].append({'subject': arm, 'metric': 'panel_span',
                           'score': max(values) - min(values) if values else None})
"""


def _synthetic_output(inputs):
    rows = []
    for arm in ('baseline', 'challenger'):
        observed = list(inputs['panels'][arm])
        for case in inputs['paired']:
            observed.extend(case['arms'][arm])
        values = [row['value'] for row in observed if row['qc'] == 'valid']
        rows.append({'subject': arm, 'metric': 'panel_span',
                     'score': max(values) - min(values) if values else None})
    return {'items': rows}


def custom_plan():
    empty = {'panels': {'baseline': [], 'challenger': []}, 'paired': []}
    observed = deepcopy(empty)
    observed['panels']['baseline'] = [
        {'sequence': 'ACDE', 'value': 2, 'qc': 'valid'},
        {'sequence': 'ACDF', 'value': 8, 'qc': 'valid'}]
    program = {
        'version': 'synthetic-span-v1',
        'definitions': [{'name': 'panel_span', 'unit': 'synthetic-units',
            'description': 'Synthetic span across every selected row, for wiring tests only.',
            'direction': 'descriptive'}],
        'code': CODE,
        'input_bindings': {'panels': '/arms', 'paired': '/cases'},
        'input_schema': {'type': 'object', 'properties': {
            'panels': {'type': 'object'}, 'paired': {'type': 'array'}},
            'required': ['panels', 'paired'], 'additionalProperties': False},
        'output_schema': {'type': 'object', 'properties': {'items': {'type': 'array'}},
            'required': ['items'], 'additionalProperties': False},
        'output_rows_pointer': '/items', 'subject_pointer': '/subject',
        'name_pointer': '/metric', 'value_pointer': '/score',
        'tests': [{'name': name, 'inputs': inputs, 'expected_output': _synthetic_output(inputs)}
                  for name, inputs in [('empty', empty), ('observed', observed)]],
    }
    return {**deepcopy(PLAN), 'metric_program': program}


@pytest.fixture
def synthetic_metric_worker(monkeypatch):
    calls = []

    def worker(store, view, args, *, pure=False):
        assert pure and view is None and args['code'] == CODE
        calls.append(deepcopy(args['inputs']))
        return {'status': 'ok', 'execution_backend': generated_profile(),
                'code_sha256': digest(CODE), 'output': _synthetic_output(args['inputs'])}

    monkeypatch.setattr('proteinrsi.evaluation_metrics.execute_code', worker)
    monkeypatch.setattr('proteinrsi.replay.sandbox.probe', lambda: {'available': True})
    return calls


def _record(campaign):
    values = campaign.store.all('evaluation_metric_results')
    assert len(values) == 1
    return next(iter(values.values()))


@pytest.mark.parametrize('target', ['workflow', 'meta'])
def test_actual_custom_table_is_online_evidence_and_reported_without_recomputation(
        campaign, monkeypatch, synthetic_metric_worker, target):
    campaign, transport, _, _, _ = make_campaign(
        campaign, monkeypatch, target=target, plan=custom_plan())
    if target == 'meta':
        monkeypatch.setattr('proteinrsi.online_meta.make_validation_team',
                            lambda campaign, store: ScriptedOffspringTeam(store))
    batch = campaign.prepare()
    assert len(synthetic_metric_worker) == 4  # Two repeats of two synthetic fixtures.
    campaign.approve(batch.batch_id, operator='synthetic-test')
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    campaign.ingest(rows)
    saved = _record(campaign)
    evidence = transport.contexts['verdict'][-1]['evidence']
    assert evidence[saved['result_ref']] == saved
    assert not any(ref.startswith('trial_metrics/') for ref in evidence)
    values = {row['subject_ref']: row['value'] for row in saved['metric_table']['rows']}
    assert values == {'baseline': 10, 'challenger': 0}
    assert saved['denominators']['baseline']['unique_valid'] == 3
    assert len(synthetic_metric_worker) == 6  # Actual input run twice, saved once.
    calls = len(synthetic_metric_worker)
    report = evaluation_report_configuration(campaign.store, campaign.state, [123])
    assert report['llm_evaluation_metric_results'] == {saved['result_ref']: saved}
    assert len(synthetic_metric_worker) == calls
    trial = campaign.store.get('trials', batch.batch_id)
    before = campaign.store.usage()
    evaluate_llm_trial(campaign, batch, rows, trial, campaign.view().task)
    assert campaign.store.usage() == before and len(synthetic_metric_worker) == calls
    trace = read_trace(campaign.store.root)
    assert saved == trace['records']['evaluation_metric_results'][saved['result_ref'].split('/')[1]]
    event = next(e for e in trace['events'] if e['kind'] == 'evaluation_metrics_computed')
    assert event['details']['evaluation_metric_results'] == saved


@pytest.mark.parametrize('namespace', ['evaluation_metric_results', 'evaluation_metric_inputs',
                                     'evaluation_metric_validations'])
def test_cached_trial_and_report_reject_metric_artifact_tampering(
        campaign, monkeypatch, synthetic_metric_worker, namespace):
    campaign, _, _, _, _ = make_campaign(campaign, monkeypatch, plan=custom_plan())
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator='synthetic-test')
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    campaign.ingest(rows)
    key, value = next(iter(campaign.store.all(namespace).items()))
    value['corruption'] = True
    campaign.store.put(namespace, key, value)
    trial = campaign.store.get('trials', batch.batch_id)
    with pytest.raises(Conflict):
        evaluate_llm_trial(campaign, batch, rows, trial, campaign.view().task)
    with pytest.raises(Conflict):
        evaluation_report_configuration(campaign.store, campaign.state)


def test_all_only_report_reader_checks_saved_tables_and_deleted_result(
        campaign, monkeypatch, synthetic_metric_worker):
    campaign, _, _, _, _ = make_campaign(campaign, monkeypatch, plan=custom_plan())
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator='synthetic-test')
    campaign.ingest(measurements(campaign, batch, [0, 0, 10], [5, 5, 5]))

    class Records:
        def __init__(self, store):
            self.store = store
            self.missing = set()

        def all(self, namespace):
            return {} if namespace in self.missing else self.store.all(namespace)

    records = Records(campaign.store)
    assert evaluation_report_configuration(records, campaign.state)['llm_evaluation_metric_results']
    records.missing.add('evaluation_metric_results')
    with pytest.raises(Conflict):
        evaluation_report_configuration(records, campaign.state)


def test_offline_passes_all_selected_rows_once_and_keeps_unselected_labels_private(
        campaign, monkeypatch, synthetic_metric_worker):
    campaign, transport, _, _, _ = make_campaign(
        campaign, monkeypatch, target='meta', plan=custom_plan())
    cases = make_meta_cases(campaign)
    hidden_sentinel = 987654321.125
    for index, case in enumerate(cases):
        case.group_id = 'one-group'
        case.labels[case.task.candidates[6]] = hidden_sentinel
        # Initial disclosed rows stay unchanged; selected rows vary across cases.
        for sequence in case.task.candidates[2:]:
            if sequence != case.task.candidates[6]:
                case.labels[sequence] += index * 10
    report = evaluate_meta(campaign, cases, promote=True, team_factory=ScriptedOffspringTeam)
    saved = _record(campaign)
    assert report['metric_facts'] == saved
    assert transport.contexts['verdict'][-1]['evidence'] == {saved['result_ref']: saved}
    actual = synthetic_metric_worker[-1]
    assert actual['panels'] == {'baseline': [], 'challenger': []}
    assert len(actual['paired']) == len(cases)
    assert len(synthetic_metric_worker) == 6
    assert str(hidden_sentinel) not in json.dumps(actual)
    assert str(hidden_sentinel) not in json.dumps(transport.contexts)
    assert saved['denominators']['n_cases'] == 4
    assert saved['denominators']['n_groups'] == 1
    assert all('observed_rows' in arm for pair in report['traces'] for arm in pair.values())
    # Aggregation spans every case instead of averaging four per-case spans of 1.
    assert {row['value'] for row in saved['metric_table']['rows']} == {31}
    assert evaluate_meta(campaign, cases, promote=True,
                         team_factory=lambda _: pytest.fail('Must reuse completed arms')) == report
    assert len(synthetic_metric_worker) == 6
    altered = deepcopy(saved)
    altered['metric_table']['rows'][0]['value'] = 999
    campaign.store.put('evaluation_metric_results', saved['result_ref'].split('/')[1], altered)
    with pytest.raises(Conflict):
        evaluate_meta(campaign, cases, promote=True)


def test_offline_frozen_traces_without_rows_are_not_silently_recomputed(
        campaign, monkeypatch, synthetic_metric_worker):
    campaign, _, _, _, _ = make_campaign(campaign, monkeypatch, target='meta', plan=custom_plan())
    cases = make_meta_cases(campaign)
    plan = {'evaluation_id': 'synthetic', 'target': 'meta', 'plan': {'top_ns': [1]}}
    traces = [{arm: {'case_id': case.case_id, 'group_id': case.group_id,
                     'selected': [], 'metric_summary': {'signed_metrics': {'avg': 1}}}
               for arm in ['baseline', 'challenger']} for case in cases]
    with pytest.raises(Conflict, match='lacks saved observed rows'):
        _offline_metric_envelope(plan, campaign.view().task, cases, traces)
    assert synthetic_metric_worker == []


@pytest.mark.parametrize('route', ['workflow', 'meta', 'offline_meta'])
def test_provider_pause_restart_reuses_paid_requests_and_exact_custom_artifact(
        campaign, monkeypatch, synthetic_metric_worker, route):
    from proteinrsi.llm import ProviderPaused
    from proteinrsi.recovery import authorize_retry
    from proteinrsi.runtime import Campaign
    from proteinrsi.storage import Store
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch,
        target='workflow' if route == 'workflow' else 'meta',
        plan=custom_plan(), pause_verdict=True)
    monkeypatch.setattr('proteinrsi.llm.time.sleep', lambda _: None)
    branches = []

    class CountingTeam(ScriptedOffspringTeam):
        def run(self, view):
            branches.append(view.workflow.version)
            return super().run(view)

    if route == 'meta':
        monkeypatch.setattr('proteinrsi.online_meta.make_validation_team',
                            lambda campaign, store: CountingTeam(store))
    if route == 'offline_meta':
        cases = make_meta_cases(campaign)

        def run(controller):
            return evaluate_meta(controller, cases, promote=True, team_factory=CountingTeam)
    else:
        batch = campaign.prepare()
        campaign.approve(batch.batch_id, operator='synthetic-test')
        rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])

        def run(controller):
            return controller.ingest(rows)

    with pytest.raises(ProviderPaused):
        run(campaign)
    reopened = Store(campaign.store.root)
    frozen_results = reopened.all('evaluation_metric_results')
    frozen_inputs = reopened.all('evaluation_metric_inputs')
    frozen_evidence = reopened.all('evaluation_evidence')
    frozen_arms = reopened.all('meta_arm_results')
    frozen_usage = reopened.usage()
    frozen_branch_count = len(branches)
    assert len(frozen_results) == 1 and len(synthetic_metric_worker) == 6
    assert len(transport.requests['plan']) == len(transport.requests['verdict']) == 1
    assert frozen_usage['llm_calls']['committed'] == 2
    assert not reopened.all('evaluation_verdicts')
    active = campaign.view().workflow if route == 'workflow' else campaign.view().meta
    assert active.version == base
    resumed = Campaign(reopened, transport.team(reopened))
    monkeypatch.setattr(resumed, 'consider_improvement', lambda: None)
    # A restart alone does not authorize resampling a failed paid request.
    with pytest.raises(ProviderPaused):
        run(resumed)
    assert reopened.usage() == frozen_usage
    assert len(synthetic_metric_worker) == 6 and len(branches) == frozen_branch_count
    key = next(key for key, record in reopened.all('llm').items() if record['state'] == 'failed')
    authorize_retry(reopened, key, operator='synthetic-test', reason='Synthetic provider restored')
    run(resumed)
    assert reopened.all('evaluation_metric_results') == frozen_results
    assert reopened.all('evaluation_metric_inputs') == frozen_inputs
    assert reopened.all('evaluation_evidence') == frozen_evidence
    assert reopened.all('meta_arm_results') == frozen_arms
    assert reopened.usage()['experimental_wells'] == frozen_usage['experimental_wells']
    assert reopened.usage()['llm_calls']['committed'] == 3
    assert len(synthetic_metric_worker) == 6 and len(branches) == frozen_branch_count
    assert len(transport.requests['plan']) == 1 and len(transport.requests['verdict']) == 2
    assert transport.requests['verdict'][0] == transport.requests['verdict'][1]
    active = resumed.view().workflow if route == 'workflow' else resumed.view().meta
    assert active.version != base


def test_failed_custom_computation_never_falls_back_or_requests_a_verdict(
        campaign, monkeypatch, synthetic_metric_worker):
    from proteinrsi.llm import ProviderPaused
    campaign, transport, _, base, _ = make_campaign(campaign, monkeypatch, plan=custom_plan())
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator='synthetic-test')
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    monkeypatch.setattr('proteinrsi.evaluation_metrics.execute_code',
                        lambda *args, **kwargs: {'status': 'failed', 'error_type': 'SyntheticFailure'})
    with pytest.raises(ProviderPaused):
        campaign.ingest(rows)
    assert campaign.store.get('measurements', batch.batch_id)
    assert campaign.store.all('evaluation_metric_inputs')
    assert not campaign.store.all('evaluation_metric_results')
    assert not campaign.store.all('trial_results')
    assert not transport.requests['verdict']
    assert campaign.view().workflow.version == base
    monkeypatch.setattr('proteinrsi.evaluation_metrics.execute_code',
                        lambda *args, **kwargs: pytest.fail('A failed measured program must stay frozen'))
    with pytest.raises(ProviderPaused):
        campaign.ingest(rows)
    assert not transport.requests['verdict']


@pytest.mark.parametrize('corruption', ['evidence_copy', 'denominator'])
def test_cached_trial_rejects_changed_custom_evidence_or_denominators(
        campaign, monkeypatch, synthetic_metric_worker, corruption):
    campaign, _, _, _, _ = make_campaign(campaign, monkeypatch, plan=custom_plan())
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator='synthetic-test')
    rows = measurements(campaign, batch, [0, 0, 10], [5, 5, 5])
    campaign.ingest(rows)
    result = campaign.store.get('trial_results', batch.batch_id)
    if corruption == 'evidence_copy':
        metric = _record(campaign)
        result['details']['evidence'][metric['result_ref']]['metric_table']['rows'][0]['value'] = 42
    else:
        result['n_baseline'] = 1000
    campaign.store.put('trial_results', batch.batch_id, result)
    with pytest.raises(Conflict):
        evaluate_llm_trial(campaign, batch, rows,
                          campaign.store.get('trials', batch.batch_id), campaign.view().task)
