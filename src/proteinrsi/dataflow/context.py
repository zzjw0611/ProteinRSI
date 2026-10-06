# SPDX-License-Identifier: MIT
"""Lossless presentation of revealed evidence in designer requests only.

The runtime TaskView and tool/code inputs retain their original contracts.
No hidden labels are read and no observations are sampled or summarized away.
"""
from copy import deepcopy

from proteinrsi.contracts import canonical


CONTEXT_FORMAT = """
Context encoding (presentation only; tool/code input contracts are unchanged):
A context_ref='/view' in tool_results refers to the same view already supplied.
When view.observations is a revealed-observations-v1 table, reconstruct each record
by merging shared_fields with dict(zip(columns, row)). All records are included;
null fitness with non-valid QC is missing feedback, never a numerical zero.
If sequence_encoding is present, the sequence cell contains residues at its
1-based positions; substitute these into view.task.reference_sequence to recover
the full sequence. All other positions are verified equal to that reference.
plate_completion.exclude_observed_sequences=true means exclude every sequence in
view.observations in addition to the remaining explicit exclude_sequences.
"""


def compact_view(raw: dict) -> dict:
    result = deepcopy(raw)
    observations = raw.get("observations", [])
    if not observations:
        return result
    rows = deepcopy(observations)
    task = raw.get("task", {})
    reference = task.get("reference_sequence", "")
    positions = task.get("mutable_positions", [])
    # Use a positional encoding only when every full sequence is exactly
    # recoverable, including observations from tasks allowing indels or ranking.
    indices = {p - 1 for p in positions}
    positional = (bool(reference and positions) and len(indices) == len(positions)
        and all(0 <= i < len(reference) for i in indices)
        and all(len(row["sequence"]) == len(reference)
                and all(a == b or i in indices
                        for i, (a, b) in enumerate(zip(row["sequence"], reference))) for row in rows))
    if positional:
        for row in rows:
            row["sequence"] = "".join(row["sequence"][p - 1] for p in positions)
    shared = {k: v for k, v in rows[0].items()
              if all(k in row and row[k] == v for row in rows)}
    # Observation records have one fixed schema; preserve nulls and all columns.
    columns = [k for k in rows[0] if k not in shared]
    table = {"format": "revealed-observations-v1", "count": len(rows),
        "shared_fields": shared, "columns": columns,
        "rows": [[row[k] for k in columns] for row in rows]}
    if positional:
        table["sequence_encoding"] = {"kind": "reference_positions", "positions_1based": list(positions)}
    # Small contexts may be more readable as the original list.
    if len(canonical(table)) < len(canonical(observations)):
        result["observations"] = table
    plate = result.get("research_context", {}).get("plate_completion", {})
    excluded = plate.get("exclude_sequences")
    observed = {row["sequence"] for row in observations}
    if isinstance(excluded, list) and observed <= set(excluded):
        plate["exclude_sequences"] = [s for s in excluded if s not in observed]
        plate["exclude_observed_sequences"] = True
    return result


def prepare_evidence(raw_view: dict, tool_results: list[dict]) -> tuple[dict, list[dict]]:
    """Replace only exact duplicate views; distinct tool evidence stays intact."""
    evidence = [{"context_ref": "/view"} if item == raw_view else deepcopy(item)
                for item in tool_results]
    return compact_view(raw_view), evidence
