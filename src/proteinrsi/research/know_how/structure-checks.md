```json
{"resource_id":"structure-checks","title":"Keep structure provenance and chain identity","description":"Check target/design mapping, fixed positions and artifacts for inverse folding and binder workflows.","task_kinds":["variant_design","binder_design","affinity_prediction"],"tags":["structure","binder","backbone","mpnn","protenix","结构"],"min_observations":0,"license":"MIT","sources":["ProteinRSI docs/LOCAL_TOOLS.md","ProteinRSI docs/MINIMUM_CAPABILITIES.md"]}
```
# Structure workflow checks

Use registered artifact references. Never guess which output chain is the binder.
RFdiffusion backbone generation, ProteinMPNN inverse folding and Protenix structure
prediction answer different questions. Preserve target identity and design constraints.
CA RMSD and contact checks are limited geometry diagnostics, not full all-atom
validation or evidence of affinity. Report missing deployed engines instead of
inventing structures, hidden scores or completed jobs. Physical assays remain external.
