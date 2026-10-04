```json
{"resource_id":"evidence-qc","title":"Inspect experimental evidence before changing design","description":"Read-only batch, control and replicate diagnostics; distinguish failure from zero.","task_kinds":["variant_design","variant_ranking","binder_design","affinity_prediction"],"tags":["qc","feedback","batch","controls","实验","质量"],"min_observations":0,"license":"MIT","sources":["ProteinRSI contracts.Observation","ProteinRSI research.analysis"]}
```
# Evidence before intervention

Use only observations currently revealed to the campaign. Record the assay, unit and source.
A failed or inconclusive measurement is not a numeric zero. Keep technical replicates
and controls identifiable; technical replicates are not independent proteins.
Batch control means and shifts are descriptive warnings, not proof of a batch effect
or a correction factor. Do not automatically normalize or exclude measurements.
An execution failure, a measurement failure and a design failure require different actions.
When evidence is insufficient, record that limitation and propose an approved follow-up.
