from itertools import product
from collections import Counter

ref = context['task']['reference_sequence']
positions = (39,40,41,54)
aa = 'ACDEFGHIKLMNPQRSTVWY'
order = 'ACFGSTVILMNHQDEKRWPY'
pc = context['research_context']['plate_completion']
excluded = {ref}
excluded.update(o['sequence'] for o in context.get('observations',[]) if o.get('sequence'))
excluded.update(pc.get('exclude_sequences',[]))
for item in pc.get('already_selected',[]):
    if isinstance(item,str):
        excluded.add(item)
    elif isinstance(item,dict) and item.get('sequence'):
        excluded.add(item['sequence'])
assert pc['required_new_candidates'] == 384
assert tuple(ref[p-1] for p in positions) == ('V','D','G','V')

def sequence(residues):
    s = list(ref)
    for p,r in zip(positions,residues):
        s[p-1] = r
    return ''.join(s)

def legal(s):
    changes = [i+1 for i,(a,b) in enumerate(zip(ref,s)) if a != b]
    return len(s)==len(ref) and set(s)<=set(aa) and 1<=len(changes)<=4 and set(changes)<=set(positions)

eligible = {}
for r41 in order:
    eligible[r41] = []
    for r54 in order:
        seqs = [sequence(('V',bg,r41,r54)) for bg in 'DWY']
        if all(s not in excluded and legal(s) for s in seqs):
            eligible[r41].append(r54)
eligible_count = sum(map(len,eligible.values()))
counts54 = Counter()
groups = []
while len(groups)<96:
    progressed = False
    for r41 in order:
        if len(groups)==96:
            break
        if not eligible[r41]:
            continue
        r54 = min(eligible[r41],key=lambda r:(counts54[r],order.index(r)))
        eligible[r41].remove(r54)
        groups.append((r41,r54))
        counts54[r54] += 1
        progressed = True
    assert progressed, 'Insufficient eligible complete groups'

matched = []
for i,(r41,r54) in enumerate(groups,1):
    label = 'M%03d_41%s_54%s'%(i,r41,r54)
    for bg in 'DWY':
        residues = ('V',bg,r41,r54)
        matched.append({'sequence':sequence(residues),'source':'agent','evidence_kind':'none','predicted_value':None,'uncertainty':None,'rationale':label+'; 40='+bg+'. Matched comparison tests whether position-40 background changes this 41/54 combination effect.'})
matched_sequences = {c['sequence'] for c in matched}

lists = ('ILMFTYVACW','WYFHASNV','ACGF','ACGV')
pool = [r for r in product(*lists) if sequence(r) not in excluded|matched_sequences and legal(sequence(r))]
exploration = []
for seed in [('V','W','A','A'),('V','Y','A','A')]:
    if seed in pool:
        exploration.append(seed)
        pool.remove(seed)
def distance(a,b):
    return sum(x!=y for x,y in zip(a,b))
while len(exploration)<96:
    assert pool
    # Pool preserves product order, hence stated residue-list tie order.
    best = max(range(len(pool)),key=lambda i:min((distance(pool[i],r) for r in exploration),default=4))
    exploration.append(pool.pop(best))
exploration_candidates = []
for i,r in enumerate(exploration,1):
    exploration_candidates.append({'sequence':sequence(r),'source':'agent','evidence_kind':'none','predicted_value':None,'uncertainty':None,'rationale':'E%03d; residues39/40/41/54=%s. Explore complementary backgrounds with greedy maximum minimum Hamming distance; ordering is experimental priority, not predicted fitness.'%(i,''.join(r))})

# Prioritize aromatic-40 extensions of the measured best pair.
seeds = [c for c in exploration_candidates if tuple(c['sequence'][p-1] for p in positions) in [('V','W','A','A'),('V','Y','A','A')]]
seed_sequences = {c['sequence'] for c in seeds}
priority_groups = [i for i,(a,b) in enumerate(groups) if a in 'AC' or b in 'AC']
other_groups = [i for i in range(96) if i not in priority_groups]
candidates = list(seeds)
for i in priority_groups:
    candidates.extend(matched[3*i:3*i+3])
remaining_exploration = [c for c in exploration_candidates if c['sequence'] not in seed_sequences]
for j in range(max(len(other_groups),len(remaining_exploration))):
    if j<len(other_groups):
        i=other_groups[j]
        candidates.extend(matched[3*i:3*i+3])
    if j<len(remaining_exploration):
        candidates.append(remaining_exploration[j])

identities = [c['sequence'] for c in candidates]
assert len(candidates)==384 and len(set(identities))==384
assert not set(identities)&excluded
assert all(legal(s) for s in identities)
assert len(matched)==288 and len(exploration_candidates)==96
for a,b in groups:
    assert all(sequence(('V',bg,a,b)) in set(identities) for bg in 'DWY')
coverage = [{'residue':r,'matched_groups_position41':sum(a==r for a,b in groups),'matched_groups_position54':counts54[r]} for r in order]
exploration_coverage = {str(p):dict(Counter(r[k] for r in exploration)) for k,p in enumerate(positions)}
group_table = [{'group':'M%03d'%(i+1),'position41':a,'position54':b,'backgrounds':['D','W','Y']} for i,(a,b) in enumerate(groups)]
result = {'candidates':candidates,'eligible_complete_groups_before_selection':eligible_count,'matched_group_table':group_table,'matched_coverage_table':coverage,'exploration_coverage':exploration_coverage,'validation':{'candidate_count':384,'unique_count':384,'matched_groups':96,'matched_candidates':288,'exploration_candidates':96,'exclusion_overlap':0,'all_legal':True},'experimental_measurements_created':False}
