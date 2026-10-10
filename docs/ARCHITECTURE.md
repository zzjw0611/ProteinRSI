# Current architecture (v0.5)

## Current mutation-only research path

See [MUTATION_RSI.md](MUTATION_RSI.md) for the current W/M architecture, autonomous
Meta tool dialogue, retained sandbox programs and actual-successor-use audit.
The sections below document the retained general infrastructure; they are not a
requirement to run multiple protein tasks for this project.

The new optional typed inner-loop runtime is documented in [RESEARCH_RUNTIME](RESEARCH_RUNTIME.md).
New CLI tasks enable it by default; pre-v0.5 campaigns are readable but require their original
executable to continue. New runs snapshot prompt templates and on-demand execution semantics.
A resource selector, explicit plan ledger and read-only analyses are shared by A/B/C;
no new permanent Agent, NIM service or unrestricted interpreter is added. M and the
trusted experiment/promotion boundaries are extended with guarded replay and shared Meta charging. [REUSE](REUSE.md) records
what was actually adapted/imported versus merely inspired by other projects.

The following sections retain the baseline architecture and still describe its
scientific/security boundaries; fixed A→B→C ordering applies to fixed mode.

# Architecture and invariants

## One durable campaign, four roles

`runtime.Campaign` owns phase transitions. `Team` composes Principal, Designer and Analyst; `MetaAgent` proposes patches. The native CLI is a persistent reference runner. `integrations.langgraph.build_graph` builds a real `StateGraph` around the same controller, with interrupts for approval and measurement. No nested upstream agent loops are required.

```
ready -> awaiting_approval -> awaiting_results -> ready/complete
          | cancel
          +-> ready (new planning attempt; no fake measured round)
```

A round boundary permits a new workflow version. A submitted batch records immutable workflow, meta and evidence versions; it is never retroactively reinterpreted under a newer workflow.

## Trusted vs evolvable

`Workflow` contains role prompts, modeling/selection parameters, Skill names and allowlisted tool names. `MetaPolicy` contains trigger thresholds, diagnostic mode, cooldown and the actual improvement prompt. Patches are typed changes against an exact content-addressed base version. No `exec`, arbitrary module import, or agent-selected shell command is used.

`GatePolicy`, task objectives, target identities, budget limits, raw observations and publication rules are outside both bundles. M cannot self-enable an administratively disabled improver. Successor publication is carried out by trusted evaluator code, never by loading a claimed score from arbitrary JSON.

## Data and budgets

TaskView includes only visible measurements. The full oracle table and its path never enter the agent context. Formal `replay` defaults to a
fresh guarded research worker with controller-mediated tool and LLM RPC; unsupported kernels fail closed. Historical datasets remain evaluator-owned. A bounded candidate preview is sampled without phenotype labels when a finite library is large.
With `candidate_access=catalogue`, it is not a restriction on legal proposals: label-free
`library_check`/`library_sample` expose existence/identities, and submission uses the full catalogue.

`Store` keeps campaign state, immutable batches/measurements, experiment trials, version archives, model/tool results, and audit events in SQLite. File locks serialize campaign writers; transactions make resource reservations atomic. Identical calls/imports are idempotent. An uncertain external call is not blindly repeated.

Base resource limits are **experimental wells, LLM calls and tool calls**. ESMC campaigns additionally enforce **plm_inputs** for uncached embedded or masked sequence examples. Controls, replicates and failed submitted assays consume wells. API tokens are recorded when provided but are not yet a separately enforced dollar/token budget. CPU/GPU seconds and money require a deployment-specific scheduler/accounting adapter.

A cancelled unapproved batch releases its reservation. An approved batch is considered submitted and cannot be refunded by rolling back W/M. Previously acquired measurements remain available after any strategy rollback.

## Workflow trial

The old/new policies see the same evidence snapshot and have the same role-call/tool-call bounds. They submit equal numbers of unique variants, with a predetermined alternating collision rule and shared controls excluded from the efficacy comparison. The final batch can be smaller than the maximum when an odd slot or library exhaustion prevents equal allocation.

In new `llm_adjudicated_v1` studies, a separate LLM evaluation call declares criteria and Top-N choices before held-out results. The plan also supplies versioned metric code, definitions, JSON I/O contracts and synthetic fixtures. The existing isolated Python worker validates fixtures before freezing, then computes task-specific metrics over immutable scoped observations. Dynamic output layouts normalize to MetricTable; E-verdict and reports cite the exact same content-addressed artifact. Controller-owned missing/QC counts accompany it, with no scientific fallback. The LLM then gives the explicit adoption verdict against its frozen plan; code does not turn a metric threshold, direction, weight, Pareto comparison or missing-value count into that verdict. Invalid schemas or evidence identities pause evaluation. Historical fixed gates retain their recorded semantics. The current workflow remains active unless a valid evaluation accepts a challenger. Each accepted method is stored with task/campaign context and `transfer_validated=false`.

Collision handling and correlated variants mean this is an exploratory policy trial, not a textbook independently randomized sample of all protein sequence space. Formal studies should prespecify scientific units, randomization, replicate structure, and independent confirmation.

## Meta evaluation

A queued meta patch is tested in separate child caches but under the same sponsor study ledger.
Initial disclosures and subsequent queries from both arms are charged, as are actual LLM/tool/PLM calls.
No evaluation can reset the main query cap; failed evaluation attempts remain recorded. Evaluator-owned cases supply a common initial W and visible data. Each frozen M proposes a descendant W; each descendant receives the same new measurement quota and call limits. Cases from the same group are aggregated before the paired comparison.

This isolates one-step improvement ability. A complete campaign additionally exercises accepted M successors over subsequent generations. These are distinct evaluation settings. A mechanism test with artificial data does not establish persistent scientific RSI.

The local trusted evaluator can read labels. This deployment is not secure against malicious Python sharing its OS permissions. See SECURITY.md before enabling outside code or scientific services.

## Native protein backbone in v0.2

Operator-only `configuration/protein_model` selects ESMC-600M. The exact resolved
HF snapshot is frozen in `protein_backend/snapshot`. Neither is in a Workflow or
MetaPolicy patch. `Team` loads the backend lazily, registers its local tools and
includes its identity (plus conversational provider/model) in team result caches.
In LLM mode, no scientific model is automatically executed. C can rank without models,
or explicitly request ESMC priors or `research_fit_predict`. Only the separately labeled
deterministic numerical baseline retains automatic pre-ranking.
The PLM embedding cache never substitutes for new experimental measurements.

The experiment controller and graph continue using the same operations. All
offspring-evaluation stores receive identical protein configuration/checkpoint
and model-input limits. Start new campaigns to use v0.5 semantics; initialization is not an in-place experiment migration.

## Operational details

See [PROMPTS](PROMPTS.md), [TOOL_CONTRACTS](TOOL_CONTRACTS.md) and
[REPLAY_SECURITY](REPLAY_SECURITY.md). The user-supplied SVG in README is an
architecture/intent overview, not a claim of wet-lab or cross-protein RSI validation.
