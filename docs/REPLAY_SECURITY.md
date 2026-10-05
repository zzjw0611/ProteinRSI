# Historical replay trust boundary — v0.5

## Execution

`replay` defaults to `--execution guarded`. The trusted controller opens CSVOracle,
pins the dataset SHA256 and verifies supplied parent evidence. It creates batches,
approves/settles queries, supplies actual returned observations and performs promotion.
Each research operation runs in a **fresh exec'd Python process** with no inherited oracle
object, credentials, database descriptors or full-label dictionary.

The worker uses a capability RPC for LLM requests, allowlisted protein tool calls and
specific research-record reads/writes. It has no SQL or arbitrary file-read RPC, cannot
change campaign state/configuration, cannot debit/refund budgets, and cannot emit trusted
experiment/promotion events. Tools and LLM providers are chosen by the controller, not
by URLs or executable paths supplied by the worker.

Linux Landlock ABI >=1 restricts reads to Python/code dependencies and writes to an empty
scratch directory. No campaign DB or label directory is mounted in the worker. Seccomp
blocks networking, process-memory/ptrace/pidfd inspection, io_uring and common privilege
paths. Seccomp also blocks truncate/ftruncate, O_TRUNC opens, creat/openat2, rename/link,
metadata changes and ioctl. These compensating restrictions cover missing filesystem rights
on older ABIs; newer supported Landlock rights are enabled as well. Worker scratch writes use
non-truncating creation, and scientific output persistence goes through the trusted RPC.
Generated Python uses a separate, stricter worker described below; generated shell is not executed. Conda alone is not a sandbox.
The controller rejects label placement within dependency/source allowlist trees. Do not
place `.env`, private labels or other secrets inside installed packages or Python stdlib.

Requires Linux x86_64/aarch64, working Landlock and libseccomp (e.g. the distribution's
libseccomp2). Probe with `proteinrsi sandbox-check`. Missing primitives abort before
experiment/LLM calls; no automatic in-process downgrade. macOS/unsupported containers
can run explicit trusted debug fixtures with `--execution inprocess`, not an isolated
benchmark. Enforcement integration tests run on supported hosts, including the local ABI 1 server;
they are skipped on hosts missing Landlock/libseccomp. The matching interpreter shared
libraries are individually allowlisted for Conda compatibility.

## Limits

Trusted local engine code and the controller remain in the TCB. This is defence in depth,
not a proof against kernel bugs, malicious installed dependencies, compromised providers
or administrator access. LLM/tool requests still contain data; follow approved provider
policies. Artifact imports are operator-only and must not import label tables. Worker
namespaces expose only research artifacts and visible task metadata, not evaluation files.
The independent Meta evaluator also offers guarded teams; no prior server-specific
`agent_guard.py` was copied (its source was not available in this repository).

## Query semantics

Open design uses `candidate_access=open` with no candidates in TaskView. The replay
index stays private to the controller; there is no library sampling or membership tool.
Sequence validity comes from scientific task constraints, not historical availability.
After approval, each submitted sequence gets either its recorded assay result or
`qc=unavailable, value=null`. Missing coverage is neither assay failure nor low fitness.
Every submitted identity consumes a query slot, including unavailable records. This
bounds feedback requests; it does not claim a missing record is a completed wet experiment.
No automatic replacement, imputation or refund occurs. The agent sees the missing
identity in feedback and can adapt in later rounds; exclude-repeat applies to it too.

Explicit user libraries and ranking sets retain closed-library validation. Their
membership can be checked without fitness, and missing records are rejected before
approval. These are intentionally different tasks, never inferred from a replay file.
Natural-language open-design campaigns created with the old automatic catalogue
injection are rejected on replay resume; preserve them and start a new corrected study.

The GB1 natural-language launcher uses `provided_parent`: the known parent sequence,
measured fitness and source are supplied before the study, at zero new-query cost.
Replay validates this value against the pinned dataset before planning. It never invents
a parent fitness or supplies other unqueried phenotypes. With two rounds of 24 and a
48-query budget, 24+24 NEW variants may be queried, in addition to the one known parent.
Reports separate provided observations from purchased queries.

`parent_once` remains an explicit legacy policy for studies that did not already know the
parent measurement. It needs controls_per_batch=0, consumes one query inside round one,
does not increment the round, and is not repeated automatically. Research
and W trials use the same ledger. Meta offspring private stores delegate actual charges
to the main study, including initial disclosures. An attempted failed evaluation is recorded
and cannot silently restart on a fresh budget. Full science conclusions require an independent
held-out protocol; exploratory bootstrap is not an anytime-valid repeated-testing guarantee.

Sources for the OS primitives (not copied implementation):
https://docs.kernel.org/userspace-api/landlock.html
https://man7.org/linux/man-pages/man2/seccomp.2.html

## Online Meta validation and operator audit

New campaigns may validate MetaPolicy descendants prospectively in the next batch.
The controller creates two sponsored stores containing the same revealed view,
task constraints, any explicitly supplied ranking inputs, operator tool configuration
and registered scientific files. Open design has no replay catalogue in either arm.
Both arms use the same guarded execution path as the main study. Neither child
receives the oracle or future labels; only the controller measures the final batch.
Actual LLM/tool charges reach the main ledger, and the batch is charged once.
Any unavailable non-control trial result makes the comparison inconclusive, avoiding
promotion from selectively observed subsets. Acceptance is task-local and does not
certify transfer to independent proteins.

Operator snapshots, LLM request/response logs and mirrored validation logs remain
outside the worker RPC read/write allowlists. The standalone HTML viewer uses
textContent and escaped embedded JSON, and never opens the original label table.
The viewer is an operator inspection tool, not an agent resource. Provider-returned
summaries are saved only when present; raw hidden reasoning is not reconstructed.

## Generated Python tool

`research_python` creates a separate exec-based process with no controller RPC. It
receives only the current revealed TaskView, explicit JSON inputs and copies of
selected registered scientific artifacts. The same Landlock boundaries are applied
before compiling/executing generated source. Additional seccomp rules deny clone,
clone3 and resource-limit modification. CPU (10s), wall (20s), address space (2GB),
output-file size (1MB) and descriptor limits bound execution. Environment variables
are constructed from an allowlist and exclude provider/model credentials.

Generated code cannot approve experiments, access the database, read unknown labels,
spawn processes, install packages or connect to the network. The generated worker has no writable directory; bounded stdout/stderr descriptors
are its only file outputs. write_artifact returns bounded text over the result protocol.
Only the controller writes/registers returned scientific files after name, size and path checks. Python results, including
self-declared scores, remain unvalidated computations, never Observation rows. Failed
programs return bounded errors for the agent to repair; their tool charge remains.
Source hashes, source files and full gateway requests/results persist for audit.
This remains defence in depth on a trusted Linux kernel and installed dependencies,
not a proof against kernel vulnerabilities.
