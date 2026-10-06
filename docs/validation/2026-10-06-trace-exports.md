# Long-study trace export validation, 2026-10-06

This repair changes operator report presentation only. The completed T7 and ParD3
campaign databases and their existing reports were read-only inputs; no frozen
engine, scientific pipeline, request prompt, budget, or historical evidence changed.

## Completed-study measurements

| Measurement | T7 | ParD3 |
| --- | ---: | ---: |
| Existing inline HTML bytes | 956,786,075 | 337,301,258 |
| New overview bytes | 2,549 | 2,549 |
| Whole compact export bytes, including exact evidence | 11,618,188 | 10,866,999 |
| Largest HTML page bytes | 334,117 | 337,637 |
| HTML detail pages | 15 | 16 |
| Events / selected records | 712 / 667 | 742 / 714 |
| Exact source cells reconstructed and verified | 1,380 | 1,457 |
| Distinct compressed evidence objects | 1,244 | 1,308 |
| Additional bytes after identical re-export | 0 | 0 |
| First export wall seconds | 6.48 | 3.45 |
| Validation process peak RSS, KiB | 21,640 | 20,008 |

The process RSS includes repeated export and streamed source-cell verification;
these are observations on this host, not cross-platform upper bounds. Every manifest
entry was decompressed, checked against its byte count and SHA-256, and compared
byte-for-byte with the corresponding SQLite cell. Source database SHA-256 values
were identical before and after export/verification. The state cell is included in
addition to the selected records and events, explaining the cell-count totals.

## Automated checks

- `ruff check .`: passed
- `pytest -q`: 548 passed, 18 skipped
- Eighteen new export tests cover legacy full-JSON compatibility, gzip output,
  exact Unicode/whitespace reconstruction across chunk boundaries, deduplication,
  bounded previews and Python allocation, validation links, HTML escaping and local
  link resolution, interrupted output, snapshot consistency, repeated exports,
  corrupt-cache refusal, source-database replacement guards, and empty traces
- Synthetic repeated-payload test: 28 repeated approximately 1 MiB records, with
  Python peak traced allocation below 5 MiB for compact and full-stream exports;
  tests never substitute character counts for token usage
- `python -m build --outdir <validation-directory>/dist`: sdist and wheel passed in
  an isolated build environment. Shared study `.venv` dependencies were not changed
- An initial `--no-isolation` build could not import absent `setuptools`; the normal
  isolated build resolved that build-only dependency and succeeded

The 18 skips are existing host/dependency limitations: missing PyTorch, opt-in real
ESMC-600M checkpoint download, and unavailable Landlock/libseccomp isolation. This
result does not claim those integration paths or a live native LLM API passed.

## Browser verification limitation

A fresh installed Chromium process could not create its required local process
socket (`Operation not permitted`), including an approved escalation attempt. The
supported cloud-browser tool rejects `file://` URLs; the local-only HTTP preview
also returned `net::ERR_BLOCKED_BY_CLIENT`. Those restrictions were not bypassed.
Generated page structure, escaped hostile payloads, links and pagination were
verified by automated HTML tests. Actual visual appearance, browser filtering,
page-number navigation, Back/Forward and file-download behavior remain unverified.
No successful screenshot or browser-runtime pass is claimed.

## Remaining context risk

Stored bridge request JSON ranged from 4,512 to 4,051,623 bytes for T7 (118 main
requests) and 4,240 to 1,040,968 bytes for ParD3 (117 main plus 8 validation).
T7 A-plan had the largest request; its largest B request was 1,555,624 bytes.
Current lossless context presentation applies to B only. The bridge checks a
32 MiB minus 1,024-byte canonical payload cap before adding request identity fields;
mailbox JSON reads cap at 32 MiB. These byte limits are not token/context-window
limits. Native HTTP requests still have no input-context guard, and native-provider
acceptance, token usage, cost and model identity were not verified by this repair.

See [trace export format and limitations](../TRACE_EXPORTS.md) for reconstruction,
sharing, retained changed-snapshot bundles, and explicit full-export options.
