# v0.4 integration note

Biomni's category retriever has one explicitly licensed source adaptation in
`research/biomni_retriever.py`; default rules mode does not invoke it. See
[REUSE](REUSE.md) and [RESEARCH_RUNTIME](RESEARCH_RUNTIME.md). Local protein engines
remain independent Python/CLI environments, not NIM services. Existing optional
LangGraph/GEPA/MCP/Virtual Lab integrations below are retained.

# Integration guide

## v0.3 local-first execution

See [LOCAL_TOOLS.md](LOCAL_TOOLS.md) for function/description separation and isolated
local engines. This is the recommended path; no NIM is used. Existing generic MCP
remains optional for previously configured external tools. Never register duplicate
local and remote implementations under the same tool name.


## Implemented reuse, not copied monorepos

| Upstream | Entry here | Actual integration |
|---|---|---|
| LangGraph | `integrations/langgraph.py` | StateGraph, SQLite saver, approval/results interrupts |
| GEPA | `integrations/gepa.py` | Adapter evaluate/reflection interface and optimize wrapper |
| Official MCP Python SDK | `integrations/mcp.py` | v1 ClientSession + Streamable HTTP, schema-pinned tool call |
| Virtual Lab | `integrations/virtual_lab.py` | Calls upstream `Agent` and `run_meeting`; explicit consent and accounting |
| ProteinMCP / protein-design-mcp | operator-hosted server | Reuse their actual engines through MCP bindings; no bundled engines |

Versions installed by extras must be tested in your environment. The MCP adapter intentionally targets **SDK v1 (`mcp<2`)**, not the changing v2 client API. Virtual Lab has its own model/provider setup; its optional meeting is a supplemental review, not a second campaign controller.

## Native ESMC-600M (default for new CLI campaigns)

Use `pip install -e '.[esmc]'`, initialize a task, then run `esmc-check --download`.
`protein/esmc.py` uses native Transformers ESMC-600M; `protein/tools.py` registers
three real local tools; `protein/analysis.py` applies embeddings and revealed-label
fitting to post-design candidates. No manual MCP normalization is needed for these
built-ins. See [ESMC600M.md](ESMC600M.md). The generic MCP path below remains for
other independent protein engines; do not configure a second ESMC backend by accident.

## LLM

Set `PROTEINRSI_MODEL`, `PROTEINRSI_BASE_URL` (including `/v1`), and `PROTEINRSI_API_KEY`, then pass `--agent llm`. The client uses HTTP Chat Completions-compatible JSON mode and validates outputs independently. It never uses a hardcoded current model name. If your provider does not support the endpoint/response format, supply an adapter rather than accepting a silent fallback.

The client limits calls and maximum output tokens per request, records provider-reported usage, and caches successful deterministic request identities. Credentials are not written to audit records. A failed or uncertain call requires operator investigation. Paid model or cloud calls are not part of offline tests.

## Normalized protein tool output

A generator binding must return an object containing a `candidates` array. Each candidate follows `contracts.Candidate`. Other fields may carry raw scoring/structure artifacts and are provided to the analyst. An operator wrapper must normalize a particular engine's real output; do not infer which PDB chain is the design or rename an energy/confidence score to Kd.

```json
{
  "candidates": [{
    "sequence": "ACDE",
    "source": "your-audited-engine-version",
    "predicted_value": null,
    "evidence_kind": "none"
  }],
  "artifacts": [{"kind": "structure", "uri": "operator-controlled-artifact-reference"}]
}
```

A binding file is an array of records:

```json
[{
  "url": "http://127.0.0.1:8000/mcp",
  "remote_tool": "YOUR_NORMALIZED_TOOL",
  "remote_input_schema_sha256": "SHA256_OF_CANONICAL_REMOTE_INPUT_SCHEMA",
  "spec": {
    "name": "local_designer",
    "capability": "sequence.generate",
    "implementation_version": "YOUR_PINNED_CODE_AND_WEIGHTS",
    "task_kinds": ["variant_design"],
    "input_schema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"], "additionalProperties": false},
    "output_schema": {"type": "object", "properties": {"candidates": {"type": "array"}}, "required": ["candidates"]},
    "protected_inputs": {"reference": "reference_sequence"},
    "license_id": "YOUR_VERIFIED_UPSTREAM_LICENSE",
    "data_egress": false
  }
}]
```

This is a template, not a claim that an upstream server exports this invented name/schema. Use its actual discovery response and `proteinrsi.contracts.digest(inputSchema)` to calculate the pin. Define any normalization wrapper explicitly and test it against real engine output. Also place `local_designer` in the chosen Workflow's `tool_names` list. Nonlocal endpoints must declare data egress and require operator opt-in.

The gateway currently expects a completed normalized response. Upstream async job tickets need an operator-provided bounded polling/normalization wrapper; do not return a job ID as a scientific result. Structure-chain mapping, engine-specific parameter validation and scheduler/GPU resource accounting remain deployment responsibilities.

## GEPA

```python
from proteinrsi.contracts import Workflow
from proteinrsi.integrations.gepa import ProteinWorkflowAdapter, optimize_workflow

# evaluator(case, workflow) must execute your controlled research task,
# enforce its budget, and return (score, visible_trace).
adapter = ProteinWorkflowAdapter(evaluator)
proposal = optimize_workflow(
    Workflow(), adapter, trainset, valset,
    reflection_lm=your_configured_reflection_model,
    max_metric_calls=20,
)
```

`evaluator`, cases and reflection model are user-supplied, not hidden dummy implementations. Unknown current-task measurements must not be available for free inside this callback. The returned Workflow is a candidate proposal; stage a corresponding Patch and pass the campaign gate before adoption. GEPA does not bypass trusted evaluation.

## LangGraph CLI

```bash
pip install -e '.[graph]'
proteinrsi graph --campaign runs/wet --agent llm
# JSON payload: {"approved": true, "operator": "..."}
proteinrsi graph --campaign runs/wet --agent llm --resume approval.json
# JSON payload: {"observations": [fully validated Observation objects]}
proteinrsi graph --campaign runs/wet --agent llm --resume measurements.json
```

Reuse the same campaign directory/thread. Checkpoints resume the graph; SQLite campaign records prevent duplicate experimental charges/imports. Do not run native and graph drivers concurrently against the same task.

## Virtual Lab

```python
from proteinrsi.integrations.virtual_lab import run_meeting
summary = run_meeting(campaign.store, campaign.view(),
                      model="your-supported-model", operator_consent=True)
```

Install `.[virtual-lab]` separately. One team discussion with PI, Designer and Analyst plus PI summary is conservatively reserved as four model calls. No PubMed or additional tool calls are enabled by this wrapper. Audit the actual installed upstream version before using its accounting in a scientific resource comparison.

## References

- https://github.com/langchain-ai/langgraph
- https://github.com/gepa-ai/gepa
- https://github.com/modelcontextprotocol/python-sdk
- https://github.com/zou-group/virtual-lab
- https://github.com/charlesxu90/ProteinMCP
- https://github.com/jasonkim8652/protein-design-mcp
