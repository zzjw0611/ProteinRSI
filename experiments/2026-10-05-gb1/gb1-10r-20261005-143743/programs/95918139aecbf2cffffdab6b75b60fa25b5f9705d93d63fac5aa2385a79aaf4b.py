import hashlib
import json
from collections import Counter
v = context.get('view', context)
t = v['task']
ref = t['reference_sequence']
pos = t['mutable_positions']
assert pos == [39,40,41,54]
assert len(ref) == 56
assert t['repeat_policy'] == 'exclude'
assert t['candidate_access'] == 'catalogue'
assert not t['allow_indels'] and t['max_mutations'] == 4
assert t['controls_per_batch'] == 0
observed = {o['sequence'] for o in v['observations']}
alphabet = set('ACDEFGHIKLMNPQRSTVWY')
mutable = {p-1 for p in pos}
labels = ['V39F/D40R/G41F','D40A/V54N','V39W/D40Y/V54Y']
anchors = []
for changes in [{39:'F',40:'R',41:'F'},{40:'A',54:'N'},{39:'W',40:'Y',54:'Y'}]:
    s = list(ref)
    for p,aa in changes.items():
        s[p-1] = aa
    anchors.append(''.join(s))
assert all(s in observed for s in anchors)
def distance(a,b):
    return sum(a[p-1] != b[p-1] for p in pos)
def pairs(s):
    return {(p,s[p-1]) for p in pos}
def legal(s):
    return isinstance(s,str) and len(s)==len(ref) and set(s)<=alphabet and sum(a!=b for a,b in zip(s,ref))<=t['max_mutations'] and all(s[i]==ref[i] for i in range(len(ref)) if i not in mutable)
rows = []
seen = set()
for index,s in enumerate(t['candidates'],1):
    if s in seen or s in observed or not legal(s):
        continue
    seen.add(s)
    ds = [distance(s,a) for a in anchors]
    rows.append({'sequence':s,'catalogue_index_1based':index,'sha256':hashlib.sha256(s.encode('ascii')).hexdigest(),'edits':[{'position':p,'from':ref[p-1],'to':s[p-1]} for p in pos if s[p-1]!=ref[p-1]],'depth':distance(s,ref),'winner_distance':ds[0],'multi_anchor_distance':min(ds),'nearest_anchor_edits':[label for label,d in zip(labels,ds) if d==min(ds)]})
counts = Counter(r['depth'] for r in rows)
capacity = {d:n//2 for d,n in counts.items()}
m = min(12,sum(capacity.values()))
assert 2*m <= t['batch_size'] and 2*m <= v['remaining_wells']
def select(pool,quota,key,group):
    selected = []
    used = Counter()
    coverage = set()
    remaining = list(pool)
    for order in range(1,m+1):
        eligible = [r for r in remaining if used[r['depth']] < quota.get(r['depth'],0)]
        assert eligible, 'Quota cannot be filled'
        r = min(eligible,key=lambda x:(x[key],-len(pairs(x['sequence'])-coverage),x['catalogue_index_1based']))
        entry = dict(r)
        entry.update(group=group,group_order=order,coverage_increment=len(pairs(r['sequence'])-coverage))
        selected.append(entry)
        used[r['depth']] += 1
        coverage.update(pairs(r['sequence']))
        remaining.remove(r)
    return selected
single = select(rows,capacity,'winner_distance','single_winner')
quota = Counter(r['depth'] for r in single)
single_sequences = {r['sequence'] for r in single}
multi = select([r for r in rows if r['sequence'] not in single_sequences],quota,'multi_anchor_distance','multi_anchor')
manifest = single + multi
ranking = [dict(r) for pair in zip(single,multi) for r in pair]
assert len(single)==len(multi)==m
assert Counter(r['depth'] for r in multi)==quota
assert len(ranking)==2*m and len({r['sequence'] for r in ranking})==2*m
catalogue = set(t['candidates'])
for r in ranking:
    s = r['sequence']
    assert legal(s) and s in catalogue and s not in observed and s not in anchors
    assert t['candidates'][r['catalogue_index_1based']-1]==s
    assert hashlib.sha256(s.encode('ascii')).hexdigest()==r['sha256']
assert all(ranking[2*i]==single[i] and ranking[2*i+1]==multi[i] for i in range(m))
assert sorted(json.dumps(r,sort_keys=True) for r in ranking)==sorted(json.dumps(r,sort_keys=True) for r in manifest)
# Independently replay each greedy ordering, including filtering full depths before ranking.
for group,pool,q,key in [(single,rows,capacity,'winner_distance'),(multi,[r for r in rows if r['sequence'] not in single_sequences],quota,'multi_anchor_distance')]:
    coverage = set()
    used = Counter()
    remaining = list(pool)
    for actual in group:
        eligible = [r for r in remaining if used[r['depth']] < q.get(r['depth'],0)]
        expected = min(eligible,key=lambda r:(r[key],-len(pairs(r['sequence'])-coverage),r['catalogue_index_1based']))
        assert actual['sequence']==expected['sequence']
        assert actual['coverage_increment']==len(pairs(expected['sequence'])-coverage)
        used[expected['depth']] += 1
        coverage.update(pairs(expected['sequence']))
        remaining.remove(expected)
result = {'manifest':manifest,'ranking':ranking,'eligible_count':len(rows),'eligible_depth_counts':dict(counts),'paired_capacity':capacity,'group_size':m,'common_quota':dict(quota),'depth_histograms':{'single_winner':dict(quota),'multi_anchor':dict(Counter(r['depth'] for r in multi))},'checks_passed':True,'constraint_violations':0,'planned_new_queries':2*m,'remaining_if_all_queried':v['remaining_wells']-2*m,'experimental_measurements_created':0,'primary_endpoint':'Within each group, fraction of valid raw reported_assay_fitness values > 1.0; null if no valid results','predicted_value':None,'uncertainty':None,'limitations':['Computed identity and selection checks are not experimental fitness evidence.','SHA256 identifies sequence text; it does not independently authenticate catalogue provenance.','Single-winner selection precedes and restricts multi-anchor selection.','Historical frozen group labels are unavailable.']}
artifact = write_artifact('round_five_frozen_manifest.json',json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2),'json')
result['artifacts'] = [artifact]
