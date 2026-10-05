# Tool contracts and model autonomy — v0.5

No tool is mandatory in the actual LLM route. ESMC availability does not imply scoring,
embedding or fitting. A tool request includes `name`, typed `arguments`, and an optional
brief `purpose`. The gateway logs requests (including cache hits), checks permission and
input/protected fields, charges a real execution once, validates the output schema, and
persists the result. Invalid/uncertain executions are not silently resubmitted.

Implementation functions remain separate from JSON descriptions. Descriptions now include
`when_to_use`, `when_not_to_use`, `cost_hint`, `examples`, `output_semantics`, and a versioned
output schema. Example sequences/artifact hashes are interface examples, not live results.
Cost hints are qualitative unless measured; query slots, model inputs and seconds are not
interchangeable, and token usage does not automatically provide a dollar price.

## Result meanings

- ESMC score: `scores[{sequence, masked_marginal_log_odds}]`, not experimental fitness.
- ESMC suggestion: canonical full sequences with provenance, not proof of improvement.
- Embedding: a local artifact reference and `[N,1152]` shape, not a scalar phenotype.
- ProteinMPNN: candidates plus actual FASTA artifacts, full residue mapping and execution provenance.
- RFdiffusion: backbones with explicit target/design chain identities; placeholders are not candidates.
- Protenix: actual structure/confidence files, entity mapping and chosen MSA mode, not Kd.
- Rosetta: energies explicitly in REU and interface area in angstrom^2, not experimental affinity.
- Geometry/QC tools: the named descriptors only; no all-atom or stability claim from CA geometry.
- `research_fit_predict`: explicit revealed-only Ridge estimates using mutation or ESMC features.
  Returns null estimates on insufficient observations. Choose `features=mutation` to avoid ESMC.
  C may attach an estimate only by referencing the actual current `task_predictions/<hash>` artifact.
- `library_check`/`library_sample`: legal identity/availability information, never phenotype labels.

Parent/target, units, complete sequences, and allowed regions remain checked by trusted
code even when no scientific tool is requested. Functions validate additional scientific
constraints that JSON Schema cannot establish (e.g. chain identity). Default engine output
schemas reject missing/mis-typed required scientific fields rather than only checking
`evidence_kind`. Model and runtime versions remain immutable operator configuration.

The stock CLI default workflow registers contextual prediction/catalogue tools; an explicit
custom workflow must include intended tool_names. Catalogue mode removes the artificial
preview-only submission restriction, but does NOT allow querying unmeasured sequences.
A one-off invalid generated candidate can still block planning; no invented result or
automatic costly retry is permitted. Use the membership tool before expensive evaluation.

Large-model engines remain optional and deployment-specific. This release validates
contracts and fixture execution, not all GPU kernels, packages or biological performance.

## Generated analysis programs

`research_python` is a context-bound optional tool for LLM-authored Python. Arguments
are `code`, optional JSON `inputs`, and optional `artifact_refs`. The program sees
`context` (revealed TaskView), `inputs`, and an `artifacts` ref-to-path mapping; assign
a JSON object to `result`. Return Candidate objects in `result.candidates` to propose
sequences, which still undergo current-task validation. Other metrics stay inside
`output` and carry `evidence_kind=computed_unvalidated`. They cannot supply experimental
labels. For scientific files, call `write_artifact(name,text,kind)` and return its
descriptors in `result.artifacts`. Standard Python and NumPy are available; protein
engines are separate tools. Errors return `status=failed` for code/plan repair.
