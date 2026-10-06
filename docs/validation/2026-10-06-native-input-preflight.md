# Native input-preflight validation — 2026-10-06

Scope: mutable ProteinRSI source based on `9668abe0c52c05cdfc771550f42329a93b8e9b7b`.
The change bounds native HTTP request bodies without altering role prompts,
scientific evidence, request/cache identity, or pinned study engines. This is a
local software validation, not a live provider or scientific-performance result.

## Verified locally

Python 3.12.14; shared project runtime dependencies were not modified. All provider
calls used HTTPX `MockTransport`; model identifiers and credentials were fixtures.

- `OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q --cov=proteinrsi --cov-report=term-missing`:
  **582 passed, 19 skipped**, **83%** total statement coverage; `llm.py` **95%**
- Focused `tests/test_llm_preflight.py`: **34 passed, 1 skipped**
- `.venv/bin/ruff check .`, source/test compilation, and `git diff --check`: passed
- Standard `.venv/bin/python -m build --outdir /tmp/proteinrsi-native-preflight-dist-final`:
  wheel and source distribution built with isolated build dependencies
- Fixed and adaptive deterministic synthetic demos: both **complete, 3 rounds**

The 19 full-suite skips are one absent Torch/native ESMC test, one explicitly
opt-in pretrained-weight test, and 17 tests requiring unavailable host sandbox /
Landlock / libseccomp capabilities. Guarded end-to-end preflight recovery is among
those skipped; an always-runnable stub-IPC test exercises the broker's pause drain,
checkpoint writes, blocking of subsequent model/tool RPCs, and diagnostic retention.
This stub is not evidence that host sandbox enforcement executed.

## Coverage added

- Exact inclusive below/at/above byte boundaries for Chat Completions and Responses,
  including UTF-8, JSON escaping, complete schema, API envelope, reasoning settings,
  requested output tokens, content length and SHA-256 of actual transmitted bytes
- Small user context with oversized schema is rejected before HTTP
- Synthetic histories with 20 rounds and over 4 MB of full evidence reject for
  A-plan, B, C-feedback and M; no truncation, network-client construction, sleep,
  reservation, paid attempt or LLM charge occurs
- Complete immutable rejection audits, idempotent repeated rejection, protected
  worker visibility, sponsored audit mirroring and full JSON/compact trace links
- Local cap correction resumes the identical request/key and plate continuation;
  method snapshots, campaign state, attempt counts and scientific budgets persist
- Completed legacy responses replay under stricter caps; permanent/uncertain legacy
  failures remain blocked; prior paid failures and unused retry allowances survive
  local rejection without becoming additional paid attempts
- A mocked provider rejects token context despite passing the byte cap; raising the
  cap or retry authorization cannot bypass that permanent provider failure
- Invalid/disabled limit configuration fails closed; existing credential redaction
  is retained and redacted bodies are identified without pretending their bytes hash
  to the original wire body
- Guarded/sponsored IPC error frames carry only the pause type, not request text or
  secrets; the controller retains actionable byte-limit details and does not mask
  unrelated worker failures

## Limits and operational follow-through

The 256 KiB default is an administrative byte budget, **not a tokenizer bound or
model-window guarantee**. Operators must validate their chosen provider's full
input/output contract before raising it. No tokenizer or remote token-count service
was added; local input token counts remain unknown. Input construction and
serialization still materialize the full payload before preflight.

The native output allowance still defaults to **4,096 tokens**. That may not fit
100 complex candidate JSON records. Output schema, sequence/edit encoding, model
limits and reserved output/reasoning allowance need independent deployment checks.
No paid/live native API call or complete 100-well native run was performed. Historical
bridge success is not evidence of native API feasibility.

No active campaign database, study prompt, hidden source landscape, pinned engine,
credential, model weight or shared `.venv` dependency was changed. Publication and
remote CI verification belong to the subsequent commit; local success does not
claim that a future commit's CI has passed.
