"""Synthetic native-output fixtures: actual parser/gateway/Protocol, no GPU claims."""
from copy import deepcopy
import json

import pytest
from jsonschema import ValidationError

from proteinrsi.contracts import MetaPolicy, Observation, TaskSpec, TaskView, Workflow, digest
from proteinrsi.dataflow.protocol import Protocol, ProtocolExecutor, OperationRegistry, register_tool_operations
from proteinrsi.dataflow.resources import ResourceStore, standard_registry, scope_for, METRICS
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.localtools.metric_extractors import extract_provider, parse_mpnn, protenix_samples
from proteinrsi.research.metric_tools import TOOL, register_metric_tool, verified_source
from proteinrsi.storage import Store
from proteinrsi.tools import ToolGateway, ToolCall, ToolSpec


def setup(tmp_path, kind="binder_design"):
    store = Store(tmp_path / "state")
    store.configure_budget({"tool_calls": 40, "llm_calls": 20, "experimental_wells": 20})
    task = TaskSpec(name="Synthetic metric test", reference_sequence="AAAA", mutable_positions=[2],
        target_sequence="CCCC" if kind == "binder_design" else None, kind=kind, controls_per_batch=0)
    view = TaskView(task=task, round_index=0, observations=[], history=[], remaining_wells=20,
        workflow=Workflow(skill_names=["protein-metrics", "protein-design-workflows"], tool_names=[TOOL]), meta=MetaPolicy())
    return store, view, ToolGateway(store), ArtifactStore(store)


def artifact(registry, tmp_path, filename, text, kind):
    path = tmp_path / (str(len(registry.catalog())) + "." + kind)
    path.write_text(text)
    return {**registry.put(path, kind), "filename": filename}


def protenix_fixture(registry, tmp_path, seeds=(7, 13), samples=2):
    files = []
    for seed in seeds:
        for sample in range(samples):
            prefix = f"outputs/proteinrsi/seed_{seed}/predictions/proteinrsi_{seed}_"
            for kind in ("pdb", "cif"):
                files.append(artifact(registry, tmp_path, prefix + f"sample_{sample}.{kind}", f"Synthetic {kind} {seed} {sample}", kind))
            files.append(artifact(registry, tmp_path, prefix + f"summary_confidence_sample_{sample}.json",
                json.dumps({"plddt": 70 + seed + sample, "iptm": 0.7 + sample / 10, "has_clash": False}), "json"))
    return {"artifacts": list(reversed(files)), "input_entities": {"A": "CCCC", "B": "AAAA"}}


def register_provider(gateway, view, name, result, counter):
    view.workflow.tool_names = [name, TOOL]
    spec = ToolSpec(name=name, capability="synthetic.test", implementation_version="fixture-v1",
        task_kinds=list(type(view.task.kind)), input_schema={"type": "object"}, output_schema={"type": "object"})
    def execute(args):
        counter.append(args)
        return deepcopy(result)
    gateway.register(spec, execute)
    register_metric_tool(gateway, view)


def execute_protocol(store, view, gateway, provider, arguments, metric_ids):
    resources = ResourceStore(store, standard_registry(), scope_for(view))
    ops = OperationRegistry(resources.registry)
    register_tool_operations(ops, gateway, view.task, view.workflow.tool_names)
    native = ops.get("tool:" + provider).output_schema_ref
    extracted = ops.get("tool:" + TOOL).output_schema_ref
    protocol = Protocol.model_validate({"hypothesis": "Synthetic metric parser integration", "steps": [
        {"step_id": "predict", "operation": "tool:" + provider, "question": "Produce fixture", "arguments": arguments},
        {"step_id": "metrics", "operation": "tool:" + TOOL, "question": "Extract real fixture fields",
         "arguments": {"metric_ids": metric_ids}, "bindings": {"source_ref": {
             "source": "step:predict.result", "schema_ref": native, "delivery": "ref"}},
         "outputs": {"table": {"schema_ref": METRICS, "pointer": "/metric_table"}}}],
        "final_outputs": {"metrics": {"source": "step:metrics.table", "schema_ref": METRICS}}})
    result = ProtocolExecutor(resources, ops).execute(protocol, {})
    return resources, ops, protocol, result, extracted


