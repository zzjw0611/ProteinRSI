"""Synthetic input-budget tests; no source landscape, credentials, or live provider."""
import copy
import gzip
import hashlib
import io
import json

import httpx
import pytest

from proteinrsi.agents import Team
from proteinrsi.contracts import canonical, digest
from proteinrsi.llm import DEFAULT_MAX_REQUEST_BYTES, JSONLLM, LLMError, ProviderPaused
from proteinrsi.recovery import authorize_retry
from proteinrsi.storage import Conflict, SponsoredStore, Store
from proteinrsi.trajectory import export_html, read_trace, write_json


ARGS = ("A-plan", "Synthetic instructions 雪", {"view": {"round_index": 20}, "text": "🧬\\\""},
        {"type": "object", "description": "Synthetic schema", "properties": {"ok": {"type": "boolean"}}})
BASE_URL = "https://example.invalid/v1"


def success(protocol="chat_completions"):
    if protocol == "responses":
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message",
            "role": "assistant", "content": [{"type": "output_text", "text": '{"ok": true}'}]}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})


def make_client(tmp_path, *, store=None, handler=None, **kwargs):
    if store is None:
        store = Store(tmp_path / "preflight")
        store.configure_budget({"llm_calls": 10, "experimental_wells": 10})
    return JSONLLM(store, model="fixture", base_url=BASE_URL, api_key="TEST_SECRET",
                   transport=httpx.MockTransport(handler or (lambda _: success())), **kwargs)


def expected_payload(protocol, args=ARGS):
    _, instructions, context, schema = args
    messages = [{"role": "system", "content": instructions +
        "\nReturn exactly one JSON object matching this schema:\n" + canonical(schema)},
        {"role": "user", "content": canonical(context)}]
    if protocol == "responses":
        messages[0]["role"] = "developer"
        return {"model": "fixture", "input": messages, "max_output_tokens": 4096,
                "text": {"format": {"type": "json_object"}}, "store": False,
                "reasoning": {"effort": "medium"}}
    return {"model": "fixture", "messages": messages, "max_tokens": 4096,
            "response_format": {"type": "json_object"}, "reasoning_effort": "medium"}


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("margin", [-1, 0, 1])
def test_exact_wire_body_inclusive_budget_and_envelope(tmp_path, protocol, margin):
    expected = expected_payload(protocol)
    wire = httpx.Request("POST", BASE_URL, json=expected).content
    calls = []
    llm = make_client(tmp_path, api_protocol=protocol, reasoning_effort="medium",
        max_request_bytes=len(wire) + margin,
        handler=lambda request: calls.append(request) or success(protocol))
    assert len(wire) > len(canonical(ARGS[2]).encode())
    if margin < 0:
        with pytest.raises(ProviderPaused, match="serialized UTF-8 JSON bytes"):
            llm.complete(*ARGS)
        assert calls == []
        audit = next(iter(llm.store.all("llm_preflights").values()))
        assert llm.store.all("llm") == llm.store.all("llm_attempts") == {}
        assert llm.store.usage()["llm_calls"]["committed"] == 0
    else:
        assert llm.complete(*ARGS) == {"ok": True}
        assert calls[0].content == wire
        assert calls[0].headers["content-type"] == "application/json"
        assert int(calls[0].headers["content-length"]) == len(wire)
        audit = next(iter(llm.store.all("llm_attempts").values()))
        assert llm.store.usage()["llm_calls"]["committed"] == 1
    assert audit["request"] == expected
    preflight = audit["input_preflight"]
    assert preflight["request_bytes"] == len(wire)
    assert preflight["request_body_sha256"] == hashlib.sha256(wire).hexdigest()
    assert preflight["input_tokens"] is None
    assert preflight["context_window_verified"] is False


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_schema_overhead_alone_can_reject_small_context(tmp_path, protocol):
    args = ("B", "test", {}, {"description": "schema" * 10000})
    llm = make_client(tmp_path, api_protocol=protocol, max_request_bytes=1024,
                      handler=lambda _: pytest.fail("must not send"))
    with pytest.raises(ProviderPaused):
        llm.complete(*args)
    rejected = next(iter(llm.store.all("llm_preflights").values()))
    messages = rejected["request"].get("messages", rejected["request"].get("input"))
    assert messages[1]["content"] == "{}"
    assert canonical(args[3]) in messages[0]["content"]


