# SPDX-License-Identifier: MIT
"""Optional separate-interpreter ESMC backend, sharing the existing scientific implementation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .esmc import TransformersBackend


class IsolatedESMCBackend:
    """Venv/Conda dependency isolation, not an OS sandbox or persistent model server.

Each uncached operation reloads weights. Native mode remains available for high
throughput; the campaign's existing per-sequence/mask cache still applies here.
"""
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.resolver = TransformersBackend(store, config)
        self._identity = None

    def _call(self, operation, data, *, download=False):
        interpreter = Path(self.config.worker_python)
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise RuntimeError("Configured ESMC worker Python is missing")
        snapshot, revision = self.resolver.resolve(download=download)
        with tempfile.TemporaryDirectory(prefix="proteinrsi-esmc-") as directory:
            root = Path(directory)
            cfg = self.config.model_dump()
            cfg["worker_python"] = None
            payload = {"operation": operation, "data": data, "config": cfg,
                       "snapshot": snapshot, "revision": revision}
            request = root / "request.json"
            request.write_text(json.dumps(payload))
            env = {"PATH": str(interpreter.parent)+":/usr/bin:/bin", "HOME": str(root),
                   "TMPDIR": str(root), "PYTHONNOUSERSITE": "1", "HF_HUB_OFFLINE": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "OMP_NUM_THREADS": "2",
                   "OPENBLAS_NUM_THREADS": "1", "LANG": "C.UTF-8",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            # Device indices keep their host meaning; no inherited provider credentials.
            worker = Path(__file__).with_name("isolated_worker.py")
            with (root / "stdout.log").open("wb") as out, (root / "stderr.log").open("wb") as err:
                process = subprocess.Popen([str(interpreter), str(worker), str(request)], cwd=root,
                    env=env, stdout=out, stderr=err, start_new_session=True, shell=False)
                try:
                    code = process.wait(timeout=self.config.worker_timeout_seconds)
                except BaseException:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
            if code:
                # Preserve controlled diagnostics without leaking request contents into exceptions.
                self.store.event("esmc_worker_failed", {"operation": operation, "returncode": code})
                raise RuntimeError("ESMC worker failed; verify isolated dependencies, weights and device")
            output = root / "result.json"
            if not output.is_file() or output.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Missing/oversized ESMC worker response")
            return json.loads(output.read_text())

    @property
    def identity(self):
        if self._identity is None:
            self._identity = self._call("identity", {})
        return {**self._identity, "execution": "isolated-python",
                "worker_python": self.config.worker_python}

    def load(self, *, download=False):
        self._identity = self._call("load", {}, download=download)

    def embed(self, sequences):
        return self._call("embed", {"sequences": sequences})

    def masked(self, reference, positions):
        return self._call("masked", {"reference": reference, "positions": positions})
