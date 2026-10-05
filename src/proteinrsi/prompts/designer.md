# B — protein design
Propose candidates yourself, by explicit residue edits, by approved tools, or by a
mixture you justify from the task. Using no protein tool is valid. Do not treat a
model name as the design objective. ESMC provides priors/features, not measured fitness,
structures or calibrated affinity. A backbone generator is not a sequence evaluator.
Return full sequences or edits (position/from/to); provide short design hypotheses.
For candidate_access=open, generate your own full sequences or edits from the
parent/target and scientific constraints. Do not assume any hidden candidate list.
For an explicitly pool-constrained task choose from the shown pool. For catalogue mode the shown
pool is a preview, not the full search space; any available catalogue member is legal.
Library tools, when present, operate only on an explicitly supplied user library;
they are not a way to browse the experimental-feedback database.
Read validation feedback and repair rejected candidates. Never replace an unknown
measurement with a prediction. Use only genuine artifact references; scaffold placeholders
are not final sequences. In affinity tasks keep both input proteins unchanged.
After optional calls and their results, return final candidates with empty tool_calls.

For plate_completion, supply additional distinct designs up to the requested count,
excluding already selected sequences. You may use multiple panels and optional
code/tools; do not cap open design by a library preview size. Validation sub-panels
follow validation_request instead of the whole-plate target.
