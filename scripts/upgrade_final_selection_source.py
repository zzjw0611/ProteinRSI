#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Operator-only 25312 -> 40e1d final-selection implementation repair.

Default is a read-only dry run. This is NOT a method adoption, E verdict, budget
reset, general migration facility, or downgrade command. Run from a quiescent,
reviewed 40e1d runtime with its original Python/dependencies and bwrap identity.
An authorization reference is an operator audit attribution, not a substitute for
external approval. Never apply to a real campaign without that approval.

Examples (use exactly the digest returned by the matching dry run):
  python scripts/upgrade_final_selection_source.py CAMPAIGN
  python scripts/upgrade_final_selection_source.py CAMPAIGN --apply \
    --expected-digest DIGEST --operator NAME --reason TEXT --authorization-ref REF
  python scripts/upgrade_final_selection_source.py CAMPAIGN --action rollback \
    --upgrade-receipt source-upgrade-HASH --target workflow

Rollback restores ONLY a baseline activated by this exact source upgrade, under
40e1d, after a later verified adoption. It preserves the original version-only
activation index. Unknown journal/evaluation schemas fail closed. Historical
records are never deleted. The source files and operator approval must remain
trusted and quiescent; hashes are integrity checks, not signatures against a
malicious database owner. No options relax hashes, budgets, gates, or backend.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from importlib import metadata, util
import json
from pathlib import Path
import platform
import sqlite3
import sys
import threading
from types import SimpleNamespace

# Imports must not write package bytecode during a dry run.
sys.dont_write_bytecode = True

OLD_COMMIT = "25312ffea4bde4241707a73216a8f13a0703682d"
NEW_COMMIT = "40e1d56c3b3ebfb0b7da1ad8212101723b31958f"
OLD_FILES = "f7a1b7e72029d044ae23ed088eab0f07ce6131e2e0b5ffcb1eb504193faae342"
NEW_FILES = "2e9de23e2e5074e87b86517f34090929eba5074b728a948e9c86753849e40207"
CHANGES = {
    "dataflow/design.py": (
        "648690b812c03cb57d9f87ad6afa9fb340555317ffecbf829ae0f50ca6c64990",
        "2852030444084f12dd9d5abd51eab1a000cb07557465aadf2a51abadd6c6404f"),
    "dataflow/integration.py": (
        "8eea49ce920e29a2c4fa6abc7bba5cd4c7d7ca5059bc6599ca2ea3f5dfc0b253",
        "5e157cf35740d84848147d1013e868591cc955f38b666a68dd80f8dcbe64bb4c"),
}
DEPENDENCIES = ("pydantic", "numpy", "httpx", "jsonschema", "gepa", "langgraph",
                "langgraph-checkpoint-sqlite", "mcp", "torch", "transformers",
                "huggingface-hub", "safetensors")
LIMITS = {"experimental_wells": 2000, "llm_calls": 600, "tool_calls": 400}
CONFIG_KEYS = ("prompt_bundle", "research", "know_how", "protein_model", "local_tools")
TARGETS = ("workflow", "meta")
TABLES = {"kv", "events", "limits", "charges", "sqlite_sequence"}
TERMINAL = {"accepted", "rejected", "inconclusive", "failed", "cancelled"}


