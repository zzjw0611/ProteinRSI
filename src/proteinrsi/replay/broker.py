# SPDX-License-Identifier: MIT
"""Trusted bridge: workers cannot choose their label source, budget or provider destination."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

from proteinrsi.agents import Team, MetaAgent, FeedbackAnalysis, MetaResponse
from proteinrsi.contracts import Candidate, canonical
from proteinrsi.tools import ToolCall
from .sandbox import SandboxUnavailable, probe, selected_backend, worker_command

READ_NAMESPACES = {"research_runs", "research_step_outputs", "resource_selections", "research_analysis",
    "team_outputs", "artifacts", "task_predictions", "embedding_artifacts", "batches"}
WRITE_NAMESPACES = {"research_runs", "research_step_outputs", "resource_selections", "research_analysis", "team_outputs"}
CONFIG_KEYS = {"research", "know_how", "prompt_bundle", "protein_model", "local_tools"}
ROLES = {"A", "A-plan", "A-review", "A-selection", "B", "C", "C-tools", "C-feedback", "M", "resource-selector", "A-resources"}


class WorkerExecutionError(RuntimeError):
    """A worker returned a complete failure receipt (not a transport interruption)."""
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__("Guarded worker failed: " + error_type + ": " + message)


def reader_roots():
    # Exact package roots, NOT project root/home/data directories.
    roots = {str(Path(p).resolve()) for p in sys.path if p and Path(p).is_dir()
             and ("site-packages" in p or "dist-packages" in p)}
    import sysconfig
    roots.add(sysconfig.get_path("stdlib"))
    roots.add(str(Path(__file__).resolve().parents[2]))  # src/proteinrsi
    # Conda extension modules need the matching shared libraries (e.g. SQLite),
    # not a fallback to incompatible system libraries after filesystem isolation.
    roots.update(str(p.resolve()) for p in (Path(sys.base_prefix)/"lib").glob("*.so*") if p.is_file())
    for p in ("/usr/lib", "/usr/lib64", "/lib", "/lib64"):
        if Path(p).exists():
            roots.add(str(Path(p).resolve()))
    return sorted(roots)


def _readline(process, timeout, limit=32*1024*1024):
    # Binary pipes + os.read avoids buffered-line readiness races.
    end, data = time.monotonic()+timeout, bytearray()
    while b"\n" not in data:
        remaining = end-time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise TimeoutError("Research worker response timed out")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError("Research worker exited without a complete response")
        data += chunk
        if len(data) > limit:
            raise ValueError("Oversized worker response")
    line, tail = data.split(b"\n",1)
    if tail.strip():
        raise ValueError("Worker must wait for each RPC reply")
    return json.loads(line)


def invoke_worker(team, view, operation, *, last_patch_round=-100, timeout=900):
    state = probe()
    if not state["available"]:
        raise SandboxUnavailable(state["reason"])
    backend = selected_backend()
    roots = reader_roots()
    from .bwrap_backend import assert_private_paths
    assert_private_paths([team.store.root], roots)
    from proteinrsi.research.analysis import register_analysis_tools
    from proteinrsi.research.prediction import register_prediction_tool
    from proteinrsi.research.library import register_library_tools
    gateway = team.tools.fork()
    core = register_analysis_tools(gateway, view)
    register_prediction_tool(gateway, view, team.protein_model)
    register_library_tools(gateway, view)
    from proteinrsi.research.code import register_code_tool
    register_code_tool(gateway, view)
    allowed = list(dict.fromkeys([*view.workflow.tool_names, *core]))
    catalogue = [t for t in gateway.catalog(view.task, allowed) if not t["data_egress"] or gateway.allow_egress]
    allowed = [t["name"] for t in catalogue]
    llm = team.llm
    with tempfile.TemporaryDirectory(prefix="proteinrsi-research-") as work:
        env = {"PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work, "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        if (Path(sys.base_prefix)/"lib").is_dir():
            env["LD_LIBRARY_PATH"] = str(Path(sys.base_prefix)/"lib")
        start = {"work": work, "read_roots": roots, "operation": operation,
            "sandbox_backend": backend,
            "view": view.model_dump(mode="json"), "last_patch_round": last_patch_round,
            "tools": catalogue, "allow_egress": gateway.allow_egress,
            "llm": {"model": llm.model, "base_url": llm.base_url,
                    "cache_settings": getattr(llm,"cache_settings",{})} if llm else None}
        stderr = Path(work)/"stderr.log"
        with stderr.open("wb") as err:
            proc = subprocess.Popen(worker_command(roots, work, "proteinrsi.replay.worker", backend=backend),
                cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                start_new_session=True, close_fds=True)
            def send(obj):
                proc.stdin.write((canonical(obj)+"\n").encode())
                proc.stdin.flush()
            send(start)
            provider_pause = None
            try:
                for _ in range(4096):
                    msg = _readline(proc, timeout)
                    if "done" in msg:
                        if provider_pause is not None:
                            raise provider_pause
                        proc.stdin.close()
                        if proc.wait(timeout=10) != 0:
                            raise RuntimeError("Worker exit failure")
                        team.store.event("guarded_worker_completed", {"operation": operation,
                            "pid": proc.pid, "sandbox": msg["sandbox"]})
                        return msg["done"]
                    if "failed" in msg:
                        if provider_pause is not None and msg["failed"] == "ProviderPaused":
                            # The worker receives only the exception type. Keep
                            # the controller's actionable limit/recovery detail
                            # when its continuation has finished draining.
                            raise provider_pause
                        raise WorkerExecutionError(msg["failed"], msg.get("message", ""))
                    try:
                        if provider_pause is not None and msg.get("rpc") in {"llm", "tool"}:
                            raise provider_pause
                        result = dispatch(team, gateway, view, allowed, msg)
                        send({"result": result})
                    except Exception as exc:
                        send({"error": type(exc).__name__})
                        from proteinrsi.llm import ProviderPaused
                        if isinstance(exc, ProviderPaused):
                            # Let the worker persist its continuation before exiting.
                            # No further model/tool work is allowed during this drain.
                            provider_pause = exc
                            continue
                        raise
                raise RuntimeError("Worker RPC count exceeded")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                if not proc.stdin.closed:
                    proc.stdin.close()
                proc.stdout.close()


def dispatch(team, gateway, view, allowed, msg):
    store, op = team.store, msg.get("rpc")
    if op == "tool":
        return gateway.call(ToolCall.model_validate(msg["call"]), view.task,
                            allowed=allowed, context_key=msg["context_key"])
    if op == "llm":
        if team.llm is None or msg["role"] not in ROLES:
            raise PermissionError("Unapproved LLM operation")
        # Destination/credentials/accounting are fixed in the trusted controller.
        return team.llm.complete(msg["role"],msg["instructions"],msg["context"],msg["schema"])
    if op in ("get","all","put"):
        ns = msg["namespace"]
        if op == "get" and ns == "configuration" and msg["key"] in CONFIG_KEYS:
            return store.get(ns,msg["key"],msg.get("default"))
        if op == "get" and ns == "protein_backend" and msg["key"] == "snapshot":
            return store.get(ns,msg["key"],msg.get("default"))
        if op == "get" and ns == "campaign" and msg["key"] == "state":
            # Only explicit user libraries are available here; open design has no replay index.
            state = store.get(ns,msg["key"])
            return {"task": state["task"]} if state else None
        if op == "put":
            if ns not in WRITE_NAMESPACES:
                raise PermissionError("Protected namespace")
            return store.put(ns,msg["key"],msg["value"],immutable=msg.get("immutable",False))
        if ns not in READ_NAMESPACES:
            raise PermissionError("Unapproved namespace")
        return store.all(ns) if op == "all" else store.get(ns,msg["key"],msg.get("default"))
    if op == "event":
        # Worker records cannot impersonate experiment/budget/promotion events.
        safe = {"resources_selected","research_plan_created","research_plan_revised","research_step_started",
                "research_step_completed","research_blocked","team_completed","analyst_decision",
                "design_tool_result","candidate_validation_feedback","ranking_repair_requested"}
        if msg["kind"] not in safe:
            raise PermissionError("Unapproved audit event kind")
        return store.event(msg["kind"],msg["payload"])
    if op == "remaining":
        return store.remaining(msg["resource"])
    if op == "usage":
        return store.usage()
    raise PermissionError("Unknown worker capability")


class GuardedTeam(Team):
    guarded = True
    def __init__(self, store, llm=None, tools=None, protein_model=None, *, register_tools=True):
        super().__init__(store, llm, tools, protein_model, register_tools=register_tools)
        self.analyst = GuardedFeedback(self)
    @classmethod
    def from_team(cls, team):
        guarded = cls(team.store, team.llm, team.tools, register_tools=False)
        guarded.protein_model = team.protein_model
        return guarded
    def run(self, view):
        self.bind_tools(view)
        return [Candidate.model_validate(c) for c in invoke_worker(self,view,"team")]


class GuardedFeedback:
    def __init__(self, team):
        self.team = team
    def feedback(self, view):
        return FeedbackAnalysis.model_validate(invoke_worker(self.team,view,"feedback"))


class GuardedMetaAgent(MetaAgent):
    def __init__(self, team):
        super().__init__(team.llm,team.store)
        self.team = team
    def propose(self, view, last_patch_round=-100):
        return MetaResponse.model_validate(invoke_worker(self.team,view,"meta",last_patch_round=last_patch_round))
