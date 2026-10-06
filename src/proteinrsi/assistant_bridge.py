# SPDX-License-Identifier: MIT
"""Opt-in, controller-only mailbox for an actual interactive assistant response.

This is a transport, not a model or a scientific-result generator. Nothing here
calls a provider, inspects source data, or estimates model usage.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from proteinrsi.contracts import canonical, digest
from proteinrsi.llm import JSONLLM, LLMError, ProviderPaused
from proteinrsi.storage import Store

PROTOCOL_VERSION = 1
TRANSPORT = "assistant_bridge"
MAX_JSON_BYTES = 32 * 1024 * 1024


# The existing guarded RPC recognizes the exact ProviderPaused wire name.
# Aliases retain that name and preserve worker checkpoints without an RPC change.
# Timeout needs a reply; rejection needs an explicitly corrected reply. Neither
# creates a new request or authorizes a native-provider retry.
AssistantBridgeTimeout = ProviderPaused
AssistantBridgeResponseError = ProviderPaused


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _loads(raw: bytes):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant)


@contextmanager
def _directory(path: Path):
    """Pin every directory component; never follow symlinks, including ancestors."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _subdirectory(parent: int, name: str):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _read(directory: int, name: str):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Mailbox files must be regular files without hard links")
        if info.st_size > MAX_JSON_BYTES:
            raise ValueError("Mailbox JSON exceeds size limit")
        raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("Mailbox JSON exceeds size limit")
        return raw


def _publish(directory: int, name: str, value: dict):
    """Atomic, fsynced publication; request identity is immutable across resume."""
    expected = canonical(value).encode("utf-8") + b"\n"
    old = _read(directory, name)
    if old is not None:
        if canonical(_loads(old)) != canonical(value):
            raise ValueError("Existing request does not match the durable request")
        return
    temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _validator(schema: dict):
    # No automatic HTTP/file resolution, even for schemas received over worker RPC.
    pending = [(schema, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise ValueError("Bridge schema nesting limit exceeded")
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                        not isinstance(value, str) or not value.startswith("#")):
                    raise ValueError("Bridge schemas may only use local references")
                pending.append((value, depth + 1))
        elif isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=Registry())


