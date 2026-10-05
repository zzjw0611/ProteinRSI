import hashlib
from collections import Counter
v = context.get('view', context)
task = v['task']
ref = task['reference_sequence']
positions = task['mutable_positions']
assert positions == [39, 40, 41, 54]
assert task['repeat_policy'] == 'exclude'
assert task['candidate_access'] == 'catalogue'
assert task['allow_indels'] is False
assert task['max_mutations'] == 4
assert task['controls_per_batch'] == 0
assert len(ref) == 56
allowed = {p-1 for p in positions}
alphabet = set('ACDEFGHIKLMNPQRSTVWY')
observed = {o['sequence'] for o in v['observations']}
anchors = []
for edits in [{39:'F',40:'R',41:'F'}, {40:'A',54:'N'}, {39:'W',40:'Y',54:'Y'}]:
    chars = list(ref)
    for p, aa in edits.items():
        chars[p-1] = aa
    anchors.append(''.join(chars))
assert all(a in observed for a in anchors)
def dist(a,b):
    return sum(a[p-1] != b[p-1] for p in positions)
def pairs(s):
    return {(p,s[p-1]) for p in positions}
def legal(s):
    return len(s)==len(ref) and set(s)<=alphabet and sum(a!=b for a,b in zip(s,ref))<=4 and all(s[i]==ref[i] for i in range(len(ref)) if i not in allowed)
rows = []
seen = set()
for index,s in enumerate(task['candidates'],1):
    if s in seen or s in observed or not legal(s):
        continue
    seen.add(s)
    distances = [dist(s,a) for a in anchors]
    rows.append({'sequence':s,'catalogue_index_1based':index,'sha256':hashlib.sha256(s.encode('ascii')).hexdigest(),'depth':dist(s,ref),'winner_distance':distances[0],'multi_anchor_distance':min(distances),'nearest_anchor_edits':[label for label,d in zip(['V39F/D40R/G41F','D40A/V54N','V39W/D40Y/V54Y'],distances) if d==min(distances)],'predicted_fitness':None,'prediction_uncertainty':None})
counts = Counter(r['depth'] for r in rows)
capacity = {d:n//2 for d,n in counts.items()}
m = min(12,int(task['batch_size'])//2,int(v['remaining_wells'])//2,sum(capacity.values()))
def select(pool, quota, distance_key, group, size):
    chosen = []
    used = Counter()
    coverage = set()
    remaining = list(pool)
    for order in range(1,size+1):
        eligible = [r for r in remaining if used[r['depth']] < quota.get(r['depth'],0)]
        assert eligible, 'Insufficient eligible candidates for quota'
        row = min(eligible,key=lambda r:(r[distance_key],-len(pairs(r['sequence'])-coverage),r['catalogue_index_1based']))
        entry = dict(row)
        entry.update({'group':group,'group_order':order,'coverage_increment':len(pairs(row['sequence'])-coverage)})
        chosen.append(entry)
        used[row['depth']] += 1
        coverage |= pairs(row['sequence'])
        remaining.remove(row)
    return chosen
baseline = select(rows,capacity,'winner_distance','single_winner',m)
quota = Counter(r['depth'] for r in baseline)
baseline_sequences = {r['sequence'] for r in baseline}
challenger = select([r for r in rows if r['sequence'] not in baseline_sequences],quota,'multi_anchor_distance','multi_anchor',m)
manifest = baseline + challenger
ranking = []
for a,b in zip(baseline,challenger):
    ranking.extend([dict(a),dict(b)])
assert len(baseline)==len(challenger)==m
assert Counter(r['depth'] for r in baseline)==Counter(r['depth'] for r in challenger)
assert len({r['sequence'] for r in ranking})==2*m
assert len(ranking)<=task['batch_size'] and len(ranking)<=v['remaining_wells']
assert len(ranking)<=24
catalogue = set(task['candidates'])
for r in ranking:
    s = r['sequence']
    assert legal(s) and s in catalogue and s not in observed
    assert task['candidates'][r['catalogue_index_1based']-1]==s
    assert hashlib.sha256(s.encode('ascii')).hexdigest()==r['sha256']
assert sorted((r['group'],r['group_order'],r['sequence'],r['sha256']) for r in ranking)==sorted((r['group'],r['group_order'],r['sequence'],r['sha256']) for r in manifest)
assert all(ranking[2*i]==baseline[i] and ranking[2*i+1]==challenger[i] for i in range(m))
result = {'manifest':manifest,'ranking':ranking,'group_size':m,'depth_histograms':{'single_winner':dict(quota),'multi_anchor':dict(Counter(r['depth'] for r in challenger))},'planned_new_queries':2*m,'remaining_if_all_queried':v['remaining_wells']-2*m,'checks_passed':True,'endpoint':'proportion of valid raw reported_assay_fitness > 1.0','limitations':['SHA256 records sequence identity; it does not independently authenticate catalogue provenance.','Historical frozen group labels are unavailable; this computation does not reconstruct them.'],'experimental_measurements_created':0}