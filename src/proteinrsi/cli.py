# SPDX-License-Identifier: MIT
"""Command-line entrypoints. External APIs and experiment submission are always explicit."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import GatePolicy, MetaPolicy, Patch, TaskSpec, Workflow
from proteinrsi.lab import CSVOracle, read_results
from proteinrsi.llm import JSONLLM
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.tools import ToolGateway


def load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def attach(directory: str, args) -> Campaign:
    store = Store(directory)
    llm = JSONLLM.from_env(store) if getattr(args, "agent", "deterministic") == "llm" else None
    tools = ToolGateway(store, allow_egress=getattr(args, "allow_data_egress", False))
    if getattr(args, "tools", None):
        from proteinrsi.integrations.mcp import load_bindings
        load_bindings(tools, args.tools)
    return Campaign(store, Team(store, llm, tools), MetaAgent(llm))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="proteinrsi")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize one persistent campaign")
    init.add_argument("--task", required=True)
    init.add_argument("--out", required=True)
    init.add_argument("--workflow")
    init.add_argument("--meta")
    init.add_argument("--gate")
    demo = sub.add_parser("demo", help="Run an explicitly artificial, no-API smoke experiment")
    demo.add_argument("--out", required=True)
    demo.add_argument("--rounds", type=int, default=5)
    demo.add_argument("--seed", type=int, default=17)
    for name in ("step", "status", "approve", "cancel", "import-results", "replay", "patch", "evaluate-meta", "graph"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--campaign", required=True)
        if name in ("step", "import-results", "replay", "graph", "evaluate-meta"):
            cmd.add_argument("--agent", choices=["deterministic", "llm"], default="deterministic")
            cmd.add_argument("--tools", help="Operator-authored MCP binding JSON")
            cmd.add_argument("--allow-data-egress", action="store_true")
        if name in ("approve", "cancel"):
            cmd.add_argument("--batch", required=True)
            cmd.add_argument("--operator", required=True)
        if name in ("import-results", "patch"):
            cmd.add_argument("--file", required=True)
        if name == "replay":
            cmd.add_argument("--dataset", required=True)
        if name == "evaluate-meta":
            cmd.add_argument("--cases", required=True)
            cmd.add_argument("--promote", action="store_true")
        if name == "graph":
            cmd.add_argument("--resume", help="JSON file with explicit approval/measurement payload")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            campaign = Campaign.initialize(args.out, TaskSpec.model_validate(load_json(args.task)),
                workflow=Workflow.model_validate(load_json(args.workflow)) if args.workflow else None,
                meta=MetaPolicy.model_validate(load_json(args.meta)) if args.meta else None,
                gate=GatePolicy.model_validate(load_json(args.gate)) if args.gate else None)
            output = campaign.report()
        elif args.command == "demo":
            from proteinrsi.synthetic import make_fixture
            out = Path(args.out)
            if (out / "campaign" / "state.sqlite3").exists():
                raise ValueError("Demo output exists; use a new directory to preserve previous results")
            task_file, labels = make_fixture(out / "fixture", seed=args.seed, rounds=args.rounds)
            task = TaskSpec.model_validate(load_json(str(task_file)))
            campaign = Campaign.initialize(str(out / "campaign"), task)
            oracle = CSVOracle(labels, task)
            while (batch := campaign.prepare()) is not None:
                campaign.approve(batch.batch_id, operator="synthetic-demo-driver")
                campaign.ingest(oracle.measure(batch))
            output = campaign.report()
            (out / "report.json").write_text(json.dumps(output, indent=2) + "\n")
        else:
            campaign = attach(args.campaign, args)
            if args.command == "step":
                campaign.prepare()
            elif args.command == "approve":
                campaign.approve(args.batch, operator=args.operator)
            elif args.command == "cancel":
                campaign.cancel_prepared(args.batch, operator=args.operator)
            elif args.command == "import-results":
                campaign.ingest(read_results(args.file))
            elif args.command == "patch":
                campaign.stage_patch(Patch.model_validate(load_json(args.file)))
            elif args.command == "replay":
                task = TaskSpec.model_validate(campaign.state["task"])
                oracle = CSVOracle(args.dataset, task)
                while (batch := campaign.prepare()) is not None:
                    campaign.approve(batch.batch_id, operator="explicit-replay-driver")
                    campaign.ingest(oracle.measure(batch))
            elif args.command == "evaluate-meta":
                from proteinrsi.evaluation import evaluate_meta, read_cases
                factory = (lambda s: Team(s, JSONLLM.from_env(s))) if args.agent == "llm" else None
                output = evaluate_meta(campaign, read_cases(args.cases), promote=args.promote, team_factory=factory)
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return
            elif args.command == "graph":
                from proteinrsi.integrations.langgraph import invoke_persistent
                output = invoke_persistent(campaign, load_json(args.resume) if args.resume else None)
                print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
                return
            output = campaign.report()
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, PermissionError, KeyError, ImportError, OSError) as exc:
        print(f"proteinrsi: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
