# ProteinRSI 0.4

**Budgeted protein research with an experimental inner loop, validated workflow
improvement, and bounded successor-improver evaluation.**

[中文](README.md) · [Research runtime](docs/RESEARCH_RUNTIME.md) · [Reuse inventory](docs/REUSE.md) · [Local tools](docs/LOCAL_TOOLS.md) · [Licensing](THIRD_PARTY.md)

This release integrates the delivered v0.3 local tools with resource-aware typed
planning. It is research software, not a validated protein design product. No NIM
service is required. No arbitrary generated Python/shell is executed.

## Four logical roles and one campaign controller

A selects resources and creates/revises the unexecuted portion of a ResearchPlan.
B proposes constrained sequences/edits or calls registered design tools. C performs
read-only evidence analysis and candidate review. M proposes persistent workflow or
MetaPolicy patches; only independent gates may adopt them. All can share one chat LLM.

```
Visible task/evidence → permission-filtered resources → A's plan
   → evidence / B design / tools / C ranking → actual outputs → A replanning
   → A final priorities → trusted checks → human approval → experiment
   → new observations → next inner round and conditional outer improvement
```

A current-plan revision is not a published workflow. A new workflow is not necessarily
a better improver. Data, workflow, MetaPolicy and research-run identities are recorded
separately. Both Meta-evaluation offspring receive identical research configuration
and Know-how snapshots; hidden labels remain evaluator-owned.

## What is genuinely reused?

| Project | Actual status |
|---|---|
| **Biomni** | One **modified source adaptation** in `research/biomni_retriever.py` (Apache-2.0), used in `resource_selection=llm`. Category retrieval/formatting adapted from fixed revision `400c1f3`; explicit budgeted JSON client, strict bounds and permissions added. Plan/observe and Know-how designs inform otherwise original code. No A1 REPL/E1 environment copied. |
| **ProteinMCP** | Environment isolation, tool registry and Skill design inspiration; no source copied or `pmcp` runtime dependency. |
| **LangGraph / GEPA / MCP SDK / Virtual Lab** | Optional actual library/API adapters; not implicitly invoked by the offline demo or merged as competing controllers. |
| **Transformers / ESMC-600M** | Real native model API. Frozen weights downloaded separately; embeddings/prior scoring and an original observed-label Ridge head. |
| **ProteinMPNN / RFdiffusion / Protenix / PyRosetta** | Original adapters call separately installed upstream programs/APIs after configuration, version/asset checks and license review. New heavy engines are not end-to-end validated in this environment. |
| **ProteinSwarm / HyperAgents / ADAS / ALDE / EVOLVEpro** | Related design/method references, not copied implementations or reproduced results. No HyperAgents noncommercial source included. |

Exact source files, modifications and limitations: [docs/REUSE.md](docs/REUSE.md).
Original code/docs are MIT; the Biomni-derived file is Apache-2.0. Distribution
metadata is **MIT AND Apache-2.0**, not a choice of licenses. The full original
Apache license and NOTICE are packaged. Model/database terms are separate.

## Run

Python 3.11+, POSIX/WSL. No weights, user research files, experiments or credentials
are bundled. After extracting the source or applying the supplied patch:

```bash
cd ProteinRSI
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
proteinrsi demo --adaptive --out runs/research-demo --rounds 3
proteinrsi research plans --campaign runs/research-demo/campaign
proteinrsi research resources --campaign runs/research-demo/campaign
proteinrsi research analyze --campaign runs/research-demo/campaign
pytest -q
```

The demo uses synthetic numerical labels and scripted roles, not real LLM reasoning,
ESMC inference or wet-lab work. Omitting `--adaptive` retains the fixed demo baseline.
New CLI `init` defaults to adaptive; use `--research-mode fixed` for the old route.
Old campaigns and the Python initialization API without research_config remain fixed.

Main configs: `configs/research.json` (retrieval/plan bounds),
`configs/protein_tools.json` (independent model environments), `.env.example` (chat
provider), `examples/wetlab_task.json` (task/assay/budget), and workflow/meta JSONs.
Configure the LLM separately from ESMC; `.env` is not automatically loaded.

The 13 local protein/structure/MSA tools are retained. Three additional context-bound
analysis tools inspect observed QC, frozen pre-measurement prediction error and
explicit-scale combination deviations. They accept no arbitrary observation tables,
paths or hidden labels. Missing constituent measurements are reported as missing.

## Boundaries

Plans are typed serial operations, not arbitrary code or a parallel DAG engine.
Completed steps cannot be rewritten; new evidence after ranking requires reranking.
Failed/uncertain jobs remain blocked for operator investigation. Model environments
isolate dependencies but are not OS security sandboxes. No new biological efficacy,
GPU performance, paid-LLM integration or wet-lab result is claimed. Quantitative
protein–protein affinity and generalized cross-protein experience transfer remain
incomplete capabilities. The generic Meta gate still rejects structural-engine cases
without case-scoped artifact/evaluation support. See [TESTING](docs/TESTING.md).
