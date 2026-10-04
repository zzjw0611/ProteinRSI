# SPDX-License-Identifier: MIT
"""Strict small PDB utilities. Explicit observed-residue maps, never guess design chains."""
from __future__ import annotations

from collections import OrderedDict
import math
from pathlib import Path

RESIDUES = dict(zip(
    "ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR".split(),
    "ACDEFGHIKLMNPQRSTVWY"))


def read_pdb(path: Path, *, complete_backbone: bool = False) -> dict:
    chains = OrderedDict()
    models = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MODEL "):
            models += 1
            if models > 1:
                raise ValueError("Multiple PDB models: select one explicitly")
        if not line.startswith("ATOM  "):
            continue
        if len(line) < 54:
            raise ValueError("Truncated PDB atom")
        if line[16] not in (" ", "A"):
            continue
        chain, number, insertion, resname = line[21], int(line[22:26]), line[26], line[17:20]
        if not chain.isalnum() or resname not in RESIDUES:
            raise ValueError("Only explicit alphanumeric chains and canonical ATOM residues supported")
        residues = chains.setdefault(chain, OrderedDict())
        key = (number, insertion)
        residue = residues.setdefault(key, {"aa": RESIDUES[resname], "atoms": {}, "lines": []})
        if residue["aa"] != RESIDUES[resname]:
            raise ValueError("Ambiguous residue identity")
        atom = line[12:16].strip()
        xyz = tuple(float(line[i:i+8]) for i in (30, 38, 46))
        if not all(math.isfinite(v) for v in xyz):
            raise ValueError("Non-finite coordinates")
        if atom in residue["atoms"]:
            raise ValueError("Duplicate atom / ambiguous alternate conformer")
        residue["atoms"][atom] = xyz
        residue["lines"].append(line)
    if not chains:
        raise ValueError("No supported protein atoms")
    result = {}
    for chain, records in chains.items():
        residues = []
        for pos, ((number, insertion), r) in enumerate(records.items(), 1):
            if "CA" not in r["atoms"] or (complete_backbone and {"N", "CA", "C", "O"} - r["atoms"].keys()):
                raise ValueError("Incomplete backbone: repair explicitly before this operation")
            residues.append({**r, "position": pos, "pdb_number": number, "insertion_code": insertion.strip()})
        result[chain] = {"sequence": "".join(r["aa"] for r in residues), "residues": residues}
    return result


def write_normalized(chains: dict, destination: Path, *, rename: dict | None = None) -> None:
    lines = []
    serial = 0
    for chain, data in chains.items():
        new_chain = (rename or {}).get(chain, chain)
        if len(data["residues"]) > 9999:
            raise ValueError("PDB residue capacity exceeded")
        for residue in data["residues"]:
            for line in residue["lines"]:
                serial += 1
                if serial > 99999:
                    raise ValueError("PDB atom capacity exceeded")
                lines.append(line[:6] + f"{serial:5d}" + line[11:16] + " " + line[17:21] + new_chain
                             + f"{residue['position']:4d}" + " " + line[27:])
        lines.append("TER")
    destination.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")


def mapping(chains: dict) -> dict:
    return {c: {"sequence": d["sequence"], "residue_map": [
        {k: r[k] for k in ("position", "pdb_number", "insertion_code")}
        for r in d["residues"]]} for c, d in chains.items()}


def read_fasta(path: Path) -> list[tuple[str, str]]:
    records = []
    header = None
    seq = []
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(seq)))
            header, seq = line[1:], []
        elif line.strip():
            if header is None:
                raise ValueError("FASTA sequence without header")
            seq.append(line.strip())
    if header is not None:
        records.append((header, "".join(seq)))
    if not records:
        raise ValueError("Empty FASTA")
    return records
