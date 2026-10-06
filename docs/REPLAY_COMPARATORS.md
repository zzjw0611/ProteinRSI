# Equal-query historical replay comparators

`scripts/replay_baseline.py` supplies two explicitly **scripted, nonadaptive**
comparators. They are not LLM substitutes and do not exercise the research roles:

- `uniform`: shuffle all legal site combinations using a recorded seed, excluding
  the supplied parent, then use the first query-budget identities
- `single-first`: shuffle all single substitutions first, then fill the remaining
  budget from a shuffled full combination space, excluding repeats and the parent

Both use only sequence constraints to propose. The entire sequence order is
persisted immutably before the oracle is opened. They do not inspect dataset
membership to improve coverage, use hidden-label rankings, or resample missing
records. Every submitted lookup costs one query, including unavailable records.
The known parent is initial evidence, as in the live studies. Re-running the same
plan resumes its existing ledger and observations without additional charges.
Changed policies, seeds or budgets require a separate output directory.

```sh
python scripts/replay_baseline.py --data-root /path/to/ssmula --landscape GB1 \
  --out runs/gb1-uniform --rounds 20 --batch-size 100 --seed 17 --policy uniform
python scripts/replay_baseline.py --data-root /path/to/ssmula --landscape GB1 \
  --out runs/gb1-single-first --rounds 20 --batch-size 100 --seed 17 --policy single-first
```

Each comparator is a **separate** historical study with its own full budget;
these queries must not be represented as free measurements in the live campaign.
Withhold comparator reports and their observed labels from live research agents
until their matching study has ended. Compare best-so-far curves at equal numbers
of charged queries, including missing values and workflow/meta-validation wells.
Include the parent in both curves and do not count repeated lookups as repeats of
the physical assay.

A higher best-so-far value than the parent demonstrates finding an already assayed
variant in this replay. It does not establish a better search policy. A single
run or seed against these simple comparators does not establish statistical
superiority, generalization, wet-lab benefit, or scientific RSI efficacy. Report
negative and inconclusive comparisons too. A stronger evaluation needs a frozen,
independent multi-seed protocol and must account for adaptive method selection.