@pytest.mark.parametrize("role", ["A-plan", "B", "C-feedback", "M"])
def test_twenty_round_history_rejects_without_truncation_network_or_spend(tmp_path, monkeypatch, role):
    context = {"view": {"round_index": 20, "history": [
        {"round": i, "revealed_fixture_evidence": "ACDE" * 52500} for i in range(20)]}}
    before = copy.deepcopy(context)
    llm = make_client(tmp_path)
    # Guard against even constructing a networking client, not merely dispatch.
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("must not create HTTP client"))
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: pytest.fail("must not retry/sleep"))
    for _ in range(2):
        with pytest.raises(ProviderPaused, match="No network call or LLM budget charge"):
            llm.complete(role, "Synthetic long history", context, {})
    assert context == before
    assert llm.max_request_bytes == DEFAULT_MAX_REQUEST_BYTES == 262144
    records = llm.store.all("llm_preflights")
    assert len(records) == 1  # Repeated explicit resumes don't rewrite or duplicate full evidence.
    ref, audit = next(iter(records.items()))
    assert audit["input_preflight"]["request_bytes"] > 4_000_000
    assert json.loads(audit["request"]["messages"][1]["content"]) == before
    assert audit["network_attempted"] is False and audit["llm_calls_charged"] == 0
    assert llm.store.all("llm") == llm.store.all("llm_attempts") == {}
    assert all(u["committed"] == u["reserved"] == 0 for u in llm.store.usage().values())
    with llm.store.connect() as con:
        assert con.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
    with pytest.raises(Conflict, match="immutable"):
        llm.store.put("llm_preflights", ref, {**audit, "request": {}}, immutable=True)
    with pytest.raises(Conflict, match="known retryable"):
        authorize_retry(llm.store, audit["request_key"], operator="fixture", reason="Not a paid call")


def test_corrected_local_cap_preserves_key_payload_audit_and_cached_result(tmp_path):
    llm = make_client(tmp_path, max_request_bytes=1)
    with pytest.raises(ProviderPaused):
        llm.complete(*ARGS)
    ref, rejected = next(iter(llm.store.all("llm_preflights").items()))
    calls = []
    resumed = make_client(tmp_path, max_request_bytes=rejected["input_preflight"]["request_bytes"],
        handler=lambda request: calls.append(request) or success())
    assert resumed.cache_settings == llm.cache_settings == {}
    assert resumed.complete(*ARGS) == {"ok": True}
    assert json.loads(calls[0].content) == rejected["request"]
    assert list(resumed.store.all("llm")) == [rejected["request_key"]]
    assert resumed.store.get("llm_preflights", ref) == rejected
    assert resumed.store.get("llm", rejected["request_key"])["attempt"] == 1
    # An even stricter policy never spends or rejects a completed identical cache hit.
    assert llm.complete(*ARGS) == {"ok": True}
    assert len(calls) == 1
    assert resumed.store.usage()["llm_calls"]["committed"] == 1


def test_rejected_retry_keeps_paid_failure_and_unused_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    calls = []
    llm = make_client(tmp_path, max_attempts=1,
        handler=lambda request: calls.append(request) or httpx.Response(503))
    with pytest.raises(ProviderPaused, match="retry limit"):
        llm.complete(*ARGS)
    key, failure = next(iter(llm.store.all("llm").items()))
    permit = authorize_retry(llm.store, key, operator="fixture", reason="Synthetic provider restored")
    blocked = make_client(tmp_path, max_attempts=1, max_request_bytes=1,
                          handler=lambda _: pytest.fail("must not send"))
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: pytest.fail("must reject before backoff"))
    for _ in range(2):
        with pytest.raises(ProviderPaused, match="preflight rejected"):
            blocked.complete(*ARGS)
    assert blocked.store.get("llm", key) == failure
    assert blocked.store.get("llm_attempts", key + "/attempt-1") == failure
    assert blocked.store.get("llm_retry_authorizations", key) == permit
    assert blocked.store.usage()["llm_calls"]["committed"] == 1
    assert not any(e["kind"] == "llm_retry_scheduled" for e in blocked.store.events())
    monkeypatch.setattr("proteinrsi.llm.time.sleep", lambda _: None)
    resumed = make_client(tmp_path, max_attempts=1,
                          handler=lambda request: calls.append(request) or success())
    assert resumed.complete(*ARGS) == {"ok": True}
    assert calls[0].content == calls[1].content
    assert resumed.store.get("llm_attempts", key + "/attempt-1") == failure
    assert resumed.store.get("llm", key)["attempt"] == 2
    assert resumed.store.usage()["llm_calls"]["committed"] == 2


