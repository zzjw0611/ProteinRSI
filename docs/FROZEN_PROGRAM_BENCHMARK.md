# Frozen program selection diagnostic (v1)

This opt-in, assistant-authored deterministic mechanism diagnostic is a new
executable policy family. It does not compile or evaluate an existing prompt-W,
and does not demonstrate autonomous M self-modification or biological efficacy.
The policy code was informed by previously revealed development evidence. Its
publication does not make it an untouched or confirmatory comparison.

## Opt-in and isolation

Normal Team behavior is unchanged unless an explicit immutable
`frozen_program_benchmark` configuration binding exists. The runner is inert
without `--execute` (Python API: `execute=False`). A supplied case matrix must
pin task, dataset and MethodProgram files by SHA-256; it is provided separately.
No empirical case matrix, task snapshots, datasets, observations, trajectories,
checkpoint or run result is included in this milestone.

```bash
python scripts/run_frozen_program_matrix.py --matrix MATRIX.json --out OUTPUT --execute
```

The runner imports the installed `proteinrsi` package. Install this repository
revision before use. Its source bundle, runner, adapter, matrix and programs are
pinned in an output manifest; a changed identity rejects resume. Output uses a
non-blocking controller lock. Historical experiment copies are not modified.

Each matrix row has exactly these keys: `case_id`, `landscape`, `arm`, integer
`seed`, `task_file`, `task_sha256`, `dataset`, `dataset_sha256`, `program_file`,
`program_sha256`. Paths refer to user-supplied local files. Task input requires
open replay, one explicitly supplied free parent, and synthetic or measured
replay feedback. Effective limits are eight rounds of 16 distinct new candidates,
128 additional paid wells, zero LLM calls and at most 16 tool calls. Successful
completion requires exactly one successful program selection per round (eight total).
Additional explicitly saved failed attempts remain charged and must reconcile to
the original tool ledger within the fixed 16-call cap; unresolved reservations,
uncertain jobs and unexplained extra successes fail the final audit. Missing
queried outcomes retain their charged slots and QC status. M and patch authority are off.

Explicit bwrap and seccomp execution is mandatory. LLM, protein-model and egress
routes are rejected. Programs see revealed TaskView evidence, never controller
source datasets or hidden labels. The existing ToolGateway, task loop, candidate
validation and replay oracle handle execution, validation and charging. Failures
have no scientific fallback. Programs return exactly candidates plus optional
bounded scalar diagnostic metadata; candidate prediction claims are rejected.
Each case has its own store. Aggregate output is descriptive purchased-evidence
metrics; raw purchased evidence remains in its case store for audit.

## Explicit run-wide budgets

The legacy invocation remains eight rounds × 16 candidates = 128 purchased
queries. New invocations may freeze a different complete-plate budget explicitly:

```bash
python scripts/run_frozen_program_matrix.py --matrix MATRIX.json --out NEW_OUTPUT \
  --batch-size 16 --max-rounds 4 --query-budget 64 --execute
python scripts/run_frozen_program_matrix.py --matrix MATRIX.json --out NEW_OUTPUT \
  --batch-size 16 --max-rounds 8 --query-budget 128 --execute
```

Python API: `run_matrix(matrix, destination, execute=True, batch_size=16,
max_rounds=4, query_budget=64)`. Each integer must be positive and `query_budget`
must equal `batch_size * max_rounds`; TaskSpec's plate-size bounds also apply.
LLM budget stays zero. The charged tool-attempt cap is twice `max_rounds`, covering
successful selections and explicitly recorded failed attempts together. Exact
purchased counts include unavailable or absent outcomes. Equal-query metrics use
the frozen plate size as their bin width. These are execution limits, not a choice
of scientific policy, initialization, or comparison matrix; supplied programs
must produce the configured plate remainder and receive the frozen task seed and
round in their existing context.

The manifest now records all effective limits and the existing strict
`runtime_identity()` including interpreter version/implementation, installed
dependency versions, package source identities, execution limits and isolation
backend identity. Interpreter or dependency drift rejects resume. Any changed limits, inputs, runner,
source bundle or sandbox identity reject resume. This revision never migrates or
rewrites an older run to the new manifest format, even for the default budget.
Keep old runtime copies and manifests for historical campaign inspection. The
completion audit derives required counts from the exact supplied saved task.

## Frozen policy code

`experiments/frozen-program-selection-v1/policies/selection_v1.py` is sandbox
program data with injected `context` and `inputs`, not a standalone Python module.
Its exact bytes are embedded in `random.json`, `additive.json` and
`conditional_pairwise.json`. Their content-derived identities and file hashes
are in `code-manifest.json`. Do not edit these inputs after a run starts.

All arms share a per-seed site-balanced single-substitution initial plate.
Subsequent random acquisition samples unseen identities; fitted arms use additive
kernel ridge with fixed exploration. The conditional arm adds pairwise terms
only when revealed measurements show opposite substitution-effect signs across
measured backgrounds. These are assistant-authored algorithms, not learned
improver publications. Domain eligibility and experimental interpretation must
be decided separately; this code does not supply an empirical study matrix.

## Verification

Run the synthetic-only extended checks explicitly:

```bash
pytest -q -m '' tests/test_program_benchmark.py tests/test_root_program_contract.py
```

They exercise real bwrap execution, exact plate/usage accounting, resume,
fail-closed hashes and invalid outputs, bounded diagnostics, shared initialization,
and three-/four-site maximum evidence contexts. Their enumeration fixture is
engineering test data and must not be treated as a scientific comparator.
These tests require Linux with bwrap/seccomp. They are extended regressions,
not default `core` tests. No actual measurements or empirical results are
asserted by these software checks.


## Read-only completion audit

`audit_completed_case(store, state, task, workflow)` in the runner inspects saved
batches, measurements, tool jobs, method receipts and charges. It returns aggregate
execution/QC counts without changing any store record or invoking replay. The
runner adds this `execution_audit` to future case aggregates. A completed historical
campaign can be inspected directly with the helper using its exact saved task and
workflow; retain its original frozen runner identity alongside the report.

The frozen-manifest equality/resume check is unchanged. A newer reporting helper
does not authorize executing changed code against an older frozen manifest.
Failed receipts, prior plate attempts and charged costs are never cleared or
rewritten by this audit. Completion means 128 distinct purchased identities across
eight 16-query rounds, not a claim that all queried outcomes are available or valid.
