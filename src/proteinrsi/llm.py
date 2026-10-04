# SPDX-License-Identifier: MIT
"""An opt-in OpenAI-compatible HTTP client; no credentials or live model is bundled."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from proteinrsi.contracts import canonical, digest
from proteinrsi.storage import Store


class LLMError(RuntimeError):
    pass


class JSONLLM:
    def __init__(self, store: Store, *, model: str, base_url: str,
                 api_key: str, max_tokens: int = 4096, timeout: float = 90,
                 api_protocol: str = "chat_completions", reasoning_effort: str | None = None,
                 allow_http: bool = False,
                 transport: httpx.BaseTransport | None = None):
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
        self.store, self.model = store, model
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout, self.transport = max_tokens, timeout, transport
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
                   api_protocol=os.environ.get("PROTEINRSI_API_PROTOCOL", "chat_completions"),
                   reasoning_effort=os.environ.get("PROTEINRSI_REASONING_EFFORT") or None,
                   allow_http=os.environ.get("PROTEINRSI_ALLOW_HTTP", "").lower() == "true",
                   timeout=float(os.environ.get("PROTEINRSI_LLM_TIMEOUT", "90")))

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
        if previous is not None:
            if previous["state"] != "done":
                raise LLMError("Prior call failed or has uncertain completion; inspect audit before retrying")
            return previous["result"]
        self.store.reserve(key, "llm_calls", 1, payload)
        self.store.settle(key)
        self.store.put("llm", key, {"state": "started"})
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport,
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self.base_url + endpoint, json=payload,
                                       headers={"Authorization": "Bearer " + self.api_key})
                response.raise_for_status()
                body = response.json()
            if self.api_protocol == "responses":
                if body.get("status") != "completed" or body.get("error"):
                    raise ValueError("Responses API did not complete successfully")
                parts = []
                for item in body["output"]:
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
            self.store.put("llm", key, {"state": "failed", "error_type": type(exc).__name__})
            self.store.event("llm_failed", {"role": role, "key": key, "error_type": type(exc).__name__})
            raise LLMError(f"Provider call failed ({type(exc).__name__}); no silent mock fallback") from None
        self.store.put("llm", key, {"state": "done", "result": result, "usage": body.get("usage", {})})
        self.store.event("llm_completed", {"role": role, "key": key, "model": self.model,
                                          "usage": body.get("usage", {})})
        return result
