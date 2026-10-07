"""Pre-outcome metric contract repair and immutable version-selection tests."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from proteinrsi.contracts import digest
from proteinrsi.llm import ProviderPaused
from proteinrsi.llm_evaluation import ensure_evaluation_plan, load_evaluation_plan
from proteinrsi.storage import Conflict, Store
from test_custom_metric_integration import custom_plan, synthetic_metric_worker as synthetic_metric_worker


class ScriptedPlanner:
    model, base_url, cache_settings = 'synthetic-custom-planner', 'https://fixture.invalid', {}

    def __init__(self, responses):
        self.responses, self.contexts = responses, []

    def complete(self, role, instructions, context, schema):
        assert role == 'E-plan'
        self.contexts.append(deepcopy(context))
        return deepcopy(self.responses[min(len(self.contexts) - 1, len(self.responses) - 1)])


def freeze(store, planner, context=None):
    return ensure_evaluation_plan(store, SimpleNamespace(llm=planner), evaluation_id='custom-plan',
        target='workflow', context=context or {'objective': 'Synthetic contract acceptance'}, max_top_n=3)


def test_new_plan_requires_program_and_cannot_fall_back_to_fixed_metrics(tmp_path):
    missing = custom_plan()
    del missing['metric_program']
    planner, store = ScriptedPlanner([missing]), Store(tmp_path)
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            freeze(store, planner)
    assert len(planner.contexts) == 3
    assert not store.all('evaluation_plans') and not store.all('evaluation_verdicts')
    assert not store.all('evaluation_metric_results')


def test_fixture_failure_repairs_once_before_freeze_with_unchanged_context(
        tmp_path, synthetic_metric_worker):
    invalid, valid = custom_plan(), custom_plan()
    invalid['metric_program']['tests'][0]['expected_output']['items'][0]['score'] = 999
    valid['metric_program']['version'] = 'synthetic-span-v2'
    planner, store = ScriptedPlanner([invalid, valid]), Store(tmp_path)
    plan = freeze(store, planner)
    assert plan['plan']['metric_program']['version'] == 'synthetic-span-v2'
    assert len(planner.contexts) == 2 and len(synthetic_metric_worker) == 6
    before, repaired = planner.contexts
    assert {key: value for key, value in repaired.items() if key != 'response_repair'} == before
    assert 'expected output' in str(repaired['response_repair']['validation_errors'])
    assert not store.all('evaluation_metric_results')
    assert len(store.all('evaluation_metric_validations')) == 1
    assert len([key for key in store.all('evaluation_requests') if key.endswith('/response')]) == 2
    after = len(synthetic_metric_worker)
    assert freeze(Store(tmp_path), planner) == plan
    assert len(planner.contexts) == 2 and len(synthetic_metric_worker) == after
    with pytest.raises(Conflict):
        freeze(store, planner, {'objective': 'Post-outcome changed task'})
    modified = deepcopy(plan)
    modified['plan']['metric_program']['code'] += '\n# post hoc change'
    store.put('evaluation_plans', plan['plan_ref'].split('/')[1], modified)
    with pytest.raises(Conflict):
        load_evaluation_plan(store, plan['plan_ref'])


def test_repeated_invalid_program_does_not_reexecute_failed_fixture_on_restart(
        tmp_path, synthetic_metric_worker):
    invalid = custom_plan()
    invalid['metric_program']['tests'][0]['expected_output']['items'][0]['score'] = 999
    planner, store = ScriptedPlanner([invalid]), Store(tmp_path)
    for _ in range(2):
        with pytest.raises(ProviderPaused):
            freeze(store, planner)
    assert len(planner.contexts) == 3
    assert len(synthetic_metric_worker) == 2  # Cached negative receipt; no repair resampling.
    assert not store.all('evaluation_plans')
    assert any(value['state'] == 'failed' for value in store.all('evaluation_metric_attempts').values())


def test_remote_schema_and_permission_fields_fail_before_code(tmp_path, synthetic_metric_worker):
    invalid = custom_plan()
    invalid['metric_program']['input_schema'] = {'type': 'object', '$ref': 'https://evil.invalid/schema'}
    planner, store = ScriptedPlanner([invalid]), Store(tmp_path)
    with pytest.raises(ProviderPaused):
        freeze(store, planner)
    assert not synthetic_metric_worker
    invalid = custom_plan()
    invalid['metric_program']['network'] = True
    planner, store = ScriptedPlanner([invalid]), Store(tmp_path / 'other')
    with pytest.raises(ProviderPaused):
        freeze(store, planner)
    assert not synthetic_metric_worker


def test_current_plan_hash_pins_program_and_contracts(tmp_path, synthetic_metric_worker):
    planner, store = ScriptedPlanner([custom_plan()]), Store(tmp_path)
    plan = freeze(store, planner)
    assert plan['metric_program_sha256'] == digest(plan['plan']['metric_program'])
    receipt = store.get('evaluation_metric_validations', plan['metric_validation_ref'].split('/')[1])
    assert receipt['program_sha256'] == plan['metric_program_sha256']
    assert receipt['input_schema_sha256'] == digest(plan['plan']['metric_program']['input_schema'])
    assert receipt['output_schema_sha256'] == digest(plan['plan']['metric_program']['output_schema'])
    assert receipt['fixture_only'] is True


def test_compact_branching_schema_cannot_expand_exponentially():
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    definitions = {'leaf': {'type': 'number'}}
    previous = 'leaf'
    for index in range(6):
        name = 'level' + str(index)
        definitions[name] = {'allOf': [{'$ref': '#/$defs/' + previous}] * 4}
        previous = name
    schema = {'type': 'object', '$defs': definitions, '$ref': '#/$defs/' + previous}
    with pytest.raises(ContractError, match='expansion'):
        SchemaRegistry().register('custom.expansion/v1', schema, custom=True)


def test_schema_input_cross_product_has_a_validation_work_budget():
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    registry = SchemaRegistry()
    registry.register('custom.work/v1', {'type': 'array',
        'items': {'allOf': [{'type': 'number'} for _ in range(500)]}}, custom=True)
    with pytest.raises(ContractError, match='bounded validation work'):
        registry.validate('custom.work/v1', list(range(10000)))


def test_unique_items_quadratic_validation_is_bounded():
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    registry = SchemaRegistry()
    registry.register('custom.unique/v1', {'type': 'array', 'uniqueItems': True}, custom=True)
    with pytest.raises(ContractError, match='bounded validation work'):
        registry.validate('custom.unique/v1', [{'value': index} for index in range(1000)])


def test_missing_dynamic_binding_pauses_with_frozen_inputs(tmp_path, synthetic_metric_worker):
    from proteinrsi.evaluation_metrics import MetricExecutionPaused, execute_evaluation_metrics
    plan_input = custom_plan()
    plan_input['metric_program']['input_bindings']['panels'] = '/arms/nonexistent'
    planner, store = ScriptedPlanner([plan_input]), Store(tmp_path)
    plan = freeze(store, planner)
    envelope = {'protocol': 'evaluation_inputs/v1', 'evaluation_id': plan['evaluation_id'],
        'target': 'workflow', 'task': {}, 'top_ns': plan['plan']['top_ns'],
        'subject_refs': ['baseline', 'challenger'], 'arms': {'baseline': [], 'challenger': []},
        'cases': [], 'denominators': {}}
    for _ in range(2):
        with pytest.raises(MetricExecutionPaused):
            execute_evaluation_metrics(store, plan, envelope)
    assert len(synthetic_metric_worker) == 4
    saved_input = next(iter(store.all('evaluation_metric_inputs').values()))
    assert saved_input['envelope'] == envelope and saved_input['inputs'] is None
    assert saved_input['projection_error']
    attempts = {key: value for key, value in store.all('evaluation_metric_attempts').items()
                if not key.startswith('validation-')}
    assert next(iter(attempts.values()))['state'] == 'failed'
    assert not store.all('evaluation_metric_results')


@pytest.mark.parametrize('keyword', ['unevaluatedItems', 'unevaluatedProperties'])
def test_unbounded_annotation_schema_keywords_are_rejected(keyword):
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    with pytest.raises(ContractError, match='annotation'):
        SchemaRegistry().register('custom.annotations/v1', {'type': 'object', keyword: False}, custom=True)


def test_large_instance_branch_errors_have_a_memory_work_budget():
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    registry = SchemaRegistry()
    registry.register('custom.error-size/v1', {'type': 'object', 'properties': {
        'payload': {'anyOf': [{'type': 'number'} for _ in range(500)]}}}, custom=True)
    with pytest.raises(ContractError, match='bounded validation work'):
        registry.validate('custom.error-size/v1', {'payload': 'X' * 500000})


def test_schema_repair_error_identity_is_order_independent():
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    errors = []
    for properties in ({'b': {'type': 'invalid-b'}, 'a': {'type': 'invalid-a'}},
                       {'a': {'type': 'invalid-a'}, 'b': {'type': 'invalid-b'}}):
        try:
            SchemaRegistry().register('custom.order/v1', {'type': 'object',
                                      'properties': properties}, custom=True)
        except ContractError as exc:
            errors.append(exc.detail())
    assert len(errors) == 2 and errors[0] == errors[1]


@pytest.mark.parametrize('schema', [
    {'type': 'object', '$defs': {'X': {}, 'Y': {'$id': 'urn:y', '$defs': {
        'X': {'$ref': '#/$defs/X'}}, '$ref': '#/$defs/X'}}, '$ref': '#/$defs/Y'},
    {'type': 'object', 'properties': {'x': {'$schema': 'http://json-schema.org/draft-04/schema#'}}},
    {'type': 'object', '$anchor': 'renamed'},
])
def test_custom_schema_cannot_rebase_refs_or_switch_dialects(schema):
    from proteinrsi.dataflow.schema import ContractError, SchemaRegistry
    with pytest.raises(ContractError, match='rebase'):
        SchemaRegistry().register('custom.rebase/v1', schema, custom=True)


@pytest.mark.parametrize('namespace', ['evaluation_metric_validations', 'evaluation_metric_inputs',
                                     'evaluation_metric_results', 'evaluation_metric_attempts'])
def test_research_workers_cannot_read_or_forge_custom_metric_receipts(campaign, namespace):
    from proteinrsi.replay.broker import dispatch
    for operation in ('get', 'all', 'put'):
        with pytest.raises(PermissionError):
            dispatch(campaign.team, campaign.team.tools, campaign.view(), [],
                {'rpc': operation, 'namespace': namespace, 'key': 'any', 'value': {}})
