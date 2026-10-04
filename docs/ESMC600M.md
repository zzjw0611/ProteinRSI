# ESMC-600M: installation, configuration and scientific semantics

ProteinRSI 0.2 has a real local protein backend, not just an MCP configuration
example. The conversational A/B/C/M LLM still uses `PROTEINRSI_*` environment
variables. **Do not set PROTEINRSI_MODEL to esmc_600m.**

## Supported implementation

The default is **`biohub/ESMC-600M-hf`**, the native Hugging Face Transformers
conversion of ESMC-600M (36 layers, 1152-dimensional representations). It is not
ESM-2, ESM3 or the legacy `esmc-600m-2024-12` SDK checkpoint. We use the native
`EsmcForMaskedLM` / `EsmcTokenizer` in **Transformers 5.16.1**. No `trust_remote_code`,
`fair-esm`, `esm` package, FlashAttention compilation, or external MCP server is
needed for this backend. Other protein-engine MCP bindings remain supported.

Primary implementation references:
- https://huggingface.co/docs/transformers/model_doc/esmc
- https://huggingface.co/biohub/ESMC-600M-hf
- https://github.com/huggingface/transformers/tree/v5.16.1/src/transformers/models/esmc

## Run a new campaign

```bash
# First install a PyTorch build appropriate for your CPU/CUDA machine.
pip install -e '.[esmc,dev]'

# Replace the task's sequence, design region and assay metadata before real use.
proteinrsi init --task examples/wetlab_task.json --out runs/esmc600m \
  --protein-config examples/esmc600m.json --device cuda

# Explicit download permission, followed by real short-sequence inference.
proteinrsi esmc-check --campaign runs/esmc600m --download

# Use your own Chat Completions-compatible provider. .env is NOT auto-loaded.
set -a
source .env
set +a
proteinrsi step --campaign runs/esmc600m --agent llm
```

Without `--device cuda`, CPU/float32 is the conservative default; CPU is useful
for smoke tests, not a speed promise for large candidate pools. The package does
not fall back from a requested unavailable GPU. `dtype=bfloat16` is optional and
requires a supported CUDA device. VRAM depends on sequence length, precision and
batch size; no fixed hardware capacity is guaranteed. Context is capped at 2046
residues plus two special tokens, with **no silent truncation**.

`init` does not download or run weights. `esmc-check --download` explicitly permits
Hugging Face downloads (public model assets, not sequence inference requests).
The resolved full checkpoint commit is saved immutably in the campaign. Subsequent
inference uses cached files only. On an offline machine, prepopulate the same HF
cache (HF_HOME), then run `esmc-check` without `--download`. HF_TOKEN, when needed,
is read by Hugging Face tooling and must not be committed. Treat the cache as
trusted, immutable model storage.

`examples/esmc600m.json` controls device, dtype, batching, context, masked-position
limit and `max_model_inputs`. These operator settings are saved at initialization;
editing the example later does not mutate an existing experiment. Explicit full
HF commit revisions are preferable for published research; `main` is resolved only
once per campaign. We do not automatically upgrade old campaigns or their weights.

New CLI `init` defaults to ESMC-600M. For an intentionally model-free baseline:

```bash
proteinrsi init --task my-task.json --out runs/baseline --protein-model none
proteinrsi demo --out runs/artificial-demo
```

The demo is always synthetic and offline. The Python `Campaign.initialize` API
retains its old model-free default unless given `protein_config=ESMCConfig()`.
Old 0.1 campaigns without protein configuration retain their original backend;
start a new campaign to compare the ESMC route without corrupting pending batches.

## Call the protein tools without an LLM

After initializing/warming the example campaign:

```bash
proteinrsi protein-tool --campaign runs/esmc600m \
  --name esmc600m_score_variants --arguments examples/esmc_score.json
proteinrsi protein-tool --campaign runs/esmc600m \
  --name esmc600m_suggest_mutations --arguments examples/esmc_suggest.json
proteinrsi protein-tool --campaign runs/esmc600m \
  --name esmc600m_embed_sequences --arguments examples/esmc_embed.json
```

The example sequences are interface examples, not validated proteins. Replace them
with your task inputs. Reference and allowed-position inputs are protected against
mismatching the campaign. When initializing without `--workflow`, the three local
tools and `esmc600m-analysis` Skill are automatically allowlisted. An explicit
custom Workflow keeps its own whitelist; include those names deliberately.

Python, using the same budget/cache:

```python
from proteinrsi.storage import Store
from proteinrsi.protein.esmc import from_store

store = Store('runs/esmc600m')
model = from_store(store)
assert model is not None
with store.lock():
    embeddings = model.embed(['ACDE', 'AVDE'])
    scores = model.score_variants('ACDE', ['AVDE'])
```

