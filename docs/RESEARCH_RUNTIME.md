# Resource-aware research runtime (v0.4)

This is an additional, typed inner-loop execution strategy. It runs inside the
existing Campaign and never submits experiments or publishes W/M versions itself.

## Activation and backward compatibility

New CLI `init` uses `ResearchConfig()` (adaptive) unless `--research-mode fixed`.
`--research-config configs/research.json` configures it explicitly. The configuration
and original Know-how text are stored once in SQLite. Existing saved campaigns
without `configuration/research`, and the Python `Campaign.initialize` API without
`research_config=ResearchConfig()`, retain the fixed runner. `demo --adaptive`
exercises the new code with deterministic roles and synthetic outcomes.

Do not upgrade code/dependencies halfway through a registered scientific experiment.
Use separate new campaign directories to compare old and new harnesses. Workflow
and MetaPolicy version calculation are unchanged. The adaptive run cache additionally
includes runtime version, tool schemas, research configuration, model identity,
Know-how snapshot and visible evidence.

## A bounded plan is not arbitrary code

Available plan operations:

| Operation | Logical owner | Execution and validation |
|---|---|---|
| `evidence` | C | Read-only QC and frozen-prediction error reports, using two metered context tools |
| `design` | B | Existing legal sequence/edit proposals and bounded registered-tool dialogue |
| `tool` | A | One explicitly named registered operation; cannot supply commands or paths outside its schema |
| `rank` | C | Optional analysis calls, ESMC/task-head numerical analysis when configured, then C review |
| `finalize` | A | Validate a permutation of the fresh ranking; return priorities, not an approved batch |

`ResearchStep` declares ID, operation, question, expected output, dependencies and
optional ToolCall. Owner is inferred by code, not supplied by the LLM. Plans are
ordered dependency graphs, executed serially; this is not a parallel DAG scheduler.
Only earlier IDs can be dependencies. There is exactly one terminal finalize step,
and design/ranking must precede it. Tools/evidence after rank invalidate that ranking.
Any newly introduced candidate must pass the task's fixed positions, library,
sequence and target constraints.

`A-plan` produces the first plan. After each completed nonterminal step, `A-review`
can keep it or replace the unexecuted suffix. Completed steps cannot be edited.
Limits on plan length, total executed steps, design/analysis calls and revisions
remain enforced. Once the revision limit is reached, the remaining plan runs without
further revision calls. All JSONLLM calls share the campaign LLM budget/cache.

For example, A can change:

```
evidence → design → rank → finalize
```

to:

```
completed evidence → combination diagnostic → design → rank → finalize
```

or schedule a second design after an analysis result. Revisions of the current plan
are inner-loop adaptation, not a W/M publication. Only the existing outer gate can
publish a persistent workflow or successor MetaPolicy.

## Selection and permissions

Resources have stable IDs, content versions, categories and descriptions:
`tools`, `data_lake` (visible campaign evidence and registered artifacts only),
`libraries` (authorized Skills in this implementation), `know_how`, `experience`.
The registry is not a filesystem crawler or web retriever. It never opens hidden
label tables or imports a global directory of experimental data.

1. Tool registration and workflow whitelist restrict the candidate catalog.
2. Task kinds and outbound-data permission are checked before selection.
3. Know-how is filtered by task kind and minimum known data; experience must match
   task kind and evidence source. These are coarse eligibility rules, not a proof of transfer.
4. `rules` ranks descriptions lexically (English tokens and Chinese characters),
   `all` keeps catalog order, and `llm` invokes the adapted Biomni selector.
5. A round-robin category merge enforces max resource count and character budget;
   omitted IDs are explicit. Whole resource entries are kept or omitted, not partly
   truncated JSON schemas. TaskView's scientific constraints and observations are
   outside this supplementary resource budget and are never dropped by retrieval.

The selected subset informs A/B; the full permission-filtered catalog remains
available to C's explicit analysis step so necessary validators are not lost.
A plan revision can select resources again for the revised question. Retrieval is
not authorization: every execution still goes through the gateway. The default rules
selector deliberately does not claim semantic/vector retrieval performance.

The adapted source lives in `research/biomni_retriever.py`, is Apache-2.0, and is
actually invoked only in LLM selection mode. The surrounding resource catalog,
knowledge loader and execution state machine are original MIT implementations.

