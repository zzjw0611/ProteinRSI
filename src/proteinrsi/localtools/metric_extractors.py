# SPDX-License-Identifier: MIT
"""Fixed parsers for real provider receipts. This module never invokes protein engines."""
from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
import re

from proteinrsi.contracts import canonical, digest, sequence_hash
from proteinrsi.dataflow.resources import Metric, MetricTable
from proteinrsi.dataflow.schema import ContractError

VERSION = "provider-metric-extractors-v1"
PROTENIX = {"protenix.plddt_mean": ("plddt", "score_0_100", 0, 100),
            "protenix.iptm": ("iptm", "dimensionless", 0, 1),
            "protenix.has_clash": ("has_clash", "boolean_0_1", 0, 1)}
PROVIDERS = {**{key: "protenix_predict" for key in PROTENIX},
             "rosetta.interface_dG": "rosetta_interface",
             "structure.ca_rmsd": "structure_compare",
             "esmc.masked_marginal_log_odds": "esmc600m_score_variants",
             "proteinmpnn.score": "proteinmpnn_design"}
MAX_TEXT_BYTES = 8 * 1024 * 1024


def number(value, *, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError("Metric field must be a finite number")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ContractError("Metric field outside its declared range")
    return float(value)


def read_text(artifacts, item, kind):
    if item.get("kind") != kind:
        raise ContractError("Unexpected metric artifact kind")
    path = artifacts.resolve(item["ref"], kind=kind)
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ContractError("Metric artifact exceeds bounded parser input")
    return path.read_text(encoding="utf-8")


def read_json(artifacts, item):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ContractError("Duplicate JSON confidence field")
            obj[key] = value
        return obj
    try:
        data = json.loads(read_text(artifacts, item, "json"), object_pairs_hook=unique)
        canonical(data)  # NaN / Infinity, including unused fields, are invalid provider data.
    except (ValueError, UnicodeError) as exc:
        raise ContractError("Invalid confidence JSON") from exc
    if not isinstance(data, dict):
        raise ContractError("Confidence JSON must be an object")
    return data


def protenix_samples(result, arguments):
    """Native Protenix name/seed/sample grammar; never zip independently sorted files."""
    entries = {}
    pattern = re.compile(r"proteinrsi_(\d+)_(summary_confidence_)?sample_(\d+)\.(json|pdb|cif)")
    for item in result.get("artifacts", []):
        filename = item.get("filename", "")
        path = PurePosixPath(filename)
        if path.is_absolute() or ".." in path.parts:
            raise ContractError("Unsafe provider artifact filename")
        match = pattern.fullmatch(path.name)
        if not match:
            if item.get("kind") in {"pdb", "cif"} or "summary_confidence" in filename:
                raise ContractError("Unknown Protenix sample naming version")
            continue
        seed, summary, sample, ext = match.groups()
        kind = "confidence" if summary and ext == "json" else ext
        if (summary and ext != "json") or (not summary and ext == "json") or item.get("kind") != ext:
            raise ContractError("Malformed Protenix sample artifact")
        key = (int(seed), int(sample))
        entry = entries.setdefault(key, {"parent": str(path.parent)})
        if entry["parent"] != str(path.parent) or kind in entry:
            raise ContractError("Duplicate or ambiguous Protenix seed/sample")
        entry[kind] = item
    expected = {(seed, sample) for seed in arguments["seeds"] for sample in range(arguments["samples"])}
    if set(entries) != expected:
        raise ContractError("Protenix seed/sample coverage mismatch")
    for entry in entries.values():
        if not {"confidence", "cif", "pdb"} <= entry.keys():
            raise ContractError("Protenix sample lacks matching confidence/structure artifacts")
    return entries


def parse_mpnn(text, candidates):
    """Read score= from designed FASTA headers, never from candidate rationale."""
    records, header, parts = [], None, []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(parts)))
            header, parts = line[1:], []
        elif header is None:
            raise ContractError("Malformed ProteinMPNN FASTA")
        else:
            parts.append(line)
    if header is not None:
        records.append((header, "".join(parts)))
    if not records or "designed_chains=" not in records[0][0]:
        raise ContractError("ProteinMPNN native header missing")
    designed = records[1:]
    if [seq for _, seq in designed] != [c["sequence"] for c in candidates]:
        raise ContractError("ProteinMPNN FASTA/candidate identity mismatch")
    parsed = []
    for head, seq in designed:
        matches = re.findall(r"(?:^|,)\s*score\s*=\s*([^,\s]+)", head)
        samples = re.findall(r"(?:^|,)\s*sample\s*=\s*(\d+)(?=\s*(?:,|$))", head)
        if len(matches) != 1 or len(samples) != 1:
            raise ContractError("Missing/ambiguous ProteinMPNN score or sample field")
        try:
            score = number(float(matches[0]), low=0)
        except ValueError as exc:
            raise ContractError("Invalid ProteinMPNN score") from exc
        parsed.append((int(samples[0]), seq, score))
    if len({sample for sample, _, _ in parsed}) != len(parsed):
        raise ContractError("Duplicate ProteinMPNN sample ID")
    return parsed


