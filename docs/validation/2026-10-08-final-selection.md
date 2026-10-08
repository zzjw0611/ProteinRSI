# Typed final-candidate adoption correction — 2026-10-08

Base: `25312ffea4bde4241707a73216a8f13a0703682d`.
Scope: code, tests and documentation only. This report contains synthetic
mechanism checks, not scientific observations or campaign data.

## Correctness defect and correction

The typed `agent:propose` adapter previously accumulated every successful tool
panel and every intermediate B proposal. A later final B decision selecting only
a revised resource did not remove an earlier valid-but-rejected panel. The outer
full-plate planner could take its first slots from that accumulated union,
submitting candidates which the final decision had not adopted.

The corrected operation publishes only the candidates resolved from B's final
response with no further tool calls. Intermediate results remain auditable and
available for explicit adoption. Explicit multiple-resource adoption, caller order,
identity deduplication, within-request format repair and tool-turn limits remain.
An empty final selection fails rather than falling back to previous tool output.
The operation implementation is `typed-designer-v3-final-selection`; legacy
cumulative-pool runners are not changed or represented as final-only runners.

## Verification

Before the patch, two isolated synthetic regressions reproduced unwanted earlier
candidates in both rejected-tool-panel and provisional-direct-proposal cases.
After the patch, the focused selection/dataflow/full-plate suite passed **120 tests**
with bwrap enabled. This includes real guarded preparation of a 100-well panel;
it verifies the selected identities and order, zero committed experimental wells
before approval, and an idempotent repeated preparation.

The selection module includes ten checks covering overlap/order, multiple tool
results and multiple adopted refs, provisional direct candidates, empty-final
failure, interrupted final adoption without rerunning completed tools, exhaustion
before and after a tool execution, guarded/full-plate behavior, and distinct
legacy proposal-step receipts in a **fresh** run. Existing within-request partial
repair and provider-resume tests are retained.

Commands (using an interpreter with project/test dependencies):

```sh
PROTEINRSI_SANDBOX_BACKEND=bwrap python -m pytest -q \
  tests/test_final_candidate_selection.py tests/dataflow tests/test_full_plate.py
python -m ruff check .
python -m build --no-isolation
```

Final full-suite results on the unchanged corrected production sources:

- Explicit bwrap: **1007 passed, 6 skipped**, 343.99 seconds. The skips are optional
  PyTorch/pretrained weights, GEPA, LangGraph, Biopython and the Landlock-only test.
- Default Landlock selection on this unsupported host: **969 passed, 44 skipped**,
  184.15 seconds. Unavailable Landlock paths were not represented as enforcement passes.
- Independent focused rerun: **21 passed, no skips**, with actual bwrap and corrected
  checkout imports verified. This signs off the code semantics, not warm-run migration.
- Ruff, diff check and wheel/source-distribution build passed.

The mandatory bwrap CI job includes the new selection regression module. Check
its status on the exact published commit separately; local results are not a
substitute for that remote result.

## Rollout boundary and historical integrity

This is not an automatic migration of paused or prepared campaigns. A new
proposal-step implementation does not invalidate existing plate caches, pending
batches, protocol journals, review decisions or every nested tool cache. Re-entering
an old operation may also incur new tool/model charges. Do not clear those records,
refund consumed queries, replace measured sequences with intended sequences, or
reset a study's budget to conceal a protocol deviation.

Use a genuinely unstarted planning scope after auditing remaining budget and
rounds. For an already-generated draft containing only unchanged direct-tool and
normalizer operations, a separately authorized old-pin completion may be reviewed:
verify exact scope/protocol/input/resource identities and the actual prepared panel,
then stop at a clean post-measurement boundary before new planning. A scientific
review requesting a different plan is not permission to force the old draft
through. Preserve all source pins and append an explicit software-transition audit.

Otherwise, draft abandonment or warm-scope migration requires its own supported,
reviewed transition. Frozen metric plans retain their original runtime checks;
this patch never silently revalidates them. Fresh-run receipt tests must not be
read as evidence that arbitrary warm-scope migration is safe.
