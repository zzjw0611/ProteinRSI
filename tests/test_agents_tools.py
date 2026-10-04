import json

import httpx
import pytest
from jsonschema import ValidationError

from proteinrsi.agents import Team, skill_text
from proteinrsi.contracts import TaskKind, TaskSpec
from proteinrsi.llm import JSONLLM, LLMError
from proteinrsi.tools import ToolCall, ToolGateway, ToolSpec


def tool_spec(**updates):
    spec = ToolSpec(name="example", capability="sequence.generate", implementation_version="test-v1",
        task_kinds=[TaskKind.VARIANT], input_schema={"type": "object", "properties": {"target": {"type": "string"}},
            "required": ["target"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"candidates": {"type": "array"}},
            "required": ["candidates"]}, protected_inputs={"target": "reference_sequence"})
    return spec.model_copy(update=updates)


def test_gateway_checks_input_and_idempotency(campaign):
    calls = []
    gateway = ToolGateway(campaign.store)
    gateway.register(tool_spec(), lambda arguments: calls.append(arguments) or {"candidates": []})
    task = TaskSpec.model_validate(campaign.state["task"])
    call = ToolCall(name="example", arguments={"target": task.reference_sequence})
    for _ in range(2):
        assert gateway.call(call, task, allowed=["example"], context_key="same") == {"candidates": []}
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        gateway.call(call, task, allowed=[], context_key="bad")
    with pytest.raises(ValueError):
        gateway.call(ToolCall(name="example", arguments={"target": "ACD"}), task,
                     allowed=["example"], context_key="bad")
    with pytest.raises(ValidationError):
        gateway.call(ToolCall(name="example", arguments={"target": task.reference_sequence, "shell": "bad"}),
                     task, allowed=["example"], context_key="bad")


def test_no_silent_egress_and_bad_output(campaign):
    task = campaign.view().task
    gateway = ToolGateway(campaign.store)
    gateway.register(tool_spec(data_egress=True), lambda a: {})
    with pytest.raises(PermissionError):
        gateway.call(ToolCall(name="example", arguments={"target": task.reference_sequence}), task,
                     allowed=["example"], context_key="x")
    gateway.allow_egress = True
    with pytest.raises(ValidationError):
        gateway.call(ToolCall(name="example", arguments={"target": task.reference_sequence}), task,
                     allowed=["example"], context_key="x")
    with pytest.raises(RuntimeError):
        gateway.call(ToolCall(name="example", arguments={"target": task.reference_sequence}), task,
                     allowed=["example"], context_key="x")


def test_llm_real_client_contract_and_cache(campaign):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"total_tokens": 12}})
    client = JSONLLM(campaign.store, model="test-model", base_url="https://provider.example/v1",
                     api_key="TEST_SECRET", transport=httpx.MockTransport(handler))
    assert client.complete("A", "test", {}, {}) == {"ok": True}
    assert client.complete("A", "test", {}, {}) == {"ok": True}
    assert len(calls) == 1
    assert campaign.store.usage()["llm_calls"]["committed"] == 1
    assert "TEST_SECRET" not in json.dumps(campaign.store.events())


def test_bad_llm_response_has_no_scripted_fallback(campaign):
    client = JSONLLM(campaign.store, model="test", base_url="https://provider.example/v1", api_key="TEST",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "bad"}}]})))
    with pytest.raises(LLMError):
        client.complete("A", "test", {}, {})
    assert campaign.store.usage()["llm_calls"]["committed"] == 1


def test_responses_reasoning_output_cache_and_usage(campaign):
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["input"][0]["role"] == "developer"
        assert payload["reasoning"]["effort"] in ("medium", "low")
        assert payload["text"]["format"] == {"type": "json_object"}
        assert payload["store"] is False
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": '{"ok":'},
                {"type": "output_text", "text": ' true}'}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10}})

    kwargs = {"model": "test-model", "base_url": "https://provider.example/v1",
              "api_key": "TEST_SECRET", "api_protocol": "responses",
              "transport": httpx.MockTransport(handler)}
    client = JSONLLM(campaign.store, reasoning_effort="medium", **kwargs)
    assert client.complete("A", "test", {}, {}) == {"ok": True}
    assert client.complete("A", "test", {}, {}) == {"ok": True}
    assert len(requests) == 1
    assert JSONLLM(campaign.store, reasoning_effort="low", **kwargs).complete(
        "A", "test", {}, {}) == {"ok": True}
    assert len(requests) == 2
    assert campaign.store.usage()["llm_calls"]["committed"] == 2
    assert "TEST_SECRET" not in json.dumps(campaign.store.all("llm"))
    assert all(record["usage"]["output_tokens"] == 10
               for record in campaign.store.all("llm").values())