def extract_provider(result, provider, arguments, metric_ids, artifacts, *, method):
    """Caller must verify the result is the committed output of this scoped tool call."""
    if not metric_ids or len(set(metric_ids)) != len(metric_ids):
        raise ContractError("Select distinct metric IDs")
    if any(PROVIDERS.get(key) != provider for key in metric_ids):
        raise ContractError("Metric unsupported or belongs to another provider")
    rows, provenance = [], []

    def add(subject, metric, value, unit, **details):
        rows.append(Metric(subject_ref=subject, name=metric, value=value, unit=unit,
            method=method + ":" + VERSION, evidence="computed" if provider in {
                "rosetta_interface", "structure_compare"} else "predicted"))
        provenance.append({"subject_ref": subject, "metric_id": metric, **details})

    if provider == "protenix_predict":
        if result.get("input_entities", {}).get("B") != arguments["sequence"]:
            raise ContractError("Protenix candidate identity mismatch")
        if "protenix.iptm" in metric_ids and arguments["assembly"] != "complex":
            raise ContractError("ipTM requires a complex prediction")
        for (seed, sample), entry in sorted(protenix_samples(result, arguments).items()):
            confidence = read_json(artifacts, entry["confidence"])
            for kind in ("cif", "pdb"):
                artifacts.resolve(entry[kind]["ref"], kind=kind)  # Verify both original and converted bytes.
            subject = entry["cif"]["ref"]
            for key in metric_ids:
                field, unit, lo, hi = PROTENIX[key]
                if field not in confidence:
                    raise ContractError("Missing requested Protenix confidence field: " + field)
                raw = confidence[field]
                if field == "has_clash":
                    if not isinstance(raw, bool) and not (type(raw) in {int, float} and raw in (0, 1)):
                        raise ContractError("Invalid Protenix clash flag")
                    raw = int(raw)
                add(subject, key, number(raw, low=lo, high=hi), unit,
                    candidate_ref="seq:" + sequence_hash(arguments["sequence"]),
                    seed=seed, sample=sample, structure_ref=subject, pdb_ref=entry["pdb"]["ref"],
                    confidence_ref=entry["confidence"]["ref"], field=field,
                    input_entities=result["input_entities"])
    elif provider == "rosetta_interface":
        artifacts.resolve(arguments["structure_ref"], kind="pdb")
        metric = result.get("metrics", {}).get("interface_dG", {})
        if metric.get("unit") != "REU":
            raise ContractError("Rosetta interface energy unit mismatch")
        add(arguments["structure_ref"], metric_ids[0], number(metric.get("value")), "REU",
            structure_ref=arguments["structure_ref"], seed=arguments["seed"],
            target_chain=arguments["target_chain"], design_chain=arguments["design_chain"])
    elif provider == "structure_compare":
        for key in ("reference_ref", "prediction_ref"):
            artifacts.resolve(arguments[key], kind="pdb")
        add(arguments["prediction_ref"], metric_ids[0], number(result.get("ca_rmsd_angstrom"), low=0), "angstrom",
            reference_ref=arguments["reference_ref"], prediction_ref=arguments["prediction_ref"],
            reference_chain=arguments["reference_chain"], prediction_chain=arguments["prediction_chain"],
            mapping=result.get("mapping"), matched_residues=result.get("matched_residues"))
    elif provider == "esmc600m_score_variants":
        scores = result.get("scores", [])
        if [r.get("sequence") for r in scores] != arguments["sequences"]:
            raise ContractError("ESMC result sequence identities/order mismatch")
        for row in scores:
            add("seq:" + sequence_hash(row["sequence"]), metric_ids[0],
                number(row.get("masked_marginal_log_odds")), "log_odds",
                reference_ref="seq:" + sequence_hash(arguments["reference"]), model=result.get("model"))
    elif provider == "proteinmpnn_design":
        fasta = [a for a in result.get("artifacts", []) if a.get("kind") == "fasta"]
        if len(fasta) != 1:
            raise ContractError("ProteinMPNN requires one actual FASTA artifact")
        for sample, sequence, value in parse_mpnn(read_text(artifacts, fasta[0], "fasta"), result["candidates"]):
            add("seq:" + sequence_hash(sequence), metric_ids[0], value, "negative_log_probability",
                fasta_ref=fasta[0]["ref"], sample=sample, seed=arguments["seed"],
                backbone_ref=arguments["backbone_ref"], design_chain=arguments["design_chain"])
    return {"metric_table": MetricTable(rows=rows).model_dump(mode="json"),
            "row_provenance": provenance, "extractor_version": VERSION,
            "raw_result_sha256": digest(result)}
