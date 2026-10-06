# Pinned SSMuLA replay preparation

The natural-language launcher requires verified `processed/<landscape>` inputs;
extracting the archive alone does not create them. The runtime preparation module supports
the reviewed original-score GB1 and ParD3 files from
[Zenodo release 15203754](https://zenodo.org/records/15203754).

Keep the complete extracted data tree **outside** the repository and installed
Python package/dependency directories. Labels are trusted-controller inputs,
never scientific artifacts or LLM resources.

```bash
python -m proteinrsi.ssmula --data-root /path/to/ssmula --landscape GB1
python -m proteinrsi.ssmula --data-root /path/to/ssmula --landscape ParD3
proteinrsi start "Use GB1 for 3 rounds, 48 new queries total, at most 16 per round" \
  --data-root /path/to/ssmula --out runs/gb1
```

The preparer validates pinned source CSV/parent FASTA hashes and reviewed sites,
expands site strings into full sequences, and preserves original reported scores.
It does **not** use `scale2max`, active/inactive labels, label-based filtering,
missing-score imputation, replicate aggregation or global extrema. Unexpected
bytes, invalid records and existing output directories fail explicitly. Unknown
releases need a separate source review, not a flag that bypasses the pin.

Only the supplied parent observation is disclosed before new queries. Prepared
tasks use open design without a candidate catalogue. Historical measurements
remain historical replay, not newly performed experiments. GB1 units describe
relative enrichment; ParD3 retains original supplied fitness units without
claiming that they are binding constants. Each assay has a distinct protocol ID.

Source data is not redistributed in the package. Preparation does not change the
source release's license or the original studies' citation obligations. Hashes
verify byte identity; they do not independently establish assay validity.

## Multi-landscape preparation without changing the runtime

`scripts/prepare_ssmula_landscapes.py` is a standalone trusted-controller adapter.
It does not change `src/proteinrsi`, contact a model/provider, run an assay, or
start a replay. Its checked-in `configs/ssmula.original_inputs.json` pins the
exact reviewed CSV and FASTA bytes, headers, sites, parent sequence digests,
source integrity counts and public provenance links. The script also pins the
manifest itself. There is no CLI override for source hashes or metadata;
synthetic tests replace pins in memory only.

```bash
python scripts/prepare_ssmula_landscapes.py \
  --data-root /path/to/ssmula --all \
  --launch-inventory /path/to/ssmula/launch-inventory.json

# Independent read-only check, including available_landscapes/load_replay:
python scripts/prepare_ssmula_landscapes.py \
  --data-root /path/to/ssmula --all --verify-only
```

The direct adapters cover GB1, ParD2, ParD3, T7, TrpB3A–TrpB3I and TrpB4.
TEV additionally uses an explicitly verified primary-source measured construct,
described below, for 15 eligible landscapes total.
`--all` verifies existing preparations without rewriting them and prepares only
missing eligible landscapes. An explicit call to `prepare()` refuses existing
output. The launch-inventory path must also be new. Every output is checked
against the original source again, including exact score text for new inputs;
the two legacy adapters are compared numerically because their original writer
serialized floats. Existing GB1 and ParD3 artifacts remain untouched.

For TrpB only, tuples containing `*` are excluded because the protein task allows
canonical amino acids. The exclusion is decided from sequence alone and counted
in provenance. Source scores are still validated on excluded records. Unexpected
symbols, malformed records, duplicate keys, missing parents and nonfinite scores
fail the whole preparation. Split amino-acid columns must agree with the tuple.
The source `active` and `# Stop` fields are never consulted. Source-score strings
are copied unchanged; full-protein output rows are ordered by sequence alone.
No candidate catalogue is created or provided to the research actor.

T7 retains its original `Fitness Mean`, including any negative values. This is
not numerically the max-scaled-and-clipped objective in the upstream `scale2max`
files. “Original score” means the supplied source column, not an assertion that
the column contains unprocessed raw assay readings.

### Proposed launch bounds and readiness

The metadata-only launch inventory proposes 20 rounds, 100 new queries per round,
2,000 new queries total, seed 17, full plates, repeat exclusion and no automatic
controls. The trusted launcher supplies exactly one parent observation outside
the 2,000-query budget. Prepared `task.json` files do not contain that value.
Existing tasks are not rewritten to update defaults; a launcher must explicitly
apply the inventory's bounds to its campaign task.

600 LLM calls and 400 tool calls per campaign are proposed hard caps, not usage or
currency estimates. Exhaustion must not automatically raise them. The inventory
records per-input hashes and source/exclusion/output counts, plus independent
catalogue/loader verification. These are integrity counts, not score summaries.

Successful preparation establishes input readiness only. It does not establish
sandbox isolation, provider availability, engine readiness, scientific validation,
RSI benefit or completion of the proposed replay. Each launch requires its own
authorization, compute configuration and guarded enforcement, or separately
explicit-approved trusted inprocess debugging labeled as weaker isolation.
Preparation never infers authorization for either mode. Do not pass the
launch inventory or its controller-only file paths to the experiment actor.

### Quarantined scientific constructs

DHFR's supplied FASTA is 219 DNA bases rather than a verified full protein parent.
Its codon keys map many-to-one to protein sequences, and upstream processing uses
`mean(exp(source fitness))` within amino-acid groups. A direct original-score
protein adapter must not silently translate the parent, select a codon row or
choose an aggregation. It remains quarantined pending a separate reviewed
objective and construct protocol.

### TEV: explicitly resolved original assay construct

The initial inventory quarantined TEV because its supplied FASTA has 236 residues
while every source `Complete Sequence` has 233. A separate primary-source review
resolved this using the exact
[FLIGHTED parent FASTA](https://github.com/vikram-sundar/FLIGHTED_public/blob/156281893e44a02bb7946a7001427f3e50df7ed0/Data/TEV_Landscape/TEV_wt_sequence.fasta)
and
[masked FASTA](https://github.com/vikram-sundar/FLIGHTED_public/blob/156281893e44a02bb7946a7001427f3e50df7ed0/Data/TEV_Landscape/TEV_mutated_sequence.fasta).
The exact SSMuLA TEV CSV matches the Git blob of the
[original FLIGHTED table](https://github.com/vikram-sundar/FLIGHTED_public/blob/156281893e44a02bb7946a7001427f3e50df7ed0/Data/TEV_Landscape/fitnesses_read_count.csv).

Preparation uses the **233-aa measured parent** and its sites **143/145/164/167**,
with a separately named `TEV FLIGHTED 233-aa` task and assay-protocol identifier.
It pins both primary FASTAs, the supplied SSMuLA FASTA, and the original CSV.
The primary files must be materialized, byte unchanged, under
`reviewed_sources/FLIGHTED_public/156281893e44a02bb7946a7001427f3e50df7ed0/`
within the external data root. There is no automatic download or guessed parent.

Every `Complete Sequence` must equal the primary parent expanded by its source
tuple. The primary masked FASTA must mark precisely the measured sites. The
236-aa bundled reference is retained only as a checked secondary reference;
its original numbering and N-terminal relationship are explicit in provenance.
`Fitness Mean` is unchanged, with no normalization, clipping or missing-variant
imputation. Earlier quarantine reasons remain recorded in the manifest as review
history. Existing preparations under the first 14-landscape manifest pin remain
read-only and undergo all current per-landscape verification checks.
