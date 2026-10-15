# ProteinRSI

**Describe a protein research goal in natural language. An LLM plans the study, designs candidates, selects tools, learns from experimental feedback, and proposes improvements to its research methods.**

The current focus is combinatorial mutation optimization with historical measured landscapes. Agents can write analysis code and optionally call ESMC, ProteinMPNN, RFdiffusion, Protenix, and PyRosetta.

[中文](README.md) · [Quick start](#quick-start) · [Documentation](#documentation)

## How it works

Two loops share a durable study. Logical roles can use the same LLM:

- **Research loop:** goal and initial evidence → planning → candidate design / tool and code execution → measured feedback → analysis → next round.
- **Method improvement loop:** inspect the research trajectory → propose changes to workflow **W** or improver **M** → apply the study's adoption policy → use the published version in subsequent rounds.

W/M changes can include prompts, strategies, and versioned sandbox programs.

The LLM chooses the scientific approach. The controller enforces task constraints, permissions, query budgets, and durable records. Predictions and computed metrics support decisions; they do not replace measured fitness. During historical replay, only the controller reads the complete label table, and agents receive feedback through submitted queries.

New studies use typed protocols and resource references between steps. The default method policy is `direct`: statically admitted changes are adopted as **unvalidated**. Use `--method-policy validate` to compare old and new methods before adoption. Adoption alone does not establish RSI benefit.

## Quick start

Python 3.11+. Guarded historical replay requires Linux with Landlock/seccomp support.

```bash
git clone https://github.com/zzjw0611/ProteinRSI.git
cd ProteinRSI
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Offline demo: synthetic data and scripted roles; no API or model weights needed
proteinrsi demo --adaptive --out runs/demo --rounds 3
```

The demo writes `runs/demo/report.json`. See [testing](docs/TESTING.md) for development setup and checks.

## Start a real study

Copy [.env.example](.env.example) to `.env` and configure your chat model, API endpoint, and key. The CLI does not automatically load `.env`:

```bash
set -a
source .env
set +a
```

For **GB1 historical measured replay**, first [prepare verified SSMuLA data](docs/SSMULA_PREPARATION.md) outside the repository, then describe the goal:

```bash
proteinrsi start "Optimize GB1 measured fitness for 2 rounds, with 48 new queries total and at most 24 per round.
Only mutate parent positions 39, 40, 41, and 54; keep all other residues fixed.
Supply the parent and its known measured fitness as free initial evidence. All protein tools are optional; improve methods when supported by evidence." \
  --data-root ~/data/ssmula \
  --out runs/gb1
```

Agents generate variants before receiving historical measurements. Missing records return `unavailable`, not zero fitness, and still consume a query slot. Main experiments and method comparisons have separate query ledgers; actual resource usage remains recorded.

Optional launch settings:

| Option | Purpose |
| --- | --- |
| `--local-tools tools.local.json` | Use installed protein engines; [setup guide](docs/LOCAL_TOOLS.md) |
| `--method-policy validate` | Validate method changes before adoption; [evaluation policies](docs/METHOD_EVALUATION.md) |
| `--input candidates.fasta` | Supply sequences or scientific files; repeatable for ranking, structure, and other task inputs |
| `--prepare-only` | Parse and save a task without running the study; still calls the LLM |

Missing information is saved as clarification questions. Continue with `start "additional details" --continue-from runs/<study>`. Computational studies iterate over actual computed outputs; wet-lab studies wait for measured results to be imported.

## Inspect the trajectory

The terminal shows live events. Each study retains state, plans, programs, measurements, method versions, and reports.

```bash
proteinrsi status --campaign runs/gb1
proteinrsi trace --campaign runs/gb1 --follow
proteinrsi trace --campaign runs/gb1 --format html
```

Records include visible evidence, actual model requests and responses, decision summaries, tool inputs and outputs, failures, repairs, and resource usage. HTML works offline; keep its adjacent asset directory when copying. See [trace exports](docs/TRACE_EXPORTS.md).

## Documentation

| Topic | Guide |
| --- | --- |
| Architecture, roles, and the two loops | [Dual loop](docs/DUAL_LOOP.md) · [Architecture](docs/ARCHITECTURE.md) |
| Plans, tools, and generated code | [Execution contracts](docs/EXECUTION_CONTRACT_REFACTOR_PLAN.md) · [Typed dataflow](docs/DATAFLOW.md) |
| W/M comparisons, adoption, and rollback | [Method evaluation](docs/METHOD_EVALUATION.md) · [Governance](docs/METHOD_GOVERNANCE.md) |
| Protein tools and ESMC | [Local tools](docs/LOCAL_TOOLS.md) · [ESMC](docs/ESMC600M.md) |
| Data, context, and label isolation | [SSMuLA](docs/SSMULA_PREPARATION.md) · [LLM context](docs/LLM_CONTEXT.md) · [Replay isolation](docs/REPLAY_SECURITY.md) |
| Tests, validation, and releases | [Testing](docs/TESTING.md) · [Interface validation](docs/INTERFACE_REFINEMENT_VALIDATION.md) · [Changelog](CHANGELOG.md) |

## Licensing and validation scope

This is research software. Passing offline tests, completing a study, or adopting a method does not by itself demonstrate improved protein function or general RSI capability. Consult each study's original records for results and limitations; task and engine validation remain specific to the deployment.

Original code is [MIT](LICENSE); the Biomni adaptation is Apache-2.0. Distribution metadata is **MIT AND Apache-2.0**. See [reuse](docs/REUSE.md), [NOTICE](NOTICE), and [third-party terms](THIRD_PARTY.md). Model weights, experimental datasets, and credentials are not bundled and retain their own terms.
