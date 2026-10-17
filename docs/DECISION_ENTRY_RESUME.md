# Fresh-campaign decision-entry checkpoints

New campaigns initialize the immutable configuration marker
`decision_entry_semantics = {"version": "decision-entry-v1"}`. Campaigns without
this marker retain their historical behavior. Existing measured or interrupted
campaigns must not be silently moved to the new executable. Start a new campaign
and retain its old audit when adopting these semantics.

## Problem and boundary

Under state-v1, Campaign.view includes live execution counters and durations.
Restarting after a provider/bridge pause previously rebuilt a different TaskView
because that pause had itself produced new transport receipts. Tool scopes,
catalogues and context-session identities could consequently change despite no
change to the scientific inputs. Python hash randomization is not the cause.

Before prepare or M improvement, the controller now saves the exact initial
TaskView for the logical decision. Only execution_summary is excluded from the
entry's lookup identity. The original summary remains present in the saved model
input; it is a decision-entry receipt snapshot, not a claim about current cost.
Campaign.view, status, event logs and charge ledgers remain live. Budget and
permission checks continue to use controller authority, never the saved summary.

Every other TaskView field remains in the entry identity, including observations,
working memory, task constraints, artifacts, W/M and contextual requests. All
configuration, normalized context-bound tool descriptions, egress setting,
packaged executable/dependency identity, deployed protein-model identity and LLM
settings also participate. The phase and planning attempt are explicit. These
changes invalidate the entry rather than reusing an old scientific decision.
No scope_for or context-session validation is weakened.

Binding the normalized tool catalogue performs no tool calls or inference.
Only the presentation of execution_summary is frozen. New observations, rounds,
methods, memory, artifacts or configuration create new entries. Existing C
feedback input checkpoints continue to use their dedicated exact-input path.

## M continuation

For decision-entry-v1, ProviderPaused from optional M improvement propagates as a
continuation. It does not mark M scientifically unsuccessful, record a no-change
answer, pause governance, consume the round's M opportunity, or replenish any
budget. An actual schema-valid M answer is still required. Historical campaigns
retain their existing optional-improvement pause behavior.

## Verification scope

Synthetic tests exercise scope/catalogue stability after transport events,
separate Python processes/hash seeds, context query and bridge pause/resume with
exact charging, observations/W/M/task/memory/config/tool/phase invalidation,
legacy behavior, live budget refusal and real MetaAgent no-change continuation.
No provider APIs, empirical datasets, model weights or experimental queries are
used by these tests. Transport fixtures establish resumability, not scientific
validity or model identity.
