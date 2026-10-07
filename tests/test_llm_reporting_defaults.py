"""Reporting choices cannot silently replace recorded LLM evaluation criteria."""
import json

from proteinrsi.cli import main
from proteinrsi.contracts import TaskSpec
from proteinrsi.reporting_metrics import evaluation_report_configuration
from proteinrsi.storage import Store


class Records:
    def __init__(self, values):
        self.values = values

    def all(self, namespace):
        return self.values.get(namespace, {})


def test_reports_use_recorded_llm_choices_without_reevaluating_verdicts():
    verdict = {"decision": "accepted", "reason": "Prespecified best-sequence preference"}
    records = Records({"evaluation_plans": {
        "first": {"plan": {"top_ns": [3], "criteria": "First recorded criteria"}},
        "second": {"plan": {"top_ns": [8, 3], "criteria": "Future recorded criteria"}},
    }, "evaluation_verdicts": {"judgment": verdict}})
    state = {"gate": {"criterion": "llm_adjudicated_v1"}}
    automatic = evaluation_report_configuration(records, state)
    assert automatic["top_ns"] == [3, 8]
    assert automatic["top_n_source"] == "union_of_recorded_llm_plan_top_ns"
    explicit = evaluation_report_configuration(records, state, [2])
    assert explicit["top_ns"] == [2]
    assert explicit["llm_chosen_top_ns"] == [3, 8]
    assert explicit["llm_evaluation_verdicts"] == automatic["llm_evaluation_verdicts"]
    assert explicit["llm_evaluation_verdicts"]["evaluation_verdicts/judgment"] == verdict


def test_no_plan_display_defaults_are_not_claimed_as_llm_criteria():
    report = evaluation_report_configuration(Records({}), {"gate": {"criterion": "llm_adjudicated_v1"}})
    assert report["top_ns"] == [5, 10]
    assert report["llm_chosen_top_ns"] == []
    assert report["top_n_source"] == "presentation_defaults_not_acceptance_criteria"
    assert report["llm_evaluation_plans"] == report["llm_evaluation_verdicts"] == {}


def test_new_cli_init_defaults_to_llm_mode_without_fabricating_a_plan(tmp_path):
    task = tmp_path / "task.json"
    task.write_text(TaskSpec(name="CLI evaluation selection", reference_sequence="ACDE",
        mutable_positions=[2], max_mutations=1).model_dump_json())
    output = tmp_path / "new"
    main(["init", "--task", str(task), "--out", str(output), "--protein-model", "none"])
    store = Store(output)
    assert store.get("campaign", "state")["gate"] == {"criterion": "llm_adjudicated_v1"}
    assert store.all("evaluation_plans") == {}
    assert store.usage()["llm_calls"]["committed"] == 0


def test_explicit_legacy_cli_configuration_remains_legacy(tmp_path):
    task = tmp_path / "task.json"
    task.write_text(TaskSpec(name="Explicit legacy selection", reference_sequence="ACDE",
        mutable_positions=[2], max_mutations=1).model_dump_json())
    gate = tmp_path / "legacy.json"
    gate.write_text(json.dumps({"min_per_arm": 2}))
    output = tmp_path / "old-mode"
    main(["init", "--task", str(task), "--out", str(output), "--protein-model", "none",
          "--gate", str(gate)])
    recorded = Store(output).get("campaign", "state")["gate"]
    assert recorded["min_per_arm"] == 2
    assert "criterion" not in recorded
