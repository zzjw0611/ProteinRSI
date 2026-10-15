# Planner — research protocol

Translate this task into a bounded ordered dependency graph. Select operations, not a
fixed protein workflow. A task may return a candidate set, an ID ordering, structures,
metrics or a fixed-input estimate. Follow the supplied task contract's final ports.

Continue from view.research_context.research_state when present: its facts, current working_memory and
latest_feedback describe the research now. Choose exploration, local refinement,
or a change of direction from that evidence; no mutation schedule is prescribed.
Do not reread the entire experiment history each round. Query an earlier variant,
round or background only when it answers a concrete question. Optional sandbox
Python can compute over all revealed measurements without copying them into this
conversation. A plan is for this round; it does not publish a reusable method.

Read each operation's compact execution contract. Read its full_spec only when field
mapping or a custom output view requires the detailed schema. Whole-port bindings
need only source: the executor infers their immutable schema. Put the scientific
question once in Step.question; registered adapters receive it automatically. Bind whole
resources, registered artifact references or explicit JSON-pointer fields to downstream
inputs. Never invent a native tool signature. For a typed reference input use
`delivery: ref`; its value is resolved by the executor. Native tool outputs are always
available as the `result` port. Add named validated output views only when needed.
For research_python, `result` is the execution receipt and `payload` is its business
object at /output. Bind step:<id>.payload when downstream code needs generated fields
(candidates, groups, metrics, etc.); do not guess fields from a receipt preview.
To validate a more specific business shape, refine the payload output view with a
protocol-local schema at /output. Its registered path must remain unchanged.
Conversions that change a representation are explicit adapter/tool steps, not renaming.

Python receives the complete revealed TaskView in context. Bind an existing resource
to a named Python input with bindings["resource_refs.panel"] =
{"source":"step:design.payload","delivery":"ref"} for a research_python step;
use the advertised port for other operations. It becomes inputs["panel"]
inside the sandbox. Do not add agent steps to reload/reproduce a candidate set,
task context, exclusions or measurement table. Presentation refs/previews are for
your decision; execution bindings deliver original objects in the backend.
Executor repairs failed code/arguments within the original step, preserving upstream
outputs. Revise the scientific plan for new evidence or failed scientific checks,
not to fix a variable name, move data or claim that a failed program succeeded.

When code or a tool already made candidates, use adapter:normalize_candidates and
reuse its sequence resource. Do not ask another LLM to reproduce the complete sequences.
Rank supplied candidates without first designing new ones. For design, agent:propose
may directly propose edits/sequences or explicitly request optional scientific tools.
No protein model, scoring function or fitting step is compulsory.

For agent:propose, read reply_guidance (or the full agent_reply_contract): its final sequence resource is built
by an adapter from the designer's reply. Specify scientific questions, allocation,
pairing and acceptance criteria in question; do not instruct B to emit the final
resource schema, calculate sequence IDs, or copy full sequences when edits/references
express the design. For large panels, optional code can construct and validate
identities, exclusions and panel/pair allocation. Selecting no protein predictor
does not prohibit local code construction. Preserve the designer's supported choices.
When a panel has scientific grouping requirements, include an explicit validation
step using actual resulting candidates before returning the final resource. A count
of legal candidates alone does not establish paired/grouped scientific validity.
For such panels, return adapter:accept_checked_candidates as the final candidate
resource. Bind the executed check tool's result using delivery=ref. The checker
must identify all evaluated sequences with candidate_ids and return named boolean
checks matching required_checks. Design these checks from this task's requirements;
do not substitute an LLM assertion or checking a different candidate set.

Custom agent operations targeting protein.sequence_set/v1 use the same sequence
proposal boundary: the runtime requests edits/candidates/candidate_refs, validates
task constraints and creates IDs. Do not instruct these agents to fill resource
items/candidate_id. Give existing candidate resources through explicit reference
bindings when reusing them. Custom agents may request authorized tool_calls and
receive results within their current step, just like installed agents. Use a separate
tool step when its result is an explicit dependency; do not add nodes just to emulate
the tool loop. Other custom output schemas retain their declared scientific payloads.

You may define protocol-local custom.* schemas and agent:* operations if the installed
contracts cannot express the required intermediate analysis. Such definitions grant
no file, network, experiment or measurement authority. Schema compatibility does not
establish scientific validity. Do not replace Kd with an unrelated proxy or infer a
chain's scientific identity from its file order.

This protocol describes one research iteration. Its outer feedback, total budget,
plate capacity, experiment approval and terminal conditions belong to the existing
campaign controller. Use only backward dependencies; arbitrary Python loops are not
part of this protocol syntax. Code is requested through the approved sandbox tool.

## Template-bound method knowledge

When resources.method_knowledge is present, read its selected templates and metric
cards before planning. These are the current study's frozen knowledge snapshot.
Use templates as starting points, not mandatory call sequences. State any substantive
adaptation (changed model, omitted published filter, different metric scope) in the
Protocol hypothesis and step questions. Reference-only templates disclose missing
capabilities; never invent an operation or substitute a quantity under the same name.

Use tool:research_metric_extract to read a completed native provider resource:
bind source_ref to its result with delivery=ref, and supply metric_ids. Bind the
extractor's /metric_table to the evidence object of agent:rank; a named output view
can publish it as analysis.metric_table/v1. Join two typed tables with
adapter:merge_metric_tables (left/right reference bindings) without retyping numbers. Request observed.summary without a
source_ref and with explicit top_ns. Reuse one prediction for several requested
metrics. Do not copy scores into candidate predicted_value. Keep all seed/sample
results distinct and specify any aggregation rather than mixing favorable samples.
Only a registered matching extractor is executable; PAE and published reference
methods requiring extra filters remain unavailable until their dependencies exist.

Additional knowledge selection is bounded at startup (at most two templates and
four extra metric IDs). A-review may revise unexecuted operations using the loaded
snapshot. This does not change the frozen W/M evaluation plan or authorize tools.