def test_single_prediction_multiple_metrics_seeds_and_resume(tmp_path):
    store, view, gateway, registry = setup(tmp_path)
    raw = protenix_fixture(registry, tmp_path)
    counter = []
    register_provider(gateway, view, "protenix_predict", raw, counter)
    resources, ops, protocol, result, _ = execute_protocol(store, view, gateway, "protenix_predict",
        {"sequence": "AAAA", "assembly": "complex", "seeds": [7, 13], "samples": 2},
        ["protenix.plddt_mean", "protenix.iptm", "protenix.has_clash"])
    rows = resources.get(result["outputs"]["metrics"]["resource_id"])["data"]["rows"]
    assert len(rows) == 12
    assert sorted(r["value"] for r in rows if r["name"] == "protenix.plddt_mean") == [77, 78, 83, 84]
    saved = next(iter(store.all("research_analysis").values()))
    assert {(p["seed"], p["sample"]) for p in saved["row_provenance"]} == {(7, 0), (7, 1), (13, 0), (13, 1)}
    assert saved["measurement_authority"] is False
    assert ProtocolExecutor(resources, ops).execute(protocol, {}) == result
    assert len(counter) == 1 and store.usage()["tool_calls"]["committed"] == 2
    assert store.usage()["experimental_wells"]["committed"] == 0


def test_source_must_be_completed_provider_not_agent_assertion(tmp_path):
    store, view, gateway, registry = setup(tmp_path)
    raw = protenix_fixture(registry, tmp_path, seeds=(7,), samples=1)
    register_provider(gateway, view, "protenix_predict", raw, [])
    resources, ops, _, _, _ = execute_protocol(store, view, gateway, "protenix_predict",
        {"sequence": "AAAA", "assembly": "complex", "seeds": [7], "samples": 1}, ["protenix.iptm"])
    schema = ops.get("tool:protenix_predict").output_schema_ref
    false_ref = resources.put(schema, raw, producer="agent:C")["resource_id"]
    with pytest.raises(ContractError, match="receipt"):
        verified_source(store, view, gateway, false_ref)
    real = next(r["source_resource_ref"] for r in store.all("research_analysis").values())
    altered = deepcopy(store.get("research_step_outputs", real))
    altered["data"]["input_entities"]["B"] = "ACAA"
    forged = "resource:" + digest(altered)
    store.put("research_step_outputs", forged, altered)
    with pytest.raises(ContractError, match="receipt"):
        verified_source(store, view, gateway, forged)
    changed_view = view.model_copy(update={"round_index": 1})
    with pytest.raises(ContractError, match="scope"):
        verified_source(store, changed_view, gateway, real)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_seed", "unsafe", "wrong_sample"])
def test_protenix_sample_mapping_rejects_bad_files(tmp_path, mutation):
    _, _, _, registry = setup(tmp_path)
    raw = protenix_fixture(registry, tmp_path, seeds=(7,), samples=1)
    if mutation == "missing":
        raw["artifacts"] = raw["artifacts"][1:]
    if mutation == "duplicate":
        raw["artifacts"].append(raw["artifacts"][0])
    if mutation == "wrong_seed":
        raw["artifacts"][0]["filename"] = raw["artifacts"][0]["filename"].replace("proteinrsi_7_", "proteinrsi_9_")
    if mutation == "unsafe":
        raw["artifacts"][0]["filename"] = "../bad.json"
    if mutation == "wrong_sample":
        raw["artifacts"][0]["filename"] = raw["artifacts"][0]["filename"].replace("sample_0", "sample_1")
    with pytest.raises(ContractError):
        protenix_samples(raw, {"seeds": [7], "samples": 1})


@pytest.mark.parametrize("confidence", [{}, {"iptm": None}, {"iptm": True}, {"iptm": 2}, {"iptm": float("nan")}])
def test_protenix_missing_or_invalid_numeric_fields(tmp_path, confidence):
    _, _, _, registry = setup(tmp_path)
    raw = protenix_fixture(registry, tmp_path, seeds=(7,), samples=1)
    target = next(a for a in raw["artifacts"] if a["kind"] == "json")
    new = artifact(registry, tmp_path, target["filename"], json.dumps(confidence), "json")
    raw["artifacts"] = [new if a is target else a for a in raw["artifacts"]]
    with pytest.raises(ContractError):
        extract_provider(raw, "protenix_predict", {"sequence": "AAAA", "assembly": "complex", "seeds": [7], "samples": 1},
            ["protenix.iptm"], registry, method="fixture")


def test_protenix_mononer_and_pae_not_silently_supported(tmp_path):
    _, _, _, registry = setup(tmp_path)
    raw = protenix_fixture(registry, tmp_path, seeds=(7,), samples=1)
    with pytest.raises(ContractError, match="complex"):
        extract_provider(raw, "protenix_predict", {"sequence": "AAAA", "assembly": "monomer"}, ["protenix.iptm"], registry, method="fixture")
    with pytest.raises(ContractError, match="unsupported"):
        extract_provider(raw, "protenix_predict", {}, ["protenix.pae"], registry, method="fixture")