def test_below_byte_cap_does_not_claim_or_bypass_provider_token_acceptance(tmp_path):
    calls = []
    llm = make_client(tmp_path, handler=lambda request: calls.append(request) or
        httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}))
    with pytest.raises(LLMError, match="Provider call failed"):
        llm.complete(*ARGS)
    key, failed = next(iter(llm.store.all("llm").items()))
    assert failed["input_preflight"]["context_window_verified"] is False
    assert failed["input_preflight"]["input_tokens"] is None
    assert "context_length_exceeded" in failed["error_response"]
    assert llm.store.all("llm_preflights") == {}
    assert llm.store.usage()["llm_calls"]["committed"] == 1
    with pytest.raises(Conflict, match="known retryable"):
        authorize_retry(llm.store, key, operator="fixture", reason="Cannot bypass model input limits")
    resumed = make_client(tmp_path, max_request_bytes=2 * DEFAULT_MAX_REQUEST_BYTES,
                          handler=lambda _: pytest.fail("must not retry provider rejection"))
    with pytest.raises(LLMError, match="Prior call failed"):
        resumed.complete(*ARGS)
    assert len(calls) == 1
    assert resumed.store.get("llm", key) == failed


@pytest.mark.parametrize("state", ["done", "failed", "started"])
def test_legacy_records_and_uncertain_failures_keep_existing_semantics(tmp_path, state):
    llm = make_client(tmp_path, max_request_bytes=1)
    payload = expected_payload("chat_completions")
    payload.pop("reasoning_effort")
    key = "llm-" + digest({"role": ARGS[0], "url": BASE_URL, "request": payload})
    old = {"state": state, "request": payload, "result": {"ok": True}, "http_status": 401}
    llm.store.put("llm", key, old)
    if state == "done":
        assert llm.complete(*ARGS) == {"ok": True}
    else:
        with pytest.raises(LLMError, match="uncertain completion"):
            llm.complete(*ARGS)
    assert llm.store.get("llm", key) == old
    assert llm.store.all("llm_preflights") == {}
    assert llm.store.usage()["llm_calls"]["committed"] == 0


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "100"])
def test_unbounded_and_invalid_constructor_limits_fail_closed(tmp_path, value):
    with pytest.raises(ValueError, match="max_request_bytes"):
        make_client(tmp_path, max_request_bytes=value)


