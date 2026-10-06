# Offline trajectory exports

Trace export is an operator-only, read-only presentation of persisted evidence. It
never queries a source landscape label table, invokes a model, changes a scientific
budget, or rewrites campaign state. It is not an input-context optimization.

## Defaults and compatibility

- `proteinrsi trace --campaign RUN --format html`: a compact overview, paginated
  previews, and an adjacent content-addressed `trajectory.html.assets-*` directory
- Each page contains at most 100 events or records. Each JSON preview shows at most
  2,048 source bytes, using its beginning and end when truncated. A split UTF-8
  character may appear as a replacement character **in a preview only**
- Every entry links to its complete original JSON cell as a `.json.gz` file. Long
  records are never silently cut from the evidence. Event links navigate to the
  corresponding record page, including validation-branch records
- Page filters search **only previews on that page**. They do not search omitted
  bytes or other pages. Previous/Next and page-number navigation require no server;
  only filtering and page-number navigation use local inline JavaScript
- Copy the HTML and its adjacent assets directory together. There are no CDN
  scripts, external fonts, network requests, or required browser extensions
- `--format json --out trace.json` preserves the full legacy JSON schema, including
  repeated linked event details. Output is streamed and can still be enormous
- `--format json --out trace.json.gz` produces the same complete JSON with gzip
  compression. JSON on stdout is also streamed; interrupted stdout may be partial
- `--format html --full --out trace-full.html` explicitly requests the old,
  unbounded, single-file inline HTML. This path and the compatible `read_trace()`
  API still materialize the full trace and should be avoided for long studies
- Plain text iterates event payloads without loading linked LLM or tool contexts

Python callers can use `export_html(directory, destination, page_size=100,
preview_bytes=2048)` or `export_json(directory, destination)`. Presentation settings
are capped at 200 entries/page and 8,192 preview bytes. They do not change evidence.

The default compact export streams SQLite cells in 64 KiB chunks instead of loading
all kv rows or expanding every linked record. A temporary on-disk lookup resolves
record links; page contents are built one page at a time. Python memory is independent
of the total repeated payload volume. SQLite itself still parses the campaign state
and event reference fields to obtain metadata, and metadata strings have their own
size. Total disk use and export time necessarily grow with unique source evidence
and entry count; only individual HTML pages and previews are bounded.

## Evidence format and reconstruction

`metadata.json` names the format `proteinrsi-compact-trace-v1`, records source/status,
round/budget summaries, counts, maximum snapshot event ID, presentation limits and
the SHA-256 of `manifest.jsonl`. `preview_only=true` describes the HTML presentation.

Each manifest JSON line has a type (`state`, `record` or `event`), identity fields,
exact uncompressed byte count, SHA-256 and a relative `objects/<sha256>.json.gz`
path. Records use `namespace` and `key`; events use `id`, `timestamp`, `kind` and
optional linked-record identities/pages. The gzip body is the **exact UTF-8 SQLite
JSON cell**, retaining whitespace, key order and Unicode bytes. Hashes are over
uncompressed bytes. Identical cells share one object, including identical successful
LLM and LLM-attempt audits. The original campaign state cell is included separately;
exported record namespaces match the legacy trace allowlist, not every kv namespace.
This is an exact trace-evidence export, not a complete restorable database backup.

For example, verify an exported entry without loading the complete trace:

```python
import gzip
import hashlib
import json
from pathlib import Path

assets = Path("trajectory.html.assets-REPLACE_WITH_EXPORT_ID")
metadata = json.loads((assets / "metadata.json").read_text())
hash_manifest = hashlib.sha256()
with (assets / "manifest.jsonl").open("rb") as stream:
    for chunk in iter(lambda: stream.read(65536), b""):
        hash_manifest.update(chunk)
assert hash_manifest.hexdigest() == metadata["manifest_sha256"]
with (assets / "manifest.jsonl").open() as manifest:
    for line in manifest:
        entry = json.loads(line)
        digest, count = hashlib.sha256(), 0
        with gzip.open(assets / entry["file"], "rb") as original:
            for chunk in iter(lambda: original.read(65536), b""):
                digest.update(chunk)
                count += len(chunk)
        assert count == entry["bytes"]
        assert digest.hexdigest() == entry["sha256"]
```

After decompression, `json.loads` yields the original event payload or record value.
The manifest identities reconstruct the full selected records and event payloads;
linked-record identities reconstruct the legacy event details without storing extra
copies. Hashes detect accidental corruption relative to the exported manifest; they
are not signatures or proof that a malicious party has not replaced the whole bundle.
As with the original database, these files may contain private scientific input and
model-returned text. Share them only with authorized recipients.

## Interrupted and repeated exports

All parts of one compact export use a consistent SQLite read transaction. Concurrent
later events appear only in a subsequent export. Every page and evidence object is
built in a temporary directory; the overview is atomically replaced only after the
new bundle is complete. Ordinary exceptions clean up that export's staging files
and preserve the previous overview and bundle. If overview publication fails after
bundle completion, that complete, unlinked bundle is retained for a safe retry;
concurrent exporters may already be using it. A killed process can leave a hidden
`.trace-*` staging directory; it is never linked as a completed export. This is not
a guarantee against filesystem damage or power loss.

Identical snapshots reuse the same content-addressed assets directory after comparing
its existing bytes with the newly generated bundle. A corrupt or incomplete existing
bundle causes an explicit error; choose a new output filename rather than overwriting
prior evidence. Changed snapshots retain older bundles so already opened or shared
links keep working. Remove obsolete bundles manually only after
checking no overview or saved links need them. The CLI avoids generating the same
bundle twice at the end of a successful `start` command. Full JSON file exports also
use a temporary sibling and atomic replacement; caller-owned streams can be partial.

## Input-context limits remain separate

`dataflow.context.compact_view` is currently used for the designer (B) request only.
It losslessly tables revealed observations, optionally encodes mutable residues,
and replaces exact duplicated views. Other roles can still receive growing context.
The assistant bridge limits its canonical request payload to 32 MiB minus 1,024
bytes before adding envelope identity fields; mailbox JSON reads have a 32 MiB cap.
Native HTTP now has a separate, default 256 KiB serialized-request byte preflight;
see [LLM context and preflight](LLM_CONTEXT.md) for configuration and safe recovery.
Rejected full requests are preserved in `llm_preflights` (and sponsored
`validation_llm_preflights`) with linked trace evidence, without a network call or
LLM charge. This is **not** an input-token/context-window guarantee. Its configured
maximum output tokens is **not** an input-context limit. The bridge does not expose
verified provider token usage, cost, or model identity. Neither byte counts nor
character counts establish token counts or native provider acceptance.

Completed local audits on 2026-10-06 measured persisted request JSON as follows:

| Study | Requests inspected | Minimum bytes | Maximum bytes |
| --- | ---: | ---: | ---: |
| T7 | 118 main | 4,512 | 4,051,623 (A-plan) |
| ParD3 | 117 main + 8 validation | 4,240 | 1,040,968 (A-plan) |

These are the completed studies' stored requests, not future bounds or the native
provider's serialized wire bodies. T7's largest B request was 1,555,624 bytes despite
B's presentation encoding. The measurements were read-only: no live native API
call was made and no study prompt, cap or pinned active engine was modified. The
subsequent mutable-source native preflight does not migrate those study engines.
