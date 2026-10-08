"""Knowledge loading tests use scripted providers, not scientific model inference."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from proteinrsi.contracts import MetaPolicy, TaskSpec, TaskView, Workflow, canonical, digest
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.research.contracts import ResearchConfig
from proteinrsi.research.resources import ResourceSelector
from proteinrsi.research import skill_library as lib
from proteinrsi.storage import Store


def make_view(kind="variant_design"):
    task = TaskSpec(name="Optimize measured outcomes", reference_sequence="AAAA", mutable_positions=[2],
        kind=kind, target_sequence="CCCC" if kind == "binder_design" else None,
        controls_per_batch=0, budget={"experimental_wells": 8})
    return TaskView(task=task, round_index=0, observations=[], history=[], remaining_wells=8,
        workflow=Workflow(skill_names=list(lib.SKILLS), tool_names=["research_metric_extract"]), meta=MetaPolicy())


def choice(template="variant.observed_optimization", extras=()):
    return {"template_ids": [template] if template else [], "extra_metric_ids": list(extras), "rationale": "Test selection"}


class ChoosingLLM:
    model = "synthetic-choice"
    base_url = "https://example.invalid"
    cache_settings = {}

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, role, instructions, context, schema):
        self.calls.append((role, context))
        return next(self.responses)


def test_library_has_nine_compact_cards_and_six_categories():
    library = lib.packaged_library()
    cards, templates = lib.validate_library(library)
    assert len(cards) == 9
    assert len({t["category"] for t in templates.values()}) == 6
    assert all(set(c) == lib.CARD_FIELDS and "limitations" not in c for c in cards.values())
    assert "protenix.pae" not in lib.EXTRACTABLE
    invalid = deepcopy(library)
    invalid["templates"][0]["metrics"][0]["id"] = "invented.metric"
    with pytest.raises(ContractError, match="Unresolved"):
        lib.validate_library(invalid)


def test_only_template_metrics_expanded_and_choice_cached(tmp_path):
    store, view = Store(tmp_path), make_view()
    llm = ChoosingLLM([choice()])
    config = ResearchConfig(resource_selection="llm")
    result = lib.select_knowledge(store, view, [{"name": "research_metric_extract"}], config, llm)
    assert result["metric_ids"] == ["observed.summary"]
    assert result["metric_availability"]["observed.summary"]["available"]
    assert [c["id"] for c in result["metrics"]] == ["observed.summary"]
    sent = llm.calls[0][1]["catalogue"]
    assert all("steps" not in t for t in sent["templates"])
    assert all("source" not in c for c in sent["metrics"])
    assert lib.select_knowledge(store, view, [{"name": "research_metric_extract"}], config, llm) == result
    assert len(llm.calls) == 1 and llm.calls[0][0] == "A-resources"
    assert len(canonical(result)) < config.max_context_chars


def test_frozen_library_survives_installed_update(tmp_path, monkeypatch):
    first = Store(tmp_path / "first")
    frozen = lib.freeze_library(first)
    changed = deepcopy(frozen["library"])
    changed["templates"][0]["description"] += " revised"
    monkeypatch.setattr(lib, "packaged_library", lambda: changed)
    assert lib.freeze_library(first) == frozen
    assert lib.freeze_library(Store(tmp_path / "second"))["sha256"] != frozen["sha256"]
    first.put(lib.NAMESPACE, "knowledge-snapshot:" + lib.VERSION, {"library": changed, "sha256": frozen["sha256"]})
    with pytest.raises(ContractError, match="integrity"):
        lib.freeze_library(first)


def test_unknown_ids_bounded_repairs_and_restart(tmp_path):
    store, view = Store(tmp_path), make_view()
    invalid = choice("../../etc/passwd")
    llm = ChoosingLLM([invalid, invalid, invalid])
    config = ResearchConfig(resource_selection="llm", max_format_repairs=2)
    with pytest.raises(ContractError, match="exhausted"):
        lib.select_knowledge(store, view, [], config, llm)
    assert len(llm.calls) == 3
    with pytest.raises(ContractError, match="exhausted"):
        lib.select_knowledge(store, view, [], config, llm)
    assert len(llm.calls) == 3


def test_repair_then_bounded_extra_selection(tmp_path):
    llm = ChoosingLLM([choice("invented"), choice(extras=["protenix.iptm"])])
    selected = lib.select_knowledge(Store(tmp_path), make_view(), [], ResearchConfig(resource_selection="llm"), llm)
    assert selected["metric_ids"] == ["observed.summary", "protenix.iptm"]
    assert not selected["metric_availability"]["protenix.iptm"]["available"]
    assert len(llm.calls) == 2


def test_reference_templates_do_not_grant_tools():
    library, view = lib.packaged_library(), make_view("binder_design")
    catalog = lib.short_catalogue(library, view, [])
    entry = next(t for t in catalog["templates"] if t["id"] == "binder.claude_reference")
    assert entry["reference_only"] and "ipSAE_min" in entry["missing_requirements"]
    expanded = lib.expand_choice(library, catalog, choice("binder.claude_reference"), max_chars=16000)
    assert expanded["templates"][0]["availability"]["reference_only"]
    assert view.workflow.tool_names == ["research_metric_extract"]


def test_empty_choice_allows_exploration_and_budget_is_enforced():
    library, view = lib.packaged_library(), make_view()
    catalogue = lib.short_catalogue(library, view, [])
    assert lib.expand_choice(library, catalogue, choice(None), max_chars=16000)["metrics"] == []
    with pytest.raises(ContractError, match="budget"):
        lib.expand_choice(library, catalogue, choice(), max_chars=100)


def test_legacy_resource_selection_has_no_new_context_or_calls(tmp_path):
    view = make_view()
    view.workflow = Workflow()
    store = Store(tmp_path)
    llm = ChoosingLLM([])
    result = ResourceSelector(store, ResearchConfig(), llm).select(view, [])
    assert "method_knowledge" not in result
    assert not llm.calls and not store.all(lib.NAMESPACE)


def test_supplementary_budget_and_knowledge_identity(tmp_path):
    store, view = Store(tmp_path), make_view()
    selector = ResourceSelector(store, ResearchConfig())
    first = selector.select(view, [{"name": "research_metric_extract", "capability": "analysis"}])
    assert first["context_chars"] <= 16000
    assert first["method_knowledge"]["metric_ids"] == ["observed.summary"]
    assert selector.select(view, [{"name": "research_metric_extract", "capability": "analysis"}]) == first
    second = selector.select(view, [], query="exploratory")
    assert second["selection_id"] != first["selection_id"]


def test_selection_role_is_permitted_by_guarded_broker(tmp_path):
    from proteinrsi.replay.broker import dispatch
    llm = ChoosingLLM([choice()])
    assert dispatch(SimpleNamespace(llm=llm, store=Store(tmp_path)), None, make_view(), [],
        {"rpc": "llm", "role": "A-resources", "instructions": "test", "context": {}, "schema": {}}) == choice()


def test_tampered_cached_choice_is_rejected(tmp_path):
    store, view = Store(tmp_path), make_view()
    result = lib.select_knowledge(store, view, [], ResearchConfig())
    record = store.get(lib.NAMESPACE, result["selection_ref"])
    record["result"]["rationale"] = "tampered"
    store.put(lib.NAMESPACE, result["selection_ref"], record)
    assert digest(record["result"]) != record["sha256"]
    with pytest.raises(ContractError, match="integrity"):
        lib.select_knowledge(store, view, [], ResearchConfig())
