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
