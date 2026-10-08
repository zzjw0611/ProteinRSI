"""Transport fixtures only: no provider, labels, or scientific-result simulation."""
import json
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from proteinrsi.assistant_bridge import (
    AssistantBridgeLLM, AssistantBridgeResponseError, AssistantBridgeTimeout,
)
from proteinrsi.contracts import canonical
from proteinrsi.llm import JSONLLM, LLMError, ProviderPaused
from proteinrsi.storage import BudgetExceeded, SponsoredStore, Store


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
ARGS = ("A", "Return the transport test decision.", {"visible": "test fixture"}, SCHEMA)
RESULT = {"ok": True}


def client(tmp_path, *, store=None, model="interactive-test-label", timeout=0.015):
    if store is None:
        store = Store(tmp_path / "store")
        store.configure_budget({"llm_calls": 20, "lab_wells": 5})
    return AssistantBridgeLLM(store, model=model, mailbox=tmp_path / "mailbox",
                              timeout=timeout, poll_interval=0.001)


def pending(llm, args=ARGS):
    with pytest.raises(AssistantBridgeTimeout, match="resume the identical call"):
        llm.complete(*args)
    key = next(e["payload"]["key"] for e in reversed(llm.store.events())
               if e["kind"] == "llm_bridge_timeout")
    return llm.store.get("llm", key)["request"]


def envelope(request, result=RESULT):
    fields = ("protocol_version", "transport", "request_id", "request_hash", "model")
    return {**{k: request[k] for k in fields}, "result": result}


def respond(llm, request, value=None, *, raw=None):
    destination = llm.mailbox / "responses" / (request["request_id"] + ".json")
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(raw if raw is not None else canonical(
        envelope(request) if value is None else value), encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def test_env_opt_in_never_reads_auth_or_calls_http(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", str(tmp_path / "mailbox"))
    monkeypatch.setenv("PROTEINRSI_MODEL", "explicit-interactive-model-label")
    monkeypatch.setenv("PROTEINRSI_ASSISTANT_BRIDGE_TIMEOUT", "0.01")
    monkeypatch.setenv("PROTEINRSI_ASSISTANT_BRIDGE_POLL_INTERVAL", "0.001")
    monkeypatch.setenv("PROTEINRSI_CODEX_AUTH_FILE", str(tmp_path / "must-not-open"))
    monkeypatch.delenv("PROTEINRSI_API_KEY", raising=False)
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("HTTP is forbidden"))
    store = Store(tmp_path / "store")
    store.configure_budget({"llm_calls": 1})
    llm = JSONLLM.from_env(store)
    assert isinstance(llm, AssistantBridgeLLM)
    request = pending(llm)
    respond(llm, request)
    assert llm.complete(*ARGS) == RESULT
    assert not hasattr(llm, "api_key")


def test_env_without_opt_in_still_uses_native_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", raising=False)
    monkeypatch.setenv("PROTEINRSI_MODEL", "native-test")
    monkeypatch.setenv("PROTEINRSI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PROTEINRSI_API_KEY", "fixture-only")
    llm = JSONLLM.from_env(Store(tmp_path))
    assert type(llm) is JSONLLM
    assert llm.cache_settings == {}


def test_empty_env_opt_in_fails_instead_of_provider_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", "")
    monkeypatch.setenv("PROTEINRSI_MODEL", "test")
    with pytest.raises(ValueError, match="explicit absolute"):
        JSONLLM.from_env(Store(tmp_path))


def test_waiting_call_accepts_atomic_actual_response(tmp_path):
    llm = client(tmp_path, timeout=2)
    errors = []
    def responder():
        try:
            until = time.monotonic() + 2
            while time.monotonic() < until:
                files = list((llm.mailbox / "requests").glob("*.json"))
                if files:
                    request = json.loads(files[0].read_text())
                    assert request["instructions"] == ARGS[1]
                    assert request["context"] == ARGS[2]
                    assert request["schema"] == SCHEMA
                    respond(llm, request)
                    return
                time.sleep(0.001)
            raise AssertionError("Request was not published")
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=responder)
    worker.start()
    try:
        assert llm.complete(*ARGS) == RESULT
    finally:
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert not errors
    assert not list((llm.mailbox / "requests").glob("*.tmp"))


