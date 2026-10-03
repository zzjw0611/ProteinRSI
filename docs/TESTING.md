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

## Verified GitHub Actions run

Run [37110153069](https://github.com/zzjw0611/ProteinRSI/actions/runs/37110153069)
completed successfully for implementation commit
`4be676de20c277018d943f7bb5f86ec6de5a2ecd` on both Python 3.11 and 3.12.
Both jobs passed dependency installation, Ruff, pytest, the three-round synthetic
demo, and source/wheel builds. The Python 3.12 log reports **58 passed, no skips,
86% statement coverage**. This includes actual LangGraph persisted interrupt/resume,
GEPA adapter objects and MCP v1 import compatibility; it does not exercise paid
model endpoints or remote protein-engine inference.

The Python 3.12 job resolved LangGraph 1.2.12, SQLite checkpointer 3.1.1, GEPA 0.1.4,
MCP 1.30.0, Pydantic 2.13.5 and NumPy 2.5.3. These are an observed compatibility
snapshot, not a claim about every release admitted by the dependency ranges.

## v0.2 ESMC validation

The v0.2 local tests cover fixed model selection, single-mask indexing/log-odds,
additive multi-mutant prior semantics, residue-mean features, observed-only refitting,
model-input accounting, cache reuse, nonfinite/failed inference handling, protected
inputs, automatic C integration and CLI initialization. A native Transformers test
uses a small random ESMC to check tokenizer offsets/padding against direct logits.
The pretrained 600M test is separate and explicitly opt-in:

```bash
pip install -e '.[esmc,dev]'
pytest -q
PROTEINRSI_RUN_ESMC600M=1 pytest -q -s -m real_esmc
```

The final command downloads actual weights and performs CPU inference; it does
not perform a wet experiment or demonstrate scientific improvement. GPU execution,
real wet-lab experiments, paid LLM endpoints and protein benchmark gains still
require independent deployment testing. The verified execution below separates
software/CPU inference validation from scientific performance claims.


### Verified ESMC-600M execution (2026-10-03)

[Actions run 37112441460](https://github.com/zzjw0611/ProteinRSI/actions/runs/37112441460)
passed all four jobs for implementation commit
`9ae4364f5644c62c491dd7463eae595ba6a8d60a`:

| Execution | Observed result |
|---|---|
| Local core suite | 77 passed, 5 explicitly skipped (optional dependencies/real weights) |
| Python 3.11 and 3.12 core CI | Both passed dependency installation, Ruff, pytest, synthetic demo and package builds |
| Native ESMC adapter tests | 23 passed, 1 pretrained test deselected |
| Official pretrained ESMC-600M CPU test | 1 passed, 23 other tests deselected; actual weight download and inference |

The pretrained job loaded all 476 checkpoint tensors and successfully returned a
`(1, 1152)` residue-mean embedding, a finite substitution log-odds score and a zero
WT score. No fake backend or randomly initialized checkpoint was used in that job.
The separate native-library test uses random *small* weights only for API checks.

Verified runtime: Python 3.12.14, PyTorch 2.14.1+cpu, Transformers 5.16.1,
Hugging Face Hub 1.33.0, float32, CPU. Exact model snapshot:

```text
biohub/ESMC-600M-hf
0fb34e7e5fe1f85d0abaa3d35e2671107c0b458c
```

Set this SHA as `revision` in an ESMC configuration to reproduce the tested weights.
The regular default resolves `main` once, then records and reuses its immutable
snapshot. Test success establishes this model-loading/embedding/scoring path, not
GPU support on arbitrary hardware, biological accuracy, new wet-lab results or
scientific improvement from RSI. Multi-round/RSI control tests use explicit fixture
backends and should not be described as real wet-lab validation.