class UpgradeRefused(ValueError):
    """A prerequisite is absent, unsupported, stale, or inconsistent."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def require(condition, message):
    if not condition:
        raise UpgradeRefused(message)


def _runtime_source():
    spec = util.find_spec("proteinrsi")
    require(spec is not None and spec.origin, "Cannot locate actual proteinrsi runtime")
    root = Path(spec.origin).resolve(strict=True).parent
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Symlink in runtime source tree")
        if path.is_file() and path.suffix in {".py", ".md", ".json"}:
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    require(digest(files) == NEW_FILES, "Actual runtime files are not exact approved 40e1d")
    for name, module in list(sys.modules.items()):
        if name == "proteinrsi" or name.startswith("proteinrsi."):
            location = getattr(module, "__file__", None)
            require(location is not None and Path(location).resolve().is_relative_to(root),
                    "A loaded proteinrsi module came from another runtime")
    versions = {}
    for name in DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"files": files, "python": platform.python_version(), "dependencies": versions}, str(root)


def _backend():
    from proteinrsi.replay.sandbox import backend_identity, selected_backend
    require(selected_backend() == "bwrap", "Explicit bwrap backend is required")
    return {"backend": "bwrap", "identity": backend_identity()}


def _assets(old, new):
    require(isinstance(old, dict) and set(old) == {"files", "python", "dependencies"},
            "Malformed original source asset")
    require(digest(old["files"]) == OLD_FILES, "Original source files are not approved 25312")
    require(digest(new["files"]) == NEW_FILES, "Target source files are not approved 40e1d")
    require(old["python"] == new["python"], "Python differs from original source pin")
    require(old["dependencies"] == new["dependencies"]
            and set(old["dependencies"]) == set(DEPENDENCIES),
            "Dependencies differ from original source pin")
    require(set(old["files"]) == set(new["files"]), "Source file set changed")
    changed = {name for name in old["files"] if old["files"][name] != new["files"][name]}
    require(changed == set(CHANGES), "Source difference is outside the two-file allowlist")
    for name, (before, after) in CHANGES.items():
        require(hashlib.sha256(old["files"][name].encode()).hexdigest() == before
                and hashlib.sha256(new["files"][name].encode()).hexdigest() == after,
                "Allowlisted source-text bytes changed: " + name)


class ReadStore:
    """In-memory read facade. Never invokes Store initialization or a write API."""
    def __init__(self, con):
        self.con = con
        self.records = {}
        for namespace, key, value in con.execute("SELECT namespace,key,value FROM kv"):
            self.records.setdefault(namespace, {})[key] = json.loads(value)
        self.events = [{"id": r[0], "timestamp": r[1], "kind": r[2], "payload": json.loads(r[3])}
                       for r in con.execute("SELECT * FROM events ORDER BY id")]

    def get(self, namespace, key, default=None):
        return deepcopy(self.records.get(namespace, {}).get(key, default))

    def all(self, namespace):
        return deepcopy(self.records.get(namespace, {}))


def _database_fingerprint(con):
    schema = [list(r) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    require({r[1] for r in schema if r[0] == "table"} == TABLES,
            "Unexpected database tables")
    require(not any(r[0] in {"trigger", "view"} for r in schema),
            "Database triggers/views are not supported")
    tables = {}
    for name in sorted(TABLES):
        tables[name] = sorted([list(r) for r in con.execute('SELECT * FROM "' + name + '"')],
                              key=canonical)
    return digest({"schema": schema, "tables": tables})


@contextmanager
def _read_database(directory):
    root = Path(directory).resolve(strict=True)
    path = root / "state.sqlite3"
    require(path.is_file() and not path.is_symlink(), "Existing campaign database required")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        con.close()


def _existing_store(directory):
    # Store.__init__ creates directories/schema and sets WAL. Apply needs none of
    # those operations; use only its existing lock/atomic transaction/write APIs.
    from proteinrsi.storage import Store
    store = Store.__new__(Store)
    store.root = Path(directory).resolve(strict=True)
    store.path = store.root / "state.sqlite3"
    require(store.path.is_file() and not store.path.is_symlink(), "Existing database required")
    store.event_sink = None
    store._transaction = threading.local()
    return store


def _snapshot(store, ref, target, source=None):
    from proteinrsi.contracts import GatePolicy, MetaPolicy, TaskSpec, Workflow
    cls = Workflow if target == "workflow" else MetaPolicy
    record = store.get("method_snapshots", ref)
    require(isinstance(record, dict) and "method-" + digest(record) == ref
            and record.get("target") == target, "Unknown or modified method snapshot")
    bundle = store.get("method_assets", record.get("source_ref"))
    require(isinstance(bundle, dict) and digest(bundle) == record["source_ref"],
            "Missing or modified source asset")
    if source is not None:
        require(bundle == source, "Snapshot uses a different executable/dependency pin")
    definition = cls.model_validate(record["definition"])
    require(record.get("version") == definition.version
            and record["definition"] == definition.model_dump(mode="json"),
            "Method version/definition mismatch")
    require(record.get("contracts") == {"method": cls.model_json_schema(),
            "task": TaskSpec.model_json_schema(), "gate": GatePolicy.model_json_schema()},
            "Contract schemas differ")
    require(record.get("configuration") == {k: store.get("configuration", k) for k in CONFIG_KEYS},
            "Method configuration differs from original snapshot")
    require(record.get("acceptance_policy") == store.get("configuration", "acceptance_policy"),
            "Acceptance policy differs from snapshot")
    return record, bundle


def _call_settings(store, key, call):
    protocol = call.get("api_protocol")
    request = call.get("request", {})
    if protocol == "assistant_bridge":
        from proteinrsi.assistant_bridge import PROTOCOL_VERSION, TRANSPORT
        payload = {k: v for k, v in request.items() if k not in {"request_id", "request_hash"}}
        scope = store.get("assistant_bridge", "scope")
        require(isinstance(scope, str) and request.get("request_hash") == digest(payload)
                and request.get("request_id") == "assistant-" + digest({"scope": scope,
                    "request_hash": request.get("request_hash")})
                and key == "llm-" + request["request_id"], "Bridge request identity differs")
        fields = ("protocol_version", "transport", "request_id", "request_hash", "model")
        require(request.get("protocol_version") == PROTOCOL_VERSION
                and request.get("transport") == TRANSPORT and call.get("transport") == TRANSPORT
                and request.get("role") == call.get("role") and request.get("model") == call.get("model")
                and call.get("response") == {**{k: request[k] for k in fields}, "result": call.get("result")}
                and call.get("raw_output") == canonical(call.get("result"))
                and store.get("llm_attempts", key + "/attempt-1") == call,
                "Completed bridge response receipt differs")
        return {"llm_transport": TRANSPORT, "llm_bridge_protocol": PROTOCOL_VERSION}
    require(protocol in {"chat_completions", "responses"}, "Unsupported completed LLM transport")
    cache_url = call.get("base_url")
    require(isinstance(cache_url, str), "Native LLM URL is missing")
    if protocol == "responses":
        cache_url += "/responses"
    require(key == "llm-" + digest({"role": call.get("role"), "url": cache_url,
                                   "request": request})
            and call.get("request_key") == key
            and request.get("model") == call.get("model"),
            "Native LLM request identity differs")
    attempt = call.get("attempt")
    require(type(attempt) is int and attempt > 0
            and store.get("llm_attempts", key + "/attempt-" + str(attempt)) == call,
            "Completed native LLM attempt receipt differs")
    raw = call.get("raw_output")
    require(isinstance(raw, str) and json.loads(raw) == call.get("result"),
            "Completed native LLM output differs")
    effort = (request.get("reasoning_effort") if protocol == "chat_completions"
              else request.get("reasoning", {}).get("effort"))
    return {} if protocol == "chat_completions" and effort is None else {
        "llm_api_protocol": protocol, "llm_reasoning_effort": effort}


def _feedback_context(store, call, frozen_view):
    """Bind an actual C turn, including packed evidence, to its frozen input."""
    request = call["request"]
    if call.get("api_protocol") == "assistant_bridge":
        context = request.get("context")
    else:
        messages = request.get("messages" if call["api_protocol"] == "chat_completions" else "input")
        require(isinstance(messages, list) and len(messages) == 2
                and messages[1].get("role") == "user", "C native message envelope differs")
        context = json.loads(messages[1]["content"])
    expected = {"view": frozen_view}
    require(isinstance(context, dict), "C request context is missing")
    if "_context" not in context:
        require(context == expected, "C request differs from frozen input")
        return
    wrapper = context["_context"]
    require(isinstance(wrapper, dict), "C evidence context is malformed")
    session = wrapper.get("session")
    root = wrapper.get("source", {}).get("evidence_ref")
    evidence = store.get("context_evidence", root)
    record = store.get("context_sessions", session, {})
    frame = store.get("context_frames", str(session) + "/" + str(wrapper.get("actions_used")), {})
    require(evidence == {"session": session, "path": "$", "value": expected}
            and root == "ev-" + digest(evidence)
            and record.get("source_ref") == root and record.get("role") == "C-feedback"
            and record.get("state") == "done"
            and frame.get("session") == session and frame.get("role") == "C-feedback"
            and frame.get("index") == wrapper.get("actions_used") and frame.get("context") == context,
            "C evidence context differs from frozen input")


def _feedback_task(state):
    """Reproduce only the pinned TaskView's deterministic proposal-pool projection."""
    from proteinrsi.contracts import TaskKind, TaskSpec
    task = TaskSpec.model_validate(state["task"])
    if (len(task.candidates) > task.proposal_pool_size and task.kind != TaskKind.RANKING
            and task.batch_fill_policy != "full_plate"):
        import numpy as np
        observed = {o["sequence"] for o in state["observations"]}
        eligible = [s for s in task.candidates if task.repeat_policy == "allow"
                    or (s not in observed and s != task.reference_sequence)]
        rng = np.random.default_rng(task.seed + state["round_index"])
        if len(eligible) > task.proposal_pool_size:
            chosen = rng.choice(len(eligible), size=task.proposal_pool_size, replace=False)
            eligible = [eligible[int(i)] for i in chosen]
        task = task.model_copy(update={"candidates": eligible})
    return task.model_dump(mode="json")


