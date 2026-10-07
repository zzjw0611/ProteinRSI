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

`--top-n 5 10` is the default descriptive panel and can be changed, for example
`--top-n 3 5 10`. This does **not** change proposal order, the immutable version-1
plan, or query purchases. Changed metric reporting is not a new experimental
replicate. Invalid/duplicate/nonpositive N values are rejected before queries are
purchased. Report bins default to `--query-bin-size 100`.

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

## Prediction denominators and posthoc ceilings

`scripts/summarize_replay_study.py --campaign ... --out ...` reads a consistent,
read-only campaign snapshot. It records each round's actual completed protocol
operations, tool execution receipts, valid and unavailable queries, frozen
prediction coverage, and the exact valid-sample denominator for MAE. Protected
prediction references are rechecked against their immutable artifact. A design
that ran a tool but did not pass its result through C may have no frozen numbers;
the audit does not fill them in after seeing measurements.

`scripts/evaluate_replay_ceiling.py --campaign ... --dataset ... --out ...` is an
operator-only, **terminal-only** evaluation. It refuses an unfinished campaign
before opening the source landscape, verifies the pinned source digest and saved
feedback, then reports the attainable score ceiling and remaining score gap in
that task's original metric. It does not export identities of unqueried variants
or modify a campaign's observations or budget.

This posthoc access can distinguish a search miss from a landscape containing no
higher-than-parent record. It cannot justify an earlier stopping decision or
turn an unqueried variant into a discovered result. Do not feed its output to
new or unfinished research agents. All baseline and ceiling results must remain
clearly separate from information available during the live search.

## Joint max / top-N / average reporting

The replay audit, comparator reports, and `Campaign.report()` now describe all
three outcome views together. They use the same `proteinrsi.metrics` aggregation:

- `best`: **max** for a maximizing objective and **min** for a minimizing one;
  `metric_display_names` makes that label explicit
- `top5mean`, `top10mean`, or configured `topNmean`: the mean of the N best
  **distinct sequences**, selected in the task's optimization direction
- `avg`: the mean across all distinct sequences with valid observed outcomes

All reported `metrics` are in the original assay units. `signed_metrics` is the
direction-adjusted view used when larger-is-better comparisons are necessary;
it negates outcomes for minimization, without changing their raw-unit meaning.
For example, minimizing scores `[1, 2, 10]` has best/min `1`, top2mean `1.5`, and
avg `13/3`. Ties do not create or discard extra sequences.

Valid technical repeats of one sequence are averaged **before** ranking or
averaging sequences. Five lookups of one sequence do not supply five top-5
entries, and repeated historical lookups are not independent physical repeats.
`technical_repeats` counts valid observations beyond distinct valid sequences.
Average and top-N scores may decrease after newly acquired evidence; noisy
repeats can also change a sequence mean. These are observed-panel descriptions,
not significance claims or monotonic improvement guarantees.

Top-N requires the **full N distinct valid sequences**. Otherwise its value is
`null`, with `top_n.topNmean.required`, `.effective`, and `.complete` explaining
the denominator. Effective N is diagnostic only: a 3-of-5 panel is not silently
reported as top5mean. Empty panels are null, never zero. Unknown, failed,
inconclusive, unavailable, and not-yet-returned results never enter numeric
means. Invalid finite-value/QC combinations or mismatched/duplicate returned
sample identities fail the audit rather than producing a misleading score.

### Keep the panel and accumulated campaign separate

The live audit has separate `round_metrics` and `arm_metrics` for each research
round; baseline reports expose the same `round_metrics`. Runtime reports include
them in `round_progress`. These describe that round/arm only. The reference
parent is excluded, including parent controls or accidental parent proposals.
`parent.excluded_submissions` and `.excluded_returned` show the exclusion; its
cost is still retained in `submitted_denominators` and the charged-query axis.

`accumulated_campaign.campaign_metrics` at a batch endpoint and the final
`campaign_metrics` describe all saved charged outcomes up to that point plus
known, already-disclosed initial parent evidence. A parent supplies at most one
unique-sequence entry even if it was also queried again. The report gives
`parent.valid_observations`, `.unique_valid_sequences`, and
`.uncharged_valid_parent_observations`; it never silently invents a parent score
from a source file. For an unfinished legacy comparator without a saved parent,
`parent_evidence_status` is `not_saved_no_source_lookup`.

Each panel includes `submitted`, `returned`, `valid`, `unavailable`,
`other_nonvalid`, `nonvalid`, `not_returned`, `unique_valid`, and
`technical_repeats` denominators. Here `nonvalid = unavailable + other_nonvalid`,
`returned = valid + nonvalid`, and `submitted = returned + not_returned`.
Round/arm `denominators` describe the parent-excluded metric cohort;
`submitted_denominators` describes every submitted sample, including the parent.
Campaign `denominators` describes charged queries only, while
`evidence_denominators` also includes uncharged initial evidence and is the
denominator for the campaign metrics. In particular, `unique_valid` is **not**
the same as the valid well count when repeats exist.

### Equal charged-query windows

`equal_query_curve` uses exactly 100 **committed** query slots per full window
by default, independently of physical batch size or round boundaries. It has
both `window_metrics` (just that window, parent excluded) and
`campaign_metrics` (all acquired slots through `query_end`, parent included).
The last shorter window has `complete_bin: false`; do not compare it to a full
100-query window or interpolate missing metrics.

Ordering is committed ledger **reservation** order, then the frozen sample order
inside each batch. A window that ends inside a physical batch is retrospective
accounting, not a claim that those outcomes were disclosed earlier to an agent.
Missing lookups, failures, parent controls, and workflow/online-meta validation
wells all consume slots. Reserved and released reservations are excluded.
Compare full windows at the same `query_end` and metric definition across runs.
The legacy `curve` / `best_measured_value` fields remain available with their
historical individual-observation semantics; use the new fields consistently
for joint multimetric comparisons.

Historical offline meta evaluations may have spent shared-ledger queries without
retaining branch samples/results. Their costs remain in `charged_queries` and
`unreconstructed_charged_queries`; unreturned metrics are not zero. In this case
`equal_query_curve_status` is `withheld_unreconstructed_charges` and the exact
comparison curve is withheld, rather than dropping validation cost or inventing
identities/ordering. The observed subset remains a descriptive campaign report.

### Read-only historical reporting and gate boundaries

```sh
# Read a live/finished campaign snapshot, without source-landscape access
python scripts/summarize_replay_study.py --campaign runs/gb1-live \
  --out reports/gb1-live-metrics.json --top-n 5 10 --query-bin-size 100

# Read a saved comparator without running, resuming, or opening its oracle
python scripts/replay_baseline.py --out runs/gb1-uniform --report-only \
  --report-out reports/gb1-uniform-metrics.json --top-n 5 10
```

Read-only summaries use a consistent SQLite read snapshot. They never rewrite
campaign observations, charges, frozen predictions/plans, previous reports, or
gate decisions. `--report-out` must be outside the comparator directory, and a
baseline execution resume leaves an already saved terminal report unchanged.
New descriptive statistics can be calculated for an old study without silently
opting that study into a new promotion rule. The audit includes
`gate_policy_as_recorded` and preserves each saved `trial` verbatim. Missing
historical predictions are not filled in after outcomes are seen.

These descriptive metrics do not establish W or M improvement by themselves.
Only a prospectively frozen, explicitly opted-in gate may use a joint panel for
promotion. Campaign-wide accumulated best/top-N/avg must not substitute for a
same-round, equal-budget incumbent/challenger comparison; the parent and past
rounds are excluded from that comparison. Existing legacy studies keep their
original rule and recorded decisions.
