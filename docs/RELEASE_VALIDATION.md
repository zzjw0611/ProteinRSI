# v0.5.0 local release verification

Base: GitHub `zzjw0611/ProteinRSI@0b0ea9d05ecf32392b4f232c95da46ba38480ed5`.
The reconstructed baseline tree matched `77b0e04d4c613208fac0f2f4f238b53c9f2fde74`
exactly before edits, including the owner's Responses/provider and engine fixes.

Observed local environment: Python 3.13.5, Linux x86_64.

| Check | Observed result |
|---|---|
| Baseline suite before changes | 147 passed, 5 skipped |
| Updated full pytest suite | 176 passed, 6 skipped |
| Statement coverage | 76.63% (2971/3877) |
| compileall, source and scripts | Passed |
| Fixed synthetic demo | 3 completed rounds; 34/36 query slots; no LLM/model API |
| Adaptive synthetic demo | 3 completed rounds; 34/36 query slots; read-only analysis calls, no LLM/model API |
| Prompt inspection CLI | Passed |
| Full RPC workflow contract | Team/feedback/Meta exercised with a clearly marked TEST-ONLY security double |
| Actual Landlock enforcement | Not run: current kernel returns ENOSYS (ABI=-1); libseccomp is installed |
| SVG | Exact byte copy; SHA256 checked, XML parsed, no external href/scripts, rendered and visually inspected |
| Wheel and sdist build | Passed using installed setuptools.build_meta |
| Ruff | Not run: unavailable in the offline build environment; basic AST/token checks are not a substitute |
| GitHub Actions for this release | Not run by this local verification |

The six skips are native Transformers API, opt-in actual ESMC-600M weights,
GEPA, LangGraph, MCP, and actual Landlock/guarded-process execution. Do not read
the successful mock/contract tests as validation of a paid LLM, model scientific
accuracy, GPU compatibility, new wet experiments or RSI improvement.

New tests cover unused-model zero inference, explicit scoring without embedding,
explicit revealed-data prediction, parent-once accounting, catalogue previews,
Meta sponsor charges/duplicate attempts, protected RPC operations, prompt snapshots,
strict tool result fields, and README's supplied SVG. No public SSMuLA labels were
loaded or real GB1 campaign started in this work.

Full local output is in `docs/validation/v0.5.0-local.txt`. Run the CI/dependency
suite and guarded replay tests on the actual target machine before formal use.
Legacy campaigns are readable; continuing under altered v0.5 semantics is rejected.
