# Interactive assistant bridge

This opt-in local transport lets an operator relay ProteinRSI's visible JSON
request to an actual interactive assistant and return its JSON decision. It is
**not a native provider run**, an autonomous model, or a synthetic fallback. It
needs no provider keys, never reads an auth file, and makes no provider API calls.
The model label is operator-supplied provenance, not verified model identity.

## Enable explicitly

```sh
export PROTEINRSI_ASSISTANT_BRIDGE_DIR=/absolute/private/path/assistant-mailbox
export PROTEINRSI_MODEL=interactive-assistant-session
export PROTEINRSI_ASSISTANT_BRIDGE_TIMEOUT=900
# Optional polling interval in seconds (default 0.25):
export PROTEINRSI_ASSISTANT_BRIDGE_POLL_INTERVAL=0.25
# Optional administrative request byte limit (example: 1 MiB):
# export PROTEINRSI_ASSISTANT_BRIDGE_MAX_REQUEST_BYTES=1048576
```

Use the normal CLI with `--agent llm` (or an existing LLM-enabled start flow).
`JSONLLM.from_env(store)` selects the bridge only when the directory variable is
present. An empty/relative directory or missing model label fails rather than
falling back to a provider. Unset the directory variable to restore native API
configuration. Timeout is finite, > 0 and <= 86400 seconds; polling is > 0 and
<= 60 seconds. Changing either does not change request identity.

Keep the mailbox outside source, installed-library, and other sandbox-readable
roots. The existing guarded worker uses its unchanged controller RPC: it receives
an opaque transport identity, never the mailbox path or environment variables.
An in-process run still has its existing weaker isolation; this bridge does not
add an OS sandbox. Protect both the mailbox and campaign store as private data.

### Request capacity

The bridge implements its own capacity contract for `legacy`, `evidence-v1` and
`state-v1`. It sizes the canonical UTF-8 JSON payload, including instructions,
context, schema and transport metadata, without constructing an HTTP request or
using a provider tokenizer. The limit defaults to 32 MiB minus 1 KiB (the existing
transport allowance); a positive integer override may lower it. The reserved
1 KiB covers request ID/hash fields and the final newline. Responses retain the
separate 32 MiB transport limit.

This is an administrative byte limit, **not** the interactive model's token window
or proof that the assistant can process a request. Set a practical limit for the
session. Native API capacity settings do not configure the bridge. Shared evidence
paging still uses the existing local tokenizer estimate; provision its cache for
offline use. Neither an estimate nor a byte count is provider-reported usage.

The context planner checks capacity before sending and can use its existing
reference/paging flow. Publication rechecks with the actual role (not supplied to
the planner's sizing interface) before reserving a call. Oversize requests pause
without publication or a new LLM charge, including on the legacy path.

Default transport request IDs and cache settings remain compatible. A custom byte
limit is included in the request and cache identity; changing it creates a different
request/context session, not a retry of the pending one. To resume a pending request
without another charge, restore the same limit, store, model and input. Capacity
settings also participate in the existing context-session identity; do not hot-swap
code/configuration into historical studies or rewrite their frozen checkpoints.

## Answer a request

1. The controller atomically publishes `requests/<request_id>.json`. It contains
   the role, instructions, caller-supplied visible context, expected JSON Schema,
   protocol version, transport, model label, request ID, and request hash. It
   never enriches that context from source tables, hidden labels, or credentials.
2. Give the actual assistant only that request's instructions, visible context,
   and schema. Obtain its decision matching the schema. Do not ask for private
   chain-of-thought, inspect withheld labels, or invent tool/scientific results.
3. Create an envelope with **exactly** these fields: `protocol_version`,
   `transport`, `request_id`, `request_hash`, and `model`, copied unchanged from
   the request, plus `result`, containing the actual JSON decision object.
4. Write the envelope to a temporary file in `responses/`, flush/fsync it, then
   atomically rename it to `responses/<request_id>.json`. Do not write the final
   filename incrementally. For a durable manual submission on POSIX:

```python
# request = parsed request JSON; decision = the actual assistant-returned object
# mailbox = the explicitly configured private mailbox directory (pathlib.Path)
import json, os, uuid
fields = ("protocol_version", "transport", "request_id", "request_hash", "model")
reply = {**{name: request[name] for name in fields}, "result": decision}
final = mailbox / "responses" / (request["request_id"] + ".json")
temporary = final.with_name("." + uuid.uuid4().hex + ".tmp")
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(reply, stream, allow_nan=False)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, final)
fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
```

The controller validates strict envelope identity and JSON Schema before accepting
the object; existing caller validation remains in effect. JSON duplicate fields,
non-finite values, non-object results, unexpected envelope fields, and mismatches
are rejected. External schema references cannot fetch files or URLs. Mailbox
ancestors and files cannot be symlinks; linked/nonregular files are rejected, and
file operations are pinned to directory descriptors. JSON is capped at 32 MiB.
The private mailbox is a trusted operator channel, **not authentication** of who
wrote a reply or proof of which model generated it.

## Timeout, correction, restart, and audit

- Creating a new durable request commits one `llm_calls` unit before publishing
  it. It charges no experimental wells and reports no invented tokens or money.
- Timeout raises the existing `ProviderPaused` continuation signal and leaves the
  request pending. After a real reply is available, repeat the original operation
  using its same store/checkpoint and model. No retry authorization command is
  needed: this is the same pending call, not a new provider attempt.
- Invalid replies also pause. Explicitly replace the bad reply atomically and
  resume. Rejected byte hashes and error types remain in audit; unsolicited raw
  text is not saved. Tampered request files and unsafe paths fail closed.
- Request IDs are bound to a persisted store scope, model label, role,
  instructions, context, schema, and transport protocol. Resume preserves the ID,
  cache, and charge, including a crash before request publication. Completed
  calls return their durable cached result even after mailbox replies are removed.
  Keep the original store and checkpoints; editing context creates a new call.
- `llm` / `llm_attempts` audit records distinguish `assistant_bridge` from native
  APIs, store the validated returned JSON decision and response hash, and mark
  model identity unverified. Token usage is `{}` because it is unavailable.
  Existing trajectory export displays these records. Only public decision notes
  supplied in the result are recorded; hidden reasoning is neither requested nor
  reconstructed.

Automated tests use transport-only fixtures; they do not demonstrate live model
quality or successful scientific experiments.

## Freezing predictions for later error analysis

A designer proposal is intentionally unscored at the design boundary. Putting
`predicted_value` or `prediction_ref` directly in B's Candidate response does not
freeze that number, even when B previously requested a real prediction tool.
A one-step `agent:propose` protocol can therefore submit valid designs with no
frozen prediction error record. An unscored batch correctly has `n=0` in the
prediction-error analysis; do not retrofit its predictions after measurement.

When the scientific decision needs prospective prediction checks, plan an explicit
prediction-to-analysis path: design candidates, bind their actual sequences to
`research_fit_predict`, supply its actual result to `agent:rank`, have C select
the returned artifact via `prediction_refs`, then pass the ranking through
`agent:select` and `adapter:ranked_sequences`. Only validated tool-derived
annotations survive into the frozen batch. Merely mentioning fitting in a
question does not execute a tool, and copying a number is not provenance.