def test_env_limit_default_override_and_invalid_values(tmp_path, monkeypatch):
    for key, value in {"PROTEINRSI_MODEL": "fixture", "PROTEINRSI_BASE_URL": BASE_URL,
                       "PROTEINRSI_API_KEY": "TEST_SECRET"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PROTEINRSI_ASSISTANT_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("PROTEINRSI_LLM_MAX_REQUEST_BYTES", raising=False)
    store = Store(tmp_path / "env")
    assert JSONLLM.from_env(store).max_request_bytes == DEFAULT_MAX_REQUEST_BYTES
    monkeypatch.setenv("PROTEINRSI_LLM_MAX_REQUEST_BYTES", "12345")
    assert JSONLLM.from_env(store).max_request_bytes == 12345
    for value in ("0", "-1", "none", "1.5", ""):
        monkeypatch.setenv("PROTEINRSI_LLM_MAX_REQUEST_BYTES", value)
        with pytest.raises(ValueError):
            JSONLLM.from_env(store)


def test_sponsored_rejection_exports_complete_evidence_and_preserves_visibility(tmp_path):
    sponsor = Store(tmp_path / "sponsor")
    sponsor.configure_budget({"llm_calls": 10})
    branch = SponsoredStore(tmp_path / "branch", sponsor, "trial")
    branch.configure_budget({"llm_calls": 5})
    sponsor.put("protected_fixture_labels", "never_read", {"secret": "UNREVEALED_SENTINEL"})
    llm = make_client(tmp_path, store=branch, max_request_bytes=1)
    with pytest.raises(ProviderPaused):
        llm.complete(*ARGS)
    ref, record = next(iter(branch.all("llm_preflights").items()))
    mirrored = sponsor.get("validation_llm_preflights", "trial/" + ref)
    assert mirrored == {"branch": "trial", **record}
    assert branch.usage()["llm_calls"]["committed"] == sponsor.usage()["llm_calls"]["committed"] == 0
    trace = read_trace(sponsor.root)
    event = next(e for e in trace["events"] if e["payload"].get("kind") == "llm_preflight_rejected")
    assert event["details"]["llm_preflights"] == mirrored
    assert "UNREVEALED_SENTINEL" not in json.dumps(trace)
    stream = io.StringIO()
    write_json(sponsor.root, stream)
    assert json.loads(stream.getvalue()) == trace
    target = tmp_path / "trace.html"
    export_html(sponsor.root, target)
    assets = next(tmp_path.glob("trace.html.assets-*"))
    entries = [json.loads(line) for line in (assets / "manifest.jsonl").read_text().splitlines()]
    entry = next(e for e in entries if e.get("namespace") == "validation_llm_preflights")
    assert json.loads(gzip.decompress((assets / entry["file"]).read_bytes())) == mirrored
    from proteinrsi.replay.broker import dispatch
    with pytest.raises(PermissionError, match="Unapproved namespace"):
        dispatch(Team(branch, llm), None, None, [], {"rpc": "get", "namespace": "llm_preflights", "key": ref})


def test_rejection_redacts_secrets_without_claiming_redacted_body_hash(tmp_path):
    llm = JSONLLM(Store(tmp_path / "redaction"), model="TEST_SECRET", api_key="TEST_SECRET",
        base_url=BASE_URL + "/TEST_SECRET", max_request_bytes=1,
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send")))
    args = ("A", "TEST_SECRET", {"api_key": "TEST_SECRET"}, {})
    with pytest.raises(ProviderPaused) as error:
        llm.complete(*args)
    record = next(iter(llm.store.all("llm_preflights").values()))
    assert "TEST_SECRET" not in str(error.value)
    assert "TEST_SECRET" not in json.dumps(read_trace(llm.store.root))
    assert record["request_redacted"] is True
    assert "[REDACTED]" in record["request"]["messages"][0]["content"]


def test_guarded_worker_pause_message_is_not_a_false_retry_claim(monkeypatch):
    from proteinrsi.replay.worker import rpc
    monkeypatch.setattr("sys.stdin", io.StringIO('{"error":"ProviderPaused"}\n'))
    with pytest.raises(ProviderPaused, match="preflight audit") as error:
        rpc("llm", role="A")
    assert "retry limit exhausted" not in str(error.value)


@pytest.mark.parametrize("sponsored", [False, True])
@pytest.mark.parametrize("failure_kind", ["ProviderPaused", "RuntimeError"])
def test_guarded_broker_drains_checkpoint_and_retains_actionable_preflight(
        campaign, monkeypatch, sponsored, failure_kind):
    """Stub IPC lifecycle, not a claim of executing the host's unavailable sandbox."""
    from proteinrsi.replay import broker
    store = campaign.store
    if sponsored:
        store = SponsoredStore(campaign.store.root / "branch", campaign.store, "fixture-trial")
        store.configure_budget({"llm_calls": 10})
    llm = make_client(campaign.store.root, store=store, max_request_bytes=1,
                      handler=lambda _: pytest.fail("must not send"))
    team = Team(store, llm)

    class CaptureBuffer(io.BytesIO):
        def close(self):
            self.sent = self.getvalue()
            super().close()

    class Process:
        def __init__(self):
            self.stdin, self.stdout = CaptureBuffer(), io.BytesIO()
        def poll(self):
            return 1
        def wait(self, **kwargs):
            return 1

    process = Process()
    role, instructions, context, schema = ARGS
    instructions += " TEST_SECRET"
    messages = iter([
        {"rpc": "llm", "role": role, "instructions": instructions, "context": context, "schema": schema},
        {"rpc": "llm"},  # Draining cannot make another provider call.
        {"rpc": "tool"},  # Nor start scientific work while paused.
        {"rpc": "put", "namespace": "research_runs", "key": "fixture-continuation",
         "value": {"state": "paused_provider", "context": context}},
        {"failed": failure_kind, "message": "Worker has no limit details"},
    ])
    monkeypatch.setattr(broker, "probe", lambda: {"available": True})
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(broker, "_readline", lambda *a, **k: next(messages))
    expected_error = ProviderPaused if failure_kind == "ProviderPaused" else broker.WorkerExecutionError
    with pytest.raises(expected_error) as error:
        broker.invoke_worker(team, campaign.view(), "team")
    if failure_kind == "ProviderPaused":
        assert "max_request_bytes=1" in str(error.value)
        assert "PROTEINRSI_LLM_MAX_REQUEST_BYTES" in str(error.value)
    else:
        assert error.value.error_type == "RuntimeError"  # Unrelated failures aren't masked.
    assert store.get("research_runs", "fixture-continuation") == {
        "state": "paused_provider", "context": context}
    assert len(store.all("llm_preflights")) == 1
    assert sum(e["kind"] == "llm_preflight_rejected" for e in store.events()) == 1
    assert store.usage()["llm_calls"]["committed"] == 0
    assert campaign.store.usage()["llm_calls"]["committed"] == 0
    if sponsored:
        assert len(campaign.store.all("validation_llm_preflights")) == 1
    outgoing = [json.loads(line) for line in process.stdin.sent.splitlines()]
    assert [msg for msg in outgoing if "error" in msg] == [{"error": "ProviderPaused"}] * 3
    assert "TEST_SECRET" not in process.stdin.sent.decode()
    assert instructions not in process.stdin.sent.decode()


@pytest.mark.parametrize("guarded", [False, True])
def test_plate_preflight_resume_keeps_same_scientific_request(tmp_path, guarded):
    from proteinrsi.plate import complete_candidates
    from proteinrsi.replay.broker import GuardedTeam, WorkerExecutionError
    from test_full_plate import plate_campaign
    if guarded:
        from proteinrsi.replay.sandbox import probe
        if not probe()["available"]:
            pytest.skip("Sandbox unavailable")
    campaign = plate_campaign(tmp_path, size=4, rounds=20, research=True, protocol="typed")
    before = copy.deepcopy(campaign.state)
    snapshots = campaign.store.all("method_snapshots")
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        instructions = payload["messages"][0]["content"]
        if "# A — resource protocol planner" in instructions:
            value = {"hypothesis": "Synthetic preflight recovery", "steps": [
                {"step_id": "design", "operation": "agent:propose", "question": "design",
                 "arguments": {"question": "Generate four variants"}}],
                "final_outputs": {"candidates": {"source": "step:design.result",
                                                "schema_ref": "protein.sequence_set/v1"}}}
        elif "# B — protein design" in instructions:
            value = {"edits": [[{"position": 1, "from": "A", "to": aa}] for aa in "CDEF"]}
        else:
            raise AssertionError(instructions[:100])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})

    llm = make_client(tmp_path, store=campaign.store, max_request_bytes=1, handler=respond)
    cls = GuardedTeam if guarded else Team
    campaign.team = cls(campaign.store, llm)
    with pytest.raises((ProviderPaused, WorkerExecutionError)):
        complete_candidates(campaign, campaign.view(), 4)
    assert calls == []
    assert campaign.state == before
    assert campaign.store.all("method_snapshots") == snapshots
    plate_key, paused = next(iter(campaign.store.all("plate_plans").items()))
    assert paused["state"] == "paused_provider" and paused["attempts"] == 1
    rejected = next(iter(campaign.store.all("llm_preflights").values()))
    campaign.team = cls(campaign.store, make_client(tmp_path, store=campaign.store, handler=respond))
    assert len(complete_candidates(campaign, campaign.view(), 4)) == 4
    assert calls[0] == rejected["request"]
    assert list(campaign.store.all("plate_plans")) == [plate_key]
    completed = campaign.store.get("plate_plans", plate_key)
    assert completed["state"] == "complete" and completed["attempts"] == 1
    assert campaign.store.usage()["experimental_wells"]["committed"] == 0
    assert campaign.state == before
    assert campaign.store.all("method_snapshots") == snapshots
