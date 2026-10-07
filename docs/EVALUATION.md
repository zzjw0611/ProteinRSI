# Evaluation protocol and evidence boundaries

## Three different claims

1. Software mechanisms work: restart, schema checks, accounting, evidence isolation, trials and successor activation
2. A workflow helps a particular scientific task under the stated resources
3. A successor improver is better at producing future workflow improvements on independent tasks

Unit tests establish the first, not the second or third. Scripted provider fixtures deliberately exercise acceptance and rejection branches. They are not protein models, actual experiments, or evidence of scientific superiority.

## LLM-defined evaluation for new LLM studies

New LLM-backed CLI/natural-language studies use `llm_adjudicated_v1`. The explicit configuration is `configs/gate.llm_adjudicated.json`, containing only:

```json
{"criterion": "llm_adjudicated_v1"}
```

Evaluation has two actual LLM calls, **E-plan** and **E-verdict**. E is an evaluation role of the research system, not a separate source of scientific measurements. Neither call has a deterministic fallback.

### E-plan: scientific criteria before validation outcomes

Before acquiring or exposing future validation outcomes, the LLM receives the public task objective, current revealed evidence, proposed method change, and available resources. It chooses:

- One or more top-N values
- Its scientific acceptance criteria and rationale
- How gains and losses should be interpreted together
- How unavailable results, QC failures, incomplete top-N coverage and other missing evidence affect its judgment

These are scientific decisions made by the LLM. The controller does not specify a numerical acceptance threshold, effect direction, weighting scheme, Pareto rule, average-only rule, or a preferred N. Criteria can be qualitative or quantitative according to the LLM's judgment. The budget-derived arm capacity is disclosed as context; it does not impose a scientific ceiling on the chosen N.

The plan is schema-validated, content-addressed and durably bound to the evaluation before arm execution. Valid criteria are not rewritten after seeing the results. Evaluation identities, request context, prompt snapshots and LLM backend identity cannot silently change on restart.

### Trusted arithmetic: describe all requested evidence

After actual returned measurements, trusted controller code computes and presents all three requested families:

- Maximum observed outcome, with best-in-task-direction also explicit
- Every top-N mean chosen by the LLM in its frozen plan
- Average over valid unique sequences in the arm

Task direction comes from the task's objective and describes the reported summaries; it is not a controller-owned acceptance rule. Technical repeats are first averaged within sequence. The reference/provided-parent, control wells and non-comparison research filler are excluded from trial-arm summaries. Provided-parent measurements remain zero-query initial evidence.

Submitted, returned, valid, unavailable, other nonvalid, not-returned, unique-valid and repeat counts remain visible. A top-N with fewer than N valid unique measurements is `null`, accompanied by required/effective counts. Missing values are never zero-imputed, missing case summaries are never silently dropped, and technical repeats never create independent units.

Equal, nonempty, disjoint submitted arms are execution/identity requirements. One submitted candidate makes an arm executable; this is not a minimum scientifically sufficient valid count. No controller rule converts small samples, QC rates, null metrics, losses, gains or ties into an acceptance verdict in this mode. The LLM sees those facts and judges their implications under its plan.

### E-verdict: the LLM decides acceptance

E-verdict receives the exact frozen plan and trusted current-evaluation evidence, including all summaries and denominators. It returns:

- `accepted`, `rejected` or `inconclusive`
- A scientific explanation
- The exact plan reference
- Nonempty supporting references drawn from this evaluation's evidence set
- An optional descriptive tradeoff label

The controller checks response structure, provenance and immutable identity. It honors every valid LLM decision without a numerical veto. In particular, an LLM may accept a justified tradeoff or reject an apparently dominating panel. A tradeoff label does not itself determine acceptance. Valid `inconclusive` is an actual completed LLM decision; an absent or malformed response is a paused evaluation, not an invented `inconclusive` result.

Only accepted, validated verdicts can drive the existing authorized successor-adoption path. The complete plan, evidence and verdict are persisted before adoption. Workflow or Meta patches cannot write their own evaluation receipts or change the controller's evidence/budget/isolation rules.

### Durable requests, repair and provider recovery

E calls use the real configured `team.llm.complete` route and ordinary LLM accounting. Every request, returned response, plan, evidence report and verdict is auditable. Successful cached calls are reused without additional provider calls, measurement spending or verdict resampling. Provider pauses preserve the same request and trial continuation; retry authorization follows the existing provider recovery protocol.

