# Contributing

Install `pip install -e '.[dev,graph,gepa,mcp]'` and run `pytest -q`, `ruff check .`, and `python -m build`.

Keep contracts strict and backward-compatible. Add tests for sequence/chain identity, independent budget accounting, replay idempotency, QC handling and data visibility when changing relevant code. Do not replace errors with synthetic scientific results.

Clearly distinguish original code, external API integration and copied upstream code. New source vendoring requires complete licensing/provenance review. Do not commit keys, weights, real private measurements, user PDFs or hidden evaluation labels.

Changes to scientific metrics, dataset splits or promotion gates require human review. They are not ordinary evolvable Agent patches. Report what was tested locally, in CI, with a real provider, and with actual experiments separately.
