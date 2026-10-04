#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect locally installed assets and write a reviewed environment configuration.

This records hashes; it does not authenticate the source of weights or grant a license.
"""
import argparse
import json
from pathlib import Path
import subprocess

from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.localtools.config import LocalToolsConfig


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--engine", required=True, choices=["proteinmpnn", "rfdiffusion", "protenix", "pyrosetta"])
    p.add_argument("--python", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--assets", required=True)
    p.add_argument("--asset", action="append", default=[], help="Relative weight/data file path; repeat for every required asset")
    p.add_argument("--license-reviewed", action="store_true", required=True)
    args = p.parse_args()
    output = Path(args.out)
    if output.exists():
        raise ValueError("Output exists; use a new config file to preserve provenance")
    config = LocalToolsConfig.model_validate(json.loads(Path(args.config).read_text()))
    cfg = config.engines[args.engine]
    cfg.python = str(Path(args.python).resolve())
    cfg.repo = str(Path(args.repo).resolve())
    cfg.assets = str(Path(args.assets).resolve())
    if args.engine != "pyrosetta":
        actual = subprocess.check_output(["git", "-C", cfg.repo, "rev-parse", "HEAD"], text=True).strip()
        if cfg.revision and actual != cfg.revision:
            raise ValueError("Repository differs from the audited pinned revision; review it before updating the template")
        cfg.revision = actual
    hashes = {}
    for relative in args.asset:
        path = Path(cfg.assets) / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink():
            raise ValueError("Unsafe asset path")
        hashes[relative] = file_sha256(path)
    cfg.asset_sha256 = hashes
    cfg.license_reviewed = True
    cfg.enabled = True
    output.write_text(json.dumps(config.model_dump(mode="json"), indent=2)+"\n")
    print(str(output.resolve()))


if __name__ == "__main__":
    main()
