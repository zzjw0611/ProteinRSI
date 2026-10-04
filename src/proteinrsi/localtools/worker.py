# SPDX-License-Identifier: MIT
"""Standalone worker: run with the engine's Python, NOT the Agent interpreter.

No ProteinRSI dependency installation is required inside the engine environment.
The Python audit hook prevents ordinary Python network calls; only Docker
--network=none provides an OS network boundary. Venv isolation is not a sandbox.
"""
from __future__ import annotations

import json
from pathlib import Path
import os
import runpy
import sys


def offline_audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise PermissionError("Local protein workers cannot use Python network APIs; preload all assets")


def invoke(script: Path, arguments: list[str], repo: Path):
    sys.path.insert(0, str(repo))
    sys.argv = [str(script), *arguments]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise RuntimeError(f"Upstream CLI exited {exc.code}") from exc


def run_proteinmpnn(request):
    from pdbio import read_fasta
    p = request["parameters"]
    repo, assets = Path(request["repo"]), Path(request["assets"])
    fixed = {"input": {p["design_chain"]: p["fixed_positions"]}}
    Path("fixed.json").write_text(json.dumps(fixed))
    invoke(repo / "protein_mpnn_run.py", [
        "--pdb_path", str(Path("input.pdb").resolve()), "--pdb_path_chains", p["design_chain"],
        "--fixed_positions_jsonl", str(Path("fixed.json").resolve()),
        "--out_folder", str(Path("outputs").resolve()),
        "--num_seq_per_target", str(p["num_sequences"]), "--batch_size", "1",
        "--sampling_temp", str(p["temperature"]), "--seed", str(p["seed"]),
        "--model_name", request["model_name"], "--path_to_model_weights", str(assets),
        "--backbone_noise", "0.0", "--suppress_print", "1"], repo)
    records = read_fasta(Path("outputs/seqs/input.fa"))
    # Upstream emits a native entry, then exactly the requested designed entries.
    native, designs = records[0], records[1:]
    if "designed_chains=" not in native[0] or len(designs) != p["num_sequences"]:
        raise ValueError("Unexpected ProteinMPNN FASTA header/count; audit upstream version")
    import ast
    import re
    match = re.search(r"designed_chains=(\[[^\]]*\])", native[0])
    if not match or ast.literal_eval(match.group(1)) != [p["design_chain"]]:
        raise ValueError("MPNN output refers to a different design chain")
    if any("/" in seq for _, seq in designs):
        raise ValueError("Expected exactly one designed chain; never concatenate chains")
    return {"candidates": [{"sequence": seq, "rationale": header,
                            "source": "proteinmpnn", "evidence_kind": "none"}
                           for header, seq in designs],
            "files": [{"path": "outputs/seqs/input.fa", "kind": "fasta"}]}


def run_rfdiffusion(request):
    p = request["parameters"]
    repo, assets = Path(request["repo"]), Path(request["assets"])
    contig = f"[A1-{p['target_length']}/0 {p['length']}-{p['length']}]"
    args = [f"inference.input_pdb={Path('target.pdb').resolve()}",
            f"inference.output_prefix={Path('outputs/design').resolve()}",
            f"inference.num_designs={p['num_designs']}",
            f"inference.design_startnum={p['seed']}", "inference.deterministic=True",
            f"inference.ckpt_override_path={assets / (request['model_name'] + '.pt')}",
            f"contigmap.contigs={contig}"]
    if p["hotspots"]:
        args.append("ppi.hotspot_res=[" + ",".join(f"A{x}" for x in p["hotspots"]) + "]")
    Path("outputs").mkdir(exist_ok=True)
    invoke(repo / "scripts/run_inference.py", args, repo)
    paths = sorted(Path("outputs").glob("design_*.pdb"))
    if len(paths) != p["num_designs"]:
        raise ValueError("RFdiffusion output count mismatch")
    # Do not unpickle .trb files. Roles are verified from PDB sequence identity by host.
    return {"files": [{"path": str(path), "kind": "pdb"} for path in paths]}


