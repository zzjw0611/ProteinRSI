# ProteinRSI 0.5

**Budgeted protein research with an experimental inner loop, validated workflow
improvement, and bounded successor-improver evaluation.**

[中文](README.md) · [Research runtime](docs/RESEARCH_RUNTIME.md) · [Reuse inventory](docs/REUSE.md) · [Local tools](docs/LOCAL_TOOLS.md) · [Licensing](THIRD_PARTY.md)

This release integrates the delivered v0.3 local tools with resource-aware typed
planning. It is research software, not a validated protein design product. No NIM
service is required. Generated Python runs only in a separate resource-limited sandbox; generated shell commands are not executed.

## LLM-defined evaluation for new studies

New CLI `init` and natural-language `start` studies select `llm_adjudicated_v1`.
Before held-out results, the LLM records its Top-N choices, criteria, rationale,
tradeoff handling and treatment of missing evidence. After the controller computes
best/max, Top-N means, average and measurement denominators, the LLM explicitly
accepts, rejects or declares the comparison inconclusive. No numeric threshold,
average-only rule or Pareto condition determines the verdict in this mode.
The controller enforces evidence identity, budgets, permissions, schemas and durable
exactly-once adoption. E-plan/E-verdict are evaluation calls, not another protein
prediction capability. Missing or invalid replies pause; they never approve a method.
Existing studies and historical gates are not reclassified or silently migrated.
See [evaluation](docs/EVALUATION.md) and `configs/gate.llm_adjudicated.json`.

## Four logical roles and one campaign controller

A selects resources and creates/revises the unexecuted portion of a ResearchPlan.
B proposes constrained sequences/edits or calls registered design tools. C performs
read-only evidence analysis and candidate review. M proposes persistent workflow or
MetaPolicy patches; adoption requires a separate recorded evaluation. All can share one chat LLM.

![User-supplied protein agent conceptual architecture](docs/assets/proteinrsi-architecture-zh.svg)

The supplied SVG is copied without alteration; its conceptual transfer/RSI arrows are not efficacy evidence.


A current-plan revision is not a published workflow. A new workflow is not necessarily
a better improver. Data, workflow, MetaPolicy and research-run identities are recorded
separately. Both Meta-evaluation offspring receive identical research configuration
and Know-how snapshots; hidden labels remain evaluator-owned.

## v0.5: optional means no implicit computation

Actual LLM execution never automatically scores with ESMC, embeds sequences or fits Ridge.
Models are available as tools; zero-call rounds are valid. The explicitly labelled
deterministic baseline retains numerical ranking. The user can prewarm weights with
`esmc-check`, but metadata inspection and role planning do not load them.

Prompts are inspectable Markdown snapshots in `prompts/`; they are ProteinRSI-authored,
not transplanted four-agent prompts. `research_fit_predict` is an explicit context-bound
operation. All 13 protein manifests have richer usage guidance and strict outputs.

GB1 starts with a supplied parent sequence and known experimental fitness at zero new-query cost.
`proteinrsi start "Optimize GB1 for 2 rounds and 48 new queries, at most 24 per round"`
uses the configured LLM to parse the goal, prepares the audited local data and starts guarded replay.
No handwritten task JSON is needed. Resource selection uses the LLM; protein tools remain optional.
`--prepare-only` parses/saves without querying new labels; parsing itself consumes one LLM call. Open design receives no replay catalogue or sampled candidate menu. The agent generates
sequences before querying the private replay backend. Missing records return `unavailable`
with no fitness value and still consume a submitted-query slot. Ranking uses explicitly
supplied sequences. Meta evaluations charge the shared
study ledger, including warm-start labels. Reports expose best sequences, query history,
unique variants and tool/LLM usage without inventing dollar prices.

