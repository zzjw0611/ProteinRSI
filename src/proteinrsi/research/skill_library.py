# SPDX-License-Identifier: MIT
"""Template-first, bounded knowledge loading. No tool execution or new permissions."""
from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json
import re

from jsonschema import Draft202012Validator

from proteinrsi.contracts import canonical, digest
from proteinrsi.dataflow.schema import ContractError

SKILLS = ("protein-metrics", "protein-design-workflows")
NAMESPACE = "research_step_outputs"
VERSION = "template-metric-skills-v1"
CARD_FIELDS = {"id", "name", "category", "description", "purpose", "provider_tool",
               "required_inputs", "source", "unit"}
# Quantity definitions which the current fixed extractor implements. PAE remains knowledge-only.
EXTRACTABLE = frozenset({"protenix.plddt_mean", "protenix.iptm", "protenix.has_clash",
    "structure.ca_rmsd", "esmc.masked_marginal_log_odds", "proteinmpnn.score",
    "rosetta.interface_dG", "observed.summary"})
CHOICE_SCHEMA = {"type": "object", "properties": {
    "template_ids": {"type": "array", "maxItems": 2, "uniqueItems": True,
                     "items": {"type": "string"}},
    "extra_metric_ids": {"type": "array", "maxItems": 4, "uniqueItems": True,
                         "items": {"type": "string"}},
    "rationale": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "required": ["template_ids", "extra_metric_ids", "rationale"], "additionalProperties": False}


def enabled(view) -> bool:
    return set(SKILLS) <= set(view.workflow.skill_names)


def _unique(items, label):
    if not isinstance(items, list) or len(items) > 128:
        raise ContractError(f"Invalid {label} catalogue")
    result = {}
    for item in items:
        key = item.get("id") if isinstance(item, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][A-Za-z0-9_.-]{0,99}", key) or key in result:
            raise ContractError(f"Duplicate or invalid {label} ID")
        result[key] = item
    return result


def validate_library(library):
    if len(canonical(library)) > 250000 or library.get("version") != VERSION:
        raise ContractError("Invalid knowledge snapshot version or size")
    cards = _unique(library["cards"], "metric")
    templates = _unique(library["templates"], "template")
    for card in cards.values():
        if set(card) != CARD_FIELDS:
            raise ContractError("Metric card fields differ from the compact contract")
        if any(not isinstance(card[k], str) or not card[k].strip() for k in CARD_FIELDS - {"required_inputs"}):
            raise ContractError("Metric metadata must be nonblank text")
        if not isinstance(card["required_inputs"], list) or any(not isinstance(x, str) for x in card["required_inputs"]):
            raise ContractError("Metric required_inputs must be text entries")
    for template in templates.values():
        for field in ("name", "category", "description", "version", "scope"):
            if not isinstance(template.get(field), str) or not template[field].strip():
                raise ContractError("Template lacks text metadata")
        for field in ("required_inputs", "required_tools", "steps", "adjustable", "external_requirements", "sources", "task_kinds"):
            if not isinstance(template.get(field), list) or any(not isinstance(x, str) for x in template[field]):
                raise ContractError("Invalid template list field")
        if not isinstance(template.get("metrics"), list):
            raise ContractError("Template metrics must be references")
        for metric in template["metrics"]:
            if not isinstance(metric, dict) or set(metric) != {"id", "role"} or metric["id"] not in cards or not metric["role"]:
                raise ContractError("Unresolved template metric reference")
    return cards, templates


def packaged_library() -> dict:
    root = files("proteinrsi").joinpath("skills")
    docs = {name: root.joinpath(name, "SKILL.md").read_text(encoding="utf-8") for name in SKILLS}
    cards = json.loads(root.joinpath(SKILLS[0], "cards.json").read_text(encoding="utf-8"))
    templates = json.loads(root.joinpath(SKILLS[1], "templates.json").read_text(encoding="utf-8"))
    library = {"version": VERSION, "cards": cards["cards"], "templates": templates["templates"],
               "document_versions": [cards["version"], templates["version"]], "instructions": docs}
    validate_library(library)
    return library


def freeze_library(store) -> dict:
    """One campaign-local first-use snapshot; installed updates do not rewrite it."""
    key = "knowledge-snapshot:" + VERSION
    saved = store.get(NAMESPACE, key)
    if saved is None:
        body = packaged_library()
        saved = {"library": body, "sha256": digest(body)}
        store.put(NAMESPACE, key, saved, immutable=True)
    if not isinstance(saved, dict) or digest(saved.get("library")) != saved.get("sha256"):
        raise ContractError("Knowledge snapshot integrity mismatch")
    validate_library(saved["library"])
    return deepcopy(saved)


def _input_present(view, requirement):
    if requirement in {"reference_sequence", "target_sequence"}:
        return bool(getattr(view.task, requirement))
    if requirement in {"pdb", "cif"}:
        return any(a.get("kind") == requirement for a in view.artifacts)
    if requirement == "observations":
        return bool(view.observations)
    return False


def metric_availability(card, tools):
    missing = []
    if card["provider_tool"] not in tools:
        missing.append("tool:" + card["provider_tool"])
    if card["id"] not in EXTRACTABLE:
        missing.append("extractor:" + card["id"])
    # This is permission/deployment metadata, not a claim that an individual result has the field.
    return {"available": not missing, "missing_requirements": missing,
            "result_fields_checked_at_execution": True}


def short_catalogue(library, view, tool_catalog):
    cards, templates = validate_library(library)
    tools = {t["name"] for t in tool_catalog}
    short_templates = []
    for item in templates.values():
        if view.task.kind.value not in item["task_kinds"]:
            continue
        missing = ["tool:" + name for name in item["required_tools"] if name not in tools]
        missing += ["input:" + name for name in item["required_inputs"] if not _input_present(view, name)]
        missing += item["external_requirements"]
        short_templates.append({k: deepcopy(item[k]) for k in ("id", "name", "category", "description")} | {
            "metric_ids": [m["id"] for m in item["metrics"]], "missing_requirements": missing,
            "reference_only": bool(missing)})
    short_metrics = [{k: card[k] for k in ("id", "name", "purpose", "provider_tool")} |
                     metric_availability(card, tools) for card in cards.values()]
    return {"templates": short_templates, "metrics": short_metrics}


def expand_choice(library, catalogue, choice, *, max_chars):
    errors = list(Draft202012Validator(CHOICE_SCHEMA).iter_errors(choice))
    if errors:
        raise ContractError("Invalid knowledge choice shape")
    cards, templates = validate_library(library)
    eligible = {t["id"]: t for t in catalogue["templates"]}
    if set(choice["template_ids"]) - eligible.keys() or set(choice["extra_metric_ids"]) - cards.keys():
        raise ContractError("Unknown or task-incompatible knowledge ID")
    selected_templates = [deepcopy(templates[key]) | {
        "availability": {k: eligible[key][k] for k in ("reference_only", "missing_requirements")}}
        for key in choice["template_ids"]]
    ids = list(dict.fromkeys([m["id"] for t in selected_templates for m in t["metrics"]] + choice["extra_metric_ids"]))
    availability = {c["id"]: {k: c[k] for k in ("available", "missing_requirements", "result_fields_checked_at_execution")}
                    for c in catalogue["metrics"]}
    result = {"templates": selected_templates, "metrics": [deepcopy(cards[key]) for key in ids],
              "metric_availability": {key: availability[key] for key in ids},
              "rationale": choice["rationale"], "template_ids": choice["template_ids"], "metric_ids": ids,
              "instructions": library["instructions"],
              "item_hashes": {key: digest((templates | cards)[key]) for key in [*choice["template_ids"], *ids]}}
    if len(canonical(result)) > max_chars:
        raise ContractError("Selected knowledge exceeds context budget; select fewer templates or metrics")
    return result


def select_knowledge(store, view, tool_catalog, config, llm=None, *, query=None):
    if not enabled(view):
        return None
    snapshot = freeze_library(store)
    catalogue = short_catalogue(snapshot["library"], view, tool_catalog)
    identity = {"snapshot": snapshot["sha256"], "task": view.task.model_dump(mode="json"),
        "workflow": view.workflow.version, "evidence": view.evidence_version,
        "round": view.round_index, "request_context": view.research_context,
        "query": query, "tools": tool_catalog, "config": config.model_dump(mode="json"),
        "llm": {"model": getattr(llm, "model", None), "base_url": getattr(llm, "base_url", None),
                "settings": getattr(llm, "cache_settings", {})}}
    key = "knowledge-selection:" + digest(identity)
    saved = store.get(NAMESPACE, key, {"attempts": 0, "errors": []})
    budget = config.max_context_chars - 1000  # Leave space for actual tool/data descriptors.
    if saved.get("result") is not None:
        body = saved["result"]
        if digest(body) != saved.get("sha256"):
            raise ContractError("Knowledge selection integrity mismatch")
        return deepcopy(body)
    while saved["attempts"] <= config.max_format_repairs:
        if config.resource_selection == "llm" and llm is not None:
            # Existing permitted A-resources role; same LLM client, budget and cache.
            choice = llm.complete("A-resources",
                "Select zero, one or two relevant workflow templates, then at most four extra metric IDs. "
                "Template-bound metrics load automatically. Read only this short catalogue. "
                "Unavailable entries are reference knowledge, not executable capabilities. "
                "No matching template is acceptable: use empty IDs and explain the exploratory route. "
                "Return the choice schema, never tool arguments or numeric metric results.",
                {"task": view.task.model_dump(mode="json"), "query": query,
                 "catalogue": catalogue, "format_attempt": saved["attempts"],
                 "validation_errors": saved["errors"], "snapshot_sha256": snapshot["sha256"]}, CHOICE_SCHEMA)
        else:
            candidates = [t for t in catalogue["templates"] if not t["reference_only"]]
            words = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", (query or view.task.objective_description or view.task.name).lower()))
            candidates.sort(key=lambda t: (-len(words & set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", (t["description"] + t["name"]).lower()))), t["id"]))
            choice = {"template_ids": [t["id"] for t in candidates[:1]], "extra_metric_ids": [],
                      "rationale": "Deterministic resource-selection mode; no unrequested model call."}
        saved["attempts"] += 1
        store.put(NAMESPACE, key, saved)
        try:
            expanded = expand_choice(snapshot["library"], catalogue, choice, max_chars=budget)
            result = {**expanded, "snapshot_sha256": snapshot["sha256"], "selection_ref": key,
                      "catalogue_ids": [t["id"] for t in catalogue["templates"]]}
            if len(canonical(result)) > config.max_context_chars - 500:
                raise ContractError("Knowledge receipt exceeds context budget")
            store.put(NAMESPACE, key, {**saved, "result": result, "sha256": digest(result)})
            return result
        except ContractError as exc:
            saved["errors"] = [str(exc)]
            store.put(NAMESPACE, key, saved)
            if llm is None or config.resource_selection != "llm":
                raise
    raise ContractError("Knowledge selection exhausted its bounded repair budget")
