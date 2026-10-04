# SPDX-License-Identifier: MIT
"""Permission-first resource catalog. Hidden labels and undeployed engines never enter it."""
from __future__ import annotations

import re
from importlib.resources import files
from proteinrsi.contracts import TaskView, canonical, digest
from .contracts import ResearchConfig
from .knowledge import KnowHow


def build_catalog(view: TaskView, tool_catalog: list[dict], store) -> dict:
    # tool_catalog must already be constrained by gateway, workflow and deployment configuration.
    tools = [{"id": "tool:" + t["name"], "name": t["name"],
              "description": (t.get("description") or t["capability"]) + " " + t.get("limitations", ""),
              "content": t, "version": digest(t)} for t in tool_catalog]
    data = [{"id": "data:revealed", "name": "Currently revealed observations",
             "description": f"{len(view.observations)} sample rows; evidence {view.evidence_version}; no unknown labels.",
             "content": {"evidence_version": view.evidence_version, "n": len(view.observations)},
             "version": view.evidence_version}]
    for artifact in view.artifacts:
        # Give identities, not raw arbitrary file contents or absolute paths.
        ref = artifact.get("artifact_ref") or artifact.get("ref")
        if not ref:
            continue
        content = {k: v for k, v in artifact.items() if k in ("artifact_ref", "ref", "kind", "sha256", "size", "metadata")}
        data.append({"id": "data:" + ref, "name": ref, "description": "Registered scientific artifact " + str(artifact.get("kind", "")),
                     "content": content, "version": digest(content)})
    skills = []
    for name in view.workflow.skill_names:
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("Invalid Skill name")
        path = files("proteinrsi").joinpath("skills", name, "SKILL.md")
        content = path.read_text(encoding="utf-8")
        skills.append({"id": "skill:" + name, "name": name, "description": "Workflow-authorized Skill " + name,
                       "content": content, "version": digest(content)})
    know_how = []
    valid = len({o.sequence for o in view.observations if o.qc == "valid"})
    for raw in store.get("configuration", "know_how", []):
        doc = KnowHow.model_validate(raw)
        if view.task.kind not in doc.task_kinds or valid < doc.min_observations:
            continue
        know_how.append({"id": "know_how:" + doc.resource_id, "name": doc.title,
                        "description": doc.description, "content": doc.model_dump(mode="json"),
                        "version": doc.version})
    experience = []
    for record in view.experience:
        scope = record.get("scope", {})
        if scope.get("task_kind") != view.task.kind.value or scope.get("evidence_source") != view.task.feedback_source:
            continue
        if record.get("status") not in ("local_support", "rejected", "inconclusive"):
            continue
        # Do not reinterpret locally supported records as validated transfer.
        key = digest(record)
        experience.append({"id": "experience:" + key, "name": "Scoped method record " + key[:12],
            "description": record.get("patch", {}).get("hypothesis", "") + "; status=" + record["status"],
            "content": record, "version": key})
    return {"tools": tools, "data_lake": data, "libraries": skills, "know_how": know_how, "experience": experience}


def _terms(text):
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()))


class ResourceSelector:
    def __init__(self, store, config: ResearchConfig, llm=None):
        self.store, self.config, self.llm = store, config, llm

    def select(self, view: TaskView, tool_catalog: list[dict], *, query: str | None = None) -> dict:
        resources = build_catalog(view, tool_catalog, self.store)
        query = query or f"{view.task.name}; {view.task.kind.value}; {view.task.metric}; round {view.round_index}"
        manifest = {k: [{"id": r["id"], "version": r["version"]} for r in v] for k, v in resources.items()}
        key = digest({"query": query, "resources": manifest, "evidence": view.evidence_version,
                      "config": self.config.model_dump(mode="json"), "llm_model": getattr(self.llm, "model", None),
                      "llm_url": getattr(self.llm, "base_url", None),
                      "llm_settings": getattr(self.llm, "cache_settings", {})})
        prior = self.store.get("resource_selections", key)
        if prior is not None:
            return prior
        if self.config.resource_selection == "llm":
            from .biomni_retriever import ToolRetriever
            selected = ToolRetriever().prompt_based_retrieval(query, resources, self.llm)
        elif self.config.resource_selection == "all":
            selected = resources
        else:
            words = _terms(query)
            selected = {k: sorted(items, key=lambda r: (
                -len(words & _terms(r["description"] + " " + r["name"])), r["id"])) for k, items in resources.items()}
        # Round-robin keeps the catalog from dropping all non-tool resources. Core
        # task constraints and observations remain in TaskView regardless of retrieval.
        flat = []
        for i in range(max((len(v) for v in selected.values()), default=0)):
            for category in selected:
                if i < len(selected[category]):
                    flat.append({"kind": category, **selected[category][i]})
        included, omitted, total = [], [], 0
        for item in flat:
            size = len(canonical(item))
            if len(included) >= self.config.max_resources or total + size > self.config.max_context_chars:
                omitted.append({"id": item["id"], "reason": "resource_context_budget"})
            else:
                total += size
                included.append(item)
        result = {"selection_id": key, "mode": self.config.resource_selection,
                  "resources": included, "omitted": omitted, "context_chars": total,
                  "evidence_version": view.evidence_version,
                  "permission_note": "Retrieval never expands workflow permissions. TaskView constraints are not truncated."}
        self.store.put("resource_selections", key, result, immutable=True)
        self.store.event("resources_selected", {"selection_id": key, "ids": [r["id"] for r in included],
                                               "omitted": omitted, "mode": result["mode"]})
        return result
