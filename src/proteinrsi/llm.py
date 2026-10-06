# SPDX-License-Identifier: MIT
"""An opt-in OpenAI-compatible HTTP client; no credentials or live model is bundled."""
from __future__ import annotations

import json
import os
import random
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
    """A known failed request exhausted retries; retain its continuation checkpoint."""


class JSONLLM:
    def __init__(self, store: Store, *, model: str, base_url: str,
                 api_key: str, max_tokens: int = 4096, timeout: float = 90,
                 api_protocol: str = "chat_completions", reasoning_effort: str | None = None,
                 allow_http: bool = False, max_attempts: int = 4,
                 transport: httpx.BaseTransport | None = None, connect_timeout: float = 15):
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
                   max_tokens=int(os.environ.get("PROTEINRSI_LLM_MAX_OUTPUT_TOKENS", "4096")),
                   api_protocol=os.environ.get("PROTEINRSI_API_PROTOCOL", "chat_completions"),
                   reasoning_effort=os.environ.get("PROTEINRSI_REASONING_EFFORT") or None,
                   allow_http=os.environ.get("PROTEINRSI_ALLOW_HTTP", "").lower() == "true",
                   timeout=float(os.environ.get("PROTEINRSI_LLM_TIMEOUT", "90")),
                   max_attempts=int(os.environ.get("PROTEINRSI_LLM_MAX_ATTEMPTS", "4")),
                   connect_timeout=float(os.environ.get("PROTEINRSI_LLM_CONNECT_TIMEOUT", "15")))

    def complete(self, role: str, instructions: str, context: dict[str, Any], schema: dict) -> dict:
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
        cache_url = self.base_url if self.api_protocol == "chat_completions" else self.base_url + endpoint
        key = "llm-" + digest({"role": role, "url": cache_url,
                              "request": payload})
        previous = self.store.get("llm", key)
        while True:
            if previous is not None:
                if previous["state"] == "done":
                    self.store.event("llm_cache_hit", {"role": role, "key": key, "model": self.model})
                    return previous["result"]
                attempt = previous.get("attempt", 1)
                if previous["state"] != "failed" or not self._retryable(previous):
                    raise LLMError("Prior call failed or has uncertain completion; inspect audit before retrying")
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
            else:
                attempt = 0
            try:
                return self._attempt(key, attempt + 1, role, context, endpoint, payload)
            except LLMError:
                previous = self.store.get("llm", key)
                if not self._retryable(previous):
                    raise

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
                 endpoint: str, payload: dict) -> dict:
        attempt_key = f"{key}/attempt-{attempt}"
        charge_key = key if attempt == 1 else attempt_key
        self.store.reserve(charge_key, "llm_calls", 1, payload)
        self.store.settle(charge_key)
        from proteinrsi.audit import redact
        started = time.time()
        audit = {"attempt": attempt, "request_key": key, "state": "started", "role": role, "model": self.model,
            "api_protocol": self.api_protocol, "base_url": self.base_url,
            "request": redact(payload, (self.api_key,)), "started_at": started,
            "round": context.get("view", {}).get("round_index"),
            "provider_reasoning_summary": [], "reasoning_note": "Only explicitly returned summaries are recorded; no hidden thoughts inferred."}
        self.store.put("llm", key, audit)
        self.store.event("llm_started", {"role": role, "key": key, "model": self.model,
                                         "round": audit["round"], "attempt": attempt, "attempt_key": attempt_key})
        content, body, response = None, None, None
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout), transport=self.transport,
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self.base_url + endpoint, json=payload,
                                       headers={"Authorization": "Bearer " + self.api_key})
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
                parts = []
                for item in body["output"]:
                    if item.get("type") == "reasoning":
                        audit["provider_reasoning_summary"].extend(
                            part["text"] for part in item.get("summary", [])
                            if part.get("type") == "summary_text" and isinstance(part.get("text"), str))
                    if item.get("type") != "message" or item.get("role") != "assistant":
                        continue
                    for part in item.get("content", []):
                        if part.get("type") == "refusal":
                            raise ValueError("Responses API refused the request")
                        if part.get("type") == "output_text":
                            parts.append(part["text"])
                content = "".join(parts)
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
        self.store.put("llm_attempts", attempt_key, audit, immutable=True)
        self.store.put("llm", key, audit)
        self.store.event("llm_completed", {"role": role, "key": key, "model": self.model,
                                          "usage": body.get("usage", {}), "attempt": attempt, "attempt_key": attempt_key})
        return result