def _context_action(reply):
    """Recognize only the read/note envelope admitted by evidence-v1."""
    if not isinstance(reply, dict) or set(reply) != {"_context_action"}:
        return False
    action = reply["_context_action"]
    if not isinstance(action, dict):
        return False
    if action.get("kind") == "read":
        return (set(action) <= {"kind", "ref", "offset", "limit"}
                and isinstance(action.get("ref"), str) and bool(action["ref"])
                and type(action.get("offset", 0)) is int and action.get("offset", 0) >= 0
                and type(action.get("limit", 1)) is int and 1 <= action.get("limit", 1) <= 50)
    fields = {"kind", "hypothesis", "evidence_refs", "counterevidence", "failures",
              "open_questions", "next_step"}
    refs = action.get("evidence_refs")
    return (action.get("kind") == "note" and set(action) == fields
            and isinstance(refs, list) and 1 <= len(refs) <= 20
            and all(isinstance(ref, str) and bool(ref) for ref in refs)
            and all(isinstance(action[k], str) for k in fields - {"evidence_refs"}))


def _completed_feedback(store, state):
    from proteinrsi.agents import FeedbackAnalysis
    from proteinrsi.contracts import Observation, TaskView
    observations = [Observation.model_validate(o).model_dump(mode="json")
                    for o in state["observations"]]
    identity = {"campaign_id": state["campaign_id"], "round": state["round_index"],
                "evidence_version": digest(observations)}
    latest = state["history"][-1]
    require(latest.get("round") == state["round_index"] - 1,
            "History does not end at the completed current round")
    from proteinrsi.contracts import Batch
    batch = Batch.model_validate(store.get("batches", latest.get("batch_id")))
    measurements = store.get("measurements", batch.batch_id)
    require(batch.campaign_id == state["campaign_id"] and batch.round_index == latest["round"]
            and isinstance(measurements, list), "History batch/measurement identity differs")
    observed = {o["sample_id"]: o for o in observations}
    require(len(measurements) == len(batch.samples)
            and {o["sample_id"] for o in measurements} == {x.sample_id for x in batch.samples}
            and all(observed.get(o["sample_id"]) == o for o in measurements),
            "Latest measurement receipts differ from current evidence")
    samples = {x.sample_id: x for x in batch.samples}
    require(all(o["batch_id"] == batch.batch_id
                and o["sequence"] == samples[o["sample_id"]].candidate.sequence for o in measurements),
            "Latest measured sequence identity differs from submitted batch")
    key = "feedback-" + digest(identity)
    saved, result = store.get("feedback_inputs", key), store.get("feedback_results", key)
    require(isinstance(saved, dict) and isinstance(result, dict),
            "Current-evidence C feedback is incomplete; finish it under the original pin")
    require(saved.get("identity") == identity and result.get("identity") == identity
            and result.get("input_digest") == digest(saved), "C checkpoint identity/input mismatch")
    require(state["history"][-1].get("analyst_feedback") == result.get("feedback"),
            "C feedback differs from committed history")
    feedback = FeedbackAnalysis.model_validate(result["feedback"])
    require(bool(feedback.summary.strip()), "C feedback has an empty summary")
    view = TaskView.model_validate(saved["view"])
    require(view.evidence_version == identity["evidence_version"]
            and view.round_index == state["round_index"], "C input view has different evidence")
    history = deepcopy(state["history"])
    history[-1].pop("analyst_feedback", None)
    require(len(history) == state["round_index"] and view.history == history
            and view.task.model_dump(mode="json") == _feedback_task(state),
            "C input task/history differs from the committed boundary")
    provenance = saved.get("provenance", {})
    require(provenance.get("backend") == "llm", "A completed actual C LLM receipt is required")
    require(provenance.get("workflow") == view.workflow.version
            and provenance.get("meta") == view.meta.version, "C input method identity differs")
    refs = provenance.get("method_snapshots", {})
    require(set(refs) == set(TARGETS), "C source snapshot identities are missing")
    for target in TARGETS:
        record, _ = _snapshot(store, refs[target], target)
        require(record["definition"] == getattr(view, target).model_dump(mode="json"),
                "C source snapshots differ from the saved view")
    completed = [e for e in store.events if e["kind"] == "analyst_feedback_completed"
                 and all(e["payload"].get(k) == v for k, v in identity.items())
                 and e["payload"].get("feedback_input_ref") == key
                 and e["payload"].get("feedback_ref") == key]
    require(bool(completed), "No C completion audit event")
    matches = []
    for call_key, call in store.all("llm").items():
        if (call.get("role") == "C-feedback" and call.get("state") == "done"
                and call.get("round") == state["round_index"]
                and call.get("model") == provenance.get("model")
                and call.get("base_url") == provenance.get("url")):
            require(_call_settings(store, call_key, call) == provenance.get("client"),
                    "C LLM client identity differs from frozen input")
            _feedback_context(store, call, saved["view"])
            reply = call.get("result")
            if _context_action(reply):
                reads = [r for r in store.all("context_reads").values()
                         if r.get("role") == "C-feedback" and r.get("request") == reply]
                require(any(store.get("context_sessions", r.get("session"), {}).get("state") == "done"
                            for r in reads), "C context action has no completed session receipt")
                continue
            if FeedbackAnalysis.model_validate(reply).model_dump(mode="json") == result["feedback"]:
                if any(e["kind"] == "llm_completed" and e["payload"].get("key") == call_key
                       and e["id"] < completed[-1]["id"] for e in store.events):
                    matches.append(call_key)
    require(bool(matches), "C success has no matching actual completed LLM receipt")
    return key, identity


