"""No downloaded weights here. Fake backends are explicitly confined to tests."""
import numpy as np
import pytest

from proteinrsi.contracts import Candidate, GatePolicy, MetaPolicy, Observation, TaskSpec, Workflow
from proteinrsi.protein.analysis import rank_with_esmc
from proteinrsi.protein.esmc import ALPHABET, ESMC600M, ESMCConfig, TransformersBackend
from proteinrsi.protein.tools import TOOL_NAMES, register_esmc_tools
from proteinrsi.runtime import Campaign
from proteinrsi.storage import BudgetExceeded
from proteinrsi.tools import ToolCall, ToolGateway


class FakeBackend:
    identity = {"model_id": "TEST-ONLY", "revision": "0" * 40}

    def __init__(self):
        self.embed_calls, self.mask_calls = [], []

    def embed(self, sequences):
        self.embed_calls.extend(sequences)
        rows = []
        for sequence in sequences:
            row = np.zeros(1152)
            for i, aa in enumerate(sequence):
                row[i % 1152] += ALPHABET.index(aa)
            rows.append(row.tolist())
        return rows

    def masked(self, reference, positions):
        self.mask_calls.extend(positions)
        return [(np.arange(20) * 0.1 + p).tolist() for p in positions]


@pytest.fixture
def esmc_campaign(tmp_path):
    task = TaskSpec(name="test", reference_sequence="ACDE", mutable_positions=[2, 3],
                    candidates=["ACDE", "AADE", "AVDE", "ACAE", "AVAE"], max_mutations=2,
                    batch_size=4, controls_per_batch=0, max_rounds=2)
    config = ESMCConfig(max_model_inputs=100)
    campaign = Campaign.initialize(str(tmp_path / "esmc"), task, protein_config=config,
                                  meta=MetaPolicy(enabled=False),
                                  workflow=Workflow(tool_names=TOOL_NAMES),
                                  gate=GatePolicy(min_per_arm=2, bootstrap_samples=100))
    backend = FakeBackend()
    model = ESMC600M(campaign.store, config, backend=backend)
    return campaign, model, backend


def observations(view, values):
    return [Observation(sequence=s, value=v, sample_id=f"s{i}", batch_id="batch1",
                        metric=view.task.metric, unit=view.task.unit,
                        assay_protocol=view.task.assay_protocol, source=view.task.feedback_source)
            for i, (s, v) in enumerate(values)]


def test_esmc_config_fixes_model_and_rejects_bad_options():
    assert ESMCConfig().model_id == "biohub/ESMC-600M-hf"
    for kwargs in ({"model_id": "anything/else"}, {"device": "shell:rm"},
                   {"max_residues": 2047}, {"dtype": "float16"}, {"max_model_inputs": 0}):
        with pytest.raises(ValueError):
            ESMCConfig(**kwargs)


def test_masked_prior_positions_sign_and_cache(esmc_campaign):
    campaign, model, backend = esmc_campaign
    values = model.score_variants("ACDE", ["ACDE", "AVDE", "ACAE", "AVAE"])
    assert values == pytest.approx([0, 1.6, -0.2, 1.4])
    assert backend.mask_calls == [2, 3]
    model.score_variants("ACDE", ["AVAE"])
    assert backend.mask_calls == [2, 3]
    assert campaign.store.usage()["plm_inputs"]["committed"] == 2


def test_embedding_cache_deduplicates_and_preserves_order(esmc_campaign):
    campaign, model, backend = esmc_campaign
    x = model.embed(["ACDE", "AVDE", "ACDE"])
    assert x.shape == (3, 1152)
    assert np.array_equal(x[0], x[2])
    assert backend.embed_calls == ["ACDE", "AVDE"]
    assert np.array_equal(model.embed(["AVDE"])[0], x[1])
    assert campaign.store.usage()["plm_inputs"]["committed"] == 2


@pytest.mark.parametrize("seq", ["", "AcDE", "ACX", "AC|DE", "AC DE"])
def test_bad_sequences_rejected_before_backend(esmc_campaign, seq):
    _, model, backend = esmc_campaign
    with pytest.raises(ValueError):
        model.embed([seq])
    assert not backend.embed_calls


def test_context_and_indels_never_silently_truncated(esmc_campaign):
    _, model, _ = esmc_campaign
    with pytest.raises(ValueError):
        model.embed(["A" * 2047])
    with pytest.raises(ValueError):
        model.score_variants("ACDE", ["ACD"])
    for position in (0, 5, True, 1.5):
        with pytest.raises(ValueError):
            model.masked("ACDE", [position])


def test_suggestions_only_change_one_allowed_position(esmc_campaign):
    _, model, _ = esmc_campaign
    rows = model.suggest("ACDE", [2], 5)
    assert len(rows) == 5
    assert all(r["sequence"][0] == "A" and r["sequence"][2:] == "DE" for r in rows)
    assert all(r["evidence_kind"] == "proxy" and "predicted_value" not in r for r in rows)


