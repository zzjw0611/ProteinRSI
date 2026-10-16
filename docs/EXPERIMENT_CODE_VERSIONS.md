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
