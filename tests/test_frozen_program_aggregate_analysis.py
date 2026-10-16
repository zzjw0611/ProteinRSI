"""Synthetic aggregate-only checks. No empirical outcomes or measurements."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/analyze_frozen_program_aggregate.py'
PREREG = ROOT / 'configs/experiments/frozen_program_preregistration_v1.json'
spec = importlib.util.spec_from_file_location('aggregate_analysis', SCRIPT)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def synthetic_aggregate(tmp_path):
    prereg = json.loads(PREREG.read_text())
    families = {landscape: family for family, landscapes in prereg['family_groups'].items()
                for landscape in landscapes}
    rows = []
    for i, landscape in enumerate(prereg['development_landscapes'] + prereg['evaluation_landscapes']):
        # Deliberately different synthetic units across landscapes. Only paired
        # signs, then equal-weight family balances, may enter the summary.
        unit = 10 ** i
        delta = {'GB1': 1, 'ParD': -1, 'TEV': 0, 'TrpB': 1, 'T7': 0}[families[landscape]]
        for seed in prereg['seeds']:
            for arm in prereg['arms']:
                value = unit * (2 + (delta if arm == 'conditional_pairwise' else 0))
                metrics = {'campaign_metrics': {'metrics': {'best': value}},
                    'equal_query_curve': [{'query_end': end,
                        'campaign_metrics': {'metrics': {'best': value}}}
                        for end in range(16, 129, 16)],
                    'charged_queries': 128, 'unreconstructed_charged_queries': 0}
                rows.append({'landscape': landscape, 'seed': seed, 'arm': arm,
                    'metrics': metrics, 'usage': {'llm_calls': {'committed': 0},
                                                'tool_calls': {'committed': 8}}})
    data = {'case_aggregates': rows, 'llm_calls': 0, 'prompt_workflow_evaluated': False}
    path = tmp_path / 'synthetic-aggregate.json'
    path.write_text(json.dumps(data))
    return path, data


@pytest.mark.core
def test_paired_signs_and_equal_weight_families_without_raw_pooling(tmp_path):
    path, _ = synthetic_aggregate(tmp_path)
    result = analysis.analyze_aggregate(path, PREREG)
    assert result['cases'] == 135 and result['new_query_slots'] == 17280
    assert result['llm_calls'] == 0 and result['tool_calls'] == 1080
    for control in ('additive', 'random'):
        group = result['comparison_summaries'][control]['evaluation']
        assert group['final_best'] == {'win': 30, 'tie': 3, 'loss': 6}
        assert group['endpoint_mean'] == group['final_best']
        # Equal weights for GB1, ParD, TEV, TrpB: (1 - 1 + 0 + 1) / 4.
        assert group['family_balanced_win_minus_loss'] == pytest.approx(0.25)
        assert group['family_balanced_win_minus_loss'] != pytest.approx((30 - 6) / 39)
        assert set(group) == {'cases', 'final_best', 'endpoint_mean', 'by_family',
                             'family_balanced_win_minus_loss', 'by_landscape'}
    assert len(result['paired_comparisons']) == 90
    assert 'no pooled raw scores' in result['aggregation_note']


@pytest.mark.core
@pytest.mark.parametrize('difference, expected', [(1, 'win'), (0, 'tie'), (-1, 'loss'),
                                                  (1e-13, 'tie'), (-1e-13, 'tie')])
def test_frozen_verdict_tolerance(difference, expected, tmp_path):
    path, data = synthetic_aggregate(tmp_path)
    for row in data['case_aggregates']:
        value = difference if row['arm'] == 'conditional_pairwise' else 0
        row['metrics']['campaign_metrics']['metrics']['best'] = value
        for window in row['metrics']['equal_query_curve']:
            window['campaign_metrics']['metrics']['best'] = value
    path.write_text(json.dumps(data))
    result = analysis.analyze_aggregate(path, PREREG)
    assert all(row['best_outcome'] == row['endpoint_mean_outcome'] == expected
               for row in result['paired_comparisons'])


@pytest.mark.core
@pytest.mark.parametrize('corruption', ['missing', 'duplicate', 'endpoint', 'usage'])
def test_incomplete_or_invalid_aggregate_refused(tmp_path, corruption):
    path, data = synthetic_aggregate(tmp_path)
    if corruption == 'missing':
        data['case_aggregates'].pop()
    elif corruption == 'duplicate':
        data['case_aggregates'][-1] = data['case_aggregates'][0]
    elif corruption == 'endpoint':
        data['case_aggregates'][0]['metrics']['equal_query_curve'][0]['query_end'] = 17
    else:
        data['case_aggregates'][0]['metrics']['charged_queries'] = 127
    path.write_text(json.dumps(data))
    with pytest.raises((AssertionError, KeyError)):
        analysis.analyze_aggregate(path, PREREG)


@pytest.mark.core
def test_portable_cli_matches_python_api(tmp_path):
    path, _ = synthetic_aggregate(tmp_path)
    output = tmp_path / 'output/comparison.json'
    subprocess.run([sys.executable, str(SCRIPT), '--aggregate', str(path),
                    '--preregistration', str(PREREG), '--out', str(output)], check=True)
    assert json.loads(output.read_text()) == analysis.analyze_aggregate(path, PREREG)