`replay` defaults to a Linux Landlock/seccomp worker, with controller-only labels and
capability RPC for LLM/tools. Unsupported kernels fail closed. The explicitly selected
`--execution inprocess` path is NOT an isolated benchmark. Landlock ABI 1+ is supported with explicit seccomp protections for truncation, rename/link,
metadata changes and networking. Unsupported hosts fail closed; enforcement tests skip only
when the required kernel primitives are unavailable.
An explicit `PROTEINRSI_SANDBOX_BACKEND=bwrap` alternative uses a read-only mount
namespace plus the same or stricter seccomp controls. Run `sandbox-check` and the
mandatory adversarial tests before starting a new campaign; there is no automatic
fallback. Frozen metrics and replay pin the selected backend.
See [PROMPTS](docs/PROMPTS.md), [TOOL_CONTRACTS](docs/TOOL_CONTRACTS.md), and
[REPLAY_SECURITY](docs/REPLAY_SECURITY.md). Start a new campaign for v0.5 semantics.

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

Plans are typed serial operations; custom Python is a budgeted isolated tool, not controller-side execution or a parallel DAG engine.
Completed steps cannot be rewritten; new evidence after ranking requires reranking.
Failed/uncertain jobs remain blocked for operator investigation. Model environments
isolate dependencies but are not OS security sandboxes. No new biological efficacy,
GPU performance, paid-LLM integration or wet-lab result is claimed. Quantitative
protein–protein affinity and generalized cross-protein experience transfer remain
incomplete capabilities. The generic Meta gate still rejects structural-engine cases
without case-scoped artifact/evaluation support. See [TESTING](docs/TESTING.md).

### Automatic task-local Meta trials and trajectory

New campaigns validate pending MetaPolicy changes in the next batch. Frozen old/new
improvers share revealed evidence and symmetric compute caps, propose descendant
workflows, and submit equal disjoint candidate sets. All queries and computation
charge the campaign ledger. Acceptance promotes only the MetaPolicy. Identical
descendants or failed/inconclusive trials do not promote; insufficient slots defer.
This is exploratory current-task evidence, not cross-protein generalization.
Configured local engines and registered artifacts are available in isolated branches.
The separate multi-case `evaluate-meta` protocol retains its existing restrictions.

`start` prints live events and exports `trajectory.html`, including on execution
failure. `proteinrsi trace --campaign RUN --follow` follows events; `--format html`
exports an offline, paginated preview with adjacent exact-evidence gzip sidecars. Keep
`trajectory.html` and its generated `.assets-*` folder together. Each page has at most
100 entries and 2,048 source bytes per preview; filtering searches only that page's
previews. Truncation is explicit. Original JSON cells remain byte-exact, deduplicated
and SHA-256-addressed; the manifest maps every exported record and event to its evidence.
`--format json --out trace.json.gz` streams the complete legacy JSON schema (including
linked event details), optionally gzip-compressed. `--format html --full` explicitly
restores the legacy unbounded inline viewer. `read_trace()` keeps its full in-memory API.
See [trace export formats and verification](docs/TRACE_EXPORTS.md) for details.
Snapshots capture revealed evidence, policy versions and budgets. LLM audits retain
requests, returned text, decision summaries, provider-returned reasoning summaries,
usage and failures, with credentials redacted. Unavailable hidden reasoning is never
reconstructed. These operator records are not available to the worker through RPC.

### Task-neutral goal intake and generated programs

`start` now asks the configured LLM to infer variant design, supplied-sequence ranking,
binder design or fixed-input affinity prediction from the goal, input artifacts and
tool capabilities. It selects computational iteration, verified measured replay or
wet-lab feedback. Missing information is saved as questions; answer with
`proteinrsi start "additional details" --continue-from RUN`. Use repeated `--input`
for FASTA/PDB/CIF/A3M/JSON scientific files. No handwritten task JSON is required.

Ranking can skip design and needs no artificial reference protein. De novo binders
use supplied targets and task-defined length bounds. Indels and repeated measurement
are explicit task policies. Agent-generated `research_python` programs can calculate
metrics, transform candidates and write scientific artifacts; errors are visible for
plan/code repair. Each call is budgeted, with 10s CPU, 20s wall time and 2GB memory.
Only revealed evidence, explicit inputs and selected registered artifacts are supplied.
Source files persist under `programs/`, with inputs/results/errors in the trajectory.
No network, subprocesses, keys, campaign database, or measurement authority is granted.

Computational rounds pass actual computed outputs into the next iteration without
creating experimental observations or consuming lab queries. These rounds do not
automatically promote M using the experimental improvement gate. Wet-lab studies
prepare a batch and wait for actual approval/import. Verified replay retains its
query budget and hidden-label boundary.
