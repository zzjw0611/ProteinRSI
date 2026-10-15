# LLM input context and native-provider preflight

## Current state first, scoped history on demand

New `start`/default `init` studies freeze context policy `state-v1`. Controller-owned
facts (round, budget, QC counts, best measured, evidence identity) and the Analyst's
bounded working memory form the current research state. Working memory replaces
the previous focus/hypotheses/questions; it is not an accumulating conversation.
State versions and source feedback receipts commit in the same transaction. Resume
cannot update the same feedback twice. Comparison arms clone the same state and
then update their own memory independently.

Stage inputs contain current task constraints, state, latest feedback/measurements,
step arguments and relevant contracts. Full history, experience, old verdicts and
raw artifacts are scoped references even when the model has room for them. There
is no automatic injection of the last three complete decisions. Original TaskViews
and records remain intact for audit and sandbox Python. Stage schema catalogues
use named references; compact operation input contracts stay directly available.

The state remains at `view.research_context.research_state` in model inputs and
`context['research_context']['research_state']` in sandbox Python. Only the explicit
stage `view` receives the scientific state projection; dictionaries inside tool
results or input-shape descriptions are not inferred to be TaskViews.
`research_python.execution_contract`, derived from the runtime TaskView model,
is supplied in tool and operation catalogues and in local repair inputs. Python
receives original lists and full sequences, never the model's compact tables or
reference descriptors. `B-repair` preserves code, errors, and type descriptions
without scientific projection; normal provider capacity, audit, and scoped
reference controls still apply.

Generated-code operations advertise both the execution receipt (`result`) and its
business object (`payload`, at `/output`) in the compact operation contract. Named
reference bindings hydrate precisely the selected port; the runtime never guesses
which `output` fields to flatten. Resource descriptions also include bounded `shape`
metadata with observed field names/types and array counts, without evidence values.
Array shapes describe only the first item and truncated branches are explicit;
the registered schema remains authoritative. These descriptions support field mapping
without asking agents to reload or reproduce complete result tables.

`research_evidence_query` and the context `query` action retrieve only previously
revealed rows by full sequence, mutable-site variant, batch, original zero-based
batch round, or a specified residue background. Each result reports its snapshot
identity, coverage and continuation cursor. No matches return
`status=no_matching_observations`, empty rows and a short explanation. This means
no record matches the filters in this revealed snapshot, not that the candidate
was never measured elsewhere, is invalid, or has zero fitness. Agents continue
their step with unknown values; only experimental submission reveals new labels.
Repeating the same query receives explicit feedback. Persistent lack of progress
pauses with `reason=context_stalled`, retained through nested comparisons and in
the context session audit; resume does not repeat paid calls.
`read_many` combines overlapping starts for the same reference;
it streams each selected reference once, retaining continuation cursors.

## One capacity policy

`JSONLLM` uses one policy for every native role: a **128,000-token working input
target**, **16,384-token output allowance**, and **1,048,576-byte (1 MiB)** native
request safeguard. Configure these with `PROTEINRSI_LLM_INPUT_TOKENS`,
`PROTEINRSI_LLM_MAX_OUTPUT_TOKENS`, and `PROTEINRSI_LLM_MAX_REQUEST_BYTES`.
If the provider declares a total context window, set
`PROTEINRSI_LLM_CONTEXT_WINDOW`; output allowance is reserved within that window.
All limits must be positive. These are request capacities, not campaign call budgets.

Context policy `evidence-v1` first supplies the **complete stage input**, including
the task, observed evidence, current protocol, completed steps and tool contracts.
It uses the same native-request capacity check as the provider client, including
instructions and response schema. Small handoffs require no reference-reading turn.
For oversized inputs, the existing scoped references retain the complete originals;
reads return whole records in batches up to 16K estimated tokens / 64 KiB. Successfully
read pages remain available until explicitly released. There is no fixed 12-read
limit: repeated actions without new evidence pause rather than looping indefinitely.
No hidden landscape labels are admitted, sampled or summarized into agent context.

The local token counter uses **o200k_base as a surrogate**, with an 8K-token reserve
for message framing and gateway instructions. The third-party model's tokenizer and
full window cannot be established from its model name. Audit records therefore
separate `estimated_input_tokens` from `input_tokens: null` and keep
`context_window_verified: false`. Provider-reported usage is recorded after each
call. A provider may still reject requests within local capacities. Install project
dependencies and initialize the tokenizer cache in the trusted controller; guarded
research workers do not download tokenizer assets or access the API directly.

The byte check covers the exact HTTPX-serialized UTF-8 JSON body, including schema,
instructions, context, model and output settings, for Chat Completions and Responses.
Those same bytes are sent. Exceeding either capacity pauses locally before a network
call or charge. Evidence is retained; raising a cap does not reset paid attempts.

Local context snapshot writes are batched into short transactions. Identical stored
values do not generate duplicate updates. Evaluation treats actual LLM failures as
provider pauses; local persistence and programming errors propagate as local failures
with a redacted diagnostic traceback in the background job record.

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
