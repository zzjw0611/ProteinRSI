# SPDX-License-Identifier: MIT
"""Operator-only environment settings. These are not evolvable workflow fields."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from proteinrsi.contracts import Model
from proteinrsi.protein.esmc import ESMCConfig

ENGINE_NAMES = ("proteinmpnn", "rfdiffusion", "protenix", "pyrosetta")


class EngineConfig(Model):
    enabled: bool = False
    runtime: Literal["process", "docker"] = "process"
    python: str = "/REPLACE/env/bin/python"
    repo: str = "/REPLACE/repository"
    revision: str = ""
    assets: str = "/REPLACE/assets"
    # Relative asset path -> mandatory hash. Used to pin the operator's actual weights/data.
    asset_sha256: dict[str, str] = Field(default_factory=dict)
    model_name: str = ""
    image: str = ""
    container_python: str = "/opt/env/bin/python"
    cuda_devices: str = "0"
    threads: int = Field(default=2, ge=1, le=128)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_output_mb: int = Field(default=512, ge=1, le=16384)
    license_reviewed: bool = False

    @model_validator(mode="after")
    def paths_and_pins(self):
        import re
        for field in ("python", "repo", "assets", "container_python"):
            if not Path(getattr(self, field)).is_absolute():
                raise ValueError(f"{field} must be an absolute path")
        if self.revision and not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("Engine revision must be a full Git commit SHA")
        for path, sha in self.asset_sha256.items():
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("Asset keys must stay within the assets directory")
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("Asset digest must be SHA256")
        if self.image and not re.fullmatch(r"[A-Za-z0-9._/:\-]+@sha256:[0-9a-f]{64}", self.image):
            raise ValueError("Docker images must use an immutable digest")
        if not re.fullmatch(r"(?:[0-9]+(?:,[0-9]+)*)?", self.cuda_devices):
            raise ValueError("cuda_devices must be GPU indices or empty for CPU")
        return self


class LocalToolsConfig(Model):
    schema_version: Literal[1] = 1
    utilities: bool = True
    esmc: ESMCConfig | None = None
    engines: dict[str, EngineConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def names(self):
        if set(self.engines) - set(ENGINE_NAMES):
            raise ValueError("Unsupported local engine name")
        return self


def load_config(path: str | Path) -> LocalToolsConfig:
    return LocalToolsConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
