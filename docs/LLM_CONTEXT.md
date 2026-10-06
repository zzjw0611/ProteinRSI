# Designer context presentation

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