A response with invalid JSON shape, a wrong plan reference, or unsupported evidence references can receive up to two bounded format/provenance repair calls. Each has its own durable request identity and the same scientific context or frozen plan/evidence, plus the recorded validation error and invalid response. All invalid receipts remain retained. Repairs are not triggered by a valid scientific decision or by unfavorable metric values. Exhausted repairs pause the evaluation; no auto-acceptance, numerical fallback or synthetic decision is produced. Further recovery requires explicit reconciliation of the recorded failure.

Replay workers cannot access evaluator-only label sources or write evaluation namespaces. Raw label paths, complete hidden label tables and unrevealed scores do not enter E-plan or E-verdict. Revealed measurements are factual evidence, never permissions or instructions to modify the protocol.

## Experimental resources and interpretation

Controls, failed submitted assays, unavailable replay queries and technical repeats consume budget. Baseline and challenger receive equal submitted variant counts and the same maximum role/tool-call bounds. Inspect actual usage: caching and different tool choices can change costs and elapsed time. Token/GPU-time/dollar fairness is not implemented as a full scheduler.

Historical measured-label replay and prospective wet experiments have different source tags. Unavailable replay records are not low fitness, assay failure or biological infeasibility. Keep future labels in a trusted service inaccessible to code-capable research agents.

Observed maxima, top-N means and averages describe the measured panel. LLM acceptance is not a confidence interval, p-value, population-level effect estimate, independent confirmation or cross-protein generalization guarantee. Related variants can remain correlated. A stronger scientific claim needs appropriate fresh evidence, grouping and controls for selection and repeated search, chosen and stated for that study. A workflow comparison does not establish that every new candidate is better. Trial data remain available after rejection. Updating the same prediction model with more data is an inner-loop baseline, not by itself RSI.

## Meta-policy evaluations

Online M evaluation freezes old and candidate improvers, gives each the same revealed context, and evaluates their descendant workflows under equal arm budgets. An E-plan is frozen before the next returned validation outcomes; E-verdict interprets their complete metrics and coverage. Only the MetaPolicy is promoted if its validated verdict is accepted.

Offline `evaluate-meta` requires a queued M patch and an evaluator-only case array:

```text
case_id, group_id, split ('development'/'validation'/'test')
task: full TaskSpec
initial: list of public Observation records
history: public prior summaries (optional)
labels: evaluator-only sequence -> measured value mapping
query_budget: equal additional queries for each descendant
```

Never supply the complete case file to an agent or publish private labels. Initial observations must agree with evaluator-owned measurements. Both descendants see identical public bootstrap evidence. Related cases or seeds remain grouped by protein; they are not independent proteins. Reports preserve aligned per-case summaries, missingness, group identities and denominators. Where group summaries are used, a per-case best and a group-average best are distinguished from a maximum over unrelated proteins. Heterogeneous metric units and directions are not silently collapsed.

The LLM plan is frozen before offline descendant outcomes. Durable branch stores and trace checkpoints allow a paused E verdict to resume without regenerating or remeasuring completed arms. A meta patch is evaluated by actual descendant evidence, not by its self-written explanation. `--promote` remains restricted to validation cases; test cases are report-only. These split and publication boundaries are data-use controls, not numerical scientific acceptance criteria.

This is a diagnostic of one-step improvement ability. Recursive claims also require multi-generation comparisons of fixed M and accepted successor M under stated total resources and suitable fresh confirmation tasks.

## Historical compatibility

Saved `mean_bootstrap` and `observed_pareto_v1` studies retain their original identities and evaluation behavior. Legacy policies and results are neither recomputed nor reclassified. The old mean/bootstrap and fixed observed-Pareto implementations remain available only for explicit compatibility and reproduction; they are not the acceptance procedure for a new `llm_adjudicated_v1` study. Deterministic demonstrations may retain the legacy configuration explicitly.

Legacy mean evaluation uses its configured signed-mean/bootstrap comparison. Legacy observed-Pareto evaluation uses its previously configured metric set, margins, tolerances and coverage rules. Their configurations remain immutable for those studies. Changing the evaluation protocol requires a new study; never edit a saved run to disguise a historical fixed-rule result as LLM-adjudicated evidence.

## Dataset roles

SSMuLA-style tables can support repeatable measured-label query simulation after explicit normalization. They do not create new wet experiments. Historical round datasets support retrospective or future-round analyses only within their label-availability assumptions. Missing counterfactual measurements cannot be invented. Dataset licenses, train-time contamination, group splits and temporal label construction remain operator responsibilities.

This repository redistributes no external datasets and reports no scientific superiority over ALDE, ProteinSwarm, Biomni, Virtual Lab or other baselines.
