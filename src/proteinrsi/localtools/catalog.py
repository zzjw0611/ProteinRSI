# SPDX-License-Identifier: MIT
"""Biomni-style separate tool metadata; executable functions are registered explicitly."""
from __future__ import annotations

from importlib.resources import files
import json

from proteinrsi.contracts import digest
from proteinrsi.tools import ToolSpec


def descriptions() -> list[dict]:
    directory = files("proteinrsi.localtools").joinpath("descriptions")
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.iterdir(), key=lambda p: p.name)
            if p.name.endswith(".json")]


def description(name: str) -> dict:
    matches = [d for d in descriptions() if d["name"] == name]
    if len(matches) != 1:
        raise ValueError("Unknown tool description: " + name)
    return matches[0]


def tool_spec(name: str, implementation: str) -> ToolSpec:
    data = description(name)
    data.pop("engine")
    data["implementation_version"] = implementation + ":schema-" + digest(data)[:12]
    return ToolSpec.model_validate(data)
