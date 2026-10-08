"""A-planned template/metric integration, using scripted roles and synthetic tool data."""
from copy import deepcopy
from types import SimpleNamespace

from proteinrsi.contracts import Candidate, TaskSpec, TaskView, Workflow, MetaPolicy
from proteinrsi.dataflow.design import Design
from proteinrsi.dataflow.integration import run_campaign_protocol
from proteinrsi.dataflow.resources import SEQUENCES, RANKING, METRICS
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.research.metric_tools import TOOL, register_metric_tool
from proteinrsi.research.skill_library import SKILLS
from proteinrsi.tools import ToolGateway, ToolSpec
from proteinrsi.localtools.artifacts import ArtifactStore


def binding(step, schema, *, pointer="", ref=False):
    return {"source": "step:" + step, "schema_ref": schema, "pointer": pointer,
            "delivery": "ref" if ref else "value"}


class Planner:
    model = "scripted-fixture"
    base_url = "https://example.invalid"
    cache_settings = {}

    def __init__(self, binder=False):
        self.calls = []
        self.binder = binder

    def complete(self, role, instructions, context, schema):
        self.calls.append((role, deepcopy(context)))
        if role == "A-resources":
            if "catalogue" not in context:
                return {}
            return {"template_ids": ["binder.structural_screen" if self.binder else "variant.observed_optimization"],
                    "extra_metric_ids": [], "rationale": "Use the task-specific fixture template"}
        assert role == "A-plan"
        knowledge = context["resources"]["method_knowledge"]
        assert len(knowledge["templates"]) == 1
        operations = {o["name"]: o for o in context["operations"]}
        metric_schema = operations["tool:" + TOOL]["output_schema_ref"]
        steps = [{"step_id": "design", "operation": "agent:propose", "question": "Propose a legal test candidate",
                  "arguments": {"question": "One fixture candidate"}}]
        if self.binder:
            native = operations["tool:protenix_predict"]["output_schema_ref"]
            ros = operations["tool:rosetta_interface"]["output_schema_ref"]
            steps += [
                {"step_id": "predict", "operation": "tool:protenix_predict", "question": "Synthetic prediction",
                 "arguments": {"sequence": "ACAA", "assembly": "complex", "seeds": [7], "samples": 1}},
                {"step_id": "confidence", "operation": "tool:" + TOOL, "question": "Read two confidence fields",
                 "arguments": {"metric_ids": ["protenix.plddt_mean", "protenix.iptm"]},
                 "bindings": {"source_ref": binding("predict.result", native, ref=True)},
                 "outputs": {"table": {"schema_ref": METRICS, "pointer": "/metric_table"}}},
                {"step_id": "energy", "operation": "tool:rosetta_interface", "question": "Synthetic energy",
                 "arguments": {"target_chain": "A", "design_chain": "B", "seed": 7},
                 "bindings": {"structure_ref": binding("predict.result", native, pointer="/artifacts/1/ref")}},
                {"step_id": "energy_metrics", "operation": "tool:" + TOOL, "question": "Read energy field",
                 "arguments": {"metric_ids": ["rosetta.interface_dG"]},
                 "bindings": {"source_ref": binding("energy.result", ros, ref=True)},
                 "outputs": {"table": {"schema_ref": METRICS, "pointer": "/metric_table"}}},
                {"step_id": "metrics", "operation": "adapter:merge_metric_tables", "question": "Join actual metric rows",
                 "bindings": {"left": binding("confidence.table", METRICS, ref=True),
                              "right": binding("energy_metrics.table", METRICS, ref=True)}}]
            evidence = binding("metrics.result", METRICS)
        else:
            assert knowledge["metric_ids"] == ["observed.summary"]
            steps += [{"step_id": "metrics", "operation": "tool:" + TOOL, "question": "Describe revealed evidence",
                       "arguments": {"metric_ids": ["observed.summary"], "top_ns": [1]}}]
            evidence = binding("metrics.result", metric_schema, pointer="/metric_table")
        steps += [
            {"step_id": "rank", "operation": "agent:rank", "question": "Rank using actual metrics",
             "bindings": {"candidates": binding("design.result", SEQUENCES, ref=True), "evidence": evidence}},
            {"step_id": "finish", "operation": "adapter:ranked_sequences", "question": "Return validated candidates",
             "bindings": {"ranking": binding("rank.result", RANKING, ref=True)}}]
        return {"hypothesis": "Fixture adaptation: template-bound metrics, unchanged task constraints",
                "steps": steps, "final_outputs": {"candidates": binding("finish.result", SEQUENCES)}}


