# W-transfer experiment archive

## Status and scope

This directory is an archive scaffold only. It contains no current W-transfer run artifacts, executed manifest, recovered method snapshots, or new experimental results. The previous W-transfer pilots' original artifacts are lost and unrecoverable; they must not be reconstructed or presented as recovered snapshots. Any later regenerated candidate is a new candidate in a new run, with its own provenance.

The scaffold is based on source commit `c7de6bf5995d513a9682f6ef8df2558878e3eb0e`. Other historical experiment directories inherited from that source are outside this archive and are not evidence of recovery of these pilots.

Archiving is authorized on `experiments/w-transfer-archive` only. This does not authorize a new run, model call, experimental budget, code modification, pull request, or merge into main. The template is unexecuted; null values mean unknown or not yet specified, never measured zero.

## Prospective archive policy

- Give each newly authorized run a unique ID and record its authorization and approved budget before execution. Use `runs/<run-id>/`; never reuse a lost pilot's ID.
- Before collecting source outcomes, preregister source-selection rules, source/target split, target holdout, evaluation metrics, seeds, query allocations, stopping rules, and decision criteria. Record exposure to any already-seen target outcomes.
- Export complete W and M method state, including prompts, policies, configurations and referenced dependencies, with canonical serialization details and SHA-256 file hashes. A descriptive summary is not a method snapshot. Remove secrets and forbidden data before export; record exclusions explicitly.
- Freeze W0 and W1 as separately hashed immutable snapshots before target testing. Target evaluation uses M off, with no inline controls or target-driven method edits. Preserve the preregistered paired evaluation design and record deviations rather than silently changing it.
- Record exact code commit/tree, environment/dependency versions, dataset identity and content hashes, split IDs, model/version, tool/schema versions, parameters, seeds, approved and consumed budgets, timestamps and artifact inventory.
- After every round, append an immutable checkpoint with complete W/M exports and hashes, the ordered revealed queries and results only, and sanitized request/response tool receipts, errors, retries and budget accounting. Commit and read back the checkpoint before starting the next round.
- Preserve original checkpoint files. Corrections, interrupted writes, resume decisions and recovery events are new append-only records referencing prior checkpoint/commit/hash; never overwrite or invent a missing receipt or snapshot. Mark incomplete artifacts and unknowns explicitly.
- Archive final summaries only alongside the actual underlying artifacts and hashes. A later summary cannot repair missing provenance.

## Exclusions and verification

Do not add hidden labels, hidden outcomes, complete data tables, secrets, tokens, credentials, or external endpoint URLs/configuration. Revealed query/result subsets may be archived only to the extent authorized and must not expose unrevealed rows. Keep dataset hashes/identifiers instead of full tables; sanitize tool receipts and method exports while documenting every exclusion.

Before each archival commit, check the file inventory and content for prohibited data; verify SHA-256 hashes, Git commit ancestry, expected archive branch head, and remote readback. Update this branch by fast-forward only with an expected-head lease. Never force-update refs. Verify main's SHA is unchanged. This document states a prospective policy and manual procedure; no automatic checkpointing or periodic archival is implemented. Future orchestration must perform and verify the remote copy after each round.

Copy `manifest.template.json` into a newly authorized run, fill it from actual evidence, and keep the template's unexecuted status until execution is separately approved. Do not populate placeholders with inferred historical values.
