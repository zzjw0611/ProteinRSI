# SPDX-License-Identifier: MIT
"""An opt-in OpenAI-compatible HTTP client; no credentials or live model is bundled."""
from __future__ import annotations

import json
import hashlib
import os
import random
from functools import lru_cache
from email.utils import parsedate_to_datetime
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from proteinrsi.contracts import canonical, digest
from proteinrsi.storage import Store


class LLMError(RuntimeError):
    pass


class ProviderPaused(LLMError):
    """An audited provider or context pause; retain its cause and continuation."""

    def __init__(self, message: str, *, reason: str = "provider_paused"):
        super().__init__(message)
        self.reason = reason


# An administrative byte budget, NOT a model token/window claim. Operators must
# validate their own provider's input/output contract before raising this limit.
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_INPUT_TOKENS = 128000
DEFAULT_OUTPUT_TOKENS = 16384


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken
    return tiktoken.get_encoding("o200k_base")


def estimate_tokens(text: str) -> int:
    """An explicit surrogate estimate, never a provider tokenizer guarantee."""
    return len(_encoding().encode(text, disallowed_special=()))


class JSONLLM:
    def __init__(self, store: Store, *, model: str, base_url: str,
                 api_key: str, max_tokens: int = DEFAULT_OUTPUT_TOKENS, timeout: float = 90,
                 api_protocol: str = "chat_completions", reasoning_effort: str | None = None,
                 allow_http: bool = False, max_attempts: int = 4,
                 transport: httpx.BaseTransport | None = None, connect_timeout: float = 15,
                 max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                 input_target_tokens: int = DEFAULT_INPUT_TOKENS,
                 context_window: int | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and
                (allow_http or parsed.hostname in ("localhost", "127.0.0.1"))):
            raise ValueError("Use HTTPS or an explicit localhost provider")
        if not parsed.hostname:
            raise ValueError("Provider URL requires a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials, query strings and fragments are not allowed in provider URLs")
        if not model or not api_key:
            raise ValueError("Explicit model and API key are required")
        if api_protocol not in ("chat_completions", "responses"):
            raise ValueError("Unsupported LLM API protocol")
        if reasoning_effort is not None and reasoning_effort not in (
                "none", "minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("Unsupported reasoning effort")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("LLM max_attempts must be between 1 and 10")
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("LLM max_tokens must be a positive integer")
        if type(max_request_bytes) is not int or max_request_bytes < 1:
            raise ValueError("LLM max_request_bytes must be a positive integer; unbounded input is unsupported")
        if type(input_target_tokens) is not int or input_target_tokens < 1:
            raise ValueError("LLM input_target_tokens must be a positive integer")
        if context_window is not None and (type(context_window) is not int or context_window <= max_tokens + 8192):
            raise ValueError("LLM context_window must leave room for input, output and gateway overhead")
        self.input_target_tokens, self.context_window = input_target_tokens, context_window
        self.max_request_bytes = max_request_bytes
        self.max_attempts = max_attempts
        self.store, self.model = store, model
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout, self.transport = max_tokens, timeout, transport
        self.connect_timeout = connect_timeout
        self.api_protocol, self.reasoning_effort = api_protocol, reasoning_effort

    @property
    def cache_settings(self) -> dict:
        # Preserve existing team/run identities for the original default client.
        if self.api_protocol == "chat_completions" and self.reasoning_effort is None:
            return {}
        return {"llm_api_protocol": self.api_protocol,
                "llm_reasoning_effort": self.reasoning_effort}

    @classmethod
    def from_env(cls, store: Store) -> JSONLLM:
        if "PROTEINRSI_ASSISTANT_BRIDGE_DIR" in os.environ:
            from proteinrsi.assistant_bridge import (
                AssistantBridgeLLM, DEFAULT_MAX_REQUEST_BYTES as BRIDGE_MAX_BYTES,
            )
            return AssistantBridgeLLM(
                store, model=os.environ.get("PROTEINRSI_MODEL", ""),
                mailbox=os.environ["PROTEINRSI_ASSISTANT_BRIDGE_DIR"],
                timeout=float(os.environ.get("PROTEINRSI_ASSISTANT_BRIDGE_TIMEOUT", "900")),
                poll_interval=float(os.environ.get("PROTEINRSI_ASSISTANT_BRIDGE_POLL_INTERVAL", "0.25")),
                max_request_bytes=int(os.environ.get("PROTEINRSI_ASSISTANT_BRIDGE_MAX_REQUEST_BYTES",
                                                    str(BRIDGE_MAX_BYTES))))
        api_key = os.environ.get("PROTEINRSI_API_KEY", "")
        auth_file = os.environ.get("PROTEINRSI_CODEX_AUTH_FILE", "")
        if not api_key and auth_file:
            auth = json.loads(Path(auth_file).expanduser().read_text())
            api_key = auth.get("OPENAI_API_KEY", "") if isinstance(auth, dict) else ""
            if not isinstance(api_key, str) or not api_key:
                raise ValueError("Codex auth file requires a nonempty OPENAI_API_KEY")
        return cls(store, model=os.environ.get("PROTEINRSI_MODEL", ""),
                   base_url=os.environ.get("PROTEINRSI_BASE_URL", ""),
                   api_key=api_key,
                   max_tokens=int(os.environ.get("PROTEINRSI_LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_OUTPUT_TOKENS))),
                   api_protocol=os.environ.get("PROTEINRSI_API_PROTOCOL", "chat_completions"),
                   reasoning_effort=os.environ.get("PROTEINRSI_REASONING_EFFORT") or None,
                   allow_http=os.environ.get("PROTEINRSI_ALLOW_HTTP", "").lower() == "true",
                   timeout=float(os.environ.get("PROTEINRSI_LLM_TIMEOUT", "90")),
                   max_attempts=int(os.environ.get("PROTEINRSI_LLM_MAX_ATTEMPTS", "4")),
                   connect_timeout=float(os.environ.get("PROTEINRSI_LLM_CONNECT_TIMEOUT", "15")),
                   max_request_bytes=int(os.environ.get("PROTEINRSI_LLM_MAX_REQUEST_BYTES",
                                                       str(DEFAULT_MAX_REQUEST_BYTES))),
                   input_target_tokens=int(os.environ.get("PROTEINRSI_LLM_INPUT_TOKENS", str(DEFAULT_INPUT_TOKENS))),
                   context_window=int(os.environ["PROTEINRSI_LLM_CONTEXT_WINDOW"])
                       if os.environ.get("PROTEINRSI_LLM_CONTEXT_WINDOW") else None)

    @property
    def capacity_settings(self) -> dict:
        return {"input_target_tokens": self.input_target_tokens,
                "context_window": self.context_window, "max_request_bytes": self.max_request_bytes}

    def request_capacity(self, instructions: str, context: dict, schema: dict) -> dict:
        """Use the complete native request, including instructions and schema."""
        endpoint, payload = self._payload(instructions, context, schema)
        body = httpx.Request("POST", self.base_url + endpoint, json=payload).content
        return self._capacity(payload, body)

    def _capacity(self, payload: dict, body: bytes) -> dict:
        messages = payload.get("input", payload.get("messages", []))
        tokens = sum(estimate_tokens(m["content"]) + 8 for m in messages) + 8192
        token_limit = self.input_target_tokens
        if self.context_window is not None:
            token_limit = min(token_limit, self.context_window - self.max_tokens)
        return {"request_bytes": len(body), "max_request_bytes": self.max_request_bytes,
                "estimated_input_tokens": tokens, "input_target_tokens": token_limit,
                "reserved_output_tokens": self.max_tokens, "gateway_reserve_tokens": 8192,
                "tokenizer": "o200k_base_surrogate", "input_tokens": None,
                "context_window": self.context_window, "context_window_verified": False,
                "fits": len(body) <= self.max_request_bytes and tokens <= token_limit}

    def complete(self, role: str, instructions: str, context: dict[str, Any], schema: dict) -> dict:
        from proteinrsi.research.context import complete, enabled
        if enabled(self.store):
            return complete(self, role, instructions, context, schema)
        return self._complete(role, instructions, context, schema)

    def _payload(self, instructions: str, context: dict, schema: dict) -> tuple[str, dict]:
        # JSON mode is widely supported; independently validate every response at the caller.
        messages = [{"role": "system", "content": instructions +
                     "\nReturn exactly one JSON object matching this schema:\n" + canonical(schema)},
                    {"role": "user", "content": canonical(context)}]
        if self.api_protocol == "responses":
            messages[0]["role"] = "developer"
            endpoint = "/responses"
            payload = {"model": self.model, "input": messages,
                       "max_output_tokens": self.max_tokens,
                       "text": {"format": {"type": "json_object"}}, "store": False}
            if self.reasoning_effort is not None:
                payload["reasoning"] = {"effort": self.reasoning_effort}
        else:
            endpoint = "/chat/completions"
            payload = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens,
                       "response_format": {"type": "json_object"}}
            if self.reasoning_effort is not None:
                payload["reasoning_effort"] = self.reasoning_effort
        return endpoint, payload

    def _complete(self, role: str, instructions: str, context: dict[str, Any], schema: dict) -> dict:
        endpoint, payload = self._payload(instructions, context, schema)
        cache_url = self.base_url if self.api_protocol == "chat_completions" else self.base_url + endpoint
        key = "llm-" + digest({"role": role, "url": cache_url,
                              "request": payload})
        previous = self.store.get("llm", key)
        request_body = None
        while True:
            if previous is not None:
                if previous["state"] == "done":
                    self.store.event("llm_cache_hit", {"role": role, "key": key, "model": self.model})
                    return previous["result"]
                attempt = previous.get("attempt", 1)
                if previous["state"] != "failed" or not self._retryable(previous):
                    raise LLMError("Prior call failed or has uncertain completion; inspect audit before retrying")
            else:
                attempt = 0
            # Serialize with the same HTTPX JSON encoder used by the legacy
            # client, then send these exact bytes. Include schema and all API
            # envelope fields, not just the user-context text. No socket opens.
            if request_body is None:
                request_body = httpx.Request("POST", self.base_url + endpoint, json=payload).content
            preflight = self._preflight(key, role, context, payload, request_body)
            if previous is not None:
                extension = self.store.get("llm_retry_authorizations", key, {}).get("attempt_limit", 0)
                if attempt >= max(self.max_attempts, extension):
                    raise ProviderPaused(f"Provider retry limit exhausted ({attempt} attempts); authorize retry for {key}")
                # Archive legacy failures as well; never erase the cause on resume.
                self.store.put("llm_attempts", f"{key}/attempt-{attempt}", previous, immutable=True)
                delay = self._retry_delay(previous, attempt)
                self.store.event("llm_retry_scheduled", {"role": role, "key": key,
                    "attempt": attempt + 1, "delay_seconds": delay,
                    "http_status": previous.get("http_status"), "error_type": previous.get("error_type")})
                time.sleep(delay)
            try:
                return self._attempt(key, attempt + 1, role, context, endpoint, payload,
                                     request_body, preflight)
            except LLMError:
                previous = self.store.get("llm", key)
                if not self._retryable(previous):
                    raise

    def _preflight(self, key: str, role: str, context: dict, payload: dict,
                   request_body: bytes) -> dict:
        """Reject locally, without spending a call or inventing provider usage.

        Limits deliberately do not enter the scientific request/cache identity:
        an operator can correct a local cap without replaying a completed call,
        changing the request, resetting paid attempts, or rewriting old evidence.
        """
        from proteinrsi.audit import redact
        preflight = {"version": 2, **self._capacity(payload, request_body),
            "request_body_sha256": hashlib.sha256(request_body).hexdigest()}
        if preflight["fits"]:
            return preflight
        # Repeated rejection of the same bytes and cap has one immutable receipt,
        # separate from paid attempts. Keep the complete request, never a preview.
        preflight_key = key + "/preflight-" + digest(preflight)
        request = redact(payload, (self.api_key,))
        reason = "request_bytes_exceeded" if len(request_body) > self.max_request_bytes else "input_target_exceeded"
        audit = {"state": "rejected", "reason": reason,
            "request_key": key, "role": role, "model": self.model,
            "api_protocol": self.api_protocol, "base_url": self.base_url,
            "round": context.get("view", {}).get("round_index"),
            "request": request, "request_redacted": request != payload,
            "input_preflight": preflight, "network_attempted": False, "llm_calls_charged": 0}
        audit = redact(audit, (self.api_key,))
        self.store.put("llm_preflights", preflight_key, audit, immutable=True)
        self.store.event("llm_preflight_rejected", {"key": key, "preflight_key": preflight_key,
            "role": audit["role"], "round": audit["round"], "reason": audit["reason"], **preflight})
        raise ProviderPaused(
            f"Native request preflight rejected {key}: {len(request_body)} serialized UTF-8 JSON bytes "
            f"(max_request_bytes={self.max_request_bytes}), estimated input "
            f"{preflight['estimated_input_tokens']} tokens (target={preflight['input_target_tokens']}). "
            "No network call or LLM budget charge. "
            "Check the provider model's tokenizer/context window and output allowance before adjusting "
            "PROTEINRSI_LLM_MAX_REQUEST_BYTES (or max_request_bytes), then resume the same request. "
            "A byte limit is not token-window verification; do not truncate evidence or authorize retries "
            "to bypass this guard.")

    @staticmethod
    def _retryable(record: dict) -> bool:
        return (record.get("http_status") in {408, 429, 500, 502, 503, 504}
                or record.get("error_type") in {"ConnectTimeout", "ReadTimeout", "WriteTimeout",
                    "PoolTimeout", "ConnectError", "ReadError", "WriteError", "RemoteProtocolError"})

    @staticmethod
    def _retry_delay(record: dict, attempt: int) -> float:
        delay = min(30.0, 2 ** attempt) + random.uniform(0, 1)
        retry_after = record.get("response_headers", {}).get("retry-after")
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            delay = max(delay, min(60.0, max(0.0, seconds)))
        return delay

    def _attempt(self, key: str, attempt: int, role: str, context: dict,
                 endpoint: str, payload: dict, request_body: bytes, preflight: dict) -> dict:
        attempt_key = f"{key}/attempt-{attempt}"
        charge_key = key if attempt == 1 else attempt_key
        self.store.reserve(charge_key, "llm_calls", 1, payload)
        self.store.settle(charge_key)
        from proteinrsi.audit import redact
        started = time.time()
        audit = {"attempt": attempt, "request_key": key, "state": "started", "role": role, "model": self.model,
            "api_protocol": self.api_protocol, "base_url": self.base_url,
            "request": redact(payload, (self.api_key,)), "started_at": started,
            "input_preflight": preflight,
            "round": context.get("view", {}).get("round_index"),
            "provider_reasoning_summary": [], "reasoning_note": "Only explicitly returned summaries are recorded; no hidden thoughts inferred."}
        self.store.put("llm", key, audit)
        self.store.event("llm_started", {"role": role, "key": key, "model": self.model,
                                         "round": audit["round"], "attempt": attempt, "attempt_key": attempt_key})
        content, body, response = None, None, None
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout), transport=self.transport,
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self.base_url + endpoint, content=request_body,
                                       headers={"Authorization": "Bearer " + self.api_key,
                                                "Content-Type": "application/json"})
                audit["http_status"] = response.status_code
                audit["response_headers"] = redact({name: response.headers[name][:1024]
                    for name in ("x-request-id", "request-id", "cf-ray", "retry-after", "date", "server")
                    if name in response.headers}, (self.api_key,))
                if response.is_error:
                    audit["error_response"] = redact(response.text, (self.api_key,))[:8000]
                response.raise_for_status()
                body = response.json()
            if self.api_protocol == "responses":
                if body.get("status") != "completed" or body.get("error"):
                    raise ValueError("Responses API did not complete successfully")
                # Preserve message boundaries/phase: commentary is not decision JSON.
                audit["provider_messages"] = [item for item in body["output"]
                    if item.get("type") == "message" and item.get("role") == "assistant"]
                for item in body["output"]:
                    if item.get("type") == "reasoning":
                        audit["provider_reasoning_summary"].extend(
                            part["text"] for part in item.get("summary", [])
                            if part.get("type") == "summary_text" and isinstance(part.get("text"), str))
                content = response_decision_text(audit["provider_messages"])
            else:
                content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            audit.update(state="failed", error_type=type(exc).__name__, ended_at=time.time(),
                         raw_output=content, duration_seconds=time.time()-started)
            if isinstance(body, dict):
                audit["usage"] = body.get("usage", {})
            audit = redact(audit, (self.api_key,))
            self.store.put("llm_attempts", attempt_key, audit, immutable=True)
            self.store.put("llm", key, audit)
            self.store.event("llm_failed", {"role": role, "key": key, "error_type": type(exc).__name__,
                "http_status": audit.get("http_status"), "attempt": attempt, "attempt_key": attempt_key})
            raise LLMError(f"Provider call failed ({type(exc).__name__}); no silent mock fallback") from None
        audit.update(state="done", result=result, raw_output=content, usage=body.get("usage", {}),
                     ended_at=time.time(), duration_seconds=time.time()-started)
        audit = redact(audit, (self.api_key,))
        with self.store.transaction():
            self.store.put("llm_attempts", attempt_key, audit, immutable=True)
            self.store.put("llm", key, audit)
            self.store.event("llm_completed", {"role": role, "key": key, "model": self.model,
                                              "usage": body.get("usage", {}), "attempt": attempt, "attempt_key": attempt_key})
        return result


def response_decision_text(messages: list[dict]) -> str:
    """Select one protocol-defined answer, never guess JSON within prose or merge answers."""
    answers = []
    for message in messages:
        phase = message.get("phase")
        if phase not in (None, "commentary", "final_answer"):
            raise ValueError("Unknown Responses message phase")
        if message.get("status", "completed") != "completed":
            raise ValueError("Responses message is incomplete")
        parts = message.get("content", [])
        if any(p.get("type") == "refusal" for p in parts):
            raise ValueError("Responses API refused the request")
        if phase != "commentary":
            answers.append("".join(p["text"] for p in parts if p.get("type") == "output_text"))
    if len(answers) != 1 or not answers[0].strip():
        raise ValueError("Expected exactly one final Responses message")
    return answers[0]
