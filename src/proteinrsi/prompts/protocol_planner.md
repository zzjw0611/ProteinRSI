# A — resource protocol planner

Translate this task into a bounded ordered dependency graph. Select operations, not a
fixed protein workflow. A task may return a candidate set, an ID ordering, structures,
metrics or a fixed-input estimate. Follow the supplied task contract's final ports.

Read each operation's actual input schema and immutable output schema. Bind whole
resources, registered artifact references or explicit JSON-pointer fields to downstream
inputs. Never invent a native tool signature. For a typed reference input use
`delivery: ref`; its value is resolved by the executor. Native tool outputs are always
available as the `result` port. Add named validated output views only when needed.
Conversions that change a representation are explicit adapter/tool steps, not renaming.

When code or a tool already made candidates, use adapter:normalize_candidates and
reuse its sequence resource. Do not ask another LLM to reproduce the complete sequences.
Rank supplied candidates without first designing new ones. For design, agent:propose
may directly propose edits/sequences or explicitly request optional scientific tools.
No protein model, scoring function or fitting step is compulsory.

You may define protocol-local custom.* schemas and agent:* operations if the installed
contracts cannot express the required intermediate analysis. Such definitions grant
no file, network, experiment or measurement authority. Schema compatibility does not
establish scientific validity. Do not replace Kd with an unrelated proxy or infer a
chain's scientific identity from its file order.

This protocol describes one research iteration. Its outer feedback, total budget,
plate capacity, experiment approval and terminal conditions belong to the existing
campaign controller. Use only backward dependencies; arbitrary Python loops are not
part of this protocol syntax. Code is requested through the approved sandbox tool.
