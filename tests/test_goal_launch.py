"""Artificial measurements and a fake HTTP provider; no real research or API calls."""
import csv
import itertools
import json

import httpx
import pytest

from proteinrsi.contracts import MetaPolicy, TaskSpec
from proteinrsi.launch import GB1, GoalIntent, load_gb1_inputs, prepare_goal
from proteinrsi.llm import JSONLLM
from proteinrsi.localtools.artifacts import file_sha256
from proteinrsi.replay.controller import run_replay
from proteinrsi.runtime import Campaign


@pytest.fixture
def gb1_data(tmp_path):
    root = tmp_path / "data"
    directory = root / "processed" / "GB1"
    directory.mkdir(parents=True)
    sequences = [GB1]
    for aa in itertools.islice(itertools.product("ACDE", repeat=4), 60):
        sequence = list(GB1)
        for index, residue in zip([38, 39, 40, 53], aa):
            sequence[index] = residue
        sequences.append("".join(sequence))
    dataset = directory / "measurements.csv"
    with dataset.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sequence", "value", "qc", "source", "label_kind"])
        writer.writeheader()
        for i, seq in enumerate(sequences):
            writer.writerow({"sequence": seq, "value": 1 if i == 0 else 987654.321+i,
                "qc": "valid", "source": "measured_replay", "label_kind": "reported_experimental_assay_score"})
    task = TaskSpec(name="TEST ONLY GB1", reference_sequence=GB1, mutable_positions=[39, 40, 41, 54],
                    max_mutations=4, feedback_source="measured_replay")
    (directory/"task.json").write_text(task.model_dump_json())
    (directory/"provenance.json").write_text(json.dumps({"status": "ready_strict_measured_replay",
        "landscape": "GB1", "parent": {"sequence": GB1},
        "prepared_assets": {"measurements_csv_sha256": file_sha256(dataset)},
        "original_study": "https://example.test/artificial-fixture"}))
    return root


def parser_factory(requests, result=None):
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "98765" not in request.content.decode()  # no hidden phenotypes
        assert "measurements.csv" not in request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            result or {"landscape": "GB1", "rounds": 2, "queries": 48, "batch_size": 24})}}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 10}})
    return lambda store: JSONLLM(store, model="fake", base_url="https://provider.test/v1",
        api_key="test-only", transport=httpx.MockTransport(respond))


@pytest.mark.parametrize("guarded", [False, True])
def test_goal_preparation_and_free_parent_two_full_rounds(tmp_path, gb1_data, guarded):
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()["available"]:
            pytest.skip("Landlock/libseccomp unavailable")
    requests = []
    campaign, dataset = prepare_goal("GB1两轮48个新查询，每轮24个", out=tmp_path/"study", data_root=gb1_data,
                                    llm_factory=parser_factory(requests))
    assert len(requests) == 1
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["parent_sequence"] == GB1 and context["parent_fitness"] == 1
    assert "candidates" not in context
    assert campaign.view().remaining_wells == 48
    assert len(campaign.view().observations) == 1
    assert campaign.report()["returned_observations"] == 0
    assert campaign.report()["provided_initial_observations"] == 1
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert campaign.report()["provider_reported_tokens"]["prompt_tokens"] == 15
    assert campaign.store.get("configuration", "research")["resource_selection"] == "llm"
    # Scripted native team only for accounting verification; no real LLM research.
    # Immutable configuration is intentional: use a second synthetic campaign for replay.
    assert campaign.view().task.candidate_access == "open"
    assert campaign.view().task.candidates == []
    # The deterministic baseline deliberately uses a separately supplied fixture library.
    # Autonomous open design is exercised with an actual HTTP-client mock in test_open_design.
    with dataset.open() as handle:
        fixture_library = [row["sequence"] for row in csv.DictReader(handle)]
    replay_task = TaskSpec.model_validate({**campaign.view().task.model_dump(),
        "candidate_access": "pool", "candidates": fixture_library})
    replay = Campaign.initialize(str(tmp_path/"replay"), replay_task, meta=MetaPolicy(enabled=False))
    report = run_replay(replay, dataset, guarded=guarded)
    assert report["completed_rounds"] == 2
    assert [r["batch_queries"] for r in report["round_progress"]] == [24, 24]
    assert [r["cumulative_campaign_queries"] for r in report["round_progress"]] == [24, 48]
    assert report["budget"]["experimental_wells"]["committed"] == 48
    assert report["provided_initial_observations"] == 1
    assert report["returned_observations"] == 48
    assert report["unique_measured_variants"] == 49
    assert report["repeat_queries"] == 0
    assert not any(s["candidate"]["sequence"] == GB1
        for b in replay.store.all("batches").values() for s in b["samples"])


