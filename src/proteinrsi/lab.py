# SPDX-License-Identifier: MIT
"""Trusted experiment backends: an explicit historical oracle and manual wet-lab I/O."""
from __future__ import annotations

import csv
from pathlib import Path

from proteinrsi.contracts import Batch, Observation, TaskSpec, canonical, sequence_hash


class UnknownMeasurement(ValueError):
    pass


class CSVOracle:
    """Trusted local replay backend. Do NOT pass this object or its path to an agent.

    It reveals real historical measurements only when the source is measured_replay.
    Synthetic demo data is explicitly labeled synthetic, never wetlab.
    This Python process is not an adversarial sandbox; use a separate OS account/service
    before allowing any untrusted code to execute alongside sensitive labels.
    """
    def __init__(self, dataset: str | Path, task: TaskSpec):
        if task.feedback_source == "wetlab":
            raise ValueError("A lookup table cannot impersonate new wet-lab measurements")
        self.task = task
        self._labels: dict[str, tuple[float | None, str]] = {}
        with Path(dataset).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                seq = row["sequence"]
                if seq in self._labels:
                    raise ValueError("Duplicate sequences: aggregate historical replicates explicitly first")
                qc = row.get("qc") or "valid"
                value = float(row["value"]) if row.get("value", "").strip() else None
                # Validate values without disclosing them to the agent-facing task view.
                Observation(sample_id="validation", sequence=seq, value=value, qc=qc,
                    metric=task.metric, unit=task.unit, source=task.feedback_source,
                    batch_id="validation", assay_protocol=task.assay_protocol)
                self._labels[seq] = (value, qc)

    def measure(self, batch: Batch) -> list[Observation]:
        unknown = [s.candidate.sequence for s in batch.samples if s.candidate.sequence not in self._labels]
        if unknown and (self.task.candidate_access != "open" or self.task.feedback_source != "measured_replay"):
            raise UnknownMeasurement("Requested variant has no historical measurement; no label was fabricated")
        return [Observation(sample_id=s.sample_id, sequence=s.candidate.sequence,
            value=self._labels.get(s.candidate.sequence, (None, "unavailable"))[0],
            qc=self._labels.get(s.candidate.sequence, (None, "unavailable"))[1],
            metric=self.task.metric, unit=self.task.unit, source=self.task.feedback_source,
            batch_id=batch.batch_id, assay_protocol=self.task.assay_protocol) for s in batch.samples]


def export_batch(batch: Batch, task: TaskSpec, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "batch.json").write_text(canonical(batch) + "\n", encoding="utf-8")
    path = directory / "results.template.csv"
    fieldnames = ["batch_id", "sample_id", "sequence", "sequence_sha256", "arm", "metric", "unit",
                  "assay_protocol", "source", "qc", "value"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in batch.samples:
            writer.writerow({"batch_id": batch.batch_id, "sample_id": sample.sample_id,
                "sequence": sample.candidate.sequence, "sequence_sha256": sequence_hash(sample.candidate.sequence),
                "arm": sample.arm, "metric": task.metric, "unit": task.unit,
                "assay_protocol": task.assay_protocol, "source": task.feedback_source, "qc": "", "value": ""})
    return path


def read_results(path: str | Path) -> list[Observation]:
    results = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("sequence_sha256") != sequence_hash(row["sequence"]):
                raise ValueError("Sequence hash mismatch in results file")
            results.append(Observation(sample_id=row["sample_id"], batch_id=row["batch_id"],
                sequence=row["sequence"], value=float(row["value"]) if row["value"].strip() else None,
                qc=row["qc"], metric=row["metric"], unit=row["unit"],
                source=row["source"], assay_protocol=row["assay_protocol"]))
    if not results:
        raise ValueError("Empty result file")
    return results