## Read-only analysis semantics

`research_evidence_summary({})` groups current observations by batch, records valid,
failed and inconclusive counts and descriptive WT statistics. It does not assume all
controls are WT, estimate assay drift causally, normalize values or exclude rows.

`research_prediction_errors({})` matches currently revealed rows against predictions
saved in their submitted Batch, excluding designated controls and missing predictions.
It reports MAE, signed bias, tied-rank Spearman where defined and errors by mutation
depth. It does NOT retrospectively fit a new head then score its own training labels.
Technical replicates remain dependent; metrics are descriptive, not out-of-sample
confidence or causal attribution. External engines must not put unrelated energy/
confidence scores into Candidate.predicted_value: this field is for task-outcome
predictions only.

`research_combination_effects({"scale":"linear", "pooling":"within_batch"})`
compares measured combinations with measured WT and constituent singles:

```
linear null = WT + sum(single_i - WT)
log null    = log(WT) + sum(log(single_i) - log(WT))
```

For k substitutions this gives a scale-dependent descriptive deviation, not an
interaction p-value. Technical replicates are averaged on the recorded scale before
an optional log transform. Log mode requires positive relevant means. The default
requires all constituents in the same batch. `pooling=pooled` is explicit and carries
a cross-batch confounding warning. Missing constituents are returned as missing,
never replaced with ESMC scores, a fitted head or hidden dataset queries.

All three functions are bound to the current TaskView; the LLM cannot supply input
measurement tables, SQL, paths or arbitrary numerical labels. Results are persisted
under `research_analysis`. No unrestricted Python/R/Bash REPL is enabled.

## Persistence, failures and experiment boundary

SQLite stores `resource_selections`, `research_runs`, `research_step_outputs` and
`research_analysis`. A run records its plan, completed-prefix ledger, current pool,
rank, actual outputs, reviews and revisions. A completed run is cached. An external
interruption can resume from completed steps; successful tool and LLM request IDs
prevent duplicate effects. These guarantees assume the same code/config/dependency
versions and the existing single-writer campaign lock.

A normal exception marks the run `blocked`; repeated invocation does not silently
resubmit a failed job. The operator must investigate, reconcile external work, and
choose a reviewed new run/strategy. There is no automatic "clear all caches and retry"
button. An uncertain remote LLM call can remain blocked by the underlying JSONLLM
client even when the plan checkpoint exists. This is deliberate, not full distributed
transactional recovery or a scheduler.

The returned ranking is sent to the existing batch builder. Only that trusted path
reserves experiments; only explicit approval commits the batch. On import, measured
facts are retained even when later reasoning fails. QC/error reports and relevant
plan/revision summaries are added to the round history seen by M. No plan command
can write measurements, alter assay definitions, change budgets or promote itself.

## Outer loop and paired Meta evaluation

A Meta proposal still goes through Patch validation and the existing experimental or
independent Meta gate. Both offspring in the numeric Meta evaluator receive the same
research configuration, Know-how snapshot, frozen protein model and resource limits.
Their evidence, computation caches and outputs remain separate. Changing the current
plan, adding a note or rerunning a fit is not by itself RSI.

The restricted numeric Meta evaluator remains one-step frozen-improver evaluation,
not a demonstration of arbitrary recursive code improvement. Structural-engine
campaigns require case-scoped artifact provisioning and are rejected by that generic
evaluator. General cross-protein experience transport is not implemented here.

## Inspecting a run

```bash
proteinrsi research plans --campaign runs/my-campaign
proteinrsi research resources --campaign runs/my-campaign
proteinrsi research analyze --campaign runs/my-campaign
```

These inspection commands do not invoke an LLM or protein inference. `analyze`
persists descriptive derived results; it neither changes scientific observations nor
triggers Meta promotion. New experiments still require the normal approval/import flow.

## Validation boundaries

Tests use real local controller, SQLite and NumPy computations with synthetic numbers,
scripted LLM objects or mocked HTTP transport. They demonstrate routing, replan
constraints, evidence isolation and accounting, NOT better protein outcomes. Compare
fixed versus adaptive, rules versus LLM retrieval, and single versus multiple research
roles under matched total budgets in a separate scientific study. The one-Agent
ablation and statistically powered biological comparisons are not shipped benchmarks.