def _historical_meta_request(store, state, key, record):
    from proteinrsi.agents import MetaResponse
    from proteinrsi.contracts import MetaPolicy, TaskView, Workflow
    from proteinrsi.prompting import compose
    require(set(record) == {"view", "workflow_schema", "meta_schema", "compute_usage"},
            "Unsupported saved M request schema")
    view = TaskView.model_validate(record["view"])
    require(0 < view.round_index < state["round_index"], "M request is not historical completed work")
    require(record["workflow_schema"] == Workflow.model_json_schema()
            and record["meta_schema"] == MetaPolicy.model_json_schema(), "Historical M schemas differ")
    task = view.task.model_dump(mode="json")
    require({k: v for k, v in task.items() if k != "candidates"}
            == {k: v for k, v in state["task"].items() if k != "candidates"}
            and set(task["candidates"]) <= set(state["task"]["candidates"]), "Historical M task differs")
    require({digest(o.model_dump(mode="json")) for o in view.observations}
            <= {digest(o) for o in state["observations"]}, "Historical M evidence is outside lineage")
    require(view.history == state["history"][:view.round_index], "Historical M history differs")
    require(set(record["compute_usage"]) == set(LIMITS)
            and all(record["compute_usage"][r].get("limit") == cap for r, cap in LIMITS.items()),
            "Historical M budget limits differ")
    context = {k: record[k] for k in ("view", "workflow_schema", "meta_schema")}
    instructions = compose(store, "meta", view.meta.prompt)
    matched = False
    for call_key, call in store.all("llm").items():
        if call.get("role") != "M" or call.get("round") != view.round_index or call.get("state") != "done":
            continue
        if _context_action(call.get("result")):
            continue
        response = MetaResponse.model_validate(call.get("result")).model_dump(mode="json")
        settings = _call_settings(store, call_key, call)
        expected = "meta-request:" + digest({"context": context, "instructions": instructions,
            "model": call.get("model"), "url": call.get("base_url"), "client": settings})
        if expected != key:
            continue
        completions = [e for e in store.events if e["kind"] == "llm_completed"
                       and e["payload"].get("key") == call_key]
        decisions = [e for e in store.events if e["kind"] == "meta_decision"
                     and e["payload"].get("round") == view.round_index
                     and e["payload"].get("meta") == view.meta.version
                     and e["payload"].get("response") == response]
        if any(call_event["id"] < decision["id"] for call_event in completions for decision in decisions):
            matched = True
    require(matched, "Historical M request has no matching completed M receipt/decision")


def _journals(store, state):
    # Historical successful journals are retained. Unscoped opaque drafts cannot
    # be proven historical and fail closed unless their completion is verifiable.
    for namespace in ("llm", "tool_jobs", "context_sessions"):
        for key, record in store.all(namespace).items():
            require(record.get("state") == "done", "Unfinished/uncertain " + namespace + "/" + key)
    for key, record in store.all("llm_attempts").items():
        latest = store.get("llm", record.get("request_key"), {})
        require(record.get("state") == "done" or (
            record.get("state") == "failed" and latest.get("state") == "done"
            and latest.get("attempt", 0) > record.get("attempt", 0)),
            "Unresolved LLM attempt " + key)
    for key, record in store.all("llm").items():
        require(record.get("round") != state["round_index"] or
                record.get("role") in {"C-feedback", "M"},
                "Current-round planning/design LLM work " + key)
    planning_events = {"plate_completion_requested", "research_plan_created", "research_blocked",
                       "team_completed", "candidate_validation_feedback"}
    for event in store.events:
        payload = event["payload"]
        require(not (event["kind"] in planning_events
                     and payload.get("round") == state["round_index"]),
                "Current-round planning/draft journal exists")
    committed = [e for e in store.events if e["kind"] == "feedback_ingested"
                 and e["payload"].get("batch_id") == state["history"][-1].get("batch_id")]
    require(bool(committed), "Latest completed round lacks feedback ingestion audit")
    require(not any(e["id"] > committed[-1]["id"] and e["kind"] in {
        "tool_requested", "tool_started", "research_step_started", "plate_completion_requested"}
        for e in store.events), "New tool/protocol/plate work exists after completed-round boundary")
    for key, record in store.all("plate_plans").items():
        require(record.get("state") == "complete" and not record.get("pending_request"),
                "Unfinished plate plan " + key)
    runs = store.all("research_runs")
    for key, record in runs.items():
        require(record.get("status") == "complete"
                and type(record.get("round")) is int and record["round"] < state["round_index"],
                "Current-round or unfinished research run " + key)
    outputs = store.all("research_step_outputs")
    completed_protocols = set()
    for key, record in outputs.items():
        if key.startswith("protocol-run:") and ":review:" not in key and ":repair:" not in key:
            require(record.get("status") == "complete", "Unfinished protocol run " + key)
            require(any(r.get("protocol_result", {}).get("run_ref") == key for r in runs.values()),
                    "Protocol run has no completed historical research run")
            for field in ("protocol", "active_protocol"):
                if record.get(field):
                    completed_protocols.add(digest(record[field]))
    old_output_ids = {entry.get("output_id") for r in runs.values() for entry in r.get("completed", [])
                      if isinstance(entry, dict) and entry.get("status") == "completed"}
    for key, record in outputs.items():
        ok = False
        if key.startswith("resource:"):
            required = {"scope", "schema_ref", "schema_sha256", "data", "producer",
                        "parents", "measurement_authority"}
            ok = (required <= set(record) and set(record) <= required | {"prediction_refs"}
                  and key == "resource:" + digest(record)
                  and isinstance(record["scope"], str) and len(record["scope"]) == 64
                  and isinstance(record["schema_ref"], str) and bool(record["schema_ref"])
                  and isinstance(record["schema_sha256"], str) and len(record["schema_sha256"]) == 64
                  and record["measurement_authority"] is False
                  and isinstance(record["producer"], str) and bool(record["producer"])
                  and isinstance(record["parents"], list)
                  and all(parent in outputs and outputs[parent].get("scope") == record["scope"]
                          for parent in record["parents"]))
        elif key.startswith("knowledge-snapshot:"):
            ok = digest(record.get("library")) == record.get("sha256")
        elif key.startswith("knowledge-selection:"):
            ok = record.get("result") is not None and digest(record["result"]) == record.get("sha256")
        elif key.startswith("meta-request:"):
            _historical_meta_request(store, state, key, record)
            ok = True
        elif key.startswith("design-handoff:"):
            ok = record.get("result") is not None
        elif key.startswith("protocol-plan:"):
            ok = bool(record.get("protocol")) and digest(record["protocol"]) in completed_protocols
        elif key.startswith("protocol-run:"):
            if ":review:" in key or ":repair:" in key:
                parent = key.split(":review:")[0].split(":repair:")[0]
                ok = "decision" in record and outputs.get(parent, {}).get("status") == "complete"
            else:
                ok = record.get("status") == "complete"
        elif key.startswith("protocol-step:"):
            if key.endswith(":format"):
                ok = outputs.get(key[:-7], {}).get("status") == "done"
            else:
                ok = record.get("status") == "done" and record.get("resource_id") in outputs
        elif key in old_output_ids:
            ok = True
        require(ok, "Unsupported or unfinished research journal " + key)
    for namespace in store.records:
        if "draft" in namespace:
            require(not store.all(namespace), "Draft namespace is not supported: " + namespace)
    for key, record in store.all("method_candidate_states").items():
        require(record.get("status") in TERMINAL, "Unfinished method candidate " + key)


