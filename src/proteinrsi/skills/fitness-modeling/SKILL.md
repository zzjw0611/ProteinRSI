---
name: fitness-modeling
description: Use revealed measurements to fit and inspect a sequence-property predictor.
---
Only use observations whose QC status is valid. Preserve assay, metric and units.
Group technical repeats by sequence; do not treat them as independent variants.
The builtin ridge predictor is a small baseline, not ESM or a calibrated affinity predictor.
Its novelty term is a selection heuristic, not calibrated uncertainty.
Refitting a fixed method is inner-loop learning. Changing the modeling or acquisition method
is a workflow patch, which requires a separate comparison before adoption.
