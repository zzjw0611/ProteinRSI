#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Trusted nonadaptive replay baselines, clearly separate from live LLM studies.

The entire proposal order is frozen before the controller opens the oracle.
Never send these baseline observations to a live research agent in another study.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from proteinrsi.contracts import Batch, Candidate, Sample, TaskSpec, digest
from proteinrsi.goal import load_replay
from proteinrsi.lab import CSVOracle
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_candidate

ALPHABET = "ACDEFGHIKLMNPQRSTVWY"


def proposals(task: TaskSpec, count: int, seed: int, policy: str) -> list[str]:
    """Generate identities from task constraints alone, with no membership oracle."""
    n = len(task.mutable_positions)
    if not 1 <= n <= 4 or task.max_mutations != n or task.allow_indels:
        raise ValueError("Baseline requires an explicitly reviewed full 1–4-site substitution space")
    if count < 1 or count >= 20 ** n:
        raise ValueError("Query budget must fit distinct non-parent identities")
    if policy not in {"uniform", "single-first"}:
        raise ValueError("Unknown baseline policy")
    rng = random.Random(seed)
    reference = task.reference_sequence
    selected, seen = [], {reference}
    if policy == "single-first":
        singles = [reference[:p-1] + aa + reference[p:]
                   for p in task.mutable_positions for aa in ALPHABET if aa != reference[p-1]]
        rng.shuffle(singles)
        selected.extend(singles[:count])
        seen.update(selected)
    # A seeded shuffled enumeration has no phenotype- or availability-dependent choice.
    order = list(range(20 ** n))
    rng.shuffle(order)
    for index in order:
        if len(selected) == count:
            break
        sequence = list(reference)
        for position in task.mutable_positions:
            sequence[position - 1] = ALPHABET[index % 20]
            index //= 20
        sequence = "".join(sequence)
        if sequence not in seen:
            validate_candidate(task, Candidate(sequence=sequence))
            selected.append(sequence)
            seen.add(sequence)
    return selected


def run(data_root: str | Path, landscape: str, out: str | Path, *, rounds: int,
        batch_size: int, seed: int, policy: str) -> dict:
    if rounds < 1 or not 2 <= batch_size <= 384:
        raise ValueError("Positive rounds and batch size 2–384 required")
    root, out = Path(data_root), Path(out)
    directory = root / "processed" / landscape
    task = TaskSpec.model_validate_json((directory / "task.json").read_text())
    proposal_order = proposals(task, rounds * batch_size, seed, policy)
    plan = {"schema_version": 1, "policy": policy, "seed": seed, "rounds": rounds,
            "batch_size": batch_size, "queries": rounds * batch_size,
            "task": task.model_dump(mode="json"), "sequences": proposal_order,
            "policy_note": "Nonadaptive scripted comparator; not an LLM or RSI claim"}
    store = Store(out)
    store.put("baseline", "plan", plan, immutable=True)
    store.configure_budget({"experimental_wells": rounds * batch_size})
    # All proposal identities are durably fixed before any measurement is loaded.
    raw, dataset = load_replay(root, landscape)
    task = TaskSpec.model_validate(raw)
    store.put("baseline", "source", {"sha256": file_sha256(dataset), "plan_sha256": digest(plan)},
              immutable=True)
    oracle = CSVOracle(dataset, task)
    parent = task.initial_parent_measurement.value
    actual_parent = oracle._labels.get(task.reference_sequence)
    if actual_parent != (parent, "valid"):
        raise ValueError("Provided parent differs from pinned oracle")
    better = max if task.direction == "maximize" else min
    best, best_sequence, observations, curve = parent, task.reference_sequence, [], []
    campaign_id = "baseline-" + digest(plan)[:20]
    for round_index in range(rounds):
        batch_id = f"{campaign_id}-{round_index:03d}"
        batch = Batch(batch_id=batch_id, campaign_id=campaign_id, round_index=round_index,
            evidence_version="nonadaptive-frozen-plan", meta_version="none",
            samples=[Sample(sample_id=f"{batch_id}-{i:03d}", arm="baseline",
                workflow_version=policy, candidate=Candidate(sequence=sequence, source=policy))
                for i, sequence in enumerate(proposal_order[round_index*batch_size:(round_index+1)*batch_size])])
        rows = store.get("measurements", batch_id)
        if rows is None:
            with store.transaction():
                store.reserve(batch_id, "experimental_wells", len(batch.samples), batch.model_dump())
                store.settle(batch_id)
                store.put("batches", batch_id, batch.model_dump(), immutable=True)
            rows = [observation.model_dump(mode="json") for observation in oracle.measure(batch)]
            store.put("measurements", batch_id, rows, immutable=True)
        observations.extend(rows)
        for observation in rows:
            if observation["qc"] == "valid" and better(best, observation["value"]) != best:
                best, best_sequence = observation["value"], observation["sequence"]
        curve.append({"round": round_index + 1, "cumulative_queries": len(observations),
            "best_measured_value": best, "best_sequence": best_sequence,
            "valid_queries": sum(o["qc"] == "valid" for o in observations),
            "unavailable_queries": sum(o["qc"] == "unavailable" for o in observations)})
    report = {"landscape": landscape, "policy": policy, "seed": seed, "rounds": rounds,
        "batch_size": batch_size, "queries": len(observations), "budget": store.usage(),
        "provided_parent": parent, "best_measured_value": best, "best_sequence": best_sequence,
        "curve": curve, "repeat_queries": len(observations)-len({o["sequence"] for o in observations}),
        "proposal_sha256": digest(plan), "dataset_sha256": file_sha256(dataset),
        "interpretation": "Separate equal-query nonadaptive comparator. Missing lookups cost a query; no resampling, free membership tests, or LLM calls. A single seed is not a superiority or RSI efficacy test."}
    store.put("baseline", "report", report)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--landscape", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--policy", choices=["uniform", "single-first"], required=True)
    args = parser.parse_args()
    run(args.data_root, args.landscape, args.out, rounds=args.rounds, batch_size=args.batch_size,
        seed=args.seed, policy=args.policy)
    print(f"Baseline persisted to {args.out}; withhold report from unfinished live studies")


if __name__ == "__main__":
    main()