def _formal_namespace(name):
    return ("trial" in name or "evaluation" in name or "metric" in name
            or name.startswith(("meta_attempt", "meta_online_attempt", "meta_arm_", "offline_", "validation_")))


def _historical_evaluations(store, source):
    """Narrow completed historical workflows; no execution or recomputation."""
    supported = {"trials", "trial_results", "evaluation_trial_inputs", "evaluation_requests",
        "evaluation_bindings", "evaluation_plans", "evaluation_evidence", "evaluation_verdicts",
        "evaluation_metric_inputs", "evaluation_metric_results", "evaluation_metric_validations",
        "evaluation_metric_attempts", "workflow_validation_outcomes",
        "meta_online_attempts", "meta_evaluations"}
    for namespace in store.records:
        if _formal_namespace(namespace):
            require(namespace in supported, "Unsupported historical evaluation namespace: " + namespace)
    trials, results = store.all("trials"), store.all("trial_results")
    require(set(trials) == set(results), "Unfinished or orphan historical trial")
    for key, trial in trials.items():
        result = results[key]
        require(result.get("decision") in {"accepted", "rejected", "inconclusive"},
                "Historical trial has no terminal result")
        patch = trial.get("patch", {})
        from proteinrsi.contracts import Patch
        patch_id = Patch.model_validate(patch).patch_id
        candidate = store.get("method_candidates", patch_id, {})
        candidate_state = store.get("method_candidate_states", patch_id, {})
        require(candidate_state.get("status") == result["decision"],
                "Trial outcome differs from candidate status")
        transition_ref = candidate_state.get("transition_ref")
        transition = store.get("method_transitions", transition_ref, {})
        require(digest(transition) == transition_ref and transition.get("patch_id") == patch_id
                and transition.get("to") == result["decision"]
                and transition.get("detail", {}).get("evaluation_ref") == "trial_results/" + key
                and transition.get("detail", {}).get("result") == result,
                "Trial result differs from its terminal governance transition")
        _snapshot(store, candidate.get("base_snapshot_ref"), patch["target"], source)
        snap, _ = _snapshot(store, candidate.get("candidate_snapshot_ref"), patch["target"], source)
        challenger_key = "challenger" if patch["target"] == "workflow" else "challenger_meta"
        require(candidate.get("patch") == patch
                and snap["definition"] == trial.get(challenger_key), "Trial source/definition mismatch")
        from proteinrsi.contracts import Batch, Observation
        batch = Batch.model_validate(store.get("batches", key))
        measurements = [Observation.model_validate(o).model_dump(mode="json")
                        for o in store.get("measurements", key, [])]
        require(batch.batch_id == key and batch.patch_id == patch_id
                and len(measurements) == len(batch.samples), "Trial batch/measurement receipts differ")
        samples = {item.sample_id: item for item in batch.samples}
        require(set(samples) == {o["sample_id"] for o in measurements}
                and all(o["batch_id"] == key
                        and o["sequence"] == samples[o["sample_id"]].candidate.sequence for o in measurements),
                "Historical trial measurement identities differ")
        if patch["target"] == "meta":
            evaluation_id = trial.get("evaluation_id")
            attempt = store.get("meta_online_attempts", evaluation_id, {})
            report = store.get("meta_evaluations", evaluation_id, {})
            require(attempt.get("state") == "completed" and attempt.get("trial") == trial
                    and report.get("batch_id") == key and report.get("result") == result
                    and all(report.get(k) == v for k, v in trial.items()),
                    "Historical online meta receipts differ")
    meta_ids = {t.get("evaluation_id") for t in trials.values() if t.get("target") == "meta"}
    require(set(store.all("meta_online_attempts")) == meta_ids
            and set(store.all("meta_evaluations")) == meta_ids,
            "Unfinished or unsupported offline meta evaluation")
    from proteinrsi.llm_evaluation import load_evaluation_plan
    from proteinrsi.evaluation_metrics import load_evaluation_metric_result, verify_plan_program, runtime_identity
    bindings = store.all("evaluation_bindings")
    plans = store.all("evaluation_plans")
    for key, binding in bindings.items():
        require(binding.get("evaluation_id") == key and binding.get("verdict_ref"),
                "Unfinished evaluation binding")
        plan = load_evaluation_plan(store, binding.get("plan_ref"))
        require(plan["evaluation_id"] == key, "Evaluation plan identity mismatch")
        ref = binding["verdict_ref"]
        require(ref.startswith("evaluation_verdicts/"), "Invalid verdict reference")
        verdict = store.get("evaluation_verdicts", ref.split("/", 1)[1], {})
        body = {k: v for k, v in verdict.items() if k not in {"verdict_ref", "result"}}
        require(ref == "evaluation_verdicts/" + digest(body)
                and verdict.get("evaluation_id") == key
                and verdict.get("plan_ref") == binding["plan_ref"], "Modified evaluation verdict")
        from proteinrsi.contracts import EvaluationVerdict, GateResult
        gate_result = GateResult.model_validate(verdict.get("result"))
        choice = EvaluationVerdict.model_validate(verdict.get("verdict"))
        evidence_ref = binding.get("evidence_ref", "")
        evidence = store.get("evaluation_evidence", evidence_ref.split("/", 1)[-1], {})
        evidence_body = {k: v for k, v in evidence.items() if k != "evidence_ref"}
        require(evidence_ref == "evaluation_evidence/" + digest(evidence_body)
                and verdict.get("evidence_ref") == evidence_ref
                and evidence.get("evaluation_id") == key and evidence.get("plan_ref") == plan["plan_ref"],
                "Historical evaluation evidence differs")
        require(gate_result.decision == choice.decision and gate_result.reason == choice.reason
                and choice.plan_ref == plan["plan_ref"]
                and gate_result.n_baseline == evidence.get("n_baseline")
                and gate_result.n_challenger == evidence.get("n_challenger")
                and gate_result.details.get("evidence") == evidence.get("evidence")
                and gate_result.details.get("verdict") == choice.model_dump(mode="json")
                and set(choice.supporting_evidence_refs) <= set(evidence.get("evidence", {})),
                "Historical verdict/result/evidence identity differs")
        matching = [r for r in results.values() if r == verdict["result"]]
        require(bool(matching), "Historical verdict has no completed trial result")
        if plan["plan"].get("metric_program"):
            verify_plan_program(store, plan, executing=True)
    for namespace, ref_field in (("evaluation_verdicts", "verdict_ref"),
                                 ("evaluation_evidence", "evidence_ref")):
        for record in store.all(namespace).values():
            require(any(b.get(ref_field) == record.get(ref_field) for b in bindings.values()),
                    "Orphan evaluation receipt")
    for key, request in store.all("evaluation_requests").items():
        if key.endswith("/response"):
            require(store.get("evaluation_requests", key[:-9]) is not None, "Orphan evaluation response")
        else:
            require(digest(request) == key and request.get("context", {}).get("evaluation_id") in bindings
                    and store.get("evaluation_requests", key + "/response") is not None,
                    "Unfinished/orphan/modified evaluation request")
    for key, inputs in store.all("evaluation_trial_inputs").items():
        require(key in bindings and inputs.get("target") == bindings[key].get("target"),
                "Orphan evaluation trial input")
    for plan in plans.values():
        require(bindings.get(plan.get("evaluation_id"), {}).get("plan_ref") == plan.get("plan_ref"),
                "Orphan evaluation plan")
    for key, attempt in store.all("evaluation_metric_attempts").items():
        require(attempt.get("state") == "completed", "Unfinished/failed metric work " + key)
        if attempt.get("result_ref"):
            require(key == digest({"evaluation_id": attempt.get("evaluation_id"),
                                   "plan_ref": attempt.get("plan_ref")}), "Metric attempt identity differs")
            result = load_evaluation_metric_result(store, attempt["result_ref"])
            require(result.get("evaluation_id") in bindings
                    and result.get("runtime") == runtime_identity(), "Metric result source identity mismatch")
        elif attempt.get("validation_ref"):
            ref = attempt["validation_ref"]
            require(key == "validation-" + ref.split("/", 1)[-1]
                    and attempt.get("fixture_only") is True, "Metric validation attempt identity differs")
            record = store.get("evaluation_metric_validations", ref.split("/", 1)[-1], {})
            body = {k: v for k, v in record.items() if k not in {"validation_ref", "validation_sha256"}}
            require(record.get("validation_ref") == ref
                    and record.get("validation_sha256") == digest(body)
                    and record.get("runtime") == runtime_identity(), "Metric validation identity mismatch")
            require(any(p.get("metric_validation_ref") == ref for p in plans.values()),
                    "Metric validation has no completed evaluation plan")
        else:
            raise UpgradeRefused("Completed metric attempt has no completion receipt")
    for record in store.all("evaluation_metric_inputs").values():
        require(any(a.get("input_ref") == record.get("ref")
                    for a in store.all("evaluation_metric_attempts").values()), "Orphan metric input")
    for namespace, field in (("evaluation_metric_results", "result_ref"),
                             ("evaluation_metric_validations", "validation_ref")):
        for record in store.all(namespace).values():
            require(any(a.get(field) == record.get(field) for a in store.all("evaluation_metric_attempts").values()),
                    "Orphan metric receipt")


