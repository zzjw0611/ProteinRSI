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
    retry = sub.add_parser("retry-llm", help="Authorize bounded retries of a failed request without resetting checkpoints or costs")
    retry.add_argument("--campaign", required=True)
    retry.add_argument("--request-key", required=True)
    retry.add_argument("--operator", required=True)
    retry.add_argument("--reason", required=True)
    retry.add_argument("--attempts", type=int, default=1)
    from proteinrsi.method_cli import add_parser as add_method_parser
    add_method_parser(sub)
    start = sub.add_parser("start", help="Interpret a protein research goal and choose a task and feedback route")
    start.add_argument("goal", help="Describe the objective, available sequences, iteration limit and feedback expectations")
    start.add_argument("--input", action="append", default=[], help="Scientific input file (FASTA/PDB/CIF/A3M/JSON); repeatable")
    start.add_argument("--continue-from", help="Answer a saved intake clarification using the new goal text")
    start.add_argument("--out", help="New campaign directory; defaults to a timestamped runs directory")
    start.add_argument("--data-root", default=str(Path.home()/"data"/"ssmula"))
    start.add_argument("--local-tools", help="Installed tool configuration; local project config is auto-detected")
    start.add_argument("--llm-calls", type=int, default=200)
    start.add_argument("--tool-calls", type=int, default=100)
    start.add_argument("--prepare-only", action="store_true", help="Parse and save the task, without experimental queries")
    start.add_argument("--full-plate", action="store_true", help="Require exactly batch_size wells before each experimental submission")
    start.add_argument("--protocol-mode", choices=["typed", "legacy"],
                       help="New studies default to typed protocols; existing intake keeps its saved mode")
    start.add_argument("--quiet", action="store_true", help="Suppress live event summaries")
    trace = sub.add_parser("trace", help="Read the persisted operator trajectory without model calls")
    trace.add_argument("--campaign", required=True)
    trace.add_argument("--format", choices=["text", "json", "html"], default="text")
    trace.add_argument("--out", help="Output path; full JSON supports .json.gz compression")
    trace.add_argument("--full", action="store_true",
                       help="Use legacy unbounded inline HTML; JSON is always complete and streamed")
    trace.add_argument("--follow", action="store_true")
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
    init.add_argument("--method-governance", help="Operator-authored candidate failure/deferral limits")
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
    if args.command == "methods":
        from proteinrsi.method_cli import run as run_methods
        try:
            print(json.dumps(run_methods(args), ensure_ascii=False, indent=2))
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            parser.error(str(exc))
        return
    try:
        if args.command == "retry-llm":
            from proteinrsi.recovery import authorize_retry
            if not (Path(args.campaign)/"state.sqlite3").is_file():
                raise ValueError("No existing campaign database")
            output = authorize_retry(Store(args.campaign), args.request_key,
                operator=args.operator, reason=args.reason, attempts=args.attempts)
        elif args.command == "start":
            from datetime import datetime
            from proteinrsi.goal import prepare_research_goal
            from proteinrsi.replay.controller import run_replay
            from proteinrsi.replay.sandbox import probe, SandboxUnavailable
            if not args.prepare_only:
                security = probe()
                if not security["available"]:
                    raise SandboxUnavailable(security["reason"])
            out = Path(args.continue_from or args.out) if (args.continue_from or args.out) else Path("runs") / datetime.now().strftime("research-%Y%m%d-%H%M%S-%f")
            local = Path(args.local_tools) if args.local_tools else None
            if local is None:
                options = [Path.cwd()/"configs"/"protein_tools.local.json",
                           Path(__file__).resolve().parents[2]/"configs"/"protein_tools.local.json"]
                local = next((p for p in options if p.is_file()), None)
            from proteinrsi.trajectory import export_html, print_event
            prepared = prepare_research_goal(args.goal, out=out, data_root=Path(args.data_root),
                local_tools=local, llm_calls=args.llm_calls, tool_calls=args.tool_calls,
                event_sink=None if args.quiet else print_event, inputs=args.input,
                continue_from=bool(args.continue_from), full_plate=args.full_plate, protocol_mode=args.protocol_mode)
            if prepared['questions']:
                print(json.dumps({'status': 'needs_clarification', **prepared,
                    'continue': f'proteinrsi start "补充说明" --continue-from {out}'}, ensure_ascii=False, indent=2))
                return
            campaign, dataset = prepared['campaign'], prepared['dataset']
            task = TaskSpec.model_validate(campaign.state["task"])
            print(json.dumps({"campaign": str(out.resolve()), "goal": task.objective_description,
                "rounds": task.max_rounds, "new_queries": task.budget.experimental_wells,
                "batch_size": task.batch_size, "batch_fill_policy": task.batch_fill_policy, "task_kind": task.kind.value, "route": prepared["route"],
                "parent_fitness": task.initial_parent_measurement.value if task.initial_parent_measurement else None,
                "parent_query_cost": 0, "resource_selection": "llm",
                "protocol_mode": campaign.store.get("configuration", "research")["protocol_mode"],
                "llm_call_limit": args.llm_calls, "tool_call_limit": args.tool_calls},
                ensure_ascii=False, indent=2), flush=True)
            if not args.prepare_only:
                llm = JSONLLM.from_env(campaign.store)
                campaign = Campaign(campaign.store, Team(campaign.store, llm), MetaAgent(llm, campaign.store))
                campaign.store.event("run_started", {})
                try:
                    if prepared['route'] == 'measured_replay':
                        run_replay(campaign, dataset, guarded=True)
                    elif prepared['route'] == 'computational':
                        from proteinrsi.computational import run_computational
                        run_computational(campaign)
                    else:
                        from proteinrsi.replay.broker import GuardedTeam, GuardedMetaAgent
                        campaign.team = GuardedTeam.from_team(campaign.team)
                        campaign.meta_agent = GuardedMetaAgent(campaign.team)
                        campaign.prepare()
                        campaign.store.event('waiting_for_laboratory', {
                            'batch_id': campaign.state['pending_batch'],
                            'notice': 'Approve and import actual laboratory results to continue'})
                except Exception as exc:
                    campaign.store.event("run_failed", {"error_type": type(exc).__name__})
                    raise
                finally:
                    export_html(out, out/"trajectory.html")
                    (out/"report.json").write_text(json.dumps(campaign.report(), ensure_ascii=False, indent=2)+"\n")
            output = campaign.report()
            output["trajectory"] = (export_html(out, out/"trajectory.html") if args.prepare_only
                                    else str((out/"trajectory.html").resolve()))
            (out/"report.json").write_text(json.dumps(output, ensure_ascii=False, indent=2)+"\n")
        elif args.command == "trace":
            from proteinrsi.trajectory import (export_html, export_json, iter_events,
                                               print_event, follow, write_json)
            if args.full and args.format == "text":
                raise ValueError("--full applies to HTML; JSON exports are always complete")
            if args.follow:
                if args.format != "text" or args.out or args.full:
                    raise ValueError("--follow requires text output to the terminal")
                follow(args.campaign)
                return
            if args.format == "html":
                destination = args.out or str(Path(args.campaign)/"trajectory.html")
                output = {"trajectory": export_html(args.campaign, destination, full=args.full)}
            elif args.format == "json":
                if args.out:
                    export_json(args.campaign, args.out)
                else:
                    write_json(args.campaign, sys.stdout)
                return
            else:
                import contextlib
                with (open(args.out, "w") if args.out else contextlib.nullcontext(sys.stdout)) as stream:
                    for event in iter_events(args.campaign):
                        print_event(event, stream)
                return
        elif args.command == "sandbox-check":
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
                gate=GatePolicy.model_validate(load_json(args.gate)) if args.gate else None,
                governance_config=load_json(args.method_governance) if args.method_governance else None)
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
                from proteinrsi.trajectory import export_html, print_event
                campaign.store.event_sink = print_event
                campaign.store.event("run_started", {"resume": True})
                try:
                    run_replay(campaign, args.dataset, guarded=args.execution == "guarded")
                except Exception as exc:
                    campaign.store.event("run_failed", {"error_type": type(exc).__name__})
                    raise
                finally:
                    directory = campaign.store.root
                    export_html(directory, directory/"trajectory.html")
                    (directory/"report.json").write_text(json.dumps(campaign.report(), ensure_ascii=False, indent=2)+"\n")
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
