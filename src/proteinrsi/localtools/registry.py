# SPDX-License-Identifier: MIT
"""Register approved local functions. Neither environment paths nor bindings are LLM-editable."""
from __future__ import annotations

from functools import partial

from proteinrsi.contracts import TaskSpec, digest
from proteinrsi.storage import Store
from proteinrsi.tools import ToolGateway
from . import functions as fn
from .artifacts import ArtifactStore
from .catalog import descriptions, tool_spec
from .config import LocalToolsConfig
from .execution import inspect_engine


def configured_names(config: LocalToolsConfig) -> list[str]:
    return [d["name"] for d in descriptions() if
            (d["engine"] == "utilities" and config.utilities) or
            (d["engine"] in config.engines and config.engines[d["engine"]].enabled)]


def doctor(config: LocalToolsConfig, *, probe: bool = False) -> list[dict]:
    results = [inspect_engine(name, engine, probe=probe) for name, engine in config.engines.items()]
    if config.esmc is not None:
        from pathlib import Path
        missing = config.esmc.worker_python and not Path(config.esmc.worker_python).is_file()
        results.insert(0, {"engine": "esmc600m", "status": "missing_requirements" if missing else
            "configured_not_inference_tested", "problems": ["Missing ESMC worker Python"] if missing else [],
            "note": "Run esmc-check after init to validate actual weights and inference"})
    return results


def register_local_tools(gateway: ToolGateway, store: Store, config: LocalToolsConfig) -> None:
    artifacts = ArtifactStore(store)
    utilities = {"protein_sequence_qc": fn.sequence_qc,
        "structure_inspect": partial(fn.inspect_structure, artifacts=artifacts),
        "interface_geometry": partial(fn.interface_geometry, artifacts=artifacts),
        "structure_compare": partial(fn.compare_structures, artifacts=artifacts),
        "msa_validate": partial(fn.validate_msa, artifacts=artifacts)}
    engines = {"proteinmpnn_design": fn.proteinmpnn_design, "rfdiffusion_binder": fn.rfdiffusion_binder,
        "protenix_predict": fn.protenix_predict,
        "rosetta_relax": partial(fn.rosetta, mode="relax"),
        "rosetta_interface": partial(fn.rosetta, mode="interface")}
    for d in descriptions():
        name, engine = d["name"], d["engine"]
        if engine == "utilities" and config.utilities:
            gateway.register(tool_spec(name, "proteinrsi-local-v1"), utilities[name])
        elif engine in config.engines and config.engines[engine].enabled:
            cfg = config.engines[engine]
            function = engines[name]
            def execute(arguments, function=function, cfg=cfg):
                state = store.get("campaign", "state")
                if not state:
                    raise ValueError("Engine call requires task-scoped campaign state")
                task = TaskSpec.model_validate(state["task"])
                previous_jobs = set(store.all("local_jobs"))
                try:
                    return function(arguments, task, store, cfg)
                except BaseException:
                    for key, record in store.all("local_jobs").items():
                        if key not in previous_jobs and record.get("state") == "executed_pending_validation":
                            store.put("local_jobs", key, {**record, "state": "failed_validation"})
                    raise
            # Make environmental/code/weight changes affect identity and cache keys.
            spec = tool_spec(name, "proteinrsi-worker-v1:" + digest(cfg)[:16])
            gateway.register(spec, execute)


def attach_stored(gateway: ToolGateway, store: Store) -> None:
    raw = store.get("configuration", "local_tools")
    if raw is not None:
        register_local_tools(gateway, store, LocalToolsConfig.model_validate(raw))
