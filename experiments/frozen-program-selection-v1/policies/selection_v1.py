# Root-authored frozen diagnostic policy; not a translation-equivalent prompt-W.
# No protein names, site numbers, known winning residues or benchmark labels.
import itertools
import numpy as np

AA = 'ACDEFGHIKLMNPQRSTVWY'
task = context['task']
positions = task['mutable_positions']
reference = task['reference_sequence']
parent = ''.join(reference[p - 1] for p in positions)
mode = inputs['mode']
assert mode in ('random', 'additive', 'conditional_pairwise')
seed = int(task['seed'])
round_index = int(context['round_index'])
rng = np.random.default_rng(seed + 1009 * round_index)
count = int(context.get('capacity', {}).get('required_plate_wells', task['batch_size']))
completion = context.get('research_context', {}).get('plate_completion', {})
count = int(completion.get('required_new_candidates', count))
observations = context['observations']
def short(sequence):
    return ''.join(sequence[p - 1] for p in positions)
seen = {short(o['sequence']) for o in observations}
seen.update(short(s) for s in completion.get('exclude_sequences', []))
valid = {short(o['sequence']): float(o['value']) for o in observations
         if o['qc'] == 'valid' and o['value'] is not None}
all_codes = [''.join(x) for x in itertools.product(AA, repeat=len(positions))]
unseen = [s for s in all_codes if s not in seen]
assert len(unseen) >= count
parent_value = valid.get(parent)
reversal_count = 0
# Detect opposite substitution-effect signs in actually measured backgrounds.
if parent_value is not None:
    for background, value in valid.items():
        for j in range(len(positions)):
            if background[j] != parent[j]:
                continue
            for residue in AA:
                if residue == parent[j]:
                    continue
                altered = background[:j] + residue + background[j+1:]
                single = parent[:j] + residue + parent[j+1:]
                if background != parent and altered in valid and single in valid:
                    direct = valid[single] - parent_value
                    conditional = valid[altered] - value
                    if direct * conditional < 0 and min(abs(direct), abs(conditional)) > 1e-12:
                        reversal_count += 1
use_pairs = mode == 'conditional_pairwise' and reversal_count > 0
initial = len(seen) == 1 and parent in seen
if initial:
    # All arms get exactly the same per-seed, site-balanced single-mutant start.
    pools = []
    for j in range(len(positions)):
        choices = [parent[:j] + a + parent[j+1:] for a in AA if a != parent[j]]
        pools.append([choices[i] for i in rng.permutation(len(choices))])
    chosen = []
    site_order = list(rng.permutation(len(positions)))
    for k in range(count):
        j = site_order[k % len(positions)]
        chosen.append(pools[j].pop())
    model_name = 'shared_balanced_single_initialization'
elif mode == 'random' or len(valid) < 2:
    ix = rng.choice(len(unseen), size=count, replace=False)
    chosen = [unseen[int(i)] for i in ix]
    model_name = 'uniform_unseen'
else:
    train_codes = list(valid)
    train = np.array([[AA.index(a) for a in s] for s in train_codes], dtype=np.int16)
    y = np.array([valid[s] for s in train_codes], dtype=float)
    centered = y - y.mean()
    scale = float(y.std())
    if scale > 0:
        centered /= scale
    m = (train[:, None, :] == train[None, :, :]).sum(axis=2).astype(float)
    kernel = m + m * (m - 1) / 2 if use_pairs else m
    dual = np.linalg.solve(kernel + np.eye(len(y)), centered)
    pool = np.array([[AA.index(a) for a in s] for s in unseen], dtype=np.int16)
    scores = np.empty(len(unseen), dtype=float)
    # Bounded chunks for four-site theoretical spaces; no catalogue membership lookups.
    for start in range(0, len(unseen), 2048):
        block = pool[start:start+2048]
        mm = (block[:, None, :] == train[None, :, :]).sum(axis=2).astype(float)
        kk = mm + mm * (mm - 1) / 2 if use_pairs else mm
        scores[start:start+len(block)] = kk @ dual
    assert np.isfinite(scores).all()
    # Fixed 25% uniform exploration. Reserve it first; identical rule for both fits.
    explore_n = max(1, count // 4)
    explore = [int(i) for i in rng.choice(len(unseen), size=explore_n, replace=False)]
    excluded = set(explore)
    tie_order = rng.random(len(unseen))
    order = np.lexsort((tie_order, -scores))
    exploit = [int(i) for i in order if int(i) not in excluded][:count-explore_n]
    chosen = [unseen[i] for i in exploit + explore]
    model_name = 'pairwise_kernel_ridge' if use_pairs else 'additive_kernel_ridge'
assert len(chosen) == count and len(set(chosen)) == count and not set(chosen) & seen
candidates = []
for code in chosen:
    sequence = list(reference)
    for p, a in zip(positions, code):
        sequence[p-1] = a
    candidates.append({'sequence': ''.join(sequence), 'predicted_value': None,
                       'rationale': 'Frozen diagnostic acquisition: ' + model_name})
result = {'candidates': candidates, 'diagnostic': {
    'policy_mode': mode, 'model_used': model_name, 'observed_reversal_count': reversal_count,
    'valid_training_count': len(valid), 'missing_values_imputed': False,
    'method_claim': 'root-authored executable mechanism diagnostic; not frozen prompt-W efficacy'}}
