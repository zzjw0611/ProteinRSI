# SPDX-License-Identifier: MIT
"""Content-addressed scientific files. Agents cannot pass arbitrary filesystem paths."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil

from proteinrsi.storage import Store

KINDS = {"pdb", "cif", "a3m", "fasta", "json"}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class ArtifactStore:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.root / "artifacts"

    def put(self, source: str | Path, kind: str, *, origin: str = "operator", task_visible: bool = True) -> dict:
        source = Path(source)
        if kind not in KINDS or source.is_symlink() or not source.is_file():
            raise ValueError("Expected a regular scientific artifact of a supported kind")
        if not 0 < source.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError("Artifact size must be between 1 byte and 64 MiB")
        sha = file_sha256(source)
        key = f"{sha}.{kind}"
        self.root.mkdir(exist_ok=True)
        destination = self.root / key
        if destination.is_symlink():
            raise ValueError("Symlink artifact destination")
        if not destination.exists():
            shutil.copyfile(source, destination)
            destination.chmod(0o444)
        if file_sha256(destination) != sha:
            raise ValueError("Artifact content changed")
        record = {"ref": f"artifact:{key}", "sha256": sha, "kind": kind,
                  "bytes": destination.stat().st_size}
        # Visibility is controller-owned, separate from immutable scientific content.
        # An explicit public import can share identical bytes previously private to M.
        with self.store.transaction():
            visibility = self.store.get("artifact_visibility", key)
            if visibility is None:
                visible = task_visible or self.store.get("artifacts", key) is not None
                self.store.put("artifact_visibility", key, {"task": visible})
            elif task_visible and not visibility["task"]:
                self.store.put("artifact_visibility", key, {"task": True})
            self.store.put("artifacts", key, record, immutable=True)
        self.store.event("artifact_registered", {**record, "origin": origin, "task_visible": self.is_task_visible(record["ref"])})
        return record

    def resolve(self, reference: str, *, kind: str | None = None) -> Path:
        if not re.fullmatch(r"artifact:[0-9a-f]{64}\.(pdb|cif|a3m|fasta|json)", reference):
            raise ValueError("Expected an artifact reference; import files before calling tools")
        key = reference.split(":", 1)[1]
        record = self.store.get("artifacts", key)
        path = self.root / key
        if not record or (kind and record["kind"] != kind):
            raise ValueError("Artifact missing or wrong kind")
        if path.is_symlink() or not path.is_file() or file_sha256(path) != record["sha256"]:
            raise ValueError("Artifact integrity check failed")
        return path

    def is_task_visible(self, reference: str) -> bool:
        key = reference.removeprefix("artifact:")
        return self.store.get("artifact_visibility", key, {"task": True})["task"]

    def require_task_access(self, value) -> None:
        """Check nested tool inputs before spending budget or serving a cached result."""
        if isinstance(value, str) and value.startswith("artifact:") and not self.is_task_visible(value):
            raise PermissionError("Improver artifact is unavailable to task tools/code")
        if isinstance(value, dict):
            for item in value.values():
                self.require_task_access(item)
        elif isinstance(value, list):
            for item in value:
                self.require_task_access(item)

    def catalog(self, *, task_only: bool = False) -> list[dict]:
        return [record for record in self.store.all("artifacts").values()
                if not task_only or self.is_task_visible(record["ref"])]
