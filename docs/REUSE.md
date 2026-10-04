# Reuse and design provenance — v0.5

This is an implementation inventory, not a claim of reproducing upstream papers.
An API adapter is not source transplantation. A design citation is not an installed model.

## Biomni: exactly what was adapted

Reviewed source revision: `snap-stanford/Biomni@400c1f366b96a35ca253e13c9b06c5076af41d65`.

| Upstream file | Local file | Nature of reuse |
|---|---|---|
| `biomni/model/retriever.py` | `src/proteinrsi/research/biomni_retriever.py` | Modified source: category-separated prompt selection and `_format_resources_for_prompt` loop |
| `biomni/agent/a1.py` | `research/runner.py`, `research/contracts.py` | Design inspiration only: plan → execute → observe → continue/revise |
| `biomni/know_how/loader.py` | `research/knowledge.py`, `research/know_how/*.md` | Design inspiration only: named Markdown resources and metadata; parser and notes are original |
| `biomni/tool/tool_registry.py`, `tool/tool_description/` | existing `tools.py`, `localtools/` | Design inspiration only; original typed gateway and independent JSON descriptions |

Canonical source links:
- https://github.com/snap-stanford/Biomni/blob/400c1f366b96a35ca253e13c9b06c5076af41d65/biomni/model/retriever.py
- https://github.com/snap-stanford/Biomni/blob/400c1f366b96a35ca253e13c9b06c5076af41d65/biomni/agent/a1.py
- https://github.com/snap-stanford/Biomni/blob/400c1f366b96a35ca253e13c9b06c5076af41d65/biomni/know_how/loader.py
- https://github.com/snap-stanford/Biomni/blob/400c1f366b96a35ca253e13c9b06c5076af41d65/LICENSE

Changes in the Apache-licensed adaptation:
1. Inject the configured `JSONLLM`; no implicit OpenAI/LangChain client or default paid model.
2. Account for retrieval through the same LLM budget/cache as other decisions.
3. Replace regex parsing with a typed JSON response; reject negatives, booleans, strings, duplicates and out-of-range indices.
4. Accept only prefiltered resources; preserve downstream gateway authorization.
5. Add scope-limited method-experience category; do not automatically prioritize every database tool.
6. Use context budgets and stable resource identities in the surrounding original selector.

The preserved license has Git blob SHA `261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64`,
matching the reviewed upstream LICENSE. The reviewed tree had no top-level NOTICE.
The adaptation's file header identifies source, license and modifications. We do not
copy upstream Know-how documents or their protocols. `licenses/` is packaged in
both source and wheel license metadata.

The modified selector is exercised by `resource_selection=llm`. Default `rules`
uses the original lightweight selector, with no retrieval LLM call. This is deliberately
not described as "running Biomni A1". We do not include its unrestricted REPL,
10+ hour environment installation, benchmark data or claimed performance results.
Neither this code review nor the upstream Git tree establishes equivalence to a journal
paper's exact experimental release. No inaccessible full-text details were implemented
as purported facts.

## Existing library/API reuse, retained

- `integrations/langgraph.py`: actual optional StateGraph, SQLite saver and interrupts;
  all experiment logic remains in `Campaign`.
- `integrations/gepa.py`: actual optional GEPA adapter/wrapper; evaluator and reflection
  model must be provided. GEPA does not bypass campaign promotion gates.
- `integrations/virtual_lab.py`: explicitly requested calls to upstream `Agent` and
  `run_meeting`; not the default multi-agent runtime.
- `integrations/mcp.py`: actual optional official SDK v1 Streamable HTTP client.
- `protein/esmc.py`: actual native Transformers ESMC loading/scoring/embedding API.
  `protein/analysis.py` is our own observed-label Ridge head, not an upstream PLM method.
- `localtools/functions.py` and `worker.py`: our adapters invoke separately installed
  ProteinMPNN, RFdiffusion, Protenix and licensed PyRosetta. They do not copy model algorithms.

These have independent licenses; see THIRD_PARTY.md. No NIM runtime or hosted model
service is introduced. No checkpoint redistribution occurs.

## References that are NOT code reuse

ProteinMCP: environment/registry/Skill design; no `pmcp` dependency or copied launcher.
ProteinSwarm: direct sequence proposals and feedback; no residue-agent implementation copied.
HyperAgents/ADAS: workflow and successor-improver concepts; no noncommercial source included.
ALDE/SSMuLA/EVOLVEpro: experimental-feedback and evaluation motivation; original numerical
baseline and user-supplied data, not those projects' implementations or results.
BioNeMo Toolkit: prior Skill organization reference; no NIM client copied or deployed.

## ProteinRSI-specific implementation

The shared experimental ledger, revealed-only task view, legal candidate checks,
structured plan revisions, read-only diagnostics, experimental two-arm trials,
independent successor-improver gate and scoped experience records are implemented
in ProteinRSI. These are implementation responsibilities, not claims that the broad
ideas were first invented here. Scientific novelty and generalization require
independent experiments.

## v0.5 additions

`prompting.py` and all `prompts/*.md` are original templates, with role/workflow influences
listed in PROMPTS.md. They do not claim to reproduce an upstream prompt experiment.
`replay/` is an original controller/capability worker and Linux restriction implementation,
not a copy of the server-local guard scripts described in the user's data report.
`research/prediction.py`, catalogue helpers, SponsoredStore and reporting are original.
Model implementations and the original Biomni adaptation retain their prior licenses.
The provided architecture SVG is byte-for-byte preserved in docs/assets; origin/hash are
recorded in architecture.provenance.json. It is not a Biomni figure or validation result.
