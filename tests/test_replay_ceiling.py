"""Artificial source values only; tests keep posthoc oracle access terminal-only."""
import importlib.util
from pathlib import Path

import pytest

from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.storage import Store

spec = importlib.util.spec_from_file_location("ceiling_script",
    Path(__file__).parents[1] / "scripts" / "evaluate_replay_ceiling.py")
ceiling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ceiling)


def fixture(tmp_path, *, status="complete", direction="maximize"):
    store = Store(tmp_path / "study")
    table = tmp_path / "synthetic.csv"
    table.write_text("sequence,value,qc\nAC,1,valid\nAD,2,valid\nAE,5,valid\n")
    store.put("configuration", "replay_dataset", {"sha256": file_sha256(table)})
    store.put("campaign", "state", {"status": status, "campaign_id": "synthetic", "round_index": 1,
        "pending_batch": None, "task": {"feedback_source": "measured_replay", "metric": "fitness",
            "unit": "synthetic", "direction": direction, "reference_sequence": "AC"},
        "observations": [{"sequence": "AC", "value": 1.0, "qc": "valid"},
                         {"sequence": "AD", "value": 2.0, "qc": "valid"},
                         {"sequence": "AF", "value": None, "qc": "unavailable"}]})
    return store, table


def test_terminal_ceiling_keeps_unqueried_identity_private_and_state_unchanged(tmp_path):
    store, table = fixture(tmp_path)
    original = store.get("campaign", "state")
    result = ceiling.evaluate(store.root, table)
    assert result["landscape_optimum_value"] == 5
    assert result["revealed_best"] == 2 and result["remaining_score_gap"] == 3
    assert result["records_strictly_better_than_revealed_best"] == 1
    assert result["records_strictly_better_than_parent"] == 2
    assert result["unqueried_sequence_identities_exported"] is False
    assert "AE" not in str(result)
    assert store.get("campaign", "state") == original


def test_nonterminal_rejected_before_missing_source_is_even_opened(tmp_path):
    store, _ = fixture(tmp_path, status="ready")
    with pytest.raises(ValueError, match="terminal"):
        ceiling.evaluate(store.root, tmp_path / "source_must_not_be_opened.csv")


def test_changed_source_rejected(tmp_path):
    store, table = fixture(tmp_path)
    table.write_text(table.read_text() + "AG,3,valid\n")
    with pytest.raises(ValueError, match="pinned"):
        ceiling.evaluate(store.root, table)


def test_saved_feedback_tampering_rejected(tmp_path):
    store, table = fixture(tmp_path)
    state = store.get("campaign", "state")
    state["observations"][1]["value"] = 4.0
    store.put("campaign", "state", state)
    with pytest.raises(ValueError, match="Saved feedback"):
        ceiling.evaluate(store.root, table)


def test_minimization_direction(tmp_path):
    store, table = fixture(tmp_path, direction="minimize")
    result = ceiling.evaluate(store.root, table)
    assert result["landscape_optimum_value"] == 1
    assert result["remaining_score_gap"] == 0
