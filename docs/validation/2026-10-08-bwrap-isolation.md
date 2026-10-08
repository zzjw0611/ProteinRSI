# Explicit bwrap/seccomp backend validation — 2026-10-08

Base: main `0d71222cf71e9bb775692111a6bbaa7606ab998c`.
Scope: code, tests and documentation only. No live-provider calls, scientific runs,
new measurements, hidden datasets or scientific results were published by this change.

## Local results

- Default backend, full suite: **960 passed, 43 skipped**, 171.42 seconds.
  This host returns ENOSYS for Landlock. Its unavailable enforcement/integration paths
  were skipped honestly; the default never downgraded itself to bwrap or in-process.
- Explicit bwrap backend, full suite: **997 passed, 6 skipped**, 317.93 seconds.
  The remaining skips are missing PyTorch, opt-in real ESMC weights, GEPA, LangGraph,
  Biopython, and the intentionally Landlock-only direct-enforcement test.
- Required real alternative-backend acceptance module: **19 passed**, 42.54 seconds.
- New frozen-provenance unit cases: **49 passed**. These simulate receipts and are not
  evidence of kernel enforcement. The full bwrap suite separately exercises actual
  custom-metric execution, fixture freezing, durable reuse, online/offline report
  integration, failures and resource bounds.
- Independent focused review/rerun: **55 passed**, including both inner-lifetime tests,
  corrected compatibility tests and frozen provenance. No remaining reviewed blocker.
- Ruff and `git diff --check`: passed.
- Wheel and source distribution: built successfully with `python -m build --no-isolation`.

Commands (with the repository and test dependencies available to the interpreter):

```sh
python -m pytest -q
PROTEINRSI_SANDBOX_BACKEND=bwrap PROTEINRSI_REQUIRE_BWRAP=1 \
  PROTEINRSI_REQUIRE_METRIC_SANDBOX=1 python -m pytest -q
PROTEINRSI_SANDBOX_BACKEND=bwrap python -m proteinrsi sandbox-check
python -m ruff check .
python -m build --no-isolation
```

The tested runtime was CPython 3.12 with NumPy 2.3.5 and bubblewrap 0.12.0.
Bwrap executable SHA256:
`573236e5328ac2ebb08f59ae3a9805b4f8d12bdef14be8af4450d5463294985f`.
The actual production preflight passed both replay scratch-writable and generated
scratch-read-only policies, each checking nine concrete denials/constraints.

## What was actually exercised

Read-only runtime mounts, a read-only constructed root and mountpoint parents,
private-file/DB/symlink canaries, no `/proc`, the individual `/dev/null` mount,
network socket variants and raw process/execution syscalls, resource-limit mutation,
synthetic inheritable file/pipe/socket descriptor and environment secrecy, permitted
NumPy/artifact inputs and replay scratch writes, bounded memory/descriptors/stdout/
stderr/CPU/wall time, and actual missing-binary/setup/direct-invocation failures.
Output-file enforcement produced the precise EFBIG error at the 1 MiB hard limit.

The host process view does not expose nested worker PIDs reliably. Lifecycle acceptance
therefore uses a process-owned POSIX record lock acquired by isolated PID 2 while it
sleeps for eight seconds. An independent host descriptor verifies contention before
controller SIGKILL/SIGINT and acquires the lock within three seconds afterward. It
retains the inode across controller cleanup: supervisor exit or path deletion alone
cannot satisfy the test. The sleeping inner program never voluntarily unlocks it.

A network-namespace smoke test was unsupported on this host (NETLINK_ROUTE socket
creation returned EPERM). The implemented policy does not request that operation:
network denial is the same seccomp control used by the original backend. No host
security setting was changed and no privileged setup or permissive fallback was used.

## Scope and limitations

Both backends assume trusted controller/runtime dependencies and kernel. The bwrap
child verifies launch invariants; the trusted launcher is responsible for constructing
the exact mount policy. This is defence in depth, not a kernel-exploit security proof.

Generated code retains its strict CPU/memory/output/descriptor/wall/lifetime limits.
Replay retains its existing bounded RPC protocol and per-response timeout; its scratch
and stderr do not acquire the generated-worker resource limits. This distinction is
explicit rather than being claimed as newly solved.

Historical metric receipts remain readable. Changing backend, executable bytes/version,
mount policy or syscall policy pauses unfinished frozen execution. Replay pins the same
backend identity. Existing Landlock behavior is preserved; actual Landlock enforcement
still needs a supporting host. Local results do not substitute for the remote CI status
of the exact published commit, optional engine tests or a live scientific experiment.