@pytest.mark.parametrize("body", [
    {"status": "incomplete", "output": [{"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": '{"ok": true}'}]}]},
    {"status": "completed", "output": [{"type": "message", "role": "assistant",
        "content": [{"type": "refusal", "refusal": "Cannot comply"}]}]},
    {"status": "completed", "output": [{"type": "reasoning", "summary": []}]},
])
def test_responses_failure_is_recorded_and_not_resubmitted(campaign, body):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)
    client = JSONLLM(campaign.store, model="test", base_url="https://provider.example/v1",
        api_key="TEST", api_protocol="responses", transport=httpx.MockTransport(handler))
    for _ in range(2):
        with pytest.raises(LLMError):
            client.complete("A", "test", {}, {})
    assert len(requests) == 1
    assert campaign.store.usage()["llm_calls"]["committed"] == 1


def test_codex_key_file_and_http_require_explicit_configuration(campaign, tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": "TEST_SECRET"}))
    monkeypatch.delenv("PROTEINRSI_API_KEY", raising=False)
    monkeypatch.setenv("PROTEINRSI_CODEX_AUTH_FILE", str(auth))
    monkeypatch.setenv("PROTEINRSI_MODEL", "test-model")
    monkeypatch.setenv("PROTEINRSI_BASE_URL", "http://provider.example:8080/v1")
    monkeypatch.setenv("PROTEINRSI_API_PROTOCOL", "responses")
    monkeypatch.setenv("PROTEINRSI_REASONING_EFFORT", "medium")
    monkeypatch.delenv("PROTEINRSI_ALLOW_HTTP", raising=False)
    with pytest.raises(ValueError):
        JSONLLM.from_env(campaign.store)
    monkeypatch.setenv("PROTEINRSI_ALLOW_HTTP", "true")
    client = JSONLLM.from_env(campaign.store)
    assert client.api_key == "TEST_SECRET"
    assert client.api_protocol == "responses"
    assert client.reasoning_effort == "medium"
    assert "TEST_SECRET" not in json.dumps(campaign.store.events())


def test_four_research_role_calls_are_wired(campaign):
    class FakeLLM:
        roles = []
        def complete(self, role, instructions, context, schema):
            self.roles.append(role)
            if role == "A":
                return {"rationale": "Test planning"}
            if role == "B":
                return {"candidates": [{"sequence": s} for s in context["view"]["task"]["candidates"][:5]]}
            if role == "C":
                return {"ranking": [c["sequence"] for c in context["candidates"]], "summary": "Reviewed"}
            if role == "A-selection":
                return {"ranking": [c["sequence"] for c in context["ranked_candidates"]], "summary": "Selected"}
            raise AssertionError(role)
    llm = FakeLLM()
    results = Team(campaign.store, llm).run(campaign.view())
    assert len(results) == 5
    assert llm.roles == ["A", "B", "C", "A-selection"]
    llm.roles = []
    llm.cache_settings = {"llm_api_protocol": "responses", "llm_reasoning_effort": "medium"}
    Team(campaign.store, llm).run(campaign.view())
    assert llm.roles == ["A", "B", "C", "A-selection"]
    Team(campaign.store, llm).run(campaign.view())
    assert llm.roles == ["A", "B", "C", "A-selection"]
    llm.roles = []
    llm.cache_settings["llm_reasoning_effort"] = "high"
    Team(campaign.store, llm).run(campaign.view())
    assert llm.roles == ["A", "B", "C", "A-selection"]


def test_skill_paths_are_not_arbitrary_reads():
    assert "revealed" in skill_text(["fitness-modeling"])
    with pytest.raises(ValueError):
        skill_text(["../../etc/passwd"])
