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
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--queries", type=int, default=480)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--llm-calls", type=int, default=2000)
    parser.add_argument("--tool-calls", type=int, default=1000)
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
    task = TaskSpec.model_validate(raw)
    if task.reference_sequence != GB1 or task.mutable_positions != [39,40,41,54]:
        raise ValueError("Prepared parent/sites differ from the reviewed GB1 experiment; do not silently substitute")
    if task.feedback_source != "measured_replay" or not task.candidates:
        raise ValueError("Expected a prepared measured_replay task and eligible sequence catalogue")
    raw = task.model_dump(mode="json")
    raw.update(name="GB1 budgeted experimental-fitness optimization",
        objective_description="Maximize the best experimentally measured GB1 fitness within the shared budget. Start with one paid parent query, then choose candidates from the eligible catalogue. Scientific tools are optional and only run when explicitly requested. No free validation labels or automatic model fitting.",
        direction="maximize", max_mutations=4, initial_observation_policy="parent_once", candidate_access="catalogue",
        controls_per_batch=0, max_rounds=args.rounds, batch_size=args.batch_size,
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
