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
    sub.add_parser("sandbox-check", help="Probe Linux Landlock/seccomp prerequisites; no model or API calls")
    prompts = sub.add_parser("prompts", help="Inspect role prompt templates and snapshot versions")
    prompts.add_argument("--campaign")
    prompts.add_argument("--role", default="all")
    init = sub.add_parser("init", help="Initialize one persistent campaign")
    init.add_argument("--task", required=True)
    init.add_argument("--out", required=True)
    init.add_argument("--workflow")
    init.add_argument("--meta")
    init.add_argument("--gate")
    init.add_argument("--research-config", help="Operator-authored resource selection and plan limits")
    init.add_argument("--research-mode", choices=["adaptive", "fixed"], default=None,
                      help="New CLI campaigns default to adaptive; fixed retains the v0.3 team path")
    research = sub.add_parser("research", help="Inspect plans/resources or analyze revealed data, without LLM/model calls")
    research.add_argument("action", choices=["plans", "resources", "analyze"])
    research.add_argument("--campaign", required=True)
    init.add_argument("--local-tools", help="Operator-authored isolated local tool configuration JSON")
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
    inventory = sub.add_parser("tools", help="Inspect local descriptions/environments without model inference")
    inventory.add_argument("action", choices=["list", "describe", "doctor"])
    inventory.add_argument("--config", help="Local environment JSON; no secrets or LLM-generated commands")
    inventory.add_argument("--name")
    inventory.add_argument("--probe", action="store_true", help="Explicitly import libraries in their isolated environments")
    inventory.add_argument("--strict", action="store_true", help="Exit nonzero if an enabled engine is not configured")
    artifact = sub.add_parser("artifact-import", help="Explicitly register a scientific input file")
    artifact.add_argument("--campaign", required=True)
    artifact.add_argument("--file", required=True)
    artifact.add_argument("--kind", required=True, choices=["pdb", "cif", "a3m", "fasta", "json"])
    demo = sub.add_parser("demo", help="Run an explicitly artificial, no-API smoke experiment")
    demo.add_argument("--out", required=True)
    demo.add_argument("--rounds", type=int, default=5)
    demo.add_argument("--seed", type=int, default=17)
    demo.add_argument("--adaptive", action="store_true", help="Exercise the typed research loop with scripted roles, not an LLM")
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
            cmd.add_argument("--execution", choices=["guarded", "inprocess"], default="guarded",
                help="Guarded research worker is default. inprocess is explicit trusted debugging, not label-isolation validation.")
        if name == "evaluate-meta":
            cmd.add_argument("--cases", required=True)
            cmd.add_argument("--promote", action="store_true")
            cmd.add_argument("--execution", choices=["guarded", "inprocess"], default="guarded")
        if name == "graph":
            cmd.add_argument("--resume", help="JSON file with explicit approval/measurement payload")
    args = parser.parse_args(argv)
    try:
        if args.command == "sandbox-check":
            from proteinrsi.replay.sandbox import probe
            output = probe()
        elif args.command == "prompts":
            from proteinrsi.prompting import packaged_prompts
            data = (Store(args.campaign).get("configuration", "prompt_bundle") if args.campaign else None)
            texts = data["templates"] if data else packaged_prompts()
            if args.role != "all" and args.role not in texts:
                raise ValueError("Unknown role")
            output = data or {"templates": texts, "origin": "packaged_defaults"}
            if args.role != "all":
                output = {"role": args.role, "text": texts[args.role]}
        elif args.command == "research":
            from proteinrsi.research.analysis import persist_analysis
            store = Store(args.campaign)
            if store.get("campaign", "state") is None:
                raise ValueError("Initialize the campaign first")
            if args.action == "plans":
                output = store.all("research_runs")
            elif args.action == "resources":
                output = store.all("resource_selections")
            else:
                with store.lock():
                    output = persist_analysis(Campaign(store).view(), store)
        elif args.command == "tools":
            from proteinrsi.localtools.config import LocalToolsConfig, load_config
            from proteinrsi.localtools.catalog import description, descriptions
            from proteinrsi.localtools.registry import configured_names, doctor
            config = load_config(args.config) if args.config else LocalToolsConfig()
            if args.action == "describe":
                if not args.name:
                    raise ValueError("tools describe requires --name")
                output = description(args.name)
            elif args.action == "doctor":
                output = doctor(config, probe=args.probe)
                if args.strict and any(r["status"] == "missing_requirements" for r in output):
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                    raise SystemExit(2)
            else:
                configured = set(configured_names(config))
                output = [{"name": d["name"], "engine": d["engine"], "capability": d["capability"],
                    "configured": (config.esmc is not None) if d["engine"] == "esmc600m" else d["name"] in configured,
                    "note": "ESMC configuration is campaign-specific" if d["engine"] == "esmc600m" else
                    "configured does not mean installed, inference-tested, or scientifically validated"}
                    for d in descriptions()]
        elif args.command == "artifact-import":
            from proteinrsi.localtools.artifacts import ArtifactStore
            store = Store(args.campaign)
            if store.get("campaign", "state") is None:
                raise ValueError("Initialize the campaign first")
            with store.lock():
                output = ArtifactStore(store).put(args.file, args.kind)
        elif args.command == "init":
            from proteinrsi.protein.esmc import ESMCConfig
            from proteinrsi.protein.tools import TOOL_NAMES
            from proteinrsi.localtools.config import LocalToolsConfig, load_config
            from proteinrsi.localtools.registry import configured_names, doctor
            local_config = load_config(args.local_tools) if args.local_tools else LocalToolsConfig()
            protein_config = None
            if args.protein_model == "esmc600m":
                protein_config = (ESMCConfig.model_validate(load_json(args.protein_config)) if args.protein_config
                                  else (local_config.esmc or ESMCConfig()))
                if args.device:
                    protein_config.device = args.device
            elif args.protein_config or args.device or local_config.esmc is not None:
                raise ValueError("Protein configuration requires --protein-model esmc600m")
            # Preflight and persist the effective override, not an unused template interpreter.
            local_config.esmc = protein_config
            problems = [r for r in doctor(local_config) if r["status"] == "missing_requirements"]
            if problems:
                raise ValueError("Local tool preflight failed: " + json.dumps(problems, ensure_ascii=False))
            workflow = Workflow.model_validate(load_json(args.workflow)) if args.workflow else Workflow()
            if protein_config is not None and not args.workflow:
                workflow.tool_names = list(TOOL_NAMES)
                workflow.skill_names = [*workflow.skill_names, "esmc600m-analysis"]
            if not args.workflow:
                workflow.tool_names += ["research_fit_predict"]
                if load_json(args.task).get("candidates"):
                    workflow.tool_names += ["library_check", "library_sample"]
                # C may ask, but this upper bound never requires any actual call.
                workflow.analysis_tool_rounds = 3
                workflow.tool_names += configured_names(local_config)
                if any(e.enabled for e in local_config.engines.values()):
                    workflow.analysis_tool_rounds = 2
                    workflow.skill_names += ["local-protein-tools"]
            from proteinrsi.research.contracts import ResearchConfig
            research_config = ResearchConfig.model_validate(load_json(args.research_config)) if args.research_config else ResearchConfig()
            if args.research_mode is not None:
                research_config.enabled = args.research_mode == "adaptive"
            campaign = Campaign.initialize(args.out, TaskSpec.model_validate(load_json(args.task)),
                workflow=workflow, protein_config=protein_config, local_tools=local_config, research_config=research_config,
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
            from proteinrsi.research.contracts import ResearchConfig
            campaign = Campaign.initialize(str(out / "campaign"), task,
                research_config=ResearchConfig() if args.adaptive else None)
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
                from proteinrsi.replay.controller import run_replay
                run_replay(campaign, args.dataset, guarded=args.execution == "guarded")
            elif args.command == "evaluate-meta":
                from proteinrsi.evaluation import evaluate_meta, read_cases
                def factory(store):
                    llm = JSONLLM.from_env(store) if args.agent == "llm" else None
                    tools = ToolGateway(store, allow_egress=args.allow_data_egress)
                    if args.tools:
                        from proteinrsi.integrations.mcp import load_bindings
                        load_bindings(tools, args.tools)
                    if args.execution == "guarded":
                        from proteinrsi.replay.broker import GuardedTeam
                        return GuardedTeam(store, llm, tools)
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
