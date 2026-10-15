# Contributing

Install `pip install -e '.[dev]'` and run `pytest -q`, `ruff check .`, and `python -m build`.
The default suite runs only the core checks. Use `pytest -q -m 'not real_esmc'` for the full
regression suite, or `pytest -q tests/test_example.py -m ''` for a specific test file.
See [TESTING](docs/TESTING.md) for scope and optional engine checks.

Keep contracts strict and backward-compatible. Reuse existing test scenarios when possible.
Mark a test `core` only when it covers a distinct essential behaviour of the active loops.
Keep detailed boundary and compatibility regressions unmarked for opt-in runs; avoid a new
default case for each field or historical fix. Do not replace errors with synthetic scientific results.

Clearly distinguish original code, external API integration and copied upstream code. New source vendoring requires complete licensing/provenance review. Do not commit keys, weights, real private measurements, user PDFs or hidden evaluation labels.

Changes to scientific metrics, dataset splits or promotion gates require human review. They are not ordinary evolvable Agent patches. Report what was tested locally, in CI, with a real provider, and with actual experiments separately.
