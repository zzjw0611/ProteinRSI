# Frozen aggregate analysis v1

This code-only milestone packages the frozen descriptive analysis as an explicit
CLI. It reads aggregate records and preregistration metadata only. It never
opens datasets, oracle labels, candidate sequences or trajectories. No empirical
aggregate, comparison output or scientific outcome is distributed here.

```bash
python scripts/analyze_frozen_program_aggregate.py \
  --aggregate AGGREGATE.json \
  --preregistration configs/experiments/frozen_program_preregistration_v1.json \
  --out OUTPUT.json
```

The analysis statements are AST-identical to the original pre-outcome script;
only input/output paths and callable/CLI packaging changed. The preregistration
is an exact byte copy of the frozen metadata. Its source-code hash identifies
the original scientific selection program, not this portable analysis wrapper.

The fixed study expects 135 unique landscape/seed/arm cases. Each case must have
128 charged queries, zero unreconstructed charges, zero LLM calls, eight tool
calls and the eight 16-query curve endpoints. Missing, duplicate or invalid
records refuse analysis. Paired signs use the frozen ±1e-12 tolerance. Endpoint
mean is the unweighted mean of the eight within-case observed endpoints.

Summaries retain within-landscape comparisons, wins/ties/losses and equal-weight
family balances. They do not pool raw score units or treat related landscapes
and seeds as independent proteins, and make no statistical significance claim.
The preregistration records study splits, prior development exposure and other
interpretation limits. This new executable mechanism diagnostic does not prove
transfer of original prompt-defined W or autonomous M self-modification.

Synthetic-only validation covers wins, ties, losses, tolerance, family balancing
with unequal landscape counts/units, complete-case refusal and CLI/API agreement:

```bash
pytest -q tests/test_frozen_program_aggregate_analysis.py
```

All 11 focused tests and Ruff passed before publication. The full regression
suite was not rerun locally for this analysis-only milestone. No empirical
outcomes were consulted or analyzed by this packaging/test step.
