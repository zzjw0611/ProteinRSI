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

- A versioned `metric_program`: task-specific definitions, Python calculation source, JSON input/output contracts, envelope bindings, and at least two synthetic fixtures with expected outputs
- One or more top-N values
- Its scientific acceptance criteria and rationale
- How gains and losses should be interpreted together
- How unavailable results, QC failures, incomplete top-N coverage and other missing evidence affect its judgment

These are scientific decisions made by the LLM. The controller does not specify a numerical acceptance threshold, effect direction, weighting scheme, Pareto rule, average-only rule, or a preferred N. Criteria can be qualitative or quantitative according to the LLM's judgment. The budget-derived arm capacity is disclosed as context; it does not impose a scientific ceiling on the chosen N.

The plan is schema-validated, content-addressed and durably bound to the evaluation before arm execution. Valid criteria are not rewritten after seeing the results. Evaluation identities, request context, prompt snapshots and LLM backend identity cannot silently change on restart.

### Frozen custom computation: one authoritative MetricTable

New E plans must supply executable custom metrics. The controller tests each synthetic fixture twice in the existing Landlock/seccomp generated-code sandbox, validates exact expected output, then freezes the program, schemas, bindings, fixture receipts and runtime hashes before either arm runs. Tests establish contract behavior, not scientific validity. Their expected values are never experiment results.

After returned measurements, the controller supplies a minimal `evaluation_inputs/v1` JSON envelope: evaluation/target identity, public task, selected top-Ns, baseline/challenger subject identities, scoped observation rows and coverage counts. Online comparisons populate `arms`; offline Meta comparisons populate aligned `cases` with group identities. Only selected, revealed observations enter the envelope, never an oracle, dataset path, campaign database, credentials, or future labels. Reference/parent, control and unrelated research wells are excluded from trial inputs. They retain their original accounting.

`input_bindings` maps task-generated input field names to JSON pointers within this stable envelope. Statically known task/identity bindings are checked before freezing; a missing dynamic observation binding pauses and retains the exact measured envelope. Generated schemas allow only bounded local, nonrecursive references, prohibit regex/format and unevaluated-annotation validators, and enforce expansion/input-work limits before controller validation. Code receives only `inputs`, empty `context` and empty `artifacts`, and sets `result` to an object matching its generated output schema. Output row and field pointers normalize varied output layouts to the existing `MetricTable`. Every declared metric must return exactly one baseline and one challenger row; undefined values are explicit nulls. The controller assigns frozen units, source-hash method identity and `computed` evidence status. Computation does not acquire measurement authority.

Metric definitions, missing-data handling, replicate aggregation and offline case/group aggregation belong to the frozen program. The framework never replaces a failed custom computation with max/top-N/mean arithmetic. Controller-owned submitted, returned, valid, unavailable, nonvalid and unique-sequence denominators remain separate evidence. Equality/disjointness of submitted arms and identity checks are execution constraints, not numerical preferences.

The worker runs twice on the same immutable input and requires exact JSON agreement. It has no network or credentials, read-only runtime/input files, no process creation, no artifact output, and existing 10-second CPU, 20-second wall, 2GB memory and 512KB output limits. Unsupported kernels fail closed. Source/interpreter/runtime changes pause measured trials rather than silently changing execution. There is no in-process generated-code fallback.

A content-addressed `evaluation_metric_results` record contains the raw schema-validated output, normalized MetricTable, program/code/input/validation/runtime hashes and denominators. E-verdict must cite that exact record. Reports read and integrity-check the same artifact and hashes; they do not recalculate custom metrics, even under a different descriptive display setting. Existing generic progress charts remain clearly descriptive. Cache/restart recovery verifies the input binding and all artifact links before reusing results.

### E-verdict: the LLM decides acceptance

E-verdict receives the exact frozen plan and trusted current-evaluation evidence, including the persisted custom MetricTable and denominators. It returns:

- `accepted`, `rejected` or `inconclusive`
- A scientific explanation
- The exact plan reference
- Nonempty supporting references drawn from this evaluation's evidence set
- An optional descriptive tradeoff label

The controller checks response structure, provenance and immutable identity. It honors every valid LLM decision without a numerical veto. In particular, an LLM may accept a justified tradeoff or reject an apparently dominating panel. A tradeoff label does not itself determine acceptance. Valid `inconclusive` is an actual completed LLM decision; an absent or malformed response is a paused evaluation, not an invented `inconclusive` result.

Only accepted, validated verdicts can drive the existing authorized successor-adoption path. The complete plan, evidence and verdict are persisted before adoption. Workflow or Meta patches cannot write their own evaluation receipts or change the controller's evidence/budget/isolation rules.

### Durable requests, repair and provider recovery

E calls use the real configured `team.llm.complete` route and ordinary LLM accounting. Every request, returned response, plan, evidence report and verdict is auditable. Successful cached calls are reused without additional provider calls, measurement spending or verdict resampling. Provider pauses preserve the same request and trial continuation; retry authorization follows the existing provider recovery protocol.

Before outcomes, invalid metric code, fixture expectations, JSON schemas or bindings can receive up to two bounded repairs. A response with invalid JSON shape, a wrong plan reference, or unsupported evidence references can also receive up to two bounded format/provenance repair calls. Each has its own durable request identity and the same scientific context or frozen plan/evidence, plus the recorded validation error and invalid response. All invalid receipts remain retained. Repairs are not triggered by a valid scientific decision or by unfavorable metric values. Exhausted repairs pause the evaluation; no auto-acceptance, numerical fallback or synthetic decision is produced. Failed pre-outcome fixture executions are cached so restart does not repeatedly execute the same broken program. After observations, computation/contract/nonfinite/nondeterminism failures preserve inputs and measurements and pause with no verdict; they cannot trigger source/schema/criterion repair against held-out results. A revised metric requires a new prospective evaluation and new version. An interrupted pure execution may retry at most once; completed results never rerun. Further recovery requires explicit reconciliation of the recorded failure.

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

Already frozen or pending pre-custom E-plan requests retain their historical fixed-summary semantics and content identities. They are not retroactively labeled as custom metric executions. New plans require `metric_program`; omitting it cannot select a fallback. No old scientific record is rewritten.

Saved `mean_bootstrap` and `observed_pareto_v1` studies retain their original identities and evaluation behavior. Legacy policies and results are neither recomputed nor reclassified. The old mean/bootstrap and fixed observed-Pareto implementations remain available only for explicit compatibility and reproduction; they are not the acceptance procedure for a new `llm_adjudicated_v1` study. Deterministic demonstrations may retain the legacy configuration explicitly.

Legacy mean evaluation uses its configured signed-mean/bootstrap comparison. Legacy observed-Pareto evaluation uses its previously configured metric set, margins, tolerances and coverage rules. Their configurations remain immutable for those studies. Changing the evaluation protocol requires a new study; never edit a saved run to disguise a historical fixed-rule result as LLM-adjudicated evidence.

## Dataset roles

SSMuLA-style tables can support repeatable measured-label query simulation after explicit normalization. They do not create new wet experiments. Historical round datasets support retrospective or future-round analyses only within their label-availability assumptions. Missing counterfactual measurements cannot be invented. Dataset licenses, train-time contamination, group splits and temporal label construction remain operator responsibilities.

This repository redistributes no external datasets and reports no scientific superiority over ALDE, ProteinSwarm, Biomni, Virtual Lab or other baselines.
