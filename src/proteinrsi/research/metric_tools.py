# SPDX-License-Identifier: MIT
"""Controller-bound metric extraction; workers supply resource IDs, never numerical tables."""
from __future__ import annotations

from proteinrsi.contracts import TaskKind, digest
from proteinrsi.dataflow.resources import NAMESPACE, Metric, MetricTable, scope_for
from proteinrsi.dataflow.schema import ContractError
from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.localtools.metric_extractors import PROVIDERS, VERSION, extract_provider
from proteinrsi.metrics import summarize_metrics
from proteinrsi.tools import ToolSpec

TOOL = "research_metric_extract"


def verified_source(store, view, gateway, ref):
    """A worker-authored resource alone is insufficient: match a real scoped tool receipt."""
    source = store.get(NAMESPACE, ref)
    if (not isinstance(source, dict) or "resource:" + digest(source) != ref
            or source.get("scope") != scope_for(view)):
        raise ContractError("Metric source is missing, modified or from another scope")
    allowed_specs = {s["name"]: s for s in gateway.catalog(view.task, view.workflow.tool_names)}
    for job_key, job in store.all("tool_jobs").items():
        spec, call = job.get("spec", {}), job.get("call", {})
        provider = spec.get("name")
        if (job.get("state") != "done" or provider not in set(PROVIDERS.values())
                or allowed_specs.get(provider) != spec or source.get("data") != job.get("result")):
            continue
        fingerprint = digest(spec)
        if (source.get("producer") != "tool:" + provider + "@" + fingerprint
                or source.get("schema_ref") != "tool.result/" + provider + "/" + fingerprint
                or source.get("schema_sha256") != digest(spec["output_schema"])):
            continue
        step_key = "protocol-step:" + digest({"scope": source["scope"],
            "operation": "tool:" + provider, "implementation": fingerprint,
            "arguments": call["arguments"], "parents": source["parents"],
            "output_schema": digest(spec["output_schema"])})
        expected_job = "tool-" + digest({"spec": spec, "call": call, "context": step_key})
        receipt = store.get(NAMESPACE, step_key, {})
        if (job.get("context") == step_key and job_key == expected_job
                and receipt.get("status") == "done" and receipt.get("resource_id") == ref):
            return provider, source["data"], call["arguments"], job_key, fingerprint
    raise ContractError("Metric source has no matching completed provider receipt in this scope")


def extract_metrics(store, view, gateway, arguments):
    metric_ids = arguments["metric_ids"]
    source_ref = arguments.get("source_ref")
    if metric_ids == ["observed.summary"]:
        if source_ref is not None:
            raise ContractError("Observed summaries use the bound TaskView only")
        if "top_ns" not in arguments:
            raise ContractError("Observed summaries require explicit top_ns")
        # Includes all revealed rows, with parent status explicit; no caller-supplied measurements.
        values = [o.model_dump(mode="json") for o in view.observations]
        if any((o["metric"], o["unit"]) != (view.task.metric, view.task.unit) for o in values):
            raise ContractError("Observation metric/unit differs from this task")
        summary = summarize_metrics(values, direction=view.task.direction, top_ns=arguments["top_ns"])
        result = {"metric_table": MetricTable(rows=[Metric(subject_ref="observations:" + view.evidence_version,
            name="observed." + name, value=value, unit=view.task.unit,
            method="revealed-summary-v1", evidence="computed") for name, value in summary["metrics"].items()]).model_dump(mode="json"),
            "summary": summary, "cohort": "all_revealed_observations_including_parent_and_controls",
            "provided_parent_present": view.task.initial_observation_policy == "provided_parent" and any(
                row["sequence"] == view.task.reference_sequence for row in values),
            "evidence_version": view.evidence_version, "extractor_version": VERSION,
            "source_resource_ref": None}
    else:
        if not source_ref or "observed.summary" in metric_ids or "top_ns" in arguments:
            raise ContractError("Provider extraction requires a source_ref and no observation parameters")
        provider, raw, params, job_ref, fingerprint = verified_source(store, view, gateway, source_ref)
        result = extract_provider(raw, provider, params, metric_ids, ArtifactStore(store),
                                  method=provider + "@" + fingerprint)
        result.update(source_resource_ref=source_ref, tool_job_ref=job_ref)
    result.update(status="ok", evidence_kind="derived_analysis", measurement_authority=False,
                  scope=scope_for(view))
    key = digest(result)
    store.put("research_analysis", key, result, immutable=True)
    return {**result, "artifact_ref": "research_analysis/" + key}


def register_metric_tool(gateway, view):
    if getattr(gateway, "remote_context_tools", False):
        return
    gateway._tools.pop(TOOL, None)
    if "protein-metrics" not in view.workflow.skill_names or TOOL not in view.workflow.tool_names:
        return
    spec = ToolSpec(name=TOOL, capability="analysis.metric_extract",
        description="Extract selected metrics from a completed native tool Protocol resource. "
            "Bind source_ref with delivery=ref to that tool step's result; do not submit numeric values. "
            "For observed.summary omit source_ref and specify top_ns. Output metric_table can be bound to C evidence or a named output view.",
        implementation_version=VERSION + ":" + scope_for(view), task_kinds=list(TaskKind),
        input_schema={"type": "object", "properties": {
            "source_ref": {"type": "string", "pattern": r"^resource:[0-9a-f]{64}$"},
            "metric_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                           "items": {"enum": [*PROVIDERS, "observed.summary"]}},
            "top_ns": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                       "items": {"type": "integer", "minimum": 1, "maximum": 10000}}},
            "required": ["metric_ids"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {
            "status": {"const": "ok"}, "metric_table": MetricTable.model_json_schema(),
            "measurement_authority": {"const": False}, "artifact_ref": {"type": "string"}},
            "required": ["status", "metric_table", "measurement_authority", "artifact_ref"]})
    # Inline nested $defs so JSON Schema references keep their original resolution root.
    table = spec.output_schema["properties"]["metric_table"]
    if "$defs" in table:
        spec.output_schema["$defs"] = table.pop("$defs")
    gateway.register(spec, lambda args: extract_metrics(gateway.store, view, gateway, args))
