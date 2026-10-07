#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Trusted nonadaptive replay baselines, clearly separate from live LLM studies.

The entire proposal order is frozen before the controller opens the oracle.
Never send these baseline observations to a live research agent in another study.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import random
import sqlite3
from pathlib import Path

from proteinrsi.contracts import Batch, Candidate, Sample, TaskSpec, digest
from proteinrsi.goal import load_replay
from proteinrsi.lab import CSVOracle
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_candidate
from proteinrsi.reporting_metrics import charged_metrics, metric_display_names, sample_metrics

ALPHABET = "ACDEFGHIKLMNPQRSTVWY"


def metric_report(plan: dict, batches: dict, measurements: dict, charges: list[dict],
                  parent: float | None, *, top_ns=(5, 10), query_bin_size: int = 100) -> dict:
    """Only purchased observations and the already disclosed parent enter reports."""
    task = plan["task"]
    direction, reference = task.get("direction", "maximize"), task["reference_sequence"]
    initial = ([] if parent is None else [{"sample_id": "provided-parent", "sequence": reference,
                                           "value": parent, "qc": "valid"}])
    accounting = charged_metrics(batches, measurements, charges, initial=initial,
        reference=reference, direction=direction, top_ns=top_ns, query_bin_size=query_bin_size)
    rounds = []
    for bid, batch in sorted(batches.items(), key=lambda item: item[1]["round_index"]):
        row = {"round": batch["round_index"] + 1, "batch_id": bid,
               "round_metrics": sample_metrics(batch["samples"], measurements.get(bid, []),
                   reference=reference, direction=direction, top_ns=top_ns),
               "accumulated_campaign": accounting["batch_endpoints"].get(bid)}
        rounds.append(row)
    return {"direction": direction, "metric": task["metric"], "unit": task["unit"],
            "metric_display_names": metric_display_names(direction, top_ns),
            "parent_evidence_status": "recorded" if parent is not None else "not_saved_no_source_lookup",
            "top_ns": list(top_ns), "round_metrics": rounds,
            **{key: value for key, value in accounting.items() if key != "batch_endpoints"},
            "multimetric_note": "Raw-unit best (direction max/min), topNmean and avg use "
                "unique-sequence valid means. TopN requires full N; null reports insufficient "
                "data with effective N. Round/window metrics exclude the parent; accumulated "
                "campaign metrics include the known parent. Missing values are not zeros. "
                "Descriptive reporting does not alter frozen proposal plans or historical decisions."}


def summarize(out: str | Path, *, top_ns=(5, 10), query_bin_size: int = 100) -> dict:
    """Read-only legacy/new comparator report, without opening the source oracle."""
    path = Path(out).resolve() / "state.sqlite3"
    if not path.is_file():
        raise ValueError("Baseline state does not exist")
    top_ns = tuple(top_ns)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        def get(namespace, key, default=None):
            row = connection.execute("SELECT value FROM kv WHERE namespace=? AND key=?",
                                     (namespace, key)).fetchone()
            return json.loads(row[0]) if row else default
        def all_records(namespace):
            return {key: json.loads(value) for key, value in connection.execute(
                "SELECT key,value FROM kv WHERE namespace=?", (namespace,))}
        plan = get("baseline", "plan")
        if plan is None:
            raise ValueError("No frozen baseline plan")
        original = get("baseline", "report", {})
        charges = [dict(zip(("key", "resource", "amount", "fingerprint", "state"), row))
                   for row in connection.execute(
                       "SELECT key,resource,amount,fingerprint,state FROM charges ORDER BY rowid")]
        metrics = metric_report(plan, all_records("batches"), all_records("measurements"), charges,
                                original.get("provided_parent"), top_ns=top_ns,
                                query_bin_size=query_bin_size)
        return {**original, **metrics, "source_note": "Read-only saved comparator observations; "
                "no source landscape opened and no report, plan, measurement or charge backfilled."}


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
        batch_size: int, seed: int, policy: str, top_ns=(5, 10), query_bin_size: int = 100) -> dict:
    if rounds < 1 or not 2 <= batch_size <= 384:
        raise ValueError("Positive rounds and batch size 2–384 required")
    top_ns = tuple(top_ns)
    # Reject invalid reporting arguments before creating a plan or buying queries.
    charged_metrics({}, {}, [], initial=[], top_ns=top_ns, query_bin_size=query_bin_size)
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
    with store.connect() as connection:
        charges = [dict(row) for row in connection.execute(
            "SELECT key,resource,amount,fingerprint,state FROM charges ORDER BY rowid")]
    report.update(metric_report(plan, store.all("batches"), store.all("measurements"), charges,
                                parent, top_ns=top_ns, query_bin_size=query_bin_size))
    # Resume/reporting never rewrites a completed historical report or frozen plan.
    if store.get("baseline", "report") is None:
        store.put("baseline", "report", report)
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root")
    parser.add_argument("--landscape")
    parser.add_argument("--out", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--policy", choices=["uniform", "single-first"])
    parser.add_argument("--top-n", nargs="+", type=int, default=[5, 10])
    parser.add_argument("--query-bin-size", type=int, default=100)
    parser.add_argument("--report-only", action="store_true", help="Read saved observations, never the oracle")
    parser.add_argument("--report-out", help="Separate destination for a read-only descriptive report")
    args = parser.parse_args()
    destination = Path(args.report_out).resolve() if args.report_out else None
    if destination is not None and destination.is_relative_to(Path(args.out).resolve()):
        parser.error("Use a separate report destination outside the saved baseline directory")
    if args.report_only:
        result = summarize(args.out, top_ns=args.top_n, query_bin_size=args.query_bin_size)
    else:
        if not all((args.data_root, args.landscape, args.policy)):
            parser.error("Execution requires --data-root, --landscape and --policy")
        result = run(args.data_root, args.landscape, args.out, rounds=args.rounds,
            batch_size=args.batch_size, seed=args.seed, policy=args.policy,
            top_ns=args.top_n, query_bin_size=args.query_bin_size)
    if destination is not None:
        destination.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Descriptive report saved to {destination}; withhold from unfinished live studies")
    elif args.report_only:
        print(json.dumps(result, indent=2))
    else:
        print(f"Baseline persisted to {args.out}; withhold report from unfinished live studies")


if __name__ == "__main__":
    main()