def run_protenix(request):
    p = request["parameters"]
    repo, assets = Path(request["repo"]), Path(request["assets"])
    os.environ["PROTENIX_ROOT_DIR"] = str(assets)
    # Direct inference runner does not start the CLI's remote MSA preparation pipeline.
    invoke(repo / "runner/inference.py", [
        "--model_name", request["model_name"],
        "--input_json_path", str(Path("input.json").resolve()),
        "--dump_dir", str(Path("outputs").resolve()),
        "--load_checkpoint_dir", str(assets / "checkpoint"),
        "--seeds", ",".join(str(s) for s in p["seeds"]),
        "--sample_diffusion.N_sample", str(p["samples"]),
        "--sample_diffusion.N_step", str(p["steps"]), "--model.N_cycle", str(p["cycles"]),
        "--dtype", p["dtype"], "--use_msa", str(p["msa_mode"] == "precomputed").lower(),
        "--use_template", "false", "--use_rna_msa", "false",
        "--triangle_attention", "torch", "--triangle_multiplicative", "torch",
        "--enable_efficient_fusion", "false"], repo)
    paths = sorted(Path("outputs").rglob("*.cif"))
    if not paths:
        raise ValueError("Protenix produced no CIF structures")
    from Bio.PDB import MMCIFParser, PDBIO
    files = []
    for path in paths:
        structure = MMCIFParser(QUIET=True).get_structure("prediction", str(path))
        if len(structure) != 1 or any(len(c.id) != 1 for c in structure.get_chains()):
            raise ValueError("Cannot losslessly export this CIF to supported single-model PDB")
        writer = PDBIO()
        writer.set_structure(structure)
        pdb_path = path.with_suffix(".pdb")
        writer.save(str(pdb_path))
        files.extend([{"path": str(path), "kind": "cif"}, {"path": str(pdb_path), "kind": "pdb"}])
    files.extend({"path": str(x), "kind": "json"} for x in Path("outputs").rglob("*summary_confidence*.json"))
    return {"files": files, "warning": "Structure/confidence only; not calibrated binding affinity."}


def run_pyrosetta(request):
    import pyrosetta
    p = request["parameters"]
    pyrosetta.init(f"-mute all -constant_seed -jran {p['seed']}")
    pose = pyrosetta.pose_from_pdb("input.pdb")
    scorefxn = pyrosetta.get_fa_scorefxn()
    files = []
    if p["mode"] == "relax":
        mover = pyrosetta.rosetta.protocols.relax.FastRelax(scorefxn, p["repeats"])
        mover.constrain_relax_to_start_coords(True)
        mover.apply(pose)
        pose.dump_pdb("relaxed.pdb")
        files = [{"path": "relaxed.pdb", "kind": "pdb"}]
        metrics = {"total_energy": {"value": float(scorefxn(pose)), "unit": "REU"}}
    else:
        mover = pyrosetta.rosetta.protocols.analysis.InterfaceAnalyzerMover(p["interface"])
        mover.set_scorefunction(scorefxn)
        mover.set_pack_separated(True)
        mover.apply(pose)
        metrics = {"interface_dG": {"value": float(mover.get_interface_dG()), "unit": "REU"},
                   "interface_delta_sasa": {"value": float(mover.get_interface_delta_sasa()), "unit": "angstrom^2"}}
    return {"metrics": metrics, "files": files,
            "warning": "Rosetta energy is a proxy, not measured or calibrated Kd."}


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    dispatch = {"proteinmpnn": run_proteinmpnn, "rfdiffusion": run_rfdiffusion,
                "protenix": run_protenix, "pyrosetta": run_pyrosetta}
    sys.addaudithook(offline_audit)
    if request.get("require_cuda") and request["engine"] != "pyrosetta":
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable; no silent CPU fallback")
    result = dispatch[request["engine"]](request)
    Path("result.json").write_text(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
