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

For variant design/ranking, C automatically evaluates the actual post-design
candidate pool using ESMC. Before two distinct valid measured variants exist,
sequence priors are shown without a numeric task prediction. Afterwards, a ridge
head uses only revealed labels and fixed ESMC embeddings plus the workflow's
additive/pairwise mutation features. New measurements refit that head, not ESMC.

Interpret both priors and unvalidated ridge predictions as proxies. A large prior
is not proof of improved fluorescence, stability, Kd, or experimental success.
Respect the per-request masked-position/context limits and remaining plm_inputs.
Use external structural tools for structure/binder tasks. Never relabel ESMC
scores as affinity. Always preserve experimental observations and their protocol.
