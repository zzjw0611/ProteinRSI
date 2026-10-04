# Historical replay trust boundary — v0.5

## Execution

`replay` defaults to `--execution guarded`. The trusted controller opens CSVOracle,
checks the eligible catalogue and pins the dataset SHA256. It creates batches,
approves/settles queries, supplies actual returned observations and performs promotion.
Each research operation runs in a **fresh exec'd Python process** with no inherited oracle
object, credentials, database descriptors or full-label dictionary.

The worker uses a capability RPC for LLM requests, allowlisted protein tool calls and
specific research-record reads/writes. It has no SQL or arbitrary file-read RPC, cannot
change campaign state/configuration, cannot debit/refund budgets, and cannot emit trusted
experiment/promotion events. Tools and LLM providers are chosen by the controller, not
by URLs or executable paths supplied by the worker.

Linux Landlock ABI >=3 restricts reads to Python/code dependencies and writes to an empty
scratch directory. No campaign DB or label directory is mounted in the worker. Seccomp
blocks networking, process-memory/ptrace/pidfd inspection, io_uring and common privilege
paths. No arbitrary generated Python or shell is executed. Conda alone is not a sandbox.
The controller rejects label placement within dependency/source allowlist trees. Do not
place `.env`, private labels or other secrets inside installed packages or Python stdlib.

Requires Linux x86_64/aarch64, working Landlock and libseccomp (e.g. the distribution's
libseccomp2). Probe with `proteinrsi sandbox-check`. Missing primitives abort before
experiment/LLM calls; no automatic in-process downgrade. macOS/unsupported containers
can run explicit trusted debug fixtures with `--execution inprocess`, not an isolated
benchmark. The development container returns ENOSYS for Landlock; enforcement integration
tests are marked skipped. Run the guarded test suite on the actual execution machine.

## Limits

Trusted local engine code and the controller remain in the TCB. This is defence in depth,
not a proof against kernel bugs, malicious installed dependencies, compromised providers
or administrator access. LLM/tool requests still contain data; follow approved provider
policies. Artifact imports are operator-only and must not import label tables. Worker
namespaces expose only research artifacts and visible task metadata, not evaluation files.
The independent Meta evaluator also offers guarded teams; no prior server-specific
`agent_guard.py` was copied (its source was not available in this repository).

## Query semantics

A required eligible sequence catalogue is checked before planning, using existence only.
The full batch is checked before approval so an unavailable identity is not approved or settled as an experimental query and yields no invented fitness.
A previously prepared batch may hold a reservation; no silent refund of an approved query occurs. No automatic imputation or random
replacement is performed. Catalogue membership isn't a fitness score.

`parent_once` needs controls_per_batch=0. Initial mother lookup consumes one shared query
inside round one, does not increment the round, and is not repeated automatically. Research
and W trials use the same ledger. Meta offspring private stores delegate actual charges
to the main study, including initial disclosures. An attempted failed evaluation is recorded
and cannot silently restart on a fresh budget. Full science conclusions require an independent
held-out protocol; exploratory bootstrap is not an anytime-valid repeated-testing guarantee.

Sources for the OS primitives (not copied implementation):
https://docs.kernel.org/userspace-api/landlock.html
https://man7.org/linux/man-pages/man2/seccomp.2.html
