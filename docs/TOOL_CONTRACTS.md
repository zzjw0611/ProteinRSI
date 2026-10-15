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

`execution_contract` describes variables supplied by an executor rather than by
model arguments. For `research_python`, it is derived from the actual TaskView
model and supplied unchanged in tool catalogues, compact operation catalogues,
and repair inputs. Python sees original observations/history lists and full
sequences. State, when present, is at
`context['research_context']['research_state']`; prompt references and compact
tables are presentation formats, not a second Python input API.

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
are `code`, optional JSON `inputs`, optional named `resource_refs`, and optional `artifact_refs`. The program sees
`context` (revealed TaskView), `inputs`, and an `artifacts` ref-to-path mapping; assign
a JSON object to `result`. Return Candidate objects in `result.candidates` to propose
sequences, which still undergo current-task validation. The execution receipt contains
status/logs and an `output` object holding all business fields, including candidates.
In typed protocols, bind `step:<id>.payload` to downstream `resource_refs` for this
business object, or `step:<id>.result` when the complete receipt is needed. The payload
port is registered from the tool manifest; a plan may refine its schema at `/output`,
but cannot change its path. No implicit flattening or copying step is required.
Existing native protein-tool results and older candidate receipts remain readable by
the candidate adapter. Metrics carry `evidence_kind=computed_unvalidated`; they cannot supply experimental
labels. For scientific files, call `write_artifact(name,text,kind)` and return its
descriptors in `result.artifacts`. Standard Python and NumPy are available; protein
engines are separate tools. The backend resolves `resource_refs={"panel":"resource:..."}`
into `inputs["panel"]`, checking resource identity, nominal schema and evidence scope.
Literal and referenced inputs cannot share a name. Bulk revealed context and inputs
travel through controlled sandbox files, without requiring the model to reproduce them.

Errors retain the original `status=failed` receipt. In typed task execution, Executor
repairs known code/argument failures in the current call; a failed attempt cannot
become a completed protocol output. A successful program returning a scientific
check with `passed=false` remains a successful computation and a negative check.
Authority errors, unknown side-effect completion and provider pauses do not become
code repair attempts. New studies freeze their authorized tools separately from
W/M tool preferences; optional tools are never mandatory.
