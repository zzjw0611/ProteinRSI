# C — experimental feedback
Interpret newly revealed measurements without modifying them. Compare only actual
pre-measurement predictions to their matching measurements. Distinguish assay/QC
problems, sparse data, model mismatch and candidate-space effects. Report missing
constituent measurements rather than imputing them. Repeated deterministic replay
values are not independent experimental repeats. Suggest bounded next questions.

Keep unavailable replay queries separate from assay/QC failures. They carry no
fitness value and cannot establish a mutation effect. Report submitted-query count,
valid measurements and unavailable records separately. They still consume the
shared query budget and do not justify inventing replacements or accessing a library.

Inspect actual plate utilization and the remaining round-limited capacity. Propose
scientifically useful follow-ups that fit the next whole plate, possibly multiple
panels together. Do not turn a three-candidate idea into an entire physical round.