def test_timeout_restart_cache_and_audit_charge_once(tmp_path):
    llm = client(tmp_path)
    request = pending(llm)
    before = (llm.mailbox / "requests" / (request["request_id"] + ".json")).read_bytes()
    again = client(tmp_path)
    assert pending(again) == request
    assert (again.mailbox / "requests" / (request["request_id"] + ".json")).read_bytes() == before
    assert again.store.usage()["llm_calls"]["committed"] == 1
    assert again.store.usage()["lab_wells"]["committed"] == 0
    reply = respond(again, request)
    assert again.complete(*ARGS) == RESULT
    reply.unlink()
    cached = client(tmp_path)
    assert cached.complete(*ARGS) == RESULT
    assert cached.store.usage()["llm_calls"]["committed"] == 1
    key, audit = next(iter(cached.store.all("llm").items()))
    assert audit["state"] == "done"
    assert audit["transport"] == "assistant_bridge"
    assert audit["api_protocol"] == "assistant_bridge"
    assert audit["model_identity_verified"] is False
    assert audit["usage"] == {}
    assert audit["provider_reasoning_summary"] == []
    assert json.loads(audit["raw_output"]) == RESULT
    assert audit["response"] == envelope(request)
    assert cached.store.get("llm_attempts", key + "/attempt-1") == audit
    events = cached.store.events()
    assert sum(e["kind"] == "llm_started" for e in events) == 1
    assert sum(e["kind"] == "llm_completed" for e in events) == 1
    assert sum(e["kind"] == "llm_bridge_timeout" for e in events) == 2