def _boundary(store, con, backend, *, rollback=False, source=None):
    state = store.get("campaign", "state")
    require(isinstance(state, dict), "No campaign state")
    require(state.get("execution_semantics") == "on-demand-v1"
            and state.get("status") == "ready"
            and all(state.get(k) is None for k in ("pending_batch", "pending_patch", "pending_meta")),
            "An idle ready boundary with no pending batch/patch/meta is required")
    require(type(state.get("round_index")) is int and state["round_index"] > 0 and state.get("history"),
            "A completed experimental round is required")
    require(store.get("configuration", "method_governance") is not None
            and set(state.get("method_governance", {}).get("active", {})) == set(TARGETS),
            "Existing W/M governance pins required")
    require(state.get("gate") == store.get("configuration", "acceptance_policy"),
            "Frozen acceptance policy must match the campaign gate")
    limits = dict(con.execute("SELECT resource,amount FROM limits"))
    require(limits == LIMITS and state["task"].get("budget") == LIMITS,
            "Budget caps must remain exactly 2000/600/400")
    charges = list(con.execute("SELECT key,resource,amount,fingerprint,state FROM charges"))
    for key, resource, amount, fingerprint, status in charges:
        require(resource in LIMITS and type(amount) is int and amount >= 0
                and isinstance(fingerprint, str) and len(fingerprint) == 64,
                "Malformed resource charge " + key)
        require(status in {"committed", "released"}, "Unaccounted reservation " + key)
    for resource, limit in LIMITS.items():
        require(sum(r[2] for r in charges if r[1] == resource and r[4] == "committed") <= limit,
                "Budget overspent")
    require(store.get("configuration", "replay_security") == backend,
            "Immutable replay_security differs from actual bwrap identity")
    _journals(store, state)
    if rollback:
        _historical_evaluations(store, source)
    else:
        require(not any(_formal_namespace(ns) for ns in store.records),
                "Source upgrade refuses all trial/evaluation/metric namespaces")
    feedback_key, identity = _completed_feedback(store, state)
    return state, feedback_key, identity


