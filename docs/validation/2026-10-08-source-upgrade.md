# Explicit final-selection source upgrade

## Scope and identities

This operator procedure repairs implementation provenance. It is not an RSI
adoption, scientific improvement, E verdict, budget reset, or generic migration.
The only supported runtime change is commit
`25312ffea4bde4241707a73216a8f13a0703682d` to
`40e1d56c3b3ebfb0b7da1ad8212101723b31958f`. All packaged files are hash checked;
only `dataflow/design.py` and `dataflow/integration.py` may differ. Python,
recorded dependency versions, bwrap identity, method definitions, configuration,
and acceptance policy must remain identical. These operator scripts live outside
`src/`; this commit does not change the 40e1 packaged runtime.

No historical source asset, snapshot, activation, batch, observation, request,
receipt, cache, budget limit, or charge is rewritten. Application atomically
appends new source assets, W/M snapshots, source-qualified activation records,
an attributed upgrade receipt and event, then changes the active snapshot
pointers. Scientific W/M version IDs stay unchanged. A complete database dry-run
fingerprint protects against concurrent or stale application. Original and new
source pins coexist. The audit must describe the resulting campaign as using
mixed software versions, even when all historical submitted panels were valid.

## Before execution

1. Obtain explicit operator authorization for this campaign and exact repair.
   A free-text authorization reference records approval; it cannot grant it.
2. Stop its controller and mailbox responder at a verified safe boundary. Keep
   immutable old and new source checkouts and the original Python environment.
   Do not copy, delete, or modify caches to make a preflight pass.
3. Verify exact-head CI and independent review of these scripts. Preserve a
   recoverable database backup using SQLite's supported backup facility while
   controllers are stopped, plus existing source manifests and receipts.
4. Keep the original model, mailbox directory, explicit
   `PROTEINRSI_SANDBOX_BACKEND=bwrap`, dependency environment and limits.
   Do not expose the mailbox or prepared measurements to sandbox allowlists.

## Complete an existing C checkpoint, then stop

An unfinished C checkpoint still contains the old method-snapshot provenance.
Complete it under the original 25312 runtime before source upgrade. Do not call
normal replay or `consider_improvement`: those may request M or plan a new batch.
The bounded driver invokes only the canonical `_checkpoint_feedback` entry point
and rejects any role other than C-feedback. It preserves the existing bridge
scope, model, request envelope and settled call charges. It can consume normal
C evidence-read/note actions and a final C response; the scientific assistant
must decide the response. The driver never supplies one. Before any unresolved call, it verifies the exact
existing bridge envelope, database key and settled charge. Earlier cached
context frames may be replayed; later C frames must stay in that same frozen
context session. A changed prompt/session cannot start a replacement decision.
The fresh guarded team binds its tools to the frozen view using canonical
`Team.bind_tools`; this restores per-view registrations such as
`research_metric_extract` without executing tools or changing source.

Use absolute paths for the reviewed scripts, old source, original mailbox and
campaign. `PYTHONPATH` must point to the old `src` and the original dependencies:

```sh
PYTHONPATH="$OLD_SRC:$ORIGINAL_DEPS" python "$SCRIPTS/complete_source_upgrade_feedback.py" \
  --campaign "$CAMPAIGN"
# Review this read-only result; use its exact preflight_sha256 after approval.
PYTHONPATH="$OLD_SRC:$ORIGINAL_DEPS" python "$SCRIPTS/complete_source_upgrade_feedback.py" \
  --campaign "$CAMPAIGN" --complete --expected-preflight "$C_PREFLIGHT" \
  --operator "$OPERATOR" --reason 'Finish the existing C checkpoint before the authorized repair'
```

A provider pause preserves normal receipts and charges. Reinspect before retry;
the expected fingerprint changes when events/requests change. A completed
checkpoint returns `already-complete` without new calls. Confirm the same round,
experimental/tool usage, limits, observations and bridge scope; no M/planning
request may have appeared. The driver does not migrate any source pin.

## Dry-run and apply the exact source upgrade

Now import the unchanged 40e1 target runtime. The default command opens the
existing SQLite database read-only and never constructs a writable Store.
It refuses unfinished C, active or unknown journals, drafts, reservations,
batches, patch/meta proposals, and any formal trial/evaluation/metric namespace.
An earlier rejected bridge-attempt receipt is deliberately unsupported even if
a later corrected response succeeded; do not delete it to make preflight pass.
Such a campaign needs separate review rather than this narrow repair path.

```sh
PYTHONPATH="$NEW_SRC:$ORIGINAL_DEPS" python "$SCRIPTS/upgrade_final_selection_source.py" "$CAMPAIGN"
# Review all old/new source and snapshot refs, round, evidence and caps.
# Apply only after the operator approves that exact dry-run result.
PYTHONPATH="$NEW_SRC:$ORIGINAL_DEPS" python "$SCRIPTS/upgrade_final_selection_source.py" "$CAMPAIGN" \
  --apply --expected-digest "$UPGRADE_DIGEST" --operator "$OPERATOR" \
  --reason 'Authorized final-B-selection implementation repair' --authorization-ref "$APPROVAL_REF"
```

The lock and SQLite transaction revalidate the fingerprint, hashes and backend.
Conflicts fail closed; partial inserts roll back. An exact same authorized retry
returns `already_applied`. Keep the receipt and compare all historical rows,
charges and limits to the preserved snapshot. Resume normal guarded replay only
under the target runtime after the operator releases the campaign. Completed C
is reused; M and the next round run with the new source-qualified method pins.
Do not change frozen configuration or pretend this was a scientific adoption.

## Source-qualified rollback

The original version-only activation index still points to old snapshots.
Consequently normal rollback to the pre-upgrade baseline may correctly reject
its obsolete source pin. The utility offers a separate, narrow rollback to this
upgrade's new-runtime baseline after a verified later workflow or online-meta
adoption:

```sh
PYTHONPATH="$NEW_SRC:$ORIGINAL_DEPS" python "$SCRIPTS/upgrade_final_selection_source.py" "$CAMPAIGN" \
  --action rollback --upgrade-receipt "$RECEIPT" --target workflow
# Following separate approval, repeat with --apply and that dry-run digest,
# operator, reason and authorization-ref.
```

This is never an executable downgrade or undo of observations/budget. Completed
adoption/evaluation evidence is verified and retained. Active, uncertain,
orphaned or unknown evidence fails closed. Offline-meta rollback is unsupported.

## Validation status

Synthetic tests cover authentic-shape bridge envelopes, the real mailbox factory,
read/pause/final-response continuation, exact envelope/charge tampering, an actual
old-runtime subprocess with bwrap C completion (including fresh metric-tool
registration), multi-action evidence context, historical M lineage, complete C-to-upgrade flow, source/runtime/config
and backend tampering, immutable history and limits, stale fingerprints,
transaction failure, idempotency and source-qualified rollback. Test replies and
measurements are synthetic; they do not establish real scientific validity.
Real application requires separately approved campaign-specific preflights.
