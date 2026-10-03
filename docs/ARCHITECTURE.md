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

TaskView includes only visible measurements. The full oracle table and its path never enter the agent context. Historical datasets remain evaluator-owned. A bounded candidate shortlist is sampled without phenotype labels when a finite library is large.

`Store` keeps campaign state, immutable batches/measurements, experiment trials, version archives, model/tool results, and audit events in SQLite. File locks serialize campaign writers; transactions make resource reservations atomic. Identical calls/imports are idempotent. An uncertain external call is not blindly repeated.

The three enforced resource limits are **experimental wells, LLM calls and tool calls**. Controls, replicates and failed submitted assays consume wells. API tokens are recorded when provided but are not yet a separately enforced dollar/token budget. CPU/GPU seconds and money require a deployment-specific scheduler/accounting adapter.

A cancelled unapproved batch releases its reservation. An approved batch is considered submitted and cannot be refunded by rolling back W/M. Previously acquired measurements remain available after any strategy rollback.

## Workflow trial

The old/new policies see the same evidence snapshot and have the same role-call/tool-call bounds. They submit equal numbers of unique variants, with a predetermined alternating collision rule and shared controls excluded from the efficacy comparison. The final batch can be smaller than the maximum when an odd slot or library exhaustion prevents equal allocation.

A single prespecified gate compares the arms. Technical replicates are aggregated by sequence. Missing assays are not zero. Excessive QC failures or insufficient independent variants produce `inconclusive`. The current workflow remains active unless a challenger is accepted. Each accepted method is stored with task/campaign context and `transfer_validated=false`.

Collision handling and correlated variants mean this is an exploratory policy trial, not a textbook independently randomized sample of all protein sequence space. Formal studies should prespecify scientific units, randomization, replicate structure, and independent confirmation.

## Meta evaluation

A queued meta patch is tested separately. Evaluator-owned cases supply a common initial W and visible data. Each frozen M proposes a descendant W; each descendant receives the same new measurement quota and call limits. Cases from the same group are aggregated before the paired comparison.

This isolates one-step improvement ability. A complete campaign additionally exercises accepted M successors over subsequent generations. These are distinct evaluation settings. A mechanism test with artificial data does not establish persistent scientific RSI.

The local trusted evaluator can read labels. This deployment is not secure against malicious Python sharing its OS permissions. See SECURITY.md before enabling outside code or scientific services.
