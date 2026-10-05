from itertools import combinations
ref = inputs['reference']
pos = inputs['positions']
alphabet = 'ACDEFGHIKLMNPQRSTVWY'
assert ''.join(ref[p-1] for p in pos) == 'VDGV'
known = {o['sequence'] for o in context.get('observations', []) if o.get('sequence')}
known.add(ref)
proposals = []
seen = set()
def add(assignments, category):
    seq = list(ref)
    for p, aa in assignments:
        seq[p-1] = aa
    seq = ''.join(seq)
    edits = [{'position': i+1, 'from': a, 'to': b} for i, (a, b) in enumerate(zip(ref, seq)) if a != b]
    assert len(seq) == len(ref) and set(seq) <= set(alphabet)
    assert 1 <= len(edits) <= 4
    assert all(e['position'] in pos for e in edits)
    assert seq not in known and seq not in seen
    seen.add(seq)
    proposals.append({'sequence': seq, 'substitutions': edits, 'category': category})
for p in pos:
    for aa in alphabet:
        if aa != ref[p-1]:
            add([(p, aa)], 'single')
for p, q in combinations(pos, 2):
    for pair in ['AA', 'FF', 'WW', 'YY', 'LI', 'IL', 'ST', 'TS']:
        add([(p, pair[0]), (q, pair[1])], 'double')
for residues in ['AAAA', 'FLAI', 'WYAF', 'YFSL']:
    add(list(zip(pos, residues)), 'quadruple')
assert len(proposals) == inputs['panel_limit'] == 128
assert len(proposals) <= context['task']['batch_size']
assert len(proposals) <= context['remaining_wells']
counts = {category: sum(p['category'] == category for p in proposals) for category in ['single', 'double', 'quadruple']}
assert counts == {'single': 76, 'double': 48, 'quadruple': 4}
result = {'proposals': proposals, 'counts': counts, 'validation': {'unique': True, 'fixed_residues_preserved': True, 'length_preserved': True, 'previously_revealed_sequences_excluded': True, 'within_round_and_remaining_query_limits': True}, 'fitness_predictions': None}