def test_budget_blocks_inference_before_execution(esmc_campaign):
    campaign, model, backend = esmc_campaign
    campaign.store.reserve("use-all", "plm_inputs", 100, {})
    campaign.store.settle("use-all")
    with pytest.raises(BudgetExceeded):
        model.embed(["ACDE"])
    assert not backend.embed_calls


def test_bad_inference_cannot_become_a_cached_prediction(esmc_campaign):
    campaign, model, backend = esmc_campaign
    backend.embed = lambda _: [[float("nan")] * 1152]
    with pytest.raises(ValueError):
        model.embed(["ACDE"])
    assert not campaign.store.all("plm_cache")
    with pytest.raises(RuntimeError, match="failed/uncertain"):
        model.embed(["ACDE"])
    assert campaign.store.usage()["plm_inputs"]["committed"] == 1


def test_no_labels_produces_prior_not_fake_fitness(esmc_campaign):
    campaign, model, _ = esmc_campaign
    ranked, report = rank_with_esmc(campaign.view(), [Candidate(sequence="AVDE"),
                                                     Candidate(sequence="AADE")], model)
    assert report["prediction_kind"] == "sequence_prior_only"
    assert all(c.predicted_value is None for c in ranked)
    assert ranked[0].sequence == "AVDE"


def test_real_measurement_updates_head_not_fixed_backbone(esmc_campaign):
    campaign, model, backend = esmc_campaign
    view = campaign.view()
    candidates = [Candidate(sequence="AVDE"), Candidate(sequence="AADE")]
    view.observations = observations(view, [("AVDE", 5), ("AADE", 1)])
    ranked1, _ = rank_with_esmc(view, candidates, model)
    calls = len(backend.embed_calls)
    view.observations = observations(view, [("AVDE", 1), ("AADE", 5)])
    ranked2, _ = rank_with_esmc(view, candidates, model)
    assert ranked1[0].sequence != ranked2[0].sequence
    assert len(backend.embed_calls) == calls
    assert all(c.uncertainty is None and c.evidence_kind == "proxy" for c in ranked2)


def test_query_batch_does_not_fit_feature_scaler(esmc_campaign):
    campaign, model, _ = esmc_campaign
    view = campaign.view()
    view.workflow.exploration = 0
    view.observations = observations(view, [("ACDE", 1), ("AVDE", 3)])
    _, report1 = rank_with_esmc(view, [Candidate(sequence="AADE")], model)
    _, report2 = rank_with_esmc(view, [Candidate(sequence="AADE"), Candidate(sequence="AVAE")], model)
    assert report1["scores"][0]["task_prediction"] == pytest.approx(report2["scores"][0]["task_prediction"])


def test_protocol_mixing_rejected(esmc_campaign):
    campaign, model, _ = esmc_campaign
    view = campaign.view()
    view.observations = observations(view, [("AVDE", 3)])
    view.observations[0].unit = "different-unit"
    with pytest.raises(ValueError, match="protocols"):
        rank_with_esmc(view, [Candidate(sequence="AVDE")], model)


def test_local_tools_and_raw_vectors_do_not_enter_context(esmc_campaign):
    campaign, model, _ = esmc_campaign
    gateway = ToolGateway(campaign.store)
    register_esmc_tools(gateway, model)
    task = campaign.view().task
    result = gateway.call(ToolCall(name=TOOL_NAMES[2], arguments={"sequences": ["ACDE"]}),
                          task, allowed=TOOL_NAMES, context_key="test")
    assert result["shape"] == [1, 1152]
    assert "embeddings" not in result
    assert campaign.store.all("embedding_artifacts")
    with pytest.raises(ValueError, match="Protected"):
        gateway.call(ToolCall(name=TOOL_NAMES[0], arguments={"reference": "AAAA", "sequences": ["AAAC"]}),
                     task, allowed=TOOL_NAMES, context_key="bad")


def test_team_automatically_scores_post_design_candidates(esmc_campaign):
    from proteinrsi.agents import Team
    campaign, model, backend = esmc_campaign
    team = Team(campaign.store, protein_model=model)
    ranked = team.run(campaign.view())
    assert backend.mask_calls
    assert any("ESMC600M" in c.rationale for c in ranked)
    assert campaign.store.all("plm_analysis")


def test_cli_defaults_and_explicit_offline_mode(tmp_path, capsys):
    import json
    from proteinrsi.cli import main
    task = tmp_path / "task.json"
    task.write_text(TaskSpec(name="test", reference_sequence="ACDE", mutable_positions=[2]).model_dump_json())
    main(["init", "--task", str(task), "--out", str(tmp_path / "real")])
    capsys.readouterr()
    from proteinrsi.storage import Store
    store = Store(tmp_path / "real")
    assert store.get("configuration", "protein_model")["model_id"] == "biohub/ESMC-600M-hf"
    assert store.usage()["plm_inputs"]["committed"] == 0
    assert store.get("protein_backend", "snapshot") is None
    main(["init", "--task", str(task), "--out", str(tmp_path / "offline"), "--protein-model", "none"])
    report = json.loads(capsys.readouterr().out)
    assert report["protein_model"] is None


