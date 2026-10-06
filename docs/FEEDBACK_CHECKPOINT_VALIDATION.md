# Post-assay C feedback checkpoint validation

Date: 2026-10-06 (UTC). Scope: code-only controller fix, tested with synthetic fixtures. The validation used Python 3.12.14 and the existing development environment. No actual study results, hidden assay labels, paid-provider calls, credentials, or private experimental payloads were used.

## Contract and change

`MetaPolicy.enabled` gates the method improver. The governance pause likewise applies to automatic method improvement. Previously, `Campaign.consider_improvement()` returned on that pause before calling the post-assay analyst, so it also suppressed fresh `C-feedback`.

The controller now checkpoints C before checking the method pause. Successful C output and its history entry commit before M starts. The existing `considered_round` remains the M decision marker: a paused round can later receive its unconsidered M decision after an explicit idle-boundary resume, without repeating C.

The new protected, immutable `feedback_inputs` and `feedback_results` records contain the evidence/round identity, frozen input, method/provider provenance, actual response, and input digest. Trace exports include these records and completion references; sponsored stores retain the corresponding operator audit. Worker namespace permissions are unchanged.

Provider provenance uses a digest of the original semantic identity for compatibility checks and a separately redacted display record. Known credential strings accidentally placed in model names or URL paths are not copied into the exported provenance. Ordinary Authorization credentials are excluded from the identity digest, so a normal key rotation preserves the pending request. Different semantic identities cannot become compatible merely because their redacted display strings match.

No changes were made to trial gates, promotion criteria, governance thresholds, administrative enablement, budget limits, candidate validation, or scientific metrics. Existing provider-pause, definite-error, and uncertain-receipt handling remains in place. No missing historical C responses are synthesized. Existing successful C history can be reused without generating a historical checkpoint; previously considered and terminal rounds remain no-ops. Bound source snapshots still reject execution under changed code/dependencies.

## Regression coverage

`tests/test_feedback_checkpoint.py` covers:

- One C success for fresh evidence while governance is paused, with no M call; repeated prepare, cancellation/replanning, restart, and new evidence
- Explicit governance resume using saved C for the still-unconsidered M decision
- Provider pause during C and after C during M, explicit retry authorization, identical request bytes, preserved charges, and no repeat of successful C
- Definite failures and unknown completion receipts, retaining existing decision/failure semantics and preventing another network call for an uncertain receipt
- Success in the provider cache before controller checkpoint creation, and an injected transaction failure between provider success and checkpoint commit
- Legacy successful history and legacy considered-round markers without fabricated backfill
- Terminal reimport/prepare no-ops, administrative M disablement, and unchanged snapshot compatibility enforcement
- Immutable/protected checkpoints, exact trace references, sponsor accounting/audit, and guarded proxy pause receipts
- Rejection of changed model, output-token allowance, protocol, or reasoning settings for unfinished C; correction of the local input-byte cap while preserving the request
- Reuse of frozen C context when non-evidence artifacts change during a pause
- Redaction of synthetic secrets misplaced in model names or URL paths, distinct fingerprints despite identical redacted display, and ordinary key rotation during preflight/retryable pauses

## Commands and results

```text
.venv/bin/python -m pytest -q \
  tests/test_feedback_checkpoint.py \
  tests/test_llm_preflight.py \
  tests/test_feedback_improvement_recovery.py \
  tests/test_method_provider_pause.py \
  tests/test_governance.py \
  tests/test_method_transactions.py \
  tests/test_trace_export.py \
  tests/test_sponsored_predictions.py
137 passed, 5 skipped

.venv/bin/python -m pytest -q
610 passed, 23 skipped

.venv/bin/ruff check .
All checks passed!

.venv/bin/python -m compileall -q src tests
Passed

git diff --check
Passed

.venv/bin/python -m build --outdir /tmp/proteinrsi-feedback-checkpoint-dist
Built sdist and wheel using the standard isolated build environment
```

The build uses isolated `setuptools` rather than installing it into the shared development environment. No dependency configuration was changed. Its existing manifest warning about absent `configs/*.yml` does not prevent either artifact from building. Build outputs are local validation artifacts and are not part of this code-only change.

## Limits

The full-suite skips comprise 21 sandbox-dependent cases and 2 ESMC cases (missing optional Torch and the explicitly opt-in real pretrained checkpoint). Four new guarded-worker integration variants require unavailable Landlock isolation and therefore skip; guarded proxy/error-receipt and namespace-permission tests pass. No unguarded substitution is claimed as isolation validation.

These tests establish control flow, persistence, budget/cache behavior, and audit coverage. They do not establish protein efficacy, a real-provider integration result, or missing C coverage in an already-running or archived study. Pinned studies were neither migrated nor rerun. This fix must not be hot-swapped into an old bound engine snapshot.