def test_restart_after_process_interruption_before_publication(tmp_path, monkeypatch):
    llm = client(tmp_path)
    import proteinrsi.assistant_bridge as module
    real_publish = module._publish
    monkeypatch.setattr(module, "_publish", lambda *a: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        llm.complete(*ARGS)
    request = next(iter(llm.store.all("llm").values()))["request"]
    assert llm.store.usage()["llm_calls"]["committed"] == 1
    assert not list((llm.mailbox / "requests").glob("*.json"))
    monkeypatch.setattr(module, "_publish", real_publish)
    resumed = client(tmp_path)
    assert pending(resumed) == request
    respond(resumed, request)
    assert resumed.complete(*ARGS) == RESULT
    assert resumed.store.usage()["llm_calls"]["committed"] == 1


def test_durable_pending_and_budget_write_are_atomic(tmp_path, monkeypatch):
    llm = client(tmp_path)
    original = llm.store.put
    def interrupt(namespace, key, value, **kwargs):
        if namespace == "llm":
            raise RuntimeError("simulated persistence interruption")
        return original(namespace, key, value, **kwargs)
    monkeypatch.setattr(llm.store, "put", interrupt)
    with pytest.raises(RuntimeError, match="persistence interruption"):
        llm.complete(*ARGS)
    assert llm.store.usage()["llm_calls"]["committed"] == 0
    assert llm.store.all("llm") == {}
    resumed = client(tmp_path)
    request = pending(resumed)
    respond(resumed, request)
    assert resumed.complete(*ARGS) == RESULT
    assert resumed.store.usage()["llm_calls"]["committed"] == 1


@pytest.mark.parametrize("field,value", [
    ("request_id", "../other-request"), ("request_hash", "wrong"),
    ("model", "other-label"), ("transport", "responses"),
    ("protocol_version", 2), ("protocol_version", True),
    ("result", {"ok": "yes"}), ("result", {}), ("result", []),
    ("result", {"ok": True, "private_reasoning": "not requested"}),
    ("unexpected", "not requested"),
])
def test_mismatched_or_schema_invalid_response_rejected_and_correctable(tmp_path, field, value):
    llm = client(tmp_path)
    request = pending(llm)
    wrong = envelope(request)
    wrong[field] = value
    respond(llm, request, wrong)
    for _ in range(2):
        with pytest.raises(AssistantBridgeResponseError, match="no new call is charged"):
            llm.complete(*ARGS)
    record = next(iter(llm.store.all("llm").values()))
    assert record["state"] == "pending" and "result" not in record
    assert llm.store.usage()["llm_calls"]["committed"] == 1
    rejects = list(llm.store.all("llm_attempts").values())
    assert len(rejects) == 1
    assert rejects[0]["state"] == "rejected"
    assert "raw_output" not in rejects[0]  # Do not persist unsolicited private text.
    respond(llm, request)
    assert client(tmp_path).complete(*ARGS) == RESULT
    assert llm.store.usage()["llm_calls"]["committed"] == 1


@pytest.mark.parametrize("raw", ['{', '{}', '[]', '{"result": NaN}', '{"result":1,"result":2}'])
def test_malformed_response_rejected(tmp_path, raw):
    llm = client(tmp_path)
    request = pending(llm)
    respond(llm, request, raw=raw)
    with pytest.raises(AssistantBridgeResponseError):
        llm.complete(*ARGS)
    assert llm.store.usage()["llm_calls"]["committed"] == 1


def test_identity_changes_for_model_context_schema_and_campaign(tmp_path):
    llm = client(tmp_path)
    first = pending(llm)
    requests = [first]
    requests.append(pending(client(tmp_path, model="different-model")))
    requests.append(pending(llm, (ARGS[0], ARGS[1], {"visible": "other"}, SCHEMA)))
    requests.append(pending(llm, (*ARGS[:3], {})))
    other_store = Store(tmp_path / "other-store")
    other_store.configure_budget({"llm_calls": 1})
    other = client(tmp_path, store=other_store)
    requests.append(pending(other))
    assert len({r["request_id"] for r in requests}) == 5
    respond(other, requests[-1], envelope(first))
    with pytest.raises(AssistantBridgeResponseError):
        other.complete(*ARGS)


def test_zero_budget_publishes_no_request(tmp_path):
    store = Store(tmp_path / "store")
    store.configure_budget({"llm_calls": 0})
    llm = client(tmp_path, store=store)
    with pytest.raises(BudgetExceeded):
        llm.complete(*ARGS)
    assert store.all("llm") == {}
    assert not list(llm.mailbox.rglob("*.json"))


def test_sponsored_store_charges_only_once_on_resume(tmp_path):
    sponsor = Store(tmp_path / "sponsor")
    sponsor.configure_budget({"llm_calls": 2})
    branch = SponsoredStore(tmp_path / "branch", sponsor, "test-arm")
    branch.configure_budget({"llm_calls": 1})
    request = pending(client(tmp_path, store=branch))
    llm = client(tmp_path, store=SponsoredStore(tmp_path / "branch", sponsor, "test-arm"))
    respond(llm, request)
    assert llm.complete(*ARGS) == RESULT
    assert sponsor.usage()["llm_calls"]["committed"] == 1
    assert branch.usage()["llm_calls"]["committed"] == 1
    assert len(sponsor.all("validation_llm_attempts")) == 1


def test_local_schema_refs_work_but_external_refs_never_fetch(tmp_path, monkeypatch):
    llm = client(tmp_path)
    schema = {"$defs": {"decision": SCHEMA}, "$ref": "#/$defs/decision"}
    args = (*ARGS[:3], schema)
    request = pending(llm, args)
    respond(llm, request)
    assert llm.complete(*args) == RESULT
    for ref in ("https://example.invalid/schema", "file:///not-allowed"):
        with pytest.raises(ValueError, match="local references"):
            llm.complete(*ARGS[:3], {"$ref": ref})
    assert llm.store.usage()["llm_calls"]["committed"] == 1


@pytest.mark.parametrize("kwargs", [
    {"model": ""}, {"mailbox": "relative"}, {"mailbox": "/tmp/../elsewhere"},
    {"mailbox": "/"}, {"timeout": 0}, {"timeout": float("inf")},
    {"timeout": float("nan")}, {"timeout": 86401},
    {"poll_interval": 0}, {"poll_interval": float("inf")},
])
def test_invalid_configuration_rejected(tmp_path, kwargs):
    options = {"model": "test", "mailbox": tmp_path / "mailbox", **kwargs}
    with pytest.raises(ValueError):
        AssistantBridgeLLM(Store(tmp_path / "store"), **options)


def test_symlink_mailbox_ancestor_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        AssistantBridgeLLM(Store(tmp_path / "store"), model="test",
                           mailbox=tmp_path / "linked" / "mailbox")
    assert not (outside / "mailbox").exists()


@pytest.mark.parametrize("name", ["requests", "responses", ".locks"])
def test_symlink_mailbox_subdirectory_rejected(tmp_path, name):
    llm = client(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (llm.mailbox / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(LLMError, match="Unsafe or conflicting"):
        llm.complete(*ARGS)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_nonregular_or_linked_response_rejected_without_reading(tmp_path, kind):
    llm = client(tmp_path)
    request = pending(llm)
    source = tmp_path / "outside.json"
    source.write_text(canonical(envelope(request)))
    destination = llm.mailbox / "responses" / (request["request_id"] + ".json")
    if kind == "symlink":
        destination.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, destination)
    else:
        os.mkfifo(destination)
    with pytest.raises(LLMError, match="Unsafe or conflicting"):
        llm.complete(*ARGS)
    assert next(iter(llm.store.all("llm").values()))["state"] == "pending"
    assert source.read_text() == canonical(envelope(request))


def test_tampered_request_never_overwritten(tmp_path):
    llm = client(tmp_path)
    request = pending(llm)
    path = llm.mailbox / "requests" / (request["request_id"] + ".json")
    path.write_text('{"not":"the durable request"}')
    with pytest.raises(LLMError, match="Unsafe or conflicting"):
        llm.complete(*ARGS)
    assert path.read_text() == '{"not":"the durable request"}'


def test_guarded_rpc_remains_controller_owned_and_has_no_mailbox_identity(tmp_path, monkeypatch):
    from proteinrsi.replay.broker import dispatch
    from proteinrsi.replay.worker import RemoteLLM
    llm = client(tmp_path)
    request = pending(llm)
    respond(llm, request)
    identity = {"model": llm.model, "base_url": llm.base_url, "cache_settings": llm.cache_settings}
    assert str(tmp_path) not in canonical(identity)
    assert "mailbox" not in canonical(identity)
    team = SimpleNamespace(store=llm.store, llm=llm)
    def controller(operation, **arguments):
        return dispatch(team, None, None, [], {"rpc": operation, **arguments})
    monkeypatch.setattr("proteinrsi.replay.worker.rpc", controller)
    remote = RemoteLLM(None, identity)
    assert remote.complete(*ARGS) == RESULT
    assert llm.store.usage()["llm_calls"]["committed"] == 1


def test_concurrent_controllers_share_one_response_and_charge(tmp_path):
    first = client(tmp_path)
    request = pending(first)
    respond(first, request)
    errors, results = [], []
    def call():
        try:
            results.append(client(tmp_path, timeout=2).complete(*ARGS))
        except BaseException as exc:
            errors.append(exc)
    workers = [threading.Thread(target=call) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)
    assert not any(worker.is_alive() for worker in workers)
    assert not errors
    assert results == [RESULT] * 3
    assert first.store.usage()["llm_calls"]["committed"] == 1
    assert len(first.store.all("llm_attempts")) == 1


def test_bridge_pause_keeps_existing_worker_wire_protocol(tmp_path, monkeypatch):
    import io
    from proteinrsi.replay.worker import rpc
    llm = client(tmp_path)
    with pytest.raises(ProviderPaused) as captured:
        llm.complete(*ARGS)
    assert type(captured.value).__name__ == "ProviderPaused"
    monkeypatch.setattr("proteinrsi.replay.worker.sys.stdin",
                        io.StringIO('{"error":"ProviderPaused"}\n'))
    with pytest.raises(ProviderPaused):
        rpc("llm", role="A")


def test_protocol_resume_reuses_completed_upstream_and_pending_bridge(tmp_path):
    from proteinrsi.dataflow.protocol import Protocol, ProtocolExecutor, Operation, OperationRegistry
    from proteinrsi.dataflow.resources import ResourceStore
    from proteinrsi.dataflow.schema import SchemaRegistry
    llm = client(tmp_path)
    registry = SchemaRegistry()
    registry.register("fixture.result/v1", SCHEMA)
    resources = ResourceStore(llm.store, registry, "bridge-transport-test")
    operations = OperationRegistry(registry)
    calls = []
    def upstream(args, key):
        calls.append("upstream")
        return RESULT
    def downstream(args, key):
        calls.append("downstream")
        return llm.complete(*ARGS)
    for name, invoke in [("first", upstream), ("second", downstream)]:
        operations.register(Operation(name, {"type": "object"}, "fixture.result/v1", invoke, "test"))
    protocol = Protocol(hypothesis="Transport checkpoint test", steps=[
        {"step_id": "first", "operation": "first", "question": "upstream"},
        {"step_id": "second", "operation": "second", "question": "downstream"}],
        final_outputs={"result": {"source": "step:second.result", "schema_ref": "fixture.result/v1"}})
    executor = ProtocolExecutor(resources, operations)
    with pytest.raises(ProviderPaused):
        executor.execute(protocol, {})
    request = next(iter(llm.store.all("llm").values()))["request"]
    respond(llm, request, envelope(request, {"ok": "incorrect"}))
    with pytest.raises(ProviderPaused):
        executor.execute(protocol, {})
    respond(llm, request)
    executor.execute(protocol, {})
    assert calls == ["upstream", "downstream", "downstream", "downstream"]
    assert llm.store.usage()["llm_calls"]["committed"] == 1


def test_evidence_context_bridge_read_and_resume(tmp_path):
    llm = client(tmp_path)
    llm.store.put('configuration', 'research', {'context_policy': 'evidence-v1'})
    args = ('A-plan', 'Inspect evidence', {'rows': [{'id': i, 'sequence': 'A'*56} for i in range(100)]}, SCHEMA)
    request = pending(llm, args)
    ref = request['context']['rows']['evidence_ref']
    respond(llm, request, envelope(request, {'_context_action': {
        'kind': 'read', 'ref': ref, 'offset': 3, 'limit': 2}}))
    next_request = pending(llm, args)
    assert next_request['context']['_context']['last_read']['value'][0]['id'] == 3
    respond(llm, next_request)
    assert llm.complete(*args) == RESULT
    assert llm.store.usage()['llm_calls']['committed'] == 2
    assert llm.store.usage()['lab_wells']['committed'] == 0
