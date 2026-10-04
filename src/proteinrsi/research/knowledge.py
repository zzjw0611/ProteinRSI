# SPDX-License-Identifier: MIT
"""Original loader inspired by Biomni KnowHowLoader; no upstream documents copied."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import json
from pydantic import Field
from proteinrsi.contracts import Model, TaskKind, digest


class KnowHow(Model):
    resource_id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    title: str
    description: str
    task_kinds: list[TaskKind]
    tags: list[str]
    min_observations: int = Field(default=0, ge=0)
    license: str
    sources: list[str]
    content: str

    @property
    def version(self):
        return digest(self)


def load_documents(directory: str | Path | None = None) -> list[KnowHow]:
    """An operator-selected directory or bundled notes only; never recursively search user files.

    Format: fenced ```json metadata at the start, followed by Markdown.
    Raw paths are never supplied by an LLM. Licenses and sources are retained.
    """
    root = Path(directory) if directory else files("proteinrsi.research").joinpath("know_how")
    documents = []
    ids = set()
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if not path.name.endswith(".md"):
            continue
        if isinstance(path, Path) and path.is_symlink():
            raise ValueError("Know-how symlinks are not accepted")
        text = path.read_text(encoding="utf-8")
        if len(text) > 50000 or not text.startswith("```json\n"):
            raise ValueError(f"Invalid know-how metadata/size: {path.name}")
        header, separator, content = text[len("```json\n"):].partition("\n```\n")
        if not separator:
            raise ValueError("Missing metadata closing fence")
        doc = KnowHow.model_validate({**json.loads(header), "content": content.strip()})
        if doc.resource_id in ids:
            raise ValueError("Duplicate know-how ID")
        ids.add(doc.resource_id)
        documents.append(doc)
    return documents


def snapshot_documents(store) -> None:
    """Persist original text and provenance once; package upgrades do not mutate campaigns."""
    if store.get("configuration", "know_how") is None:
        store.put("configuration", "know_how", [d.model_dump(mode="json") for d in load_documents()], immutable=True)