def make_team(store, tmp_path, binder=False):
    task = TaskSpec(name="Template fixture", reference_sequence="AAAA", mutable_positions=[2],
        target_sequence="CCCC" if binder else None, kind="binder_design" if binder else "variant_design",
        controls_per_batch=0)
    names = [TOOL, *(["protenix_predict", "rosetta_interface"] if binder else [])]
    view = TaskView(task=task, round_index=0, observations=[], history=[], remaining_wells=20,
        workflow=Workflow(skill_names=list(SKILLS), tool_names=names), meta=MetaPolicy())
    llm, gateway, design_contexts, rank_contexts, native_calls = Planner(binder), ToolGateway(store), [], [], []
    def propose(v, p, results, catalog):
        design_contexts.append(results)
        return Design(candidates=[Candidate(sequence="ACAA")])
    def rank(v, candidates, evidence):
        rank_contexts.append(evidence)
        return candidates
    if binder:
        registry = ArtifactStore(store)
        native_artifacts = []
        for kind, contents in [("cif", "synthetic cif"), ("pdb", "synthetic pdb"), ("json", '{"plddt":85,"iptm":0.8,"has_clash":false}')]:
            path = tmp_path / ("native." + kind)
            path.write_text(contents)
            name = "proteinrsi_7_" + ("summary_confidence_" if kind == "json" else "") + "sample_0." + kind
            native_artifacts.append({**registry.put(path, kind), "filename": "outputs/" + name})
        for name, output in [("protenix_predict", {"artifacts": native_artifacts, "input_entities": {"A": "CCCC", "B": "ACAA"}}),
                             ("rosetta_interface", {"metrics": {"interface_dG": {"value": -8, "unit": "REU"}}})]:
            spec = ToolSpec(name=name, capability="synthetic.fixture", implementation_version="fixture-v1",
                task_kinds=[task.kind], input_schema={"type": "object"}, output_schema={"type": "object"})
            def invoke(args, output=output, name=name):
                native_calls.append(name)
                return deepcopy(output)
            gateway.register(spec, invoke)
    register_metric_tool(gateway, view)
    team = SimpleNamespace(store=store, llm=llm, tools=gateway, protein_model=None,
        designer=SimpleNamespace(propose=propose), analyst=SimpleNamespace(rank=rank),
        principal=SimpleNamespace(finalize=lambda v, candidates: candidates))
    return team, view, design_contexts, rank_contexts, native_calls


def test_variant_template_actual_protocol_and_cache(store, tmp_path):
    team, view, designs, analyses, calls = make_team(store, tmp_path)
    config = ResearchConfig(protocol_mode="typed", resource_selection="llm", review_after_step=False)
    result = run_campaign_protocol(team, view, config)
    assert [c.sequence for c in result] == ["ACAA"]
    assert designs[0][0]["method_knowledge"]["metric_ids"] == ["observed.summary"]
    assert analyses[0][1]["rows"][0]["name"] == "observed.best"
    assert not calls
    n = len(team.llm.calls)
    assert run_campaign_protocol(team, view, config) == result
    assert len(team.llm.calls) == n and len(designs) == 1
    assert store.usage()["tool_calls"]["committed"] == 1


def test_binder_template_multiple_providers_to_c(store, tmp_path):
    team, view, designs, analyses, calls = make_team(store, tmp_path, binder=True)
    config = ResearchConfig(protocol_mode="typed", resource_selection="llm", review_after_step=False)
    result = run_campaign_protocol(team, view, config)
    assert [c.sequence for c in result] == ["ACAA"]
    rows = analyses[0][1]["rows"]
    assert {r["name"]: r["value"] for r in rows} == {
        "protenix.plddt_mean": 85, "protenix.iptm": 0.8, "rosetta.interface_dG": -8}
    assert calls == ["protenix_predict", "rosetta_interface"]
    assert store.usage()["tool_calls"]["committed"] == 4
    assert store.usage()["experimental_wells"]["committed"] == 0
    assert run_campaign_protocol(team, view, config) == result
    assert calls == ["protenix_predict", "rosetta_interface"]
