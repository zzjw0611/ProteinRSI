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

The implemented metric is mean signed outcome among the submitted variants, with sign determined by the immutable task direction. The outcome metric, units and assay protocol must match. Controls are excluded from arm efficacy. Technical repeats are aggregated by sequence; related variants may still be correlated.

A single exploratory bootstrap interval is computed using configured confidence, minimum independent-unit count and effect margin. Insufficient units, excessive assay failures, or overlap produce rejection/errors or `inconclusive`. Missing observations are never converted to zero. This is not a familywise-error-controlled guarantee after repeated patch searches. A confirmatory study needs independent validation, multiplicity/selection control and appropriately grouped experimental units.

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

The old and candidate M are frozen during the one-step test, each starting from the same W. Their proposed descendant W is run with the same cases and limits. Group means are compared pairwise. A meta patch needs improvement in descendants, not a better self-written explanation. `--promote` is allowed only on validation cases. Test cases are report-only. The evaluator generates the gate result internally; there is no endpoint to publish arbitrary claimed scores.

This is a diagnostic of one-step improvement ability. For recursive evaluation, additionally compare full multi-generation campaigns using (a) fixed M and (b) accepted successor M, with prespecified total resources and fresh confirmation tasks. Do not count reusing the same protein under many seeds as independent cross-protein evidence.

## Dataset roles

SSMuLA-style tables can support repeatable measured-label query simulation after explicit normalization. They do not create new wet experiments. Historical round datasets can support retrospective/future-round prediction only within their label availability assumptions. Missing counterfactual measurements cannot be invented. Dataset-specific licenses, train-time contamination, group splits and temporal label construction are operator responsibilities.

This repository redistributes no external datasets and reports no scientific superiority over ALDE, ProteinSwarm, Biomni, Virtual Lab or other baselines.