These low-level methods validate sequence syntax/context, not the campaign's full
experimental constraints; go through Team/ToolGateway for constrained task actions.
Large embedding arrays live in SQLite `plm_cache` / `embedding_artifacts`, not in
LLM prompts. The artifact reference can be read with `store.get(namespace, key)`.

## Inner loop and prediction meaning

```
A plans -> B proposes legal variants (or requests ESMC suggestions)
  -> ESMC scores post-design variants
  -> with >=2 unique valid measured variants: ESMC embeddings + ridge head
  -> C reviews evidence -> A selects -> approved experiment -> measurement
  -> head refits using revealed data -> next round
```

For position i, mask that residue in the **reference** sequence and compute
`log P(mutant_aa | masked reference) - log P(reference_aa | masked reference)`.
A multi-mutant score is the sum over changed positions. This is an explicitly
additive WT-context prior, **not** a jointly conditioned epistasis predictor.
Higher sequence plausibility is not necessarily a better task phenotype; in a
minimization task the sequence prior is not merely multiplied by minus one.

Embeddings mean-pool the final normalized hidden states over actual residues,
excluding BOS/EOS/padding. Token-to-residue identity is checked explicitly. The
lightweight task head uses these embeddings plus the workflow's additive or
pairwise mutation encoding. Feature centering/scaling and phenotype fitting use
revealed valid measurements only; technical replicates are averaged by sequence.
All observations must match metric, unit, assay protocol and evidence source.
The head is refit after feedback; ESMC weights remain frozen. The dimensionality
of the small head, ridge strength and exploration settings do not calibrate its
uncertainty automatically. We return `uncertainty=null`, not a fictitious confidence.

Before two distinct measured variants, `predicted_value=null` and the prior is
stored separately. Afterwards `predicted_value` is an **uncalibrated task-head
prediction**, never a measured result or validated Kd. Actual affinity and
structure/binder design still need appropriate external models and validation.

## Double loop, RSI and reproducibility

ESMC configuration/checkpoint access remains outside W/M's edit permissions.
The workflow may still change prompts, tool use, additive/pairwise feature
augmentation and selection rules. Those changes undergo the same experimental
trial gate. A new protein backbone or checkpoint is an operator-controlled study,
not a method improvement the Agent can claim by changing a model name.

Meta evaluations copy the same model configuration and exact resolved checkpoint
to both independent offspring stores, with equal `plm_inputs` limits. Task feature
caches are isolated between those evaluator runs. No unknown measurement becomes
accessible through the model. Historical training-data overlap is still a separate
benchmark limitation; this code does not claim pretrained models are leak-free.

`plm_inputs` counts actual uncached sequence examples: one per embedded sequence
or masked sequence context. Batch size does not change this count. It supplements
LLM/tool-call and experimental-well budgets, but is **not** GPU-seconds accounting.
Successful caches are reused across rounds; failures remain charged and do not
silently fall back or resubmit. The software versions, model SHA, dtype, device,
and adapter version are recorded. Control the same cache starting conditions when
comparing methods; cached inputs are not new experimental evidence.

## Validation and license

Core tests use an explicitly fake backend. A separate native-library test uses a
small randomly initialized ESMC to check token alignment, padding and masked logits;
it is not a 600M pretrained result. An explicitly opt-in `real_esmc` test downloads
and runs the official 600M checkpoint. See TESTING.md for actual run results and
remaining limitations. Tests are not evidence of improved protein function.

See THIRD_PARTY.md: ProteinRSI is MIT, native Transformers code is Apache-2.0,
PyTorch has its own BSD-style license. Biohub's model card currently includes
both `mit` and `other` metadata and links use conditions; retain and review the
exact downloaded revision's card/license/notices. We do not redistribute weights,
relicense earlier noncommercial releases, or grant rights to unrelated engines.


## v0.3 separate interpreter option

`worker_python` selects an absolute interpreter from a dedicated ESMC venv/Conda
installation. The unchanged native Transformers computation then runs in that
interpreter, receiving only sequences/positions and a pinned public weight path,
not the campaign database or hidden labels. Example configuration:
`configs/esmc600m.isolated.json`, or the `esmc` section of
`configs/protein_tools.json`. Install the worker with
`scripts/setup_local_tools.py --engine esmc600m --execute` after reviewing its plan.

The parent still owns all caches/input charges and resolves downloads explicitly.
Each uncached operation starts a process and reloads weights; this is not a
high-throughput resident model service. Native mode (`worker_python=null`) remains
available. The new isolated path has not repeated the v0.2 pretrained inference
validation in this build environment; run esmc-check on the deployment machine.
