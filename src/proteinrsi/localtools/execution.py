# SPDX-License-Identifier: MIT
"""Environment-isolated jobs, no shell, sanitized environment, bounded output and time."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from proteinrsi.contracts import digest
from proteinrsi.storage import Store
from .artifacts import file_sha256
from .config import EngineConfig

ENTRYPOINTS = {"proteinmpnn": "protein_mpnn_run.py", "rfdiffusion": "scripts/run_inference.py",
               "protenix": "runner/inference.py"}
IMPORTS = {"proteinmpnn": ["torch", "numpy"], "rfdiffusion": ["torch", "hydra", "dgl"],
           "protenix": ["torch", "protenix", "Bio"], "pyrosetta": ["pyrosetta"]}


def inspect_engine(name: str, config: EngineConfig, *, probe: bool = False) -> dict:
    """Preflight is not a claim of successful inference or scientific validation."""
    if not config.enabled:
        return {"engine": name, "status": "disabled", "problems": []}
    problems = []
    if not config.license_reviewed:
        problems.append("Review exact upstream code/weight terms and set license_reviewed=true")
    if config.runtime == "docker":
        if not config.image or not shutil.which("docker"):
            problems.append("Pinned Docker image and working Docker CLI required")
    elif not Path(config.python).is_file() or not os.access(config.python, os.X_OK):
        problems.append("Missing executable: " + config.python)
    repo = Path(config.repo)
    actual_revision = None
    if name in ENTRYPOINTS:
        if not (repo / ENTRYPOINTS[name]).is_file():
            problems.append("Missing upstream entrypoint: " + str(repo / ENTRYPOINTS[name]))
        if not config.revision:
            problems.append("Pin revision to a full 40-character Git commit")
        try:
            actual_revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                                       stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
            if actual_revision != config.revision:
                problems.append("Upstream revision differs from configuration")
            dirty = subprocess.run(["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--"], timeout=10)
            if dirty.returncode:
                problems.append("Tracked upstream files are modified")
        except (OSError, subprocess.SubprocessError):
            problems.append("Cannot verify upstream Git checkout")
    if name != "pyrosetta" and not config.asset_sha256:
        problems.append("Provide asset_sha256 pins for actual model weights and required data")
    required = {"proteinmpnn": f"{config.model_name}.pt", "rfdiffusion": f"{config.model_name}.pt",
                "protenix": f"checkpoint/{config.model_name}.pt"}.get(name)
    if required and required not in config.asset_sha256:
        problems.append("Required pinned asset: " + required)
    if name == "protenix" and not any(p.startswith("common/") for p in config.asset_sha256):
        problems.append("Pin Protenix inference CCD/data assets under common/")
    for relative, expected in config.asset_sha256.items():
        path = Path(config.assets) / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(Path(config.assets).resolve()):
            problems.append("Missing/unsafe asset: " + relative)
        elif file_sha256(path) != expected:
            problems.append("Asset hash mismatch: " + relative)
    runtime = None
    if probe and not problems:
        try:
            if config.runtime == "docker":
                # Offline: never pull an image silently.
                command = ["docker", "run", "--rm", "--pull=never", "--network=none",
                           config.image, config.container_python, "-c"]
            else:
                command = [config.python, "-c"]
            code = ("import importlib, json, sys; "
                    f"mods={IMPORTS[name]!r}; "
                    "print(json.dumps({'python':sys.version,'modules':"
                    "{m:getattr(importlib.import_module(m),'__version__','imported') for m in mods}}))")
            result = subprocess.run(command + [code], env=clean_env(config, Path.cwd()),
                                    capture_output=True, text=True, timeout=60, check=True)
            runtime = json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            problems.append("Runtime probe failed: " + type(exc).__name__)
    return {"engine": name, "status": "missing_requirements" if problems else
            ("runtime_checked" if probe else "configured_not_inference_tested"),
            "problems": problems, "revision": actual_revision, "runtime": runtime}


def clean_env(config: EngineConfig, work: Path) -> dict[str, str]:
    # No inherited API keys, proxy credentials, PYTHONPATH, or model access tokens.
    return {"PATH": str(Path(config.python).parent) + ":/usr/local/bin:/usr/bin:/bin",
            "HOME": str(work), "TMPDIR": str(work), "LANG": "C.UTF-8",
            "CUDA_VISIBLE_DEVICES": config.cuda_devices,
            "OMP_NUM_THREADS": str(config.threads), "OPENBLAS_NUM_THREADS": str(config.threads),
            "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "WANDB_MODE": "disabled"}


class LocalJob:
    def __init__(self, store: Store, engine: str, config: EngineConfig):
        self.store, self.engine, self.config = store, engine, config
        self.job_id = "j-" + uuid.uuid4().hex
        self.work = store.root / "local_jobs" / self.job_id
        self.work.mkdir(parents=True)
        self.start = None

    def run(self, parameters: dict) -> dict:
        config = self.config
        check = inspect_engine(self.engine, config, probe=True)
        if check["status"] in ("disabled", "missing_requirements"):
            raise RuntimeError("Engine unavailable: " + json.dumps(check, ensure_ascii=False))
        pin_key = self.engine
        pin = {"config": config.model_dump(mode="json"), "revision": check["revision"], "runtime": check.get("runtime")}
        self.store.put("engine_pins", pin_key, pin, immutable=True)
        request = {"engine": self.engine, "parameters": parameters,
                   "repo": config.repo, "assets": config.assets, "model_name": config.model_name,
                   "require_cuda": bool(config.cuda_devices)}
        worker_dir = Path(__file__).parent.resolve()
        if config.runtime == "docker":
            request.update(repo="/engine", assets="/assets")
            command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=512",
                       "--name", "proteinrsi-" + self.job_id,
                       "--user", f"{os.getuid()}:{os.getgid()}", "--workdir=/work",
                       "--mount", f"type=bind,src={self.work},dst=/work",
                       "--mount", f"type=bind,src={worker_dir},dst=/worker,readonly",
                       "--mount", f"type=bind,src={config.repo},dst=/engine,readonly",
                       "--mount", f"type=bind,src={config.assets},dst=/assets,readonly",
                       "--tmpfs", "/tmp:rw,nosuid,size=1g"]
            if config.cuda_devices:
                command += ["--gpus", '"device=' + config.cuda_devices + '"']
            for key, value in clean_env(config, Path("/work")).items():
                if key != "PATH":
                    command += ["--env", f"{key}={value}"]
            command += [config.image, config.container_python, "/worker/worker.py", "request.json"]
        else:
            command = [config.python, str(worker_dir / "worker.py"), "request.json"]
        (self.work / "request.json").write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
        info = {"id": self.job_id, "engine": self.engine, "state": "running", "pin": digest(pin)}
        self.store.put("local_jobs", self.job_id, info)
        self.start = time.monotonic()
        try:
            with (self.work / "stdout.log").open("wb") as out, (self.work / "stderr.log").open("wb") as err:
                process = subprocess.Popen(command, cwd=self.work, env=clean_env(config, self.work),
                                           stdout=out, stderr=err, start_new_session=True, shell=False)
                try:
                    while process.poll() is None:
                        if time.monotonic() - self.start > config.timeout_seconds:
                            raise TimeoutError("Local engine timeout")
                        size = 0
                        for p in self.work.rglob("*"):
                            if p.is_symlink():
                                raise ValueError("Engine created a symlink artifact")
                            if p.is_file():
                                size += p.stat().st_size
                        if size > config.max_output_mb * 1024 * 1024:
                            raise RuntimeError("Local job output quota exceeded")
                        time.sleep(0.1)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        if config.runtime == "docker":
                            subprocess.run(["docker", "rm", "--force", "proteinrsi-" + self.job_id],
                                capture_output=True, timeout=30, check=False)
                if process.returncode:
                    raise RuntimeError(f"{self.engine} exited {process.returncode}; inspect {self.work}/stderr.log")
            total = 0
            for file in self.work.rglob("*"):
                if file.is_symlink():
                    raise ValueError("Engine output contains a symlink")
                if file.is_file():
                    total += file.stat().st_size
            if total > config.max_output_mb * 1024 * 1024:
                raise RuntimeError("Local job output quota exceeded")
            path = self.work / "result.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Missing or oversized normalized engine result")
            result = json.loads(path.read_text())
            if not isinstance(result, dict):
                raise ValueError("Engine result must be an object")
            # The caller still validates science-specific outputs before tool completion.
            info.update(state="executed_pending_validation", wall_seconds=time.monotonic()-self.start)
            self.store.put("local_jobs", self.job_id, info)
            result["provenance"] = {"engine": self.engine, "job_id": self.job_id,
                "revision": check["revision"], "asset_sha256": config.asset_sha256,
                "wall_seconds": info["wall_seconds"], "runtime": config.runtime}
            return result
        except BaseException:
            info.update(state="failed", wall_seconds=time.monotonic()-self.start)
            self.store.put("local_jobs", self.job_id, info)
            raise