def test_native_transformers_token_alignment_and_padding_when_installed(esmc_campaign):
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from transformers import EsmcConfig, EsmcForMaskedLM, EsmcTokenizer
    campaign, _, _ = esmc_campaign
    backend = TransformersBackend(campaign.store, ESMCConfig())
    config = EsmcConfig(hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                        num_attention_heads=4, vocab_size=64)
    backend.model = EsmcForMaskedLM(config).eval()
    backend.tokenizer = EsmcTokenizer()
    x = np.asarray(backend.embed(["ACDE", "AC"]))
    assert x.shape == (2, 32)
    assert np.allclose(x[1], backend.embed(["AC"])[0], atol=1e-5)
    logs = backend.masked("ACDE", [2])
    assert np.asarray(logs).shape == (1, 20)
    inputs, _, offsets = backend._inputs(["ACDE"])
    inputs["input_ids"][0, offsets[0][1]] = backend.tokenizer.mask_token_id
    with torch.inference_mode():
        expected = backend.model(**inputs).logits[0, offsets[0][1]].float().log_softmax(-1)
    indices = backend.tokenizer.convert_tokens_to_ids(list(ALPHABET))
    assert np.allclose(logs[0], expected[indices].tolist(), atol=1e-6)


@pytest.mark.real_esmc
def test_official_esmc600m_weights_only_with_explicit_opt_in(tmp_path):
    import os
    if os.environ.get("PROTEINRSI_RUN_ESMC600M") != "1":
        pytest.skip("Set PROTEINRSI_RUN_ESMC600M=1 to download and run the real 600M checkpoint")
    from proteinrsi.storage import Store
    store = Store(tmp_path / "real")
    store.configure_budget({"plm_inputs": 10})
    model = ESMC600M(store, ESMCConfig())
    model.backend.load(download=True)
    vector = model.embed(["ACDEFGHIK"])
    scores = model.score_variants("ACDEFGHIK", ["ACDEFGHIK", "AVDEFGHIK"])
    assert vector.shape == (1, 1152) and np.isfinite(vector).all()
    assert scores[0] == 0 and np.isfinite(scores[1])
    assert len(store.get("protein_backend", "snapshot")["revision"]) == 40
    print("Real checkpoint:", model.identity)


def test_meta_offspring_inherits_exact_protein_configuration(esmc_campaign, tmp_path):
    from proteinrsi.agents import Team
    from proteinrsi.evaluation import MetaCase, _offspring_score
    campaign, model, _ = esmc_campaign
    view = campaign.view()
    labels = {s: float(i) for i, s in enumerate(view.task.candidates)}
    initial = observations(view, [("ACDE", labels["ACDE"]), ("AADE", labels["AADE"])])
    case = MetaCase(case_id="test", group_id="one-protein", split="validation", task=view.task,
                    initial=initial, labels=labels, query_budget=1)
    pin = {"model_id": model.config.model_id, "revision": "a" * 40}
    def factory(store):
        assert store.get("configuration", "protein_model") == model.config.model_dump()
        assert store.get("protein_backend", "snapshot") == pin
        assert store.usage()["plm_inputs"]["limit"] == 100
        backend = ESMC600M(store, model.config, backend=FakeBackend())
        return Team(store, protein_model=backend)
    score, trace = _offspring_score(case, view.workflow, MetaPolicy(enabled=False),
                                   str(tmp_path / "offspring"), factory,
                                   model.config.model_dump(), pin)
    assert np.isfinite(score) and trace["usage"]["plm_inputs"]["committed"] > 0


def test_feedback_round_uses_same_esmc_and_new_data(esmc_campaign):
    from proteinrsi.agents import Team
    campaign, model, _ = esmc_campaign
    campaign.team = Team(campaign.store, protein_model=model)
    batch = campaign.prepare()
    campaign.approve(batch.batch_id, operator="test")
    task = campaign.view().task
    measured = [Observation(sequence=sample.candidate.sequence, value=float(i),
                  sample_id=sample.sample_id, batch_id=batch.batch_id, metric=task.metric,
                  unit=task.unit, assay_protocol=task.assay_protocol, source=task.feedback_source)
                for i, sample in enumerate(batch.samples)]
    campaign.ingest(measured)
    assert campaign.state["round_index"] == 1
    campaign.team.run(campaign.view())
    reports = campaign.store.all("plm_analysis").values()
    assert any(r["prediction_kind"] == "sequence_prior_only" for r in reports)
    assert any(r["prediction_kind"] == "observed_label_ridge" for r in reports)


def test_esmc_skill_is_loadable():
    from proteinrsi.agents import skill_text
    assert "ESMC-600M" in skill_text(["esmc600m-analysis"])
