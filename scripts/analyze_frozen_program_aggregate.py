"""Aggregate-only descriptive analysis; no oracle access or policy modifications.

Portable packaging of the frozen analysis. All comparisons/statistics are
unchanged; inputs and output are explicit CLI paths.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path


def analyze_aggregate(source, preregistration):
    source = Path(source)
    prereg = json.loads(Path(preregistration).read_text())
    data = json.loads(source.read_text())
    rows = data['case_aggregates']
    assert len(rows) == prereg['total_cases'] == 135
    assert data['llm_calls'] == 0 and data['prompt_workflow_evaluated'] is False
    index = {(r['landscape'], r['seed'], r['arm']): r for r in rows}
    assert len(index) == len(rows)
    family_for = {landscape: family for family, landscapes in prereg['family_groups'].items()
                  for landscape in landscapes}

    def best(row):
        return row['metrics']['campaign_metrics']['metrics']['best']

    def curve(row):
        windows = row['metrics']['equal_query_curve']
        assert len(windows) == 8 and [w['query_end'] for w in windows] == list(range(16,129,16))
        return [w['campaign_metrics']['metrics']['best'] for w in windows]

    def verdict(x):
        return 'win' if x > 1e-12 else 'loss' if x < -1e-12 else 'tie'

    comparisons = []
    for landscape in prereg['development_landscapes'] + prereg['evaluation_landscapes']:
        for seed in prereg['seeds']:
            a = index[landscape, seed, 'conditional_pairwise']
            for control in ('additive', 'random'):
                b = index[landscape, seed, control]
                ac, bc = curve(a), curve(b)
                comparisons.append({'landscape': landscape, 'family': family_for[landscape],
                    'seed': seed, 'control': control,
                    'split': 'development' if landscape in prereg['development_landscapes'] else 'evaluation',
                    'best_policy': best(a), 'best_control': best(b), 'best_delta': best(a)-best(b),
                    'best_outcome': verdict(best(a)-best(b)),
                    'endpoint_mean_delta': sum(x-y for x,y in zip(ac,bc))/8,
                    'endpoint_mean_outcome': verdict(sum(x-y for x,y in zip(ac,bc))/8),
                    'policy_curve': ac, 'control_curve': bc})

    def counts(items, field='best_outcome'):
        c=collections.Counter(x[field] for x in items)
        return {k:c[k] for k in ('win','tie','loss')}

    summaries = {}
    for control in ('additive','random'):
        selected=[x for x in comparisons if x['control']==control]
        groups={}
        for split in ('development','evaluation'):
            ss=[x for x in selected if x['split']==split]
            families={family:counts([x for x in ss if x['family']==family])
                      for family in sorted({x['family'] for x in ss})}
            balances={family:(c['win']-c['loss'])/sum(c.values()) for family,c in families.items()}
            groups[split]={'cases':len(ss),'final_best':counts(ss),
                'endpoint_mean':counts(ss,'endpoint_mean_outcome'),
                'by_family':families,'family_balanced_win_minus_loss':sum(balances.values())/len(balances),
                'by_landscape':{landscape:counts([x for x in ss if x['landscape']==landscape])
                                for landscape in sorted({x['landscape'] for x in ss})}}
        cross=[x for x in selected if x['family'] in ('GB1','ParD','TEV')]
        groups['distinct_from_source_families']={'cases':len(cross),'final_best':counts(cross),
            'note':'GB1 and ParD explored by root previously; only TEV is an unqueried family in this main session.'}
        summaries[control]=groups
    for row in rows:
        m=row['metrics']
        assert m['charged_queries']==128 and m['unreconstructed_charged_queries']==0
        assert row['usage']['llm_calls']['committed']==0
        assert row['usage']['tool_calls']['committed']==8
    result={'source_file_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'cases':len(rows),'new_query_slots':sum(r['metrics']['charged_queries'] for r in rows),
            'llm_calls':0,'tool_calls':sum(r['usage']['tool_calls']['committed'] for r in rows),
            'comparison_summaries':summaries,'paired_comparisons':comparisons,
            'limitations':prereg['known_limits'],
            'aggregation_note':'Within-landscape paired signs only; no pooled raw scores, no independent-protein count inflation, no significance claim. Endpoint mean is unweighted mean of eight observed batch endpoints.'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--out", required=True, help="Destination JSON file")
    args = parser.parse_args()
    result = analyze_aggregate(args.aggregate, args.preregistration)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
