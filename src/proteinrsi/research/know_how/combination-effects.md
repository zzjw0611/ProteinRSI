```json
{"resource_id":"combination-effects","title":"Assess combination effects on an explicit scale","description":"Compare measured combinations to measured WT and constituent singles without inventing missing labels.","task_kinds":["variant_design","variant_ranking"],"tags":["combination","epistasis","mutation","组合","突变"],"min_observations":3,"license":"MIT","sources":["ProteinRSI research.analysis.combination_effects","ProteinRSI docs/RESEARCH_RUNTIME.md"]}
```
# Combination diagnostics

Select a null model explicitly. On a linear outcome scale compare the measured
combination to WT plus the sum of measured single-mutant deviations. On a positive
multiplicative scale do the analogous calculation in log space. These are different
scientific assumptions. A deviation is descriptive and scale-dependent, not a
statistical significance claim. Missing WT or constituent singles must be reported
as missing evidence, not replaced with ESMC scores or surrogate predictions.
The report may motivate a new workflow hypothesis; it cannot promote a patch.
