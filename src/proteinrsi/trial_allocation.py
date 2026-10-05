# SPDX-License-Identifier: MIT
"""Feasible, equal, disjoint arms chosen before any outcomes are revealed."""

def allocate_trial(arms, versions, slots, minimum, excluded, seed):
    excluded = set(excluded)
    maps = {a: {c.sequence: c for c in arms[a] if c.sequence not in excluded}
            for a in ('baseline', 'challenger')}
    keys = {a: set(m) for a, m in maps.items()}
    if list(maps['baseline']) == list(maps['challenger']):
        return [], {'reason': 'Identical candidate priorities; no distinct policy comparison', 'per_arm': 0}
    size = min(slots // 2, len(keys['baseline']), len(keys['challenger']),
               len(keys['baseline'] | keys['challenger']) // 2)
    metadata = {'per_arm': size, 'maximum_plate_slots': slots,
                'available_by_arm': {a: len(m) for a, m in maps.items()},
                'rule': 'unique-to-arm first, then ranked shared identities with alternating priority'}
    if size < minimum:
        return [], {**metadata, 'reason': 'Insufficient distinct candidates for the minimum equal-arm sample size'}
    selected = {a: [] for a in maps}
    used = set()
    for arm, other in [('baseline', 'challenger'), ('challenger', 'baseline')]:
        for seq in maps[arm]:
            if seq not in keys[other] and len(selected[arm]) < size:
                selected[arm].append(seq)
                used.add(seq)
    order = ['baseline', 'challenger'] if seed % 2 == 0 else ['challenger', 'baseline']
    while any(len(v) < size for v in selected.values()):
        for arm in order:
            if len(selected[arm]) >= size:
                continue
            seq = next((s for s in maps[arm] if s not in used), None)
            if seq is None:
                raise ValueError('Internal disjoint allocation inconsistency')
            selected[arm].append(seq)
            used.add(seq)
    chosen = [(maps[a][s], a, versions[a]) for a in order for s in selected[a]]
    return chosen, metadata
