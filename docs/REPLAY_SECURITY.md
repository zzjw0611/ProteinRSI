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

## Explicit bubblewrap alternative (bwrap + seccomp)

Landlock remains the default. On a supported Linux host, an operator may explicitly
set `PROTEINRSI_SANDBOX_BACKEND=bwrap` for both `sandbox-check` and the entire campaign
controller command. There is **no automatic fallback** after either backend fails.
Unknown selections, missing binaries, failed mounts, failed seccomp synchronization,
or failed enforcement probes abort before generated code or model/tool spending.

The alternative keeps fresh interpreter workers, sanitized environment, closed inherited
file descriptors, controller-only oracle/DB/provider authority and the same bounded
capability RPC. Bubblewrap must be an unprivileged executable; the backend neither
requires setuid nor changes host security settings. Its mandatory user/mount/PID/IPC/UTS
namespaces expose only the existing runtime read allowlist, the exact interpreter and
loader aliases, and `/dev/null`. The newly constructed root and all mountpoint parents
are read-only. There is no `/proc` or host home/project/campaign mount. Replay has one
writable scratch directory; generated Python has no writable directory. Labels and
campaign storage must remain outside dependency/source allowlist trees.

Networking is denied by the same syscall controls as Landlock. This backend deliberately
does **not** request a network namespace or assume that the host supports its setup.
The shared filter retains every original denial and additionally denies clone/clone3,
resource-limit changes and prctl for alternate-backend replay workers as well as generated
workers. Mandatory seccomp thread synchronization prevents an existing thread from
escaping the filter; scientific imports happen afterwards. No `/proc` mount is added
merely to count threads. Read-denial errors may be ENOENT; read-only writes may be EROFS,
instead of Landlock's EACCES/EPERM.

The production probe launches both actual mount policies and applies their actual
seccomp filter, then checks synthetic private/read-only canaries, network/process
creation, root writes, truncation and scratch rules. It does not execute experiment
code. Before running a campaign, also run the mandatory adversarial acceptance suite:

```sh
export PROTEINRSI_SANDBOX_BACKEND=bwrap
proteinrsi sandbox-check
PROTEINRSI_REQUIRE_BWRAP=1 PROTEINRSI_REQUIRE_METRIC_SANDBOX=1 \
  pytest -q tests/test_bwrap_isolation.py tests/test_custom_metric_execution.py \
  tests/test_general_research.py tests/test_study_protocol.py
```

Generated workers retain CPU 10s, wall 20s, address-space 2GB, file-output 1MB and
64-descriptor hard limits. Their bwrap supervisor and namespace reaper are bound to the
controller lifetime; the Python worker additionally binds itself to its inner parent
before the filter denies modifications. Controller interruption/timeout must terminate
the actual inner worker, not merely a supervisor. As before, guarded replay uses bounded
RPC messages and a per-response timeout; it does not claim the generated-code CPU/memory
limits apply to the full research/team workflow.

Audit receipts distinguish `bwrap_seccomp_generated_v1` from
`landlock_seccomp_generated_v1`. Frozen metric runtimes additionally pin the exact bwrap
path, binary bytes, version and policy implementation. Historical supported receipts
remain readable without installing their old backend. Executing unfinished frozen
metrics requires the exact original runtime; changing a backend/binary/policy pauses
execution, and never relabels or automatically revalidates a measured trial. Replay
stores an immutable backend identity too; an existing unpinned legacy replay with
batches cannot be silently moved to bwrap. Preserve the old campaign and start a new one.

These guarantees assume a trusted kernel, installed runtime dependencies and controller.
This is defence in depth, not a kernel-exploit sandbox or a claim of scientific validity.
