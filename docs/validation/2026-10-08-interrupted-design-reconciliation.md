# One-case TrpB3A interrupted-design reconciliation

This is an operator recovery for one diagnosed process interruption, not an experimental result or a generic receipt-reset API. No scientific method, runtime source, assay output, measurement label, model identity, request, response, charge, or budget is changed by the utility.

## Diagnosis

The controller disappeared while an `agent:propose` operation waited for the
assistant bridge, leaving a `started` receipt. Ordinary resume correctly failed
closed with `uncertain_completion`.

The original design entry and evidence-v1 replay must reconstruct read-only to
the exact pending request and its original committed LLM charge. A valid mailbox
response must exist but remain unaccepted. The operation must have executed no
tool and produced no candidates. Campaign-specific proof and experimental
progress remain outside this code publication.

The utility is pinned to the complete diagnosed metadata/ledger/event snapshot, the exact runtime source fingerprint, step/run/scope, request identity, response bytes, model, and scientific operation implementation. Any state drift requires a new diagnosis; it is not automatically repaired.

## Utility and boundaries

`scripts/reconcile_interrupted_design.py` defaults to read-only dry-run. It opens the existing campaign lock without creating/truncating it, takes an exclusive nonblocking lock, and checks for an observable live controller. The database transaction and snapshot establish consistent proof state.

It reads only execution metadata and revealed TaskView context. It never selects measurement or batch values; empirical key presence alone is checked. Request/response files are bounded, regular, single-link, no-follow reads. Unexpected SQLite triggers reject. Runtime modules are imported only from the fingerprint-verified source.

Apply requires the exact dry-run proof SHA256. In one transaction, it inserts immutable original-receipt/proof evidence, compare-and-swaps exactly `started` to `paused_provider`, and appends an audit event. Any error rolls the entire transition back. It never deletes a receipt or asserts completion. Repeated apply is a no-op only while the expected reconciliation receipt and matching audit remain intact. If ordinary execution has advanced the receipt, inspect that execution separately.

Ordinary runtime resume remains responsible for accepting the existing response and continuing the design. The bridge recognizes the same pending request and reuses its charge. The reconciliation itself executes no tool and produces no scientific output.

## Operator sequence

1. Stop all campaign controllers. Preserve the existing mailbox files. Do not retry the blocked campaign before reconciliation.
2. Run the script with `--campaign`, `--runtime-root`, and `--mailbox`. All three paths must already exist. Review the complete dry-run proof and returned `proof_hash`.
3. Obtain approval for this exact diagnosed transition. The dry-run does not authorize applying it.
4. Invoke the same command with `--apply --expected-proof-sha256 <reviewed hash>`.
5. Verify the appended evidence and audit, unchanged ledger, and the paused receipt. Resume with the unchanged ordinary runtime and monitor the genuine assistant-led continuation.
6. If any validation or hash check rejects, stop. Do not loosen constants, delete receipts, reset charges, regenerate the response, or substitute another model to make it pass.

## Validation

`tests/test_reconcile_interrupted_design.py` includes disposable tests for read-only dry-run, explicit/stale proof, exact CAS, immutable evidence/audit, idempotency, live writer and orphan detection, trigger rejection, transaction rollback, and ordinary ProtocolExecutor/AssistantBridge reuse without a new charge or duplicate execution.

Additional opt-in semantic tests use `PROTEINRSI_RECONCILIATION_METADATA_FIXTURE`, pointing to a separately prepared directory marked `METADATA_ONLY_FIXTURE`. Only revealed context and explicit execution-metadata namespaces are copied there. Measurement/batch values are never selected; placeholders are used. Tests copy that fixture to fresh temporary directories. They exercise the real proof and reject altered source, request, response, context, input scope, tool state/effect, ledger, charge fingerprint, and audit drift. This local execution fixture is not part of the repository or a publishable scientific dataset.

No application to the actual campaign is implied by passing these tests.