def test_replay_rejects_incorrect_provided_parent_before_any_query(tmp_path, gb1_data):
    task, dataset = load_gb1_inputs(gb1_data)
    raw = task.model_dump()
    raw["initial_parent_measurement"]["value"] = 100
    campaign = Campaign.initialize(str(tmp_path/"wrong-parent"), TaskSpec.model_validate(raw))
    with pytest.raises(ValueError, match="Provided parent measurement differs"):
        run_replay(campaign, dataset, guarded=False)
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
    assert not campaign.store.all("batches")


def test_invalid_data_is_rejected_before_goal_api(tmp_path, gb1_data):
    dataset = gb1_data/"processed"/"GB1"/"measurements.csv"
    dataset.write_text(dataset.read_text()+"tampered\n")
    requests = []
    with pytest.raises(ValueError, match="provenance"):
        prepare_goal("GB1两轮48个查询", out=tmp_path/"invalid", data_root=gb1_data,
                     llm_factory=parser_factory(requests))
    assert requests == []


@pytest.mark.parametrize("result", [
    {"landscape": "GB1", "rounds": 2},
    {"landscape": "GB1", "rounds": 2, "queries": 48, "issues": ["不支持改变位点"]},
    {"landscape": "TEV", "rounds": 2, "queries": 48},
    {"landscape": "GB1", "rounds": 2, "queries": 49, "batch_size": 24},
])
def test_ambiguous_or_unsupported_goal_never_starts_study(tmp_path, gb1_data, result):
    requests = []
    with pytest.raises(ValueError):
        prepare_goal("an unsupported request", out=tmp_path/"invalid-goal", data_root=gb1_data,
                     llm_factory=parser_factory(requests, result))
    assert len(requests) == 1
    assert not (tmp_path/"invalid-goal"/"state.sqlite3").exists()
    assert (tmp_path/"invalid-goal"/"goal-intake"/"state.sqlite3").exists()


def test_intent_derives_only_arithmetic_limits():
    assert GoalIntent(landscape="GB1", rounds=2, batch_size=24).queries == 48
    assert GoalIntent(landscape="GB1", rounds=2, queries=48).batch_size == 24


def test_free_parent_requires_a_real_input_value_and_source(gb1_data):
    task, _ = load_gb1_inputs(gb1_data)
    raw = task.model_dump()
    raw["initial_parent_measurement"] = None
    with pytest.raises(ValueError, match="explicit measurement"):
        TaskSpec.model_validate(raw)
    raw = task.model_dump()
    raw["initial_parent_measurement"]["value"] = float("nan")
    with pytest.raises(ValueError):
        TaskSpec.model_validate(raw)


@pytest.mark.parametrize("full_plate", [False, True])
def test_cli_prepare_only_uses_natural_goal_without_queries(tmp_path, gb1_data, monkeypatch, capsys, full_plate):
    from proteinrsi.cli import main
    requests = []
    metadata = json.loads((gb1_data/"processed"/"GB1"/"task.json").read_text())
    intent = {"landscape": "GB1", "task_kind": "variant_design", "route": "measured_replay",
        "rationale": "User requested GB1 replay", "rounds": 2, "queries": 48, "batch_size": 24,
        **{k: metadata[k] for k in ("reference_sequence", "mutable_positions", "max_mutations",
                                  "metric", "unit", "direction", "assay_protocol")}}
    monkeypatch.setattr(JSONLLM, "from_env", staticmethod(parser_factory(requests, intent)))
    monkeypatch.chdir(tmp_path)
    # Explicit empty local configuration avoids dependence on the developer's engines.
    tools = tmp_path/"tools.json"
    tools.write_text('{"utilities": false}')
    out = tmp_path/"cli-study"
    main(["start", "GB1两轮48个新查询，每轮24个", "--data-root", str(gb1_data),
          "--out", str(out), "--local-tools", str(tools), "--prepare-only",
          *(["--full-plate"] if full_plate else [])])
    assert len(requests) == 1
    report = json.loads((out/"report.json").read_text())
    assert report["completed_rounds"] == 0
    saved_task = json.loads((out/"task.json").read_text())
    assert saved_task["candidate_access"] == "open" and saved_task["candidates"] == []
    assert saved_task["batch_fill_policy"] == ("full_plate" if full_plate else "flexible")
    assert report["budget"]["experimental_wells"]["committed"] == 0
    assert report["initial_evidence"]["observations"][0]["value"] == 1
    assert "parent_query_cost" in capsys.readouterr().out


def test_start_requires_isolation_before_even_parsing(tmp_path, monkeypatch):
    from proteinrsi.cli import main
    monkeypatch.setattr("proteinrsi.replay.sandbox.probe", lambda: {"available": False, "reason": "no sandbox"})
    with pytest.raises(SystemExit) as exc:
        main(["start", "GB1两轮48个查询", "--out", str(tmp_path/"blocked")])
    assert exc.value.code == 2
    assert not (tmp_path/"blocked").exists()
