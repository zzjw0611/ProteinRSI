from itertools import combinations
ref = inputs['reference']
positions = inputs['positions']
task = context['task']
alphabet = 'ACDEFGHIKLMNPQRSTVWY'
assert ref == task['reference_sequence']
assert positions == task['mutable_positions']
assert ''.join(ref[p-1] for p in positions) == 'VDGV'
assert task['allow_indels'] is False
known = {o['sequence'] for o in context.get('observations', []) if o.get('sequence')}
known.add(ref)
candidates = []
edits = []
categories = []
seen = set()
def add(assignments, category, hypothesis):
    seq = list(ref)
    for p, aa in assignments:
        assert p in positions and aa in alphabet and aa != ref[p-1]
        seq[p-1] = aa
    seq = ''.join(seq)
    changes = [{'position': i+1, 'from': old, 'to': new} for i, (old, new) in enumerate(zip(ref, seq)) if old != new]
    assert len(seq) == len(ref) and set(seq) <= set(alphabet)
    assert len(changes) == len(assignments)
    assert 1 <= len(changes) <= task['max_mutations']
    assert all(e['position'] in positions for e in changes)
    assert all(seq[i] == ref[i] for i in range(len(ref)) if i+1 not in positions)
    assert seq not in known and seq not in seen
    seen.add(seq)
    label = ', '.join(str(e['position']) + e['from'] + '>' + e['to'] for e in changes)
    candidates.append({'sequence': seq, 'source': 'agent', 'evidence_kind': 'none', 'predicted_value': None, 'uncertainty': None, 'rationale': category + ': ' + label + '. ' + hypothesis})
    edits.append(changes)
    categories.append(category)
# Round-robin positions; each position receives every nonparent amino acid.
choices = {p: [aa for aa in alphabet if aa != ref[p-1]] for p in positions}
for index in range(19):
    for p in positions:
        add([(p, choices[p][index])], 'single', 'Measure the individual substitution effect to establish a complete local baseline; benefit is unknown.')
# Round-robin position pairs; pair letters follow ascending position order.
pairs = list(combinations(positions, 2))
for residues in ['AA', 'FF', 'WW', 'YY', 'LI', 'IL', 'ST', 'TS']:
    for p, q in pairs:
        add([(p, residues[0]), (q, residues[1])], 'double', 'Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown.')
for residues in ['AAAA', 'FLAI', 'WYAF', 'YFSL']:
    add(list(zip(positions, residues)), 'quadruple', 'Probe a four-position combination spanning different side-chain compositions; this is exploratory and has no established fitness advantage.')
counts = {name: categories.count(name) for name in ['single', 'double', 'quadruple']}
assert counts == {'single': 76, 'double': 48, 'quadruple': 4}
assert len(candidates) == len(edits) == len(seen) == inputs['panel_limit'] == 128
assert len(candidates) <= task['proposal_pool_size']
assert len(candidates) <= task['batch_size']
assert len(candidates) <= context['remaining_wells']
result = {'candidates': candidates, 'edits': edits, 'counts': counts, 'validation': {'canonical_alphabet': True, 'length_preserved': True, 'fixed_residues_preserved': True, 'mutation_limit_respected': True, 'unique': True, 'revealed_sequences_and_parent_excluded': True, 'within_proposal_batch_and_remaining_limits': True}, 'ordering': '76 singles round-robin by position, 48 doubles round-robin by position pair, then 4 quadruples; ordering does not predict fitness', 'fitness_predictions': None}
