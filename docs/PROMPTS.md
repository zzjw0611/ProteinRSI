# Prompt design and provenance — v0.5

The role templates are **original ProteinRSI prompts**, not purported copies of tested
Biomni/Virtual Lab/ProteinSwarm prompts. Biomni informs retrieval and plan/observe/revise;
Virtual Lab informs PI/expert responsibilities; HyperAgents/ADAS inform the distinction
between workflow and successor-improver changes. Only the previously documented Biomni
resource-selector file is a source adaptation (Apache-2.0).

| Role/call | Template in src/proteinrsi/prompts/ | Output |
|---|---|---|
| A planning | principal_plan.md / principal_fixed_plan.md | typed research plan or compact plan |
| A review | principal_review.md | keep/replace unexecuted suffix |
| A selection | principal_selection.md | permutation of reviewed candidates |
| B | designer.md | full sequences, legal edits, or optional tool requests |
| C tools | analysis_tools.md | optional requests, including empty list |
| C rank | analyst.md | ranking, brief explanation, optional verified prediction refs |
| C feedback | feedback.md | evidence interpretation and alternatives |
| M | meta.md | one scoped workflow/meta patch or abstention |
| Common constraints | common.md | optional scientific computation; mandatory identity/budget/safety rules |

`compose()` combines role template, separately versioned Workflow/Meta strategy, optional
Skill reference text, and invariant text. `JSONLLM` adds the JSON schema; TaskView supplies
task/evidence context. No hidden label file is part of these inputs. Ask for concise
justification and evidence IDs, not private model reasoning. Model output schemas are
validated regardless of what instructions say.

At initialization, `prompt_bundle` content and hash are snapshotted. Current plans may
change, W/M strategy strings may change after gates, but those actions cannot edit core
prompt invariants. To test new invariant templates use a new campaign. An existing task
never silently hot-reloads prompt files. Inspect with `proteinrsi prompts --campaign ...`.
`docs/REUSE.md` lists actual code reuse separately from these design influences.
