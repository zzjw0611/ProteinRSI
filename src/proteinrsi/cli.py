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
    init.add_argument("--protein-model", choices=["esmc600m", "none"], default="esmc600m",
                      help="New campaigns default to real ESMC-600M; none is the offline baseline")
    init.add_argument("--protein-config", help="Operator-authored ESMC configuration JSON")
    init.add_argument("--device", choices=["cpu", "cuda"], help="Override ESMC device at initialization")
    check = sub.add_parser("esmc-check", help="Download/check the configured real ESMC-600M")
    check.add_argument("--campaign", required=True)
    check.add_argument("--download", action="store_true", help="Explicitly allow model download")
    tool = sub.add_parser("protein-tool", help="Call a configured protein tool without an LLM")
    tool.add_argument("--campaign", required=True)
    tool.add_argument("--name", required=True)
    tool.add_argument("--arguments", required=True, help="JSON file with tool arguments")
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
            from proteinrsi.protein.esmc import ESMCConfig
            from proteinrsi.protein.tools import TOOL_NAMES
            protein_config = None
            if args.protein_model == "esmc600m":
                protein_config = ESMCConfig.model_validate(load_json(args.protein_config)) if args.protein_config else ESMCConfig()
                if args.device:
                    protein_config.device = args.device
            elif args.protein_config or args.device:
                raise ValueError("Protein configuration requires --protein-model esmc600m")
            workflow = Workflow.model_validate(load_json(args.workflow)) if args.workflow else Workflow()
            if protein_config is not None and not args.workflow:
                workflow.tool_names = list(TOOL_NAMES)
                workflow.skill_names = [*workflow.skill_names, "esmc600m-analysis"]
            campaign = Campaign.initialize(args.out, TaskSpec.model_validate(load_json(args.task)),
                workflow=workflow, protein_config=protein_config,
                meta=MetaPolicy.model_validate(load_json(args.meta)) if args.meta else None,
                gate=GatePolicy.model_validate(load_json(args.gate)) if args.gate else None)
            output = campaign.report()
        elif args.command == "esmc-check":
            from proteinrsi.protein.esmc import from_store
            store = Store(args.campaign)
            model = from_store(store)
            if model is None:
                raise ValueError("This campaign has no ESMC configuration; initialize a new ESMC campaign")
            with store.lock():
                model.backend.load(download=args.download)
                vectors = model.embed(["ACDEFGHIK"])
                score = model.score_variants("ACDEFGHIK", ["AVDEFGHIK"])[0]
                output = {"model": model.identity, "embedding_shape": list(vectors.shape),
                          "smoke_log_odds": score, "evidence_kind": "computational_smoke_test"}
        elif args.command == "protein-tool":
            from proteinrsi.tools import ToolCall
            campaign = attach(args.campaign, args)
            with campaign.store.lock():
                view = campaign.view()
                output = campaign.team.tools.call(
                    ToolCall(name=args.name, arguments=load_json(args.arguments)), view.task,
                    allowed=view.workflow.tool_names, context_key="operator-protein-tool")
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
                def factory(store):
                    llm = JSONLLM.from_env(store) if args.agent == "llm" else None
                    tools = ToolGateway(store, allow_egress=args.allow_data_egress)
                    if args.tools:
                        from proteinrsi.integrations.mcp import load_bindings
                        load_bindings(tools, args.tools)
                    return Team(store, llm, tools)
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
