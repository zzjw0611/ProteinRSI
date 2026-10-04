# SPDX-License-Identifier: MIT
"""Scientific tool functions. Descriptions/schemas live in descriptions/*.json."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import shutil

import numpy as np

from proteinrsi.contracts import AMINO_ACIDS, Candidate, TaskKind, TaskSpec
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_candidate
from .artifacts import ArtifactStore
from .config import EngineConfig
from .execution import LocalJob
from .pdbio import mapping, read_fasta, read_pdb, write_normalized


def sequence_qc(arguments: dict) -> dict:
    reference = arguments["reference"]
    result = []
    for seq in arguments["sequences"]:
        if not seq or set(seq) - AMINO_ACIDS:
            raise ValueError("Noncanonical protein sequence")
        counts = Counter(seq)
        substitutions = ([{"position": i + 1, "from": a, "to": b}
                          for i, (a, b) in enumerate(zip(reference, seq)) if a != b]
                         if len(seq) == len(reference) else None)
        result.append({"sequence": seq, "length": len(seq), "substitutions": substitutions,
            "entropy_bits": -sum((n/len(seq)) * math.log2(n/len(seq)) for n in counts.values()),
            "cysteine_count": counts["C"], "canonical": True})
    return {"qc": result, "evidence_kind": "computed_descriptor",
            "warning": "Sequence checks, not a solubility/stability/fitness prediction."}


def inspect_structure(arguments: dict, artifacts: ArtifactStore) -> dict:
    chains = read_pdb(artifacts.resolve(arguments["structure_ref"], kind="pdb"))
    return {"chains": mapping(chains), "evidence_kind": "computed_geometry",
            "warning": "Positions are 1-based observed-residue indices; missing residues are not reconstructed."}


def interface_geometry(arguments: dict, artifacts: ArtifactStore) -> dict:
    chains = read_pdb(artifacts.resolve(arguments["structure_ref"], kind="pdb"))
    first, second = arguments["chain_a"], arguments["chain_b"]
    if first == second or first not in chains or second not in chains:
        raise ValueError("Select two distinct existing chains")
    # CA geometry is deliberately named as such; it is not an all-atom interface score.
    x = np.asarray([r["atoms"]["CA"] for r in chains[first]["residues"]])
    y = np.asarray([r["atoms"]["CA"] for r in chains[second]["residues"]])
    count, minimum = 0, float("inf")
    for start in range(0, len(x), 128):
        distances = np.linalg.norm(x[start:start+128, None, :] - y[None, :, :], axis=-1)
        count += int((distances < arguments["cutoff_angstrom"]).sum())
        minimum = min(minimum, float(distances.min()))
    return {"ca_contact_pairs": count, "minimum_ca_distance_angstrom": minimum,
            "cutoff_angstrom": arguments["cutoff_angstrom"], "evidence_kind": "computed_geometry",
            "warning": "CA contacts only; not an all-atom clash, binding or affinity prediction."}


def compare_structures(arguments: dict, artifacts: ArtifactStore) -> dict:
    ref = read_pdb(artifacts.resolve(arguments["reference_ref"], kind="pdb"))[arguments["reference_chain"]]
    pred = read_pdb(artifacts.resolve(arguments["prediction_ref"], kind="pdb"))[arguments["prediction_chain"]]
    if len(ref["residues"]) != len(pred["residues"]) or len(ref["residues"]) < 3:
        raise ValueError("CA RMSD requires equal observed lengths and at least 3 residues")
    x, y = [np.asarray([r["atoms"]["CA"] for r in chain["residues"]]) for chain in (ref, pred)]
    x, y = x-x.mean(0), y-y.mean(0)
    u, _, vt = np.linalg.svd(x.T @ y)
    d = np.eye(3)
    d[2, 2] = np.linalg.det(u @ vt)
    rmsd = float(np.sqrt(np.mean(np.sum((x @ (u @ d @ vt) - y)**2, axis=1))))
    return {"ca_rmsd_angstrom": rmsd, "matched_residues": len(x),
            "mapping": "explicit equal-length observed-residue order", "evidence_kind": "computed_geometry",
            "warning": "No sequence alignment; operator must ensure the two chains share residue correspondence."}


def validate_msa(arguments: dict, artifacts: ArtifactStore) -> dict:
    path = artifacts.resolve(arguments["msa_ref"], kind="a3m")
    records = read_fasta(path)
    reference = arguments["sequence"]
    if records[0][1] != reference:
        raise ValueError("A3M query must exactly match the supplied protein sequence")
    for _, seq in records:
        aligned = "".join(c for c in seq if not c.islower() and c != ".")
        if len(aligned) != len(reference) or set(aligned) - (AMINO_ACIDS | {"-", "X"}):
            raise ValueError("Invalid A3M alignment length/alphabet")
    return {"records": len(records), "query_length": len(reference),
            "evidence_kind": "input_validation", "warning": "Format check; not an evolutionary-quality guarantee."}


def _finish(result: dict, job: LocalJob, artifacts: ArtifactStore) -> dict:
    published = []
    for item in result.pop("files", []):
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Worker output path escapes job directory")
        source = job.work / relative
        if source.is_symlink() or not source.resolve().is_relative_to(job.work.resolve()):
            raise ValueError("Unsafe worker output")
        published.append({**artifacts.put(source, item["kind"], origin=job.job_id),
                          "filename": relative.as_posix()})
    result["artifacts"] = published
    result["evidence_kind"] = "proxy"
    record = job.store.get("local_jobs", job.job_id)
    job.store.put("local_jobs", job.job_id, {**record, "state": "validated"})
    return result


def proteinmpnn_design(arguments: dict, task: TaskSpec, store: Store, config: EngineConfig) -> dict:
    artifacts = ArtifactStore(store)
    chains = read_pdb(artifacts.resolve(arguments["backbone_ref"], kind="pdb"), complete_backbone=True)
    chain = arguments["design_chain"]
    if chain not in chains:
        raise ValueError("Design chain not present in backbone")
    reference = task.reference_sequence
    if len(chains[chain]["sequence"]) != len(reference):
        raise ValueError("Backbone/design reference lengths differ")
    mutable = set(task.mutable_positions)
    if not mutable:
        raise ValueError("No mutable positions")
    for i, aa in enumerate(chains[chain]["sequence"], 1):
        if i not in mutable and aa != reference[i-1]:
            raise ValueError("Fixed backbone residue disagrees with task reference")
    if task.target_sequence:
        target_chain = arguments.get("target_chain")
        if (not target_chain or target_chain == chain or target_chain not in chains
                or chains[target_chain]["sequence"] != task.target_sequence):
            raise ValueError("Explicit fixed target chain must match the immutable task target")
    job = LocalJob(store, "proteinmpnn", config)
    write_normalized(chains, job.work / "input.pdb")
    params = {"design_chain": chain, "fixed_positions": [i for i in range(1, len(reference)+1) if i not in mutable],
              "num_sequences": arguments["num_sequences"], "temperature": arguments["temperature"],
              "seed": arguments["seed"]}
    result = job.run(params)
    if len(result.get("candidates", [])) != params["num_sequences"]:
        raise ValueError("MPNN candidate count mismatch")
    for value in result["candidates"]:
        validate_candidate(task, Candidate.model_validate(value))
    result["input_residue_mapping"] = mapping(chains)
    return _finish(result, job, artifacts)


def rfdiffusion_binder(arguments: dict, task: TaskSpec, store: Store, config: EngineConfig) -> dict:
    if task.kind != TaskKind.BINDER:
        raise ValueError("This minimal RFdiffusion adapter is for fixed-length binder scaffolds")
    if arguments["length"] != len(task.reference_sequence) or set(task.mutable_positions) != set(range(1, arguments["length"]+1)):
        raise ValueError("De novo binder scaffolds require matching length and fully mutable binder")
    artifacts = ArtifactStore(store)
    chains = read_pdb(artifacts.resolve(arguments["target_ref"], kind="pdb"), complete_backbone=True)
    target_chain = arguments["target_chain"]
    if target_chain not in chains or chains[target_chain]["sequence"] != task.target_sequence:
        raise ValueError("Target structure must exactly cover the immutable target sequence")
    if any(p > len(task.target_sequence) for p in arguments["hotspots"]):
        raise ValueError("Hotspot outside target observed sequence")
    job = LocalJob(store, "rfdiffusion", config)
    write_normalized({target_chain: chains[target_chain]}, job.work / "target.pdb", rename={target_chain: "A"})
    result = job.run({"target_length": len(task.target_sequence), "length": arguments["length"],
                     "num_designs": arguments["num_designs"], "seed": arguments["seed"],
                     "hotspots": arguments["hotspots"]})
    roles = []
    for item in result.get("files", []):
        path = job.work / item["path"]
        if not path.resolve().is_relative_to(job.work.resolve()):
            raise ValueError("Output path escaped job")
        out = read_pdb(path, complete_backbone=True)
        targets = [c for c, d in out.items() if d["sequence"] == task.target_sequence]
        if len(out) != 2 or len(targets) != 1:
            raise ValueError("Cannot unambiguously identify the unchanged target in generated PDB")
        design = next(c for c in out if c != targets[0])
        if len(out[design]["sequence"]) != arguments["length"]:
            raise ValueError("Generated binder length mismatch")
        roles.append({"filename": item["path"], "target_chain": targets[0], "design_chain": design})
    result = _finish(result, job, artifacts)
    lookup = {a["filename"]: a["ref"] for a in result["artifacts"]}
    result["backbones"] = [{**r, "backbone_ref": lookup[r["filename"]]} for r in roles]
    result["warning"] = "Backbones only. Do not submit scaffold placeholder sequences as final candidates."
    return result


def protenix_predict(arguments: dict, task: TaskSpec, store: Store, config: EngineConfig) -> dict:
    artifacts = ArtifactStore(store)
    seq = arguments["sequence"]
    validate_candidate(task, Candidate(sequence=seq))
    if arguments["assembly"] == "complex" and not task.target_sequence:
        raise ValueError("Complex prediction requires an immutable target")
    if task.kind == TaskKind.AFFINITY and arguments["assembly"] != "complex":
        raise ValueError("Affinity evidence must describe both fixed inputs")
    entities = [("B", seq)]
    if arguments["assembly"] == "complex":
        entities.insert(0, ("A", task.target_sequence))
    mode = arguments["msa_mode"]
    msas = arguments["msas"]
    if mode == "none" and msas:
        raise ValueError("MSA-free means no MSA files; do not silently ignore supplied data")
    if mode == "precomputed" and set(msas) != {c for c, _ in entities}:
        raise ValueError("Precomputed mode requires explicit MSA files for every input chain")
    job = LocalJob(store, "protenix", config)
    native = []
    for chain, sequence in entities:
        protein = {"sequence": sequence, "count": 1, "id": [chain]}
        if mode == "precomputed":
            for key, ref in msas[chain].items():
                if key not in ("unpaired", "paired"):
                    raise ValueError("Only explicit paired/unpaired A3M supported")
                validate_msa({"msa_ref": ref, "sequence": sequence}, artifacts)
                name = f"{chain}-{key}.a3m"
                shutil.copyfile(artifacts.resolve(ref, kind="a3m"), job.work / name)
                base = Path("/work") if config.runtime == "docker" else job.work
                protein[{"unpaired": "unpairedMsaPath", "paired": "pairedMsaPath"}[key]] = str(base / name)
            if "unpaired" not in msas[chain]:
                raise ValueError("Unpaired query-aligned A3M is required in precomputed mode")
        native.append({"proteinChain": protein})
    (job.work / "input.json").write_text(json.dumps([{"name": "proteinrsi", "sequences": native}]))
    result = job.run({k: arguments[k] for k in ("msa_mode", "seeds", "samples", "steps", "cycles", "dtype")})
    pdbs = [item for item in result.get("files", []) if item["kind"] == "pdb"]
    if not pdbs or len(pdbs) != len(arguments["seeds"]) * arguments["samples"]:
        raise ValueError("Incomplete Protenix result batch")
    for item in pdbs:
        path = job.work / item["path"]
        if not path.resolve().is_relative_to(job.work.resolve()):
            raise ValueError("Unsafe Protenix output path")
        predicted = read_pdb(path)
        if {c: d["sequence"] for c, d in predicted.items()} != dict(entities):
            raise ValueError("Protenix output chain/sequence mapping differs; no inferred role substitution")
    result["input_entities"] = dict(entities)
    result["msa_mode"] = mode
    return _finish(result, job, artifacts)


def rosetta(arguments: dict, task: TaskSpec, store: Store, config: EngineConfig, *, mode: str) -> dict:
    artifacts = ArtifactStore(store)
    chains = read_pdb(artifacts.resolve(arguments["structure_ref"], kind="pdb"), complete_backbone=True)
    if mode == "interface":
        first, second = arguments["target_chain"], arguments["design_chain"]
        if first == second or set(chains) != {first, second}:
            raise ValueError("Interface adapter requires exactly two explicitly assigned chains")
        if not task.target_sequence or chains[first]["sequence"] != task.target_sequence:
            raise ValueError("Target mismatch")
        validate_candidate(task, Candidate(sequence=chains[second]["sequence"]))
    else:
        if task.target_sequence and not any(c["sequence"] == task.target_sequence for c in chains.values()):
            raise ValueError("Expected target missing from relaxation input")
    job = LocalJob(store, "pyrosetta", config)
    write_normalized(chains, job.work / "input.pdb")
    params = {"mode": mode, "seed": arguments["seed"], "repeats": arguments.get("repeats", 1)}
    if mode == "interface":
        params["interface"] = first + "_" + second
    result = job.run(params)
    if mode == "relax":
        output = read_pdb(job.work / "relaxed.pdb", complete_backbone=True)
        if {c: v["sequence"] for c, v in output.items()} != {c: v["sequence"] for c, v in chains.items()}:
            raise ValueError("Relax changed sequence identities")
    result["input_residue_mapping"] = mapping(chains)
    return _finish(result, job, artifacts)
