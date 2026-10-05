# A — review actual execution
Review the most recent completed step and real outputs. Return pending_steps=null
when the plan still makes sense, otherwise replace ONLY the unexecuted suffix.
Do not alter completed steps. Revised plans must rank current candidates and finalize once; design is optional
when candidates were supplied or produced by a tool. Inspect generated-code status,
stdout and error details; a failed program can be repaired in the pending steps. Repair concrete problems, not by inventing evidence.
This is within-task replanning, not a persistent workflow or Meta policy update.

Check task fidelity: open design must generate proposals from task inputs and
revealed evidence, not reinterpret replay as a supplied candidate shortlist.
Tool-generated candidates may be ranked directly; no particular generator or
protein tool is mandatory. Unavailable replay feedback is missing coverage,
not evidence of assay failure or low fitness.

Check plate_completion and task.batch_fill_policy. In full-plate research a small
panel is partial work, not a completed round. Revise pending work to generate the
remaining identities before final handoff. Keep all sequence identities immutable.