def _verified_receipt(store, ref, source, state):
    receipt = store.get("source_upgrades", ref)
    require(isinstance(receipt, dict) and ref == "source-upgrade-" + digest(receipt),
            "Unknown or modified upgrade receipt")
    require(receipt.get("schema_version") == 1 and receipt.get("action") == "source_upgrade"
            and receipt.get("campaign_id") == state["campaign_id"]
            and receipt.get("from_files_digest") == OLD_FILES
            and receipt.get("to_files_digest") == NEW_FILES
            and receipt.get("to_commit") == NEW_COMMIT
            and receipt.get("new_source_ref") == digest(source)
            and receipt.get("scientific_improvement_claimed") is False
            and all(isinstance(receipt.get(k), str) and receipt[k].strip()
                    for k in ("operator", "reason", "authorization_ref")), "Invalid upgrade receipt scope")
    require(state["method_governance"].get("source_transition_ref") == ref,
            "Upgrade receipt is not the campaign's activated transition")
    old = store.get("method_assets", receipt.get("old_source_ref"))
    require(isinstance(old, dict) and digest(old) == receipt["old_source_ref"], "Old source asset changed")
    _assets(old, source)
    require(set(receipt.get("old_snapshots", {})) == set(TARGETS)
            and set(receipt.get("new_snapshots", {})) == set(TARGETS), "Upgrade snapshot map differs")
    for target in TARGETS:
        old_snap, _ = _snapshot(store, receipt["old_snapshots"][target], target, old)
        new_snap, _ = _snapshot(store, receipt["new_snapshots"][target], target, source)
        require(new_snap == {**old_snap, "source_ref": digest(source)},
                "Upgrade changed a scientific definition, contract, or configuration")
        activation = store.get("source_qualified_activations", target + "/" + ref)
        require(activation == {"target": target, "version": new_snap["version"],
                "snapshot_ref": receipt["new_snapshots"][target], "source_ref": digest(source),
                "upgrade_receipt": ref, "origin": "operator_source_upgrade"},
                "Missing/modified source-qualified activation")
        require(store.get("method_activations", target + "/" + old_snap["version"], {}).get("snapshot_ref")
                == receipt["old_snapshots"][target], "Original activation index changed")
    require(any(e["kind"] == "source_upgrade_applied" and e["payload"].get("receipt_ref") == ref
                and e["payload"].get("scientific_improvement_claimed") is False for e in store.events),
            "Upgrade activation audit is missing")
    return receipt


def _plan(con, source, runtime_root, backend, *, action, upgrade_receipt=None, target=None):
    fingerprint = _database_fingerprint(con)
    store = ReadStore(con)
    state, feedback_key, identity = _boundary(store, con, backend,
                                              rollback=action == "rollback", source=source)
    active = state["method_governance"]["active"]
    if action == "upgrade":
        require(upgrade_receipt is None and target is None, "Upgrade has no generic target/pin options")
        require(not state["method_governance"].get("source_transition_ref"), "Source upgrade already applied")
        old_refs, new_refs, snapshots = {}, {}, {}
        old_source = None
        for kind in TARGETS:
            record, old = _snapshot(store, active[kind], kind)
            _assets(old, source)
            require(old_source is None or old_source == old, "W/M source pins differ")
            old_source = old
            require(record["definition"] == state[kind], "Active method definition differs")
            require(store.get("method_activations", kind + "/" + record["version"], {}).get("snapshot_ref")
                    == active[kind], "Original activation does not identify current snapshot")
            old_refs[kind] = active[kind]
            snapshots[kind] = {**record, "source_ref": digest(source)}
            new_refs[kind] = "method-" + digest(snapshots[kind])
        feedback = store.get("feedback_inputs", feedback_key)
        require(feedback["provenance"]["method_snapshots"] == old_refs,
                "Current C was not completed under the original active pins")
        details = {"old_source_ref": digest(old_source), "new_source_ref": digest(source),
                   "old_snapshots": old_refs, "new_snapshots": new_refs}
    else:
        require(action == "rollback" and target in TARGETS and upgrade_receipt,
                "Rollback requires the exact upgrade receipt and workflow/meta target")
        receipt = _verified_receipt(store, upgrade_receipt, source, state)
        for kind in TARGETS:
            snap, _ = _snapshot(store, active[kind], kind, source)
            require(snap["definition"] == state[kind], "Active snapshot/definition differs")
        chosen = receipt["new_snapshots"][target]
        require(active[target] != chosen, "Requested source-qualified baseline is already active")
        base, _ = _snapshot(store, chosen, target, source)
        current, _ = _snapshot(store, active[target], target, source)
        require(base["version"] != current["version"], "Rollback requires a genuine method version change")
        switches = store.all("method_switches")
        adoption = [event for key, event in switches.items() if digest(event) == key
                    and event.get("action") == "adopt" and event.get("target") == target
                    and event.get("snapshot_ref") == active[target]
                    and event.get("to_version") == current["version"]]
        require(bool(adoption), "Current version has no validated prior adoption provenance")
        evidence_ref = adoption[-1].get("evaluation_ref", "")
        require(evidence_ref.startswith("trial_results/"),
                "Only completed trial-backed later adoptions are supported for rollback")
        trial_key = evidence_ref.split("/", 1)[1]
        trial = store.get("trials", trial_key, {})
        result = store.get("trial_results", trial_key, {})
        require(result.get("decision") == "accepted" and trial.get("target") == target
                and trial.get("challenger" if target == "workflow" else "challenger_meta") == current["definition"],
                "Current adoption is not bound to its accepted trial")
        details = {"upgrade_receipt": upgrade_receipt, "target": target,
                   "from_snapshot": active[target], "to_snapshot": chosen,
                   "from_version": current["version"], "to_version": base["version"],
                   "adoption_evaluation_ref": evidence_ref, "new_source_ref": digest(source)}
    body = {"schema_version": 1, "action": action, "campaign_id": state["campaign_id"],
        "campaign_root": str(Path(con.execute("PRAGMA database_list").fetchone()[2]).parent.resolve()),
        "round": state["round_index"], "evidence_version": identity["evidence_version"],
        "feedback_ref": feedback_key, "database_fingerprint": fingerprint,
        "runtime_root": runtime_root, "runtime_source_ref": digest(source),
        "runtime_files_digest": NEW_FILES, "replay_security": backend,
        "budget_limits": LIMITS, "scientific_improvement_claimed": False, **details}
    return {**body, "dry_run_digest": digest(body)}


def dry_run(directory, *, action="upgrade", upgrade_receipt=None, target=None):
    source, root = _runtime_source()
    backend = _backend()
    with _read_database(directory) as con:
        return _plan(con, source, root, backend, action=action,
                     upgrade_receipt=upgrade_receipt, target=target)


