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
        self.root.mkdir(exist_ok=True)

    def put(self, source: str | Path, kind: str, *, origin: str = "operator") -> dict:
        source = Path(source)
        if kind not in KINDS or source.is_symlink() or not source.is_file():
            raise ValueError("Expected a regular scientific artifact of a supported kind")
        if not 0 < source.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError("Artifact size must be between 1 byte and 64 MiB")
        sha = file_sha256(source)
        key = f"{sha}.{kind}"
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
        self.store.put("artifacts", key, record, immutable=True)
        self.store.event("artifact_registered", {**record, "origin": origin})
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

    def catalog(self) -> list[dict]:
        return list(self.store.all("artifacts").values())
