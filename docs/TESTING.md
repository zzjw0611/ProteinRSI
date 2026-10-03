# Initial build verification

The initial implementation was exercised locally with Python 3.13.5, Pydantic 2.13.4,
NumPy 2.3.5, HTTPX 0.28.1, jsonschema 4.26.0 and pytest 9.0.2.

Local result at the first complete core checkpoint: **55 passed, 3 skipped**.
The skipped tests require GEPA, LangGraph/SQLite checkpointer and MCP; these packages
were not present and the build environment could not resolve package-registry hosts.
The GitHub Actions workflow installs these optional packages and runs the real-import
and graph-resume tests. Its actual result is authoritative; a workflow file alone is
not a claim that CI passed.

Covered mechanisms include canonical residues, 1-based mutations, immutable affinity
inputs and binder scaffold constraints; fixed-position enforcement; nonfinite/QC
measurement rejection; budget concurrency and idempotency; no refunds after submission;
round persistence and cancellation; repeated/conflicting import handling; wet-lab CSV
round trips; separation of labels from task views; feedback-dependent next-round
predictions; staged workflow trials, acceptance/inconclusive paths; trusted patch
boundaries; grouped successor-improver evaluation and successor activation; final-test
selection blocking; tool allowlists, egress and schema checks; HTTP model contract tests
using MockTransport; and no-key CLI demonstrations.

Not exercised in this environment: a paid/live LLM endpoint, actual protein engines,
GPU inference, a real external MCP protein service, Virtual Lab model calls, physical
wet experiments, external experimental benchmark performance, or scientific RSI gains.
The controlled mocked/fixture tests intentionally do not establish those capabilities.

The source dependency ranges are compatibility constraints, not a complete frozen
transitive lockfile. For a published experiment, capture the exact installed packages,
OS/container image, backend code and model-weight versions, seeds, dataset hashes,
assay versions, resource usage and independent evaluation protocol.
