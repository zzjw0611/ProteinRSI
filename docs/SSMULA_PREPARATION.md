# Pinned SSMuLA replay preparation

The natural-language launcher requires verified `processed/<landscape>` inputs;
extracting `data.zip` alone does not create them. The preparation module supports
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
