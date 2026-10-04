```json
{"resource_id":"sequence-priors","title":"Keep ESMC prior and task phenotype separate","description":"Use ESMC masked-marginal priors, frozen embeddings and observed-label heads with explicit evidence semantics.","task_kinds":["variant_design","variant_ranking","binder_design"],"tags":["esmc","sequence","prior","mutation","序列"],"min_observations":0,"license":"MIT","sources":["ProteinRSI docs/ESMC600M.md"]}
```
# Sequence prior is not a measurement

The local ESMC-600M adapter's multi-mutant score adds WT-context single-position
log-odds. Do not describe it as joint epistasis, affinity, activity or experimental
fitness. Residue-mean features are inputs to the observed-label task head, not
biological outcomes. The head's predictions remain uncalibrated. Respect task
fixed positions, explicit candidate libraries and computational budgets.