class AssistantBridgeLLM(JSONLLM):
    """JSONLLM-compatible manual transport. Only the trusted controller owns it."""

    def __init__(self, store: Store, *, model: str, mailbox: str | Path,
                 timeout: float = 900, poll_interval: float = 0.25):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Assistant bridge requires an explicit model label")
        if not mailbox:
            raise ValueError("Assistant bridge requires an explicit absolute mailbox path")
        path = Path(mailbox).expanduser()
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("Assistant bridge requires an absolute path without traversal")
        if not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise ValueError("Assistant bridge timeout must be > 0 and <= 86400 seconds")
        if not math.isfinite(poll_interval) or not 0 < poll_interval <= 60:
            raise ValueError("Assistant bridge poll interval must be > 0 and <= 60 seconds")
        self.store, self.model, self.mailbox = store, model, path
        self.timeout, self.poll_interval = timeout, poll_interval
        # Workers receive this opaque identity, never the controller's mailbox path.
        self.base_url = "assistant-bridge://local"
        self.api_protocol = TRANSPORT
        with _directory(self.mailbox):
            pass  # Validate the whole path before any budget is charged.

    @property
    def cache_settings(self) -> dict:
        return {"llm_transport": TRANSPORT, "llm_bridge_protocol": PROTOCOL_VERSION}

    def _request(self, role: str, instructions: str, context: dict, schema: dict):
        if not isinstance(role, str) or not isinstance(instructions, str):
            raise ValueError("Bridge role and instructions must be strings")
        if not isinstance(context, dict) or not isinstance(schema, dict):
            raise ValueError("Bridge context and schema must be JSON objects")
        _validator(schema)
        payload = {"protocol_version": PROTOCOL_VERSION, "transport": TRANSPORT,
                   "model": self.model, "role": role, "instructions": instructions,
                   "context": context, "schema": schema}
        encoded = canonical(payload)
        if len(encoded.encode("utf-8")) > MAX_JSON_BYTES - 1024:
            raise ValueError("Bridge request exceeds size limit")
        # Detach mutable caller input. Store-scoped identity prevents cross-campaign
        # reply reuse when several campaigns share an operator mailbox.
        payload = json.loads(encoded)
        with self.store.transaction():
            scope = self.store.get("assistant_bridge", "scope")
            if scope is None:
                scope = uuid.uuid4().hex
                self.store.put("assistant_bridge", "scope", scope, immutable=True)
        request_hash = digest(payload)
        request_id = "assistant-" + digest({"scope": scope, "request_hash": request_hash})
        return {**payload, "request_id": request_id, "request_hash": request_hash}

    def _pending(self, request: dict):
        key = "llm-" + request["request_id"]
        with self.store.transaction():
            previous = self.store.get("llm", key)
            if previous is not None:
                if previous.get("request") != request or previous.get("transport") != TRANSPORT:
                    raise LLMError("Bridge cache identity mismatch; inspect audit")
                if previous["state"] not in {"pending", "done"}:
                    raise LLMError("Unknown bridge state; inspect audit")
                return key, previous
            self.store.reserve(key, "llm_calls", 1, request)
            self.store.settle(key)
            view = request["context"].get("view")
            record = {"attempt": 1, "request_key": key, "request_id": request["request_id"],
                "request_hash": request["request_hash"], "state": "pending", "role": request["role"],
                "model": self.model, "model_identity_verified": False,
                "transport": TRANSPORT, "api_protocol": TRANSPORT,
                "base_url": self.base_url, "request": request, "started_at": time.time(),
                "round": view.get("round_index") if isinstance(view, dict) else None,
                "usage": {}, "usage_note": "Provider token usage and cost are unavailable for this transport.",
                "provider_reasoning_summary": [],
                "reasoning_note": "Only the returned JSON decision is recorded; no private reasoning is requested or inferred."}
            self.store.put("llm", key, record)
            self.store.event("llm_started", {"role": request["role"], "key": key,
                "model": self.model, "transport": TRANSPORT, "request_id": request["request_id"],
                "round": record["round"], "attempt": 1})
        return key, record

    def complete(self, role: str, instructions: str, context: dict, schema: dict) -> dict:
        request = self._request(role, instructions, context, schema)
        key, record = self._pending(request)
        if record["state"] == "done":
            return self._cached(key, record, schema)
        deadline = time.monotonic() + self.timeout
        name = request["request_id"] + ".json"
        try:
            with _directory(self.mailbox) as root, \
                    _subdirectory(root, "requests") as requests, \
                    _subdirectory(root, "responses") as responses, \
                    _subdirectory(root, ".locks") as locks:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=locks)
                with os.fdopen(fd, "r+b") as lock:
                    info = os.fstat(lock.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Bridge lock must be a regular file without hard links")
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            self._pause(deadline, key, request)
                    # A concurrent controller may already have completed this request.
                    record = self.store.get("llm", key)
                    if record["state"] == "done":
                        return self._cached(key, record, schema)
                    _publish(requests, name, request)
                    self.store.event("llm_bridge_waiting", {"key": key, "role": role,
                        "transport": TRANSPORT, "request_id": request["request_id"]})
                    while True:
                        raw = _read(responses, name)
                        if raw is not None:
                            return self._accept(key, record, request, raw)
                        self._pause(deadline, key, request)
        except (OSError, ValueError) as exc:
            self.store.event("llm_bridge_mailbox_rejected", {"key": key,
                "request_id": request["request_id"], "error_type": type(exc).__name__})
            raise LLMError("Unsafe or conflicting assistant mailbox; inspect audit and correct it before resuming") from exc

    def _cached(self, key: str, record: dict, schema: dict):
        result = record["result"]
        _validator(schema).validate(result)
        self.store.event("llm_cache_hit", {"key": key, "role": record["role"],
            "model": self.model, "transport": TRANSPORT})
        return result

    def _pause(self, deadline: float, key: str, request: dict):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.store.event("llm_bridge_timeout", {"key": key, "transport": TRANSPORT,
                "request_id": request["request_id"], "state": "pending"})
            raise AssistantBridgeTimeout(
                f"Assistant response pending: {request['request_id']}; resume the identical call "
                "with the same store/model to reuse this request and its existing charge")
        time.sleep(min(self.poll_interval, remaining))

    def _accept(self, key: str, record: dict, request: dict, raw: bytes):
        response_hash = hashlib.sha256(raw).hexdigest()
        try:
            response = _loads(raw)
            expected_fields = {"protocol_version", "transport", "request_id", "request_hash", "model", "result"}
            if not isinstance(response, dict) or set(response) != expected_fields:
                raise ValueError("Unexpected response envelope fields")
            for field in expected_fields - {"result"}:
                if type(response[field]) is not type(request[field]) or response[field] != request[field]:
                    raise ValueError("Response identity mismatch")
            result = response["result"]
            if not isinstance(result, dict):
                raise ValueError("Response result must be a JSON object")
            content = canonical(result)
            _validator(request["schema"]).validate(result)
        except (ValueError, TypeError, ValidationError, RecursionError, Unresolvable) as exc:
            # Retain the rejected byte digest, not unsolicited reasoning or another
            # request's contents. The operator must atomically replace the reply.
            receipt = {"request_key": key, "request_id": request["request_id"],
                       "transport": TRANSPORT, "state": "rejected", "response_sha256": response_hash,
                       "error_type": type(exc).__name__}
            rejection_key = key + "/rejected-" + response_hash
            with self.store.transaction():
                self.store.put("llm_attempts", rejection_key, receipt, immutable=True)
                self.store.event("llm_bridge_response_rejected", {**receipt, "key": key,
                    "attempt_key": rejection_key})
            raise AssistantBridgeResponseError(
                f"Assistant response rejected ({type(exc).__name__}) for {request['request_id']}; "
                "correct the response envelope/schema and resume; no new call is charged") from None
        completed = {**record, "state": "done", "result": result, "raw_output": content,
                     "response": response, "response_sha256": response_hash,
                     "ended_at": time.time(), "duration_seconds": time.time() - record["started_at"]}
        attempt_key = key + "/attempt-1"
        with self.store.transaction():
            self.store.put("llm_attempts", attempt_key, completed, immutable=True)
            self.store.put("llm", key, completed)
            self.store.event("llm_completed", {"key": key, "role": request["role"],
                "model": self.model, "transport": TRANSPORT, "request_id": request["request_id"],
                "usage": {}, "attempt": 1, "attempt_key": attempt_key})
        return result
