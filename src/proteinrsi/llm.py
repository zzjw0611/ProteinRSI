# SPDX-License-Identifier: MIT
"""An opt-in OpenAI-compatible HTTP client; no credentials or live model is bundled."""
from __future__ import annotations

import json
import os
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
                 transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
            raise ValueError("Use HTTPS or an explicit localhost provider")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials, query strings and fragments are not allowed in provider URLs")
        if not model or not api_key:
            raise ValueError("Explicit model and API key are required")
        self.store, self.model = store, model
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout, self.transport = max_tokens, timeout, transport

    @classmethod
    def from_env(cls, store: Store) -> JSONLLM:
        return cls(store, model=os.environ.get("PROTEINRSI_MODEL", ""),
                   base_url=os.environ.get("PROTEINRSI_BASE_URL", ""),
                   api_key=os.environ.get("PROTEINRSI_API_KEY", ""))

    def complete(self, role: str, instructions: str, context: dict[str, Any], schema: dict) -> dict:
        # JSON mode is widely supported; independently validate every response at the caller.
        messages = [{"role": "system", "content": instructions +
                     "\nReturn exactly one JSON object matching this schema:\n" + canonical(schema)},
                    {"role": "user", "content": canonical(context)}]
        payload = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens,
                   "response_format": {"type": "json_object"}}
        key = "llm-" + digest({"role": role, "url": self.base_url, "request": payload})
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
                response = client.post(self.base_url + "/chat/completions", json=payload,
                                       headers={"Authorization": "Bearer " + self.api_key})
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            self.store.put("llm", key, {"state": "failed", "error_type": type(exc).__name__})
            self.store.event("llm_failed", {"role": role, "key": key, "error_type": type(exc).__name__})
            raise LLMError(f"Provider call failed ({type(exc).__name__}); no silent mock fallback") from None
        self.store.put("llm", key, {"state": "done", "result": result, "usage": body.get("usage", {})})
        self.store.event("llm_completed", {"role": role, "key": key, "model": self.model,
                                          "usage": body.get("usage", {})})
        return result