def apply(directory, *, expected_digest, operator, reason, authorization_ref,
          action="upgrade", upgrade_receipt=None, target=None):
    require(isinstance(expected_digest, str) and len(expected_digest) == 64,
            "Apply requires the exact expected dry-run digest")
    for name, value in (("operator", operator), ("reason", reason), ("authorization_ref", authorization_ref)):
        require(isinstance(value, str) and bool(value.strip()), "Apply requires " + name)
    source, root = _runtime_source()
    backend = _backend()
    # Preflight read first: invalid apply cannot create a database or lock file.
    with _read_database(directory) as con:
        _database_fingerprint(con)
    store = _existing_store(directory)
    with store.lock(), store.transaction(), store.connect() as con:
        read = ReadStore(con)
        # Idempotent retries are bound to the same plan AND operator authorization.
        namespace = "source_upgrades" if action == "upgrade" else "source_rollbacks"
        prior = [(ref, r) for ref, r in read.all(namespace).items()
                 if r.get("expected_dry_run_digest") == expected_digest]
        if prior:
            require(len(prior) == 1, "Ambiguous existing operation receipt")
            ref, record = prior[0]
            require(all(record.get(k) == v for k, v in {"operator": operator, "reason": reason,
                    "authorization_ref": authorization_ref}.items()), "Retry authorization differs")
            state = read.get("campaign", "state")
            if action == "upgrade":
                _verified_receipt(read, ref, source, state)
                require(state["method_governance"]["active"] == record["new_snapshots"],
                        "Campaign advanced after the source upgrade")
            else:
                require(ref == "source-rollback-" + digest(record)
                        and record.get("upgrade_receipt") == upgrade_receipt
                        and record.get("target") == target, "Modified rollback receipt")
                _verified_receipt(read, upgrade_receipt, source, state)
                require(state["method_governance"]["active"][target] == record["to_snapshot"],
                        "Campaign advanced after rollback")
            require(read.get("configuration", "replay_security") == backend, "Backend changed after apply")
            return {"status": "already_applied", "receipt_ref": ref}
        plan = _plan(con, source, root, backend, action=action,
                     upgrade_receipt=upgrade_receipt, target=target)
        require(plan["dry_run_digest"] == expected_digest, "Stale dry-run digest; inspect a fresh dry run")
        state = store.get("campaign", "state")
        provenance = {"operator": operator, "reason": reason, "authorization_ref": authorization_ref,
                      "expected_dry_run_digest": expected_digest,
                      "database_fingerprint": plan["database_fingerprint"],
                      "campaign_id": state["campaign_id"], "round": state["round_index"],
                      "evidence_version": plan["evidence_version"],
                      "scientific_improvement_claimed": False, "schema_version": 1}
        if action == "upgrade":
            record = {**provenance, "action": "source_upgrade", "from_commit": OLD_COMMIT,
                "to_commit": NEW_COMMIT, "from_files_digest": OLD_FILES, "to_files_digest": NEW_FILES,
                **{k: plan[k] for k in ("old_source_ref", "new_source_ref", "old_snapshots", "new_snapshots")}}
            ref = "source-upgrade-" + digest(record)
            store.put("method_assets", digest(source), source, immutable=True)
            for kind in TARGETS:
                snapshot = store.get("method_snapshots", plan["old_snapshots"][kind])
                snapshot["source_ref"] = digest(source)
                store.put("method_snapshots", plan["new_snapshots"][kind], snapshot, immutable=True)
                store.put("source_qualified_activations", kind + "/" + ref,
                    {"target": kind, "version": snapshot["version"],
                     "snapshot_ref": plan["new_snapshots"][kind], "source_ref": digest(source),
                     "upgrade_receipt": ref, "origin": "operator_source_upgrade"}, immutable=True)
            store.put("source_upgrades", ref, record, immutable=True)
            state["method_governance"]["active"] = plan["new_snapshots"]
            state["method_governance"]["source_transition_ref"] = ref
            event_kind = "source_upgrade_applied"
        else:
            record = {**provenance, "action": "source_qualified_rollback",
                      **{k: plan[k] for k in ("upgrade_receipt", "target", "from_snapshot", "to_snapshot",
                                             "from_version", "to_version", "new_source_ref", "adoption_evaluation_ref")}}
            ref = "source-rollback-" + digest(record)
            from proteinrsi.governance import MethodGovernance, _packaged_source
            require(_packaged_source() == source, "Cached runtime source differs from actual files")
            campaign = SimpleNamespace(store=store,
                view=lambda current: SimpleNamespace(evidence_version=digest(current["observations"])))
            MethodGovernance(campaign)._switch(state, target, plan["to_snapshot"], operator=operator,
                reason=reason, action="source_qualified_rollback", evaluation_ref=None)
            state["last_patch_round"] = state["round_index"]
            state["considered_round"] = state["round_index"]
            store.put("source_rollbacks", ref, record, immutable=True)
            event_kind = "source_qualified_rollback_applied"
        store.put("campaign", "state", state)
        store.event(event_kind, {"receipt_ref": ref, "campaign_id": state["campaign_id"],
                                "scientific_improvement_claimed": False})
        require(_runtime_source() == (source, root) and _backend() == backend,
                "Runtime changed during transaction; rolled back")
        return {"status": "applied", "receipt_ref": ref}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("campaign")
    parser.add_argument("--action", choices=("upgrade", "rollback"), default="upgrade")
    parser.add_argument("--upgrade-receipt")
    parser.add_argument("--target", choices=TARGETS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="Read-only validation (the default)")
    parser.add_argument("--expected-digest")
    parser.add_argument("--operator")
    parser.add_argument("--reason")
    parser.add_argument("--authorization-ref")
    args = parser.parse_args(argv)
    try:
        kwargs = {"action": args.action, "upgrade_receipt": args.upgrade_receipt, "target": args.target}
        if args.apply:
            result = apply(args.campaign, expected_digest=args.expected_digest, operator=args.operator,
                           reason=args.reason, authorization_ref=args.authorization_ref, **kwargs)
        else:
            require(not any((args.expected_digest, args.operator, args.reason, args.authorization_ref)),
                    "Apply-only authorization arguments supplied without --apply")
            result = dry_run(args.campaign, **kwargs)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, RuntimeError) as exc:
        print("Source upgrade refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
