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

## Optional execution
In LLM mode no Ridge or PLM model is automatically fitted. Use the explicit
`research_fit_predict` tool only when useful, choose mutation/ESMC features, and
reference its task_predictions artifact for numeric estimates. Direct evidence-based
reasoning without a regressor is valid; numeric confidence must not be invented.
