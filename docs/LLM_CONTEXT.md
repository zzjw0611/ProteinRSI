# LLM input context and native-provider preflight

## Native HTTP request budget

`JSONLLM` applies an inclusive **262,144-byte (256 KiB)** default budget to each
new native HTTP request before opening a networking client, reserving/committing
an LLM call, or sleeping for a provider retry. Configure it with
`PROTEINRSI_LLM_MAX_REQUEST_BYTES` or the Python constructor's `max_request_bytes`.
The value must be a positive integer; zero, negative values and an unbounded mode
are unsupported. This is an administrative safety default, not a model-specific
capacity estimate. It applies to all native roles and sponsored validation calls.

The measured bytes are the complete HTTPX-serialized UTF-8 JSON body: instructions,
schema, context, model name, API envelope, requested output allowance and reasoning
settings. Both Chat Completions and Responses are covered. The client transmits
those **same bytes**, rather than separately estimating context size and encoding
another body. HTTP headers are outside this input-body budget. Input construction
and serialization still materialize the complete request in memory; this is not a
streaming-memory bound or a prompt compression mechanism.

**Passing this byte preflight does not verify token/context-window fit.** No bundled
tokenizer, bytes-to-token ratio, model-name heuristic or remote token-count endpoint
is used. Audit fields explicitly record `input_tokens: null` and
`context_window_verified: false`. Output-token settings and the assistant bridge's
32 MiB transport envelope are independent limits. The provider may reject a request
below the byte cap because of token/window, output, protocol or other restrictions.
Provider-reported usage after a real call remains separate from local admission.

The native output allowance independently defaults to **4,096 tokens**, configurable
through `PROTEINRSI_LLM_MAX_OUTPUT_TOKENS` / `max_tokens`. That may be insufficient
for 100 complex candidate JSON entries, depending on the schema, sequence-versus-edit
representation and model. This repair does not increase that setting or the possible
cost of each call. Validate expected output size and reserve the appropriate output
allowance within the provider's total context contract separately. No whole
100-well native loop or live native endpoint was verified by these mocked tests.

Before raising the byte cap, an operator must check the selected provider/model's
actual tokenizer and full message/schema overhead, context-window contract,
requested output/reasoning allowance, and provider-specific limits. Do not derive
a token count from this byte count. This patch does not establish that any completed
20-round bridge study can fit a native model's input window. There is no automatic
truncation, history summarization, prompt editing, model switching or hidden-label
access to make an oversized request fit.

## Rejection evidence and safe resume

An oversized request raises the existing `ProviderPaused` type with its measured
bytes, configured limit, stable request key and recovery guidance. Existing plate,
research and method continuations retain their pause checkpoints, including the
guarded worker route. It is a local rejection, not a failed or paid provider call:
no network request, new LLM reservation/charge, paid attempt, invented response,
provider token usage or cost is recorded. It does not automatically retry or sleep.

Every distinct rejected request/body/limit combination has an immutable
`llm_preflights` record containing the complete request (with the existing credential
redaction), body byte count and SHA-256, limit and explicit no-network/no-charge
flags. Repeating an identical rejection retains the same record and adds an audit
event. The hash and size describe the original serialized body; when credential
redaction changes the stored request, `request_redacted` is true and the stored
redacted JSON cannot reproduce that original hash. Nothing is truncated. Passed
preflight metadata is stored with each actual provider attempt. Sponsored branches
mirror rejection evidence into the sponsor's `validation_llm_preflights`; these are
operator-only records unavailable through worker store RPC. Offline trace exports
include and link both rejection namespaces with full immutable evidence sidecars.

To resume:

1. Inspect the rejection through the campaign trace and validate the provider's
   real input/output contract. Do not raise the limit solely because a previous
   bridge run or local test succeeded.
2. If the identical request is supported and the local administrative cap was too
   restrictive, explicitly set a suitable byte budget and repeat the original
   operation. A local cap change does not enter the scientific request, run or
   cache identity, reset attempts, alter prompts, or rewrite prior evidence.
3. If it does not fit, stop and design/version a bounded scientific context method
   before a new study. This guard does not silently migrate pinned existing engines
   or change frozen scientific methods to force a paused study forward.

Completed identical cached responses still replay under a tighter cap without a
network call or new charge. Legacy permanent failures and uncertain started calls
retain their original blocking behavior. An existing known-retryable paid failure
and its unused retry authorization survive a local preflight rejection unchanged;
after correcting the cap, the original paid-attempt limits still apply. The
`retry-llm` authorization is for known failed provider calls, cannot authorize a
never-sent local rejection, and cannot bypass this input budget.

## Designer context presentation

`dataflow/context.py` compacts the evidence sent to B through `request_design`.
This applies to both typed protocols and the existing designer entrypoint.

- An exact duplicate of the current TaskView in `tool_results` becomes
  `{"context_ref":"/view"}`. Distinct evidence and tool outputs are retained.
- Observations may use a table of `columns` and `rows` plus `shared_fields`.
  Each record is reconstructed by merging shared fields with its row. Every
  sample ID, fitness value, QC status, source, protocol and batch is preserved.
- Sequences may be represented by residues at the task's mutable positions,
  with an explicit 1-based position list. This is used only when **all** observed
  sequences can be recovered exactly from the reference. Other sequences,
  including indels or differences outside those positions, retain full strings.
- If all observed sequences are excluded from a plate, the exclusion list refers
  to those observations once and retains any additional explicit exclusions.
  Partial exclusion lists are not expanded to exclude additional observations.

The encoding is explained in the designer instructions. It changes only the
LLM request presentation: controller state, TaskView schemas, tool/code inputs,
candidate validation and measurement records keep their existing representation.
There is no sampling, inferred fitness, access to unrevealed labels or automatic
protein tool execution. Tool specifications remain available in the request.

The audited round-two GB1 request from `gb1-10r-384-20261006-152323` was replayed
locally through this transformation without an API call: JSON request size fell
from 405,847 to 99,283 characters (75.5%), retaining all 385 observations.
These are character counts, not token estimates or a guarantee against timeouts.

Existing method snapshots pin executable source. This change does not silently
migrate or restart paused campaigns, reset ambiguous calls, or refund costs.
