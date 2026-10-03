# ProteinRSI

A budgeted protein-research agent framework with experimental feedback, validated workflow changes, and bounded meta-policy evolution. See the [Chinese README](README.md) for the complete operational guide.

## Four logical agents

A (principal) plans and reviews selection; B (designer) proposes sequences/edits or requests tools; C (analyst) ranks and interprets feedback; M (method improver) proposes changes to the research workflow or its own improvement policy. The database, experiment gateway, budget ledger, and promotion gate are trusted programs, not additional agents.

The inner loop is **plan → design → analysis → approval → measurement → feedback**. The outer loop is **diagnosis → bounded patch → checks → trial → independent gate → versioned adoption or abstention**. Accepted successor meta policies are actually loaded in subsequent rounds.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
proteinrsi demo --out runs/demo --rounds 5
pytest -q
```

Python 3.11+, Linux/macOS or WSL. The native runtime is immediately usable without optional agent packages. Install `.[graph,mcp,gepa]` for actual LangGraph/MCP/GEPA integrations.

**The default demo is a deterministic baseline on an artificial fixture. It is not an LLM experiment, protein-model prediction, historical measurement, or new wet-lab result.** No model keys, weights or third-party datasets are bundled. Real model calls are opt-in and fail explicitly rather than silently substituting mocks.

## Implemented scope

Persistent multi-round campaigns; canonical sequence/position constraints; observed-only ridge ranking; direct LLM sequence edits; allowlisted JSON-Schema/MCP tools; manual experiment approval and CSV feedback; idempotent budget/accounting; equal-allocation workflow trials; scoped experience records; and separate improver-descendant evaluation with successor activation.

RSI is constrained to typed workflow/meta-policy configurations and prompts. Arbitrary generated Python is deliberately not executed. Binder support is fixed-length scaffold redesign with an external backend, not a ready-made de novo platform. Affinity predictions require a suitable external model; the built-in ridge/novelty heuristic is not an affinity predictor.

## Reuse and license

Original code and documentation are [MIT](LICENSE). LangGraph, GEPA, the official MCP SDK and Virtual Lab are reused through optional dependencies/APIs; protein-tool servers remain independent deployments. No upstream source, model weights or experimental dataset is vendored. HyperAgents, ProteinSwarm and ALDE are architectural references, not disguised copied implementations. Consult [THIRD_PARTY](THIRD_PARTY.md), [INTEGRATIONS](docs/INTEGRATIONS.md), [EVALUATION](docs/EVALUATION.md), and [SECURITY](SECURITY.md).

Tests demonstrate software mechanisms, not scientific improvement. Read [TESTING](docs/TESTING.md) for tested environments, optional skips and external capabilities that have not been exercised.
