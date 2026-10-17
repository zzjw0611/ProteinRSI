# Frozen global/local portfolio diagnostic

This is a root-authored deterministic selection mechanism, stored as portable
MethodProgram data. It is not native M self-modification, an interpretation of an
existing prompt workflow, or evidence of biological efficacy. Its design is
adaptive development; publication supplies no confirmatory performance claim.

## Mechanism

The first plate uses the existing per-seed site-balanced single-substitution
initialization. Later plates exclude every previously queried identity, including
missing or failed observations, and use only finite, valid revealed measurements.
Fewer than two numeric observations trigger uniform unseen sampling.

For a 16-candidate plate, four slots are reserved for uniform unseen exploration,
six for global additive-kernel ranking, and six for local UCB acquisition around
one-substitution neighbors of the four best observed identities. Ridge strength,
outcome standardization, UCB weight and diversity penalty are fixed in the source.
Selections are disjoint. If the bounded local frontier is exhausted, unused local
slots are filled by global ranking. Missing values are never imputed.

The program receives the normal generated-Python TaskView contract. It uses only
revealed observations and supplied task/round seeds. It requires maximization,
no indels, one to four mutable positions and the standard amino-acid alphabet.
The caller must provide a compatible task, a free parent and frozen matrix;
this directory contains none of those empirical inputs.

## Frozen artifacts and execution

- policies/global_local_portfolio_v2.py: sandbox program source
- policies/global_local_portfolio_v2.json: MethodProgram with identical embedded code
- code-manifest.json: SHA-256 identities of both artifacts

The source is program data with injected context and inputs, not a standalone
script. Use the opt-in frozen-program runner described in
../../docs/FROZEN_PROGRAM_BENCHMARK.md with an explicitly frozen matrix.
Mandatory bwrap/seccomp isolation, zero LLM/model calls, no network egress and the
existing 20-second per-tool wall limit remain enforced by that runner. No policy
or stored campaign is changed automatically by publishing these files.

## Synthetic engineering checks

From a repository checkout or source distribution, run the extended contract
tests explicitly:

    pytest -q -m '' tests/test_portfolio_program_contract.py

These checks use actual bwrap and synthetic values exclusively. They cover shared
initialization, deterministic/legal novel selections, the fixed allocation and
fallbacks, missing outcomes and bounded large evidence contexts. Software checks
establish execution behavior only. No datasets, empirical results, trajectories,
assessment records or biological claims accompany this mechanism.
