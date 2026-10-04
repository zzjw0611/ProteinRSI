#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Print or explicitly execute upstream installation steps. No automatic license acceptance.

This prepares independent environments/code, not a claim that all model assets
are installed or that an engine has passed inference. Inspect the emitted plan.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def commands(engine: str, prefix: Path, python: str, micromamba: str) -> list[list[str]]:
    sources = json.loads((ROOT / "configs/sources.lock.json").read_text())
    env, repo = prefix / "envs" / engine, prefix / "engines" / engine
    if engine == "pyrosetta":
        raise ValueError("Install your legally obtained PyRosetta distribution manually in its own environment")
    if engine == "esmc600m":
        return [[python, "-m", "venv", str(env)],
                [str(env / "bin/python"), "-m", "pip", "install", "-e", str(ROOT)+"[esmc]"]]
    source = sources[engine]
    result = [["git", "clone", "--no-checkout", source["repository"], str(repo)],
              ["git", "-C", str(repo), "checkout", "--detach", source["revision"]]]
    if engine == "rfdiffusion":
        result += [[micromamba, "create", "-y", "-p", str(env), "-f", str(repo / "env/SE3nv.yml")],
                   [str(env / "bin/python"), "-m", "pip", "install", "--no-deps", "-e", str(repo / "env/SE3Transformer")],
                   [str(env / "bin/python"), "-m", "pip", "install", "--no-deps", "-e", str(repo)]]
    else:
        result += [[python, "-m", "venv", str(env)]]
        if engine == "proteinmpnn":
            result += [[str(env / "bin/python"), "-m", "pip", "install", "numpy<2", "torch>=2.6,<3"]]
        else:
            result += [[str(env / "bin/python"), "-m", "pip", "install", "-e", str(repo)]]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, choices=["esmc600m", "proteinmpnn", "rfdiffusion", "protenix", "pyrosetta"])
    parser.add_argument("--prefix", default="/opt/proteinrsi")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--micromamba", default="micromamba")
    parser.add_argument("--execute", action="store_true", help="Explicitly permit downloads and upstream installer execution")
    args = parser.parse_args()
    prefix = Path(args.prefix).expanduser().resolve()
    plan = commands(args.engine, prefix, args.python, args.micromamba)
    for command in plan:
        print(shlex.join(command), flush=True)
    print("Next: install required weights/data, review licenses, pin asset hashes, run tools doctor --probe.")
    if args.execute:
        for folder in ("engines", "envs", "assets"):
            (prefix / folder).mkdir(parents=True, exist_ok=True)
        if (prefix / "envs" / args.engine).exists() or (prefix / "engines" / args.engine).exists():
            raise ValueError("Environment/repository already exists; inspect it rather than overwrite")
        for command in plan:
            subprocess.run(command, check=True, shell=False)


if __name__ == "__main__":
    main()
