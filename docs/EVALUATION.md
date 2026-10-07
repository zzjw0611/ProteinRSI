# Evaluation protocol and evidence boundaries

## Three claims that must not be confused

1. Software mechanisms work: restart, schema checks, accounting, trials, rollback and successor activation.
2. A workflow improves a particular scientific task under fixed resources.
3. A successor improver is better at producing future workflow improvements on independent tasks.

Unit tests establish the first, not the second or third. The artificial demo uses a known generated numerical landscape and deterministic roles. The test `ScriptedOffspringTeam` intentionally makes the acceptance branch observable; it is not a protein model or experimental result.

## Experimental resources

Controls, failed submitted assays and technical repeats consume budget. Baseline and challenger arms receive equal submitted variant counts in a trial. Both see the same revealed data and use the same maximum role/tool-call bounds. Inspect actual usage; caches and different tool choices make wall time differ. Token/GPU-time/dollar fairness is not implemented as a full scheduler and must be added for those claims.

Historical query simulation and prospective wet experiments have different source tags. An unknown replay variant causes an error, not a surrogate 'ground truth'. Keep future labels in a trusted service inaccessible to any code-capable agent.

## Workflow gate

The legacy default is `mean_bootstrap`: mean signed outcome among submitted variants,
with task direction determining the sign. Its existing exploratory bootstrap and
loading behavior are unchanged. Old saved results are not recomputed or reclassified.
Changing criteria requires a new study, never an edit to an existing run.

### Opt-in observed multi-metric gate

Use `init --gate configs/gate.observed_pareto.json` **when creating a new study**. The
immutable acceptance policy is stored at initialization, included in method snapshots
and candidate validation plans, and shown in each role's research context. A changed
policy on resume is rejected. Neither W nor M can change the gate.

`observed_pareto_v1` prespecifies these equally required objectives:

- `best`: maximum for maximize tasks; minimum for minimize tasks
- `top5mean` and `top10mean`: means of the best 5 and 10 unique sequences, respectively
- `avg`: mean over all valid unique sequences in that comparison arm

`top_ns` is configurable before initialization; `[5, 10]` is the default. Technical
repeats are first averaged within sequence, so repeated wells neither weight `avg`
nor inflate independent-unit counts. Valid-only means are descriptive: submitted,
returned, valid, unavailable, other nonvalid, not-returned, unique-valid and repeat
counts remain explicit. Every top-N requires its full N; it is `null` with the
required/effective N reported when unavailable, never a silently smaller top-N.
Controls, research filler wells and the reference/parent are excluded from the arm
gate. The provided-parent measurement remains zero-query initial evidence and cannot
inflate either arm's best score. Outcome metric, units, direction and assay must match.

All comparisons use signed metrics so higher is better, including minimize tasks.
For each metric j, delta_j = challenger_j - baseline_j in that signed orientation.
The fixed rule is:

1. Accept only if every delta_j >= -absolute_tolerance_j and at least one
   delta_j > improvement_margin_j
2. Mixed gains and losses beyond the declared tolerance are `inconclusive`, with
   descriptive outcome `tradeoff`; no weights or scalar utility hide the loss
3. No positive delta and at least one worsening beyond tolerance is `rejected`
4. Ties/below-margin changes, too few units, missing historical coverage, excessive
   QC failure, or any incomplete required top-N are `inconclusive`

The checked-in policy declares **zero absolute tolerances and zero improvement
margins for every metric**: exact weak dominance plus one strictly positive gain.
This does not claim assay noise is zero; it is a deterministic rule for observed
panels. If nonzero tolerances/margins are appropriate, declare them in the task's
measurement units before creating the study. No relative scaling, data-dependent
threshold, preference weight or adaptive margin is used. `min_effect` must be zero;
use per-metric `improvement_margins`. Legacy `confidence` and `bootstrap_samples`
fields are retained for loading compatibility but unused in this mode.

W and online M require at least `max(min_per_arm, max(top_ns))` distinct eligible
sequences per planned arm. Capacity below that threshold defers validation without
spending an impossible validation panel. Final valid unique-unit count and full-N
coverage are checked again after results. Overlapping arm sequences remain invalid;
missing values are never imputed as zero. Failed assays still consume their slots,
and the prespecified maximum QC-failure fraction remains enforced.

This gate computes **no bootstrap confidence interval, p-value or significance claim**
for maxima or any other metric. The legacy bootstrap-of-the-mean is not reused as a
max test. Observed panel dominance is exploratory task-local evidence, not a
population, cross-protein or repeated-search error-control guarantee. Related
variants can still be correlated. Confirmatory evidence needs fresh independent
validation, appropriate grouping, and prespecified selection/multiplicity control.

The selection of a new workflow is a policy comparison, not proof that every new candidate is better. Data acquired during a trial remain available after rejection. The same prediction model updated with more data is an inner-loop baseline and must be included in comparisons.

## Meta-policy case format

`evaluate-meta` requires a queued M patch and an evaluator-only JSON array. Each element includes:

```text
case_id, group_id, split ('development'/'validation'/'test')
task: full TaskSpec
initial: list of Observation (public bootstrap measurements)
history: public prior summaries (optional)
labels: evaluator-only sequence -> measured value mapping
query_budget: equal additional queries for each descendant
```

Never give the full case file to the agent or put private case labels in the repository. The initial measurements must agree with labels. Common bootstrap measurements are supplied identically to both policies; the comparison budgets additional queries separately. Group related cases/seeds from one protein together. Different metric units/directions are not averaged into one acceptance test.

The old and candidate M are frozen during the one-step test, each starting from the same W. Their proposed descendant W is run with the same cases and limits. Under the legacy gate, signed scalar group means are compared pairwise. Under the
multi-metric gate, each descendant's selected **unique-sequence** panel produces the
full same metric vector first. Each metric is averaged equally over aligned cases
within a group, then equally over groups. Thus `best` means the mean of per-case
bests, not the maximum across unrelated proteins. All case/group IDs are aligned;
missing case metrics are never dropped. Each case must supply full top-N coverage,
while `min_per_arm` counts independent groups, not related seeds or wells. The same
observed Pareto rule gates the resulting group-aggregate vector. Per-case summaries,
denominators, group vectors and policy are retained in the report.

A meta patch needs improvement in descendants, not a better self-written explanation. `--promote` is allowed only on validation cases. Test cases are report-only. The evaluator generates the gate result internally; there is no endpoint to publish arbitrary claimed scores.

This is a diagnostic of one-step improvement ability. For recursive evaluation, additionally compare full multi-generation campaigns using (a) fixed M and (b) accepted successor M, with prespecified total resources and fresh confirmation tasks. Do not count reusing the same protein under many seeds as independent cross-protein evidence.

## Dataset roles

SSMuLA-style tables can support repeatable measured-label query simulation after explicit normalization. They do not create new wet experiments. Historical round datasets can support retrospective/future-round prediction only within their label availability assumptions. Missing counterfactual measurements cannot be invented. Dataset-specific licenses, train-time contamination, group splits and temporal label construction are operator responsibilities.

This repository redistributes no external datasets and reports no scientific superiority over ALDE, ProteinSwarm, Biomni, Virtual Lab or other baselines.
