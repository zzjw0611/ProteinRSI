# Design execution and recovery

A receives an operation registry with an explicit designer reply contract. The
scientific plan specifies what to design and how to evaluate it. B replies with
the installed Design schema: edits, direct sequences, candidate resource references,
optional tool calls, or a reason to replan. The adapter reconstructs sequences,
validates task constraints and creates sequence IDs and resources. A must not
replace that interface with prose requiring a different JSON format.

The preferred representation follows task constraints: fixed-reference substitutions
use edits or code/resource references; de novo or variable-length designs may use
full sequences. Full-sequence compatibility is retained. Local Python construction
and protein prediction are separate optional capabilities; no engine is compulsory.

Each design response is checked item by item. Valid candidates are persisted as
scope-bound resources. Repair requests include retained counts/references and the
original rejected items with structural/scientific diagnostics. No Unicode correction,
whitespace removal or guessed mutation is silently applied. Corrected contributions
are merged and deduplicated. Only a response with no validation errors is published;
the outer controller still enforces complete plate size and prior-query exclusions.

Retained candidates are not proof that a scientific grouping is complete. Plans
requiring pairs or panel allocation should include actual code/tool validation and
finish through `adapter:accept_checked_candidates`. Its `check_result` must reference
an executed tool resource reporting `status: ok`, the exact `candidate_ids`, and named
boolean `checks`. Every required check must be present and pass. The meaning of those
checks remains task-specific, defined by the plan and inspectable generated code;
the controller does not invent GB1-specific scientific acceptance criteria.

Provider failures do not consume a design/planner format-repair attempt. Exhausted
retryable calls raise ProviderPaused; typed steps and full-plate requests keep their
exact continuation identity. Guarded workers can write the paused checkpoint before
exit, but cannot perform additional LLM/tool calls during that shutdown. Successful
upstream steps and request/tool receipts are reused on continuation.

To authorize up to four additional attempts for a known failed request:

```bash
proteinrsi retry-llm --campaign runs/YOUR_RUN --request-key llm-REQUEST_HASH \
  --operator YOUR_NAME --reason 'Provider available again' --attempts 1
```

This records permission, sends no request and refunds nothing. Then repeat the original
replay/step command using the same campaign and frozen code/environment. Every new
attempt consumes the shared LLM budget. Permanent errors and uncertain `started`
calls cannot be reset this way. Code-version migration and ambiguous experiment/tool
completion are separate operator reconciliation tasks. Existing campaigns are not
silently migrated. Research paused inside a W/M validation still obeys its governance
reconciliation rules.

`PROTEINRSI_LLM_CONNECT_TIMEOUT` controls connection establishment (default 15 seconds);
`PROTEINRSI_LLM_TIMEOUT` controls response waiting. More retries do not guarantee
provider recovery. Formal research is not automatically restarted by these changes.