def test_observed_summary_uses_bound_evidence_only(tmp_path):
    store, view, gateway, _ = setup(tmp_path, "variant_design")
    view.observations = [Observation(sample_id=str(i), sequence=seq, value=value, metric=view.task.metric,
        unit=view.task.unit, qc=qc, source="synthetic", batch_id="test", assay_protocol="fixture")
        for i, (seq, value, qc) in enumerate([("ACAA", 2., "valid"), ("ACAA", 4., "valid"), ("ADAA", 5., "valid"), ("AEAA", None, "failed")])]
    register_metric_tool(gateway, view)
    call = ToolCall(name=TOOL, arguments={"metric_ids": ["observed.summary"], "top_ns": [2, 3]})
    result = gateway.call(call, view.task, allowed=[TOOL], context_key="observed-fixture")
    assert result["summary"]["metrics"] == {"best": 5, "top2mean": 4, "top3mean": None, "avg": 4}
    assert result["summary"]["denominators"]["unique_valid"] == 2
    assert gateway.call(call, view.task, allowed=[TOOL], context_key="observed-fixture") == result
    with pytest.raises(ValidationError):
        gateway.call(ToolCall(name=TOOL, arguments={**call.arguments, "observations": [{"value": 100}]}),
                     view.task, allowed=[TOOL], context_key="forged-input")


def test_rosetta_units_and_rmsd_scope(tmp_path):
    _, _, _, registry = setup(tmp_path)
    struct = artifact(registry, tmp_path, "sample.pdb", "Synthetic structure", "pdb")
    params = {"structure_ref": struct["ref"], "target_chain": "A", "design_chain": "B", "seed": 7}
    raw = {"metrics": {"interface_dG": {"value": -5.0, "unit": "REU"}}}
    result = extract_provider(raw, "rosetta_interface", params, ["rosetta.interface_dG"], registry, method="fixture")
    assert result["metric_table"]["rows"][0]["value"] == -5
    raw["metrics"]["interface_dG"]["unit"] = "kcal/mol"
    with pytest.raises(ContractError, match="unit"):
        extract_provider(raw, "rosetta_interface", params, ["rosetta.interface_dG"], registry, method="fixture")
    result = extract_provider({"ca_rmsd_angstrom": 1.2, "matched_residues": 4, "mapping": "explicit"},
        "structure_compare", {"reference_ref": struct["ref"], "prediction_ref": struct["ref"],
        "reference_chain": "B", "prediction_chain": "B"}, ["structure.ca_rmsd"], registry, method="fixture")
    assert result["row_provenance"][0]["matched_residues"] == 4


def test_mpnn_from_fasta_not_rationale_and_esmc_identity(tmp_path):
    _, _, _, registry = setup(tmp_path)
    text = ">native, designed_chains=['B']\nAAAA\n>T=0.1, sample=1, score=1.25, global_score=9\nACAA\n"
    assert parse_mpnn(text, [{"sequence": "ACAA", "rationale": "score=100"}]) == [(1, "ACAA", 1.25)]
    with pytest.raises(ContractError, match="identity"):
        parse_mpnn(text, [{"sequence": "ADAA"}])
    fasta = artifact(registry, tmp_path, "outputs/seqs/input.fa", text, "fasta")
    result = extract_provider({"artifacts": [fasta], "candidates": [{"sequence": "ACAA", "rationale": "score=100"}]},
        "proteinmpnn_design", {"seed": 7, "backbone_ref": "fixture-backbone", "design_chain": "B"},
        ["proteinmpnn.score"], registry, method="fixture")
    assert result["metric_table"]["rows"][0]["value"] == 1.25
    args = {"reference": "AAAA", "sequences": ["ACAA"]}
    result = extract_provider({"scores": [{"sequence": "ACAA", "masked_marginal_log_odds": -0.3}]},
        "esmc600m_score_variants", args, ["esmc.masked_marginal_log_odds"], registry, method="fixture")
    assert result["metric_table"]["rows"][0]["value"] == -0.3
    with pytest.raises(ContractError, match="identities"):
        extract_provider({"scores": [{"sequence": "ADAA", "masked_marginal_log_odds": -0.3}]},
            "esmc600m_score_variants", args, ["esmc.masked_marginal_log_odds"], registry, method="fixture")
