# Experiment code versions

Small, reviewable code/configuration milestones are retained on
`work/assistant-cloud`. Each milestone is a normal fast-forward commit; its parent
preserves the previous version. Run-specific datasets, measurements, candidate
panels, trajectories, checkpoints and credentials are outside this code record.

## planner-delegation-w0 / 2026-10-16

- Source code revision: `c7de6bf5995d513a9682f6ef8df2558878e3eb0e`.
- Input: `configs/experiments/planner_delegation_w0.json`.
- Authorship: operator-approved, assistant-authored initial guidance (outside the
  autonomous M loop), not an autonomous method improvement or a new package default.
- Change: only the frozen W0 `principal_prompt` is extended. Planner defines the
  objective, scientific hypothesis, budget and necessary constraints, delegates
  exact candidate choice to Designer by default, and justifies necessary exact
  panels. Checks enforce required scientific properties rather than equality to a
  preferred panel. Designer guidance and every other W field remain unchanged.
- Original W identity: `w-cfe9141564fc1ec9`.
- Revised W identity: `w-6ccf8d89b4d80098`.
- Original W file SHA-256:
  `82e52b78f5c709e4796ff0a462ff67deda57b4630c5d1166b0a282b405ffbbd7`.
- Published input SHA-256:
  `9855c00c37e64f247102bd2e4c1d0a4882c20b2c45dff2f47e1a148ee9a0fcd8`.
- Existing enabled M0 was byte-identical to its frozen input during local trial
  preparation; this milestone does not change M or controller behavior.

### Validation and interpretation

On 2026-10-16 the local trial's two focused infrastructure checks passed: only the
Planner field changed (including unchanged M bytes), and the revised guidance
reached the generic `A-plan` request plumbing. The portable regression file included in
this milestone repeats the W identity/delta and generic `A-plan` plumbing checks using a
fake LLM and no measurements or experimental queries. Both portable tests passed
under the source project's pytest configuration; Ruff passed on the new test file.
The full regression suite and real-model/biological checks were not rerun for
this configuration-only milestone:

```bash
pytest -q tests/test_planner_delegation_config.py
```

These are software/configuration checks. They do not demonstrate biological
improvement, RSI benefit, cross-task transfer or causal before/after improvement.
This saved input does not change defaults or select an experiment automatically.

For future milestones, append a version entry with the source revision, changed
files, content-derived identities or hashes, tests actually run, and remaining
limitations. Preserve older entries and code history.

## frozen-program-selection-v1 / 2026-10-16

- Parent code milestone: `84f3358c2d5b069358eb80cafbc8a9ac1c2955f1`.
- Source base: `c7de6bf5995d513a9682f6ef8df2558878e3eb0e`.
- New opt-in selection adapter, narrow Team/replay guards, portable matrix runner,
  synthetic regression checks and frozen assistant-authored policy source/JSON.
- Portable packaging imports the installed package and writes test runtime
  diagnostics only to temporary test directories. Scientific policy bytes are
  unchanged. See [contract](FROZEN_PROGRAM_BENCHMARK.md) and the policy
  `code-manifest.json` for exact identities/hashes.
- This is a new deterministic mechanism diagnostic, not a transfer test of the
  original prompt-W and not autonomous M self-modification.
- Active local adapter checks passed (14); root-policy synthetic checks passed
  (2). Portable packaged checks passed separately (16 tests); Ruff passed on
  staged adapter, runner and tests. No full-regression rerun was performed.
- Focused existing regression: 100 passed, 1 skipped, 6 failures reproduced with
  the original source/fixtures. This is not a clean full-regression result.
- No empirical matrix, task snapshot, dataset, trajectory or result is published.

## frozen-aggregate-analysis-v1 / 2026-10-16

- Parent milestone: `c33fce20a627591e54673bad3da1820d34a71bc5`.
- Portable aggregate-only CLI: `scripts/analyze_frozen_program_aggregate.py`.
- Exact frozen metadata: `configs/experiments/frozen_program_preregistration_v1.json`.
- Statistics AST unchanged from pre-outcome analysis; only explicit input/output
  paths and callable/CLI packaging added. No policy or scientific comparison changed.
- Original analysis SHA-256: `e3c38faa1509ab38a6d6dfc6510f9161fd0134d92df2dddcaf2756c8a3058357`.
- Portable analysis SHA-256: `fa08474cbb3706f34311fde90280ed4ace8368dc8f4bd4ee2a608ce75935308a`.
- Preregistration SHA-256: `cb354e2a0f968f5a8ea9ef472dd0dea046d2c6eea7db5737e7591f90bcbc0d78`.
- Synthetic focused validation: 11 passed; Ruff passed. No local full-regression rerun.
- See [analysis contract](AGGREGATE_ANALYSIS.md). No empirical aggregate,
  comparison output, dataset, raw trajectory or outcome is published.
