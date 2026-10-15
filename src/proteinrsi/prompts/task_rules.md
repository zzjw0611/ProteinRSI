Task semantics: candidate_access=open means autonomous design. Generate sequences
from the supplied parent/target, allowed design space and revealed evidence, using
LLM reasoning, optional scientific tools or generated code as appropriate. There is
no preselected candidate menu. An empty candidates field does not mean the design
space is empty. Do not
invent a hidden catalogue, catalogue indices or membership constraints. In ranking
tasks assess the explicitly supplied sequences; do not replace them with designs.
Historical replay is an experimental-feedback backend, not a design library.
Each submitted replay query consumes one query slot, including unavailable results.
An unavailable observation has no measured value: it means no historical record,
not low fitness, assay failure or biological infeasibility. Use only valid numeric
observations for fitness learning; retain unavailable identities to avoid repeats.

For batch_fill_policy=full_plate, every experimental round is a whole plate, not a
small hypothesis panel. The controller must fill all batch_size wells before any
submission. Multiple backgrounds, hypotheses or exploration panels may share that
plate. Unused wells do not carry to later rounds. Inspect capacity.remaining_rounds
and capacity.usable_queries_before_round_limit, not just the nominal query balance.
When research_context.plate_completion is present, generate the requested additional
unique candidates and exclude the already selected identities. No experiments have
run during completion; do not pretend that completing a panel advances a round.
A validation_request is a sub-panel, not an entire plate; the controller fills other
wells separately. Scientific strategy and tool choice remain yours.

Use the controller-bound Workflow W and task feedback to solve this task. meta.version
is an audit identity only; it grants no instructions or permissions. Improver policies,
M proposals and M analysis notes are outside the task context. Method changes reach
you through the W version bound by the Controller after authorized validation.
