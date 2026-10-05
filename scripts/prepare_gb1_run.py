#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Create an operator task from label-free prepared GB1 inputs; never read a fitness table."""
import argparse
import json
from pathlib import Path
from proteinrsi.contracts import TaskSpec
from proteinrsi.tasks import validate_task

GB1 = "MQYKLILNGKTLKGETTTEAVDAATAEKVFKQYANDNGVDGEWTYDDATKTFTVTE"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--library", help="Prepared label-free JSON array or object with sequences/candidates")
    parser.add_argument("--out", required=True)
    parser.add_argument("--full-plate", action="store_true", help="Require a whole plate on every round")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--queries", type=int, default=480)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--llm-calls", type=int, default=2000)
    parser.add_argument("--tool-calls", type=int, default=1000)
    parser.add_argument("--parent-fitness", type=float, help="Known pre-study parent measurement, not a new query")
    parser.add_argument("--parent-source", help="Source of the supplied parent measurement")
    args = parser.parse_args()
    raw = json.loads(Path(args.task).read_text())
    if args.library:
        library = json.loads(Path(args.library).read_text())
        if isinstance(library, dict):
            keys = [k for k in ("sequences", "candidates") if k in library]
            if len(keys) != 1:
                raise ValueError("Expected one explicit sequences/candidates key; do not infer label-bearing formats")
            library = library[keys[0]]
        if not isinstance(library, list) or not all(isinstance(s, str) for s in library):
            raise ValueError("Library must contain full sequence strings only; no phenotype dictionaries")
        raw["candidates"] = library
    raw.update(candidates=raw["candidates"] if args.library else [],
               candidate_access="pool" if args.library else "open")
    task = TaskSpec.model_validate(raw)
    if task.reference_sequence != GB1 or task.mutable_positions != [39,40,41,54]:
        raise ValueError("Prepared parent/sites differ from the reviewed GB1 experiment; do not silently substitute")
    if task.feedback_source != "measured_replay":
        raise ValueError("Expected a prepared measured_replay task")
    raw = task.model_dump(mode="json")
    if args.parent_fitness is not None:
        if not args.parent_source:
            parser.error("--parent-fitness requires --parent-source")
        raw["initial_parent_measurement"] = {"value": args.parent_fitness, "source_ref": args.parent_source}
    elif args.parent_source:
        parser.error("--parent-source requires --parent-fitness")
    if raw.get("initial_parent_measurement") is None:
        parser.error("Supply the known parent fitness/source, or use proteinrsi start for verified GB1 inputs")
    raw.update(name="GB1 budgeted experimental-fitness optimization",
        objective_description="Maximize the best experimentally measured GB1 fitness within the new-query budget. The parent sequence and measured fitness are supplied initial evidence with zero query cost. Design new variants from the parent and revealed evidence; use a supplied library only if explicitly requested. Scientific tools are optional and only run when explicitly requested. Method validation uses the same budget; no automatic model fitting.",
        direction="maximize", max_mutations=4, initial_observation_policy="provided_parent", candidate_access="pool" if args.library else "open",
        controls_per_batch=0, max_rounds=args.rounds, batch_size=args.batch_size,
        batch_fill_policy="full_plate" if args.full_plate else "flexible",
        budget={"experimental_wells":args.queries,"llm_calls":args.llm_calls,"tool_calls":args.tool_calls})
    updated = TaskSpec.model_validate(raw)
    validate_task(updated)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x",encoding="utf-8") as f:
        json.dump(updated.model_dump(mode="json"),f,indent=2,ensure_ascii=False)
        f.write("\n")
    print(f"Task written to {path}; no model or experimental query executed.")


if __name__ == "__main__":
    main()
