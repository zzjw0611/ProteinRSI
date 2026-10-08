# Reviewed interrupted-design reconciliation

`scripts/reconcile_reviewed_designs.py` is an outside-runtime operator command
for two independently diagnosed interruptions. It does not generalize or modify
the earlier `reconcile_interrupted_design.py` recipe. Packaged `src/` remains
identical to accepted runtime `40e1d56c3b3ebfb0b7da1ad8212101723b31958f`.

The two private recipes are accepted only by hardcoded canonical SHA256 pins.
Their identities, real context, response bytes and scientific records are not
published. There is no command-line override to trust arbitrary recipes. A new
case requires diagnosis, a separate code review, tests, and explicit approval.
Synthetic tests substitute recipe pins only inside disposable test processes.

## Proof and mutation boundary

A dry run takes the existing campaign lock, opens SQLite read-only, and verifies
an exact snapshot of every durable key, all events, the schema, ledger and limits.
This includes the outer scheduler, complete history, and empirical-row hashes.
Empirical values are hashed opaquely inside SQLite and are never returned,
parsed, printed, or used by the proof. No external measurement file is opened.
The dry-run output is private operational evidence, not a publication artifact.

The proof checks the frozen runtime, scientific TaskView scope and history,
operation implementation/arguments/output schema, pending bridge request and its
committed charge, original mailbox envelope and exact genuine response hash.
The response must be a schema-valid context-read action. It is never consumed.
Only a single-step running `agent:propose` journal with an exact `started`
receipt is supported. All tool jobs must be completed with matching content
fingerprints and charges. Unknown effects fail closed.

Reconstruction uses the original DesignerAgent, context engine and ToolGateway.
Store writes may only repeat identical immutable values. Charges and tool
executors are forbidden. One recipe permits no in-step tool effects; the other
requires precisely its reviewed completed generated-code jobs, matching bwrap
completion events and exact ToolGateway cache hits. Reaching a different request,
a cache miss, unfinished job, differing resource write, or unexpected effect
rejects the entire reconciliation.

Apply additionally requires the exact dry-run proof hash. Under the campaign
lock and one SQLite transaction it revalidates the full proof, inserts immutable
evidence preserving the original receipt bytes and their hash, compare-and-swaps
only that receipt to `paused_provider`, then appends an operator event. Any
failure rolls back. It never marks work done, deletes or resets receipts,
changes journals/scheduler state, resets charges, consumes the response, or runs
a scientific tool. Idempotency verifies the preserved receipt evidence and
single audit event; a progressed receipt is handed back for separate inspection.

## Operator sequence

1. Stop controllers and response relays at a verified safe boundary. Verify the
   accepted source and free locks. Preserve the original campaign and mailbox.
2. Run the command with existing `--campaign`, `--runtime-root`, `--mailbox` and
   the independently reviewed private `--recipe` path. Save and inspect its
   complete dry-run proof privately.
3. Obtain separate approval for that exact real campaign transition after
   independent review, disposable real-bwrap tests, build/lint and exact-head CI.
4. Repeat with `--apply --expected-proof-sha256` and the approved proof hash.
5. Independently verify the one receipt transition, immutable original evidence,
   single appended event, unchanged ledger/limits/history and empirical hashes.
6. Resume only through the unchanged runtime and original mailbox/model with
   explicit `PROTEINRSI_SANDBOX_BACKEND=bwrap`. Use the existing bounded bridge
   timeout so absent responses unwind normally as `ProviderPaused`.

The native scheduler may replan rather than resume the old design request. The
utility does not claim request reuse and never forces a response to be consumed.
Audit the actual continuation. An orphaned old request remains charged and
retained; it must not be deleted, refunded, or silently described as reused.
Stop on any source, state, response, job, cache or proof mismatch. Do not weaken
pins or manufacture a blocked journal to make recovery pass.

Passing tests or producing a dry run does not authorize a real campaign apply.
