---
name: esmc600m-analysis
description: Use local ESMC-600M for masked-marginal mutation priors and sequence embeddings, alongside revealed experimental measurements.
---

# ESMC-600M analysis

The operator selects and pins the protein backbone outside your workflow. ESMC is
NOT the conversational model, a structure generator, or an affinity oracle.

Available local tools (only when allowlisted):
- `esmc600m_score_variants`: reference plus full candidate sequences; returns
  WT-context single-mask log-odds. For multiple substitutions these are summed;
  the sum does not model joint epistasis or predict measured activity.
- `esmc600m_suggest_mutations`: reference, the task's complete allowed positions,
  and top_k; proposes single substitutions. Do not use unrestricted suggestions
  if a finite candidate library is enforced or mutations are forbidden.
- `esmc600m_embed_sequences`: stores residue-mean, final-layer embeddings locally
  and returns an artifact reference, not thousands of numbers in the discussion.

All ESMC operations are OPTIONAL. There is no automatic post-design scoring,
embedding extraction or task-head fitting in the LLM route. No-call rounds are valid.
Only request a tool to answer a concrete question. To learn from already revealed
measurements, explicitly request `research_fit_predict` with `features=esmc` (or
`features=mutation` without ESMC). This is not run merely because ESMC is configured.
Only an explicitly obtained task prediction artifact can supply a numeric phenotype
estimate; do not translate a sequence prior into fitness. ESMC weights stay frozen.

Interpret both priors and unvalidated ridge predictions as proxies. A large prior
is not proof of improved fluorescence, stability, Kd, or experimental success.
Respect the per-request masked-position/context limits and remaining plm_inputs.
Use external structural tools for structure/binder tasks. Never relabel ESMC
scores as affinity. Always preserve experimental observations and their protocol.
