# SPDX-License-Identifier: MIT
"""Immutable nominal schemas. Unknown conversions are errors, never guessed casts."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from proteinrsi.contracts import canonical, digest

SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_.-]*(?:/[a-zA-Z0-9_.-]+)+$")


class ContractError(ValueError):
    """A recoverable shape/connection error, not a model/provider failure."""

    def __init__(self, message: str, *, path: str = "", code: str = "contract_mismatch"):
        super().__init__(message)
        self.path, self.code = path, code

    def detail(self) -> dict:
        return {"path": self.path, "code": self.code, "message": str(self)}


def pointer(value: Any, path: str) -> Any:
    """Small RFC 6901 reader: no expressions, attributes, files or implicit conversion."""
    if path == "":
        return value
    if not path.startswith("/"):
        raise ContractError("Expected an empty or slash-prefixed JSON pointer", path=path)
    result = value
    for raw in path[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ContractError("Invalid JSON pointer escape", path=path)
        key = raw.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(result, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise KeyError(key)
                result = result[int(key)]
            elif isinstance(result, dict):
                result = result[key]
            else:
                raise KeyError(key)
        except (KeyError, IndexError, ValueError) as exc:
            raise ContractError("Referenced field does not exist", path=path) from exc
    return result


def _check_schema(schema: dict, *, custom: bool) -> int:
    """Only local nonrecursive refs; bound schema size before meta-validation.

    Custom schemas cannot introduce regex validators or measurement authority.
    These are data contracts, not permission grants or executable validator code.
    """
    if len(canonical(schema).encode()) > 100_000:
        raise ContractError("Schema exceeds 100KB")
    refs: list[str] = []
    expanded_nodes = 0

    def walk(node: Any, depth: int = 0):
        nonlocal expanded_nodes
        expanded_nodes += 1
        if custom and expanded_nodes > 4096:
            raise ContractError("Custom schema expansion exceeds bounded validation work")
        if depth > 32:
            raise ContractError("Schema nesting limit exceeded")
        if isinstance(node, dict):
            if "$dynamicRef" in node or "$recursiveRef" in node:
                raise ContractError("Dynamic and recursive schemas are unsupported")
            if custom and any(key in node for key in ("$id", "$schema", "$anchor", "$dynamicAnchor")):
                raise ContractError("Custom schemas cannot rebase references or change schema dialect")
            if custom and any(k in node for k in ("pattern", "patternProperties", "format",
                                                  "unevaluatedItems", "unevaluatedProperties")):
                raise ContractError("Custom schemas cannot add regex/format or unevaluated annotation validators")
            if "$ref" in node:
                ref = node["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                    raise ContractError("Only local $defs references are allowed")
                if ref in refs:
                    raise ContractError("Recursive schema references are unsupported")
                refs.append(ref)
                walk(pointer(schema, ref[1:]), depth + 1)
                refs.pop()
            for key, child in node.items():
                if key != "$ref":
                    walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ContractError("Invalid JSON Schema: " + exc.message[:500],
                            path="/" + "/".join(map(str, exc.absolute_path)),
                            code="invalid_schema") from exc
    return expanded_nodes


def _unique_checks(schema):
    pending, count = [schema], 0
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            count += node.get("uniqueItems") is True
            if "$ref" in node:
                pending.append(pointer(schema, node["$ref"][1:]))
            pending.extend(value for key, value in node.items() if key != "$ref")
        elif isinstance(node, list):
            pending.extend(node)
    return count


def _json_nodes(value):
    """Iterative size bound, independent of task-authored schema traversal."""
    pending, count = [value], 0
    while pending:
        node = pending.pop()
        count += 1
        if count > 100_000:
            raise ContractError("Custom schema input exceeds bounded JSON node count")
        if isinstance(node, dict):
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return count


class SchemaRegistry:
    """Schema names include versions; an existing name cannot acquire new semantics."""

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._input_only: set[str] = set()
        self._custom_work: dict[str, int] = {}
        self._custom_unique: dict[str, int] = {}

    def register(self, name: str, schema: dict, *, custom: bool = False,
                 input_only: bool = False) -> str:
        if not SCHEMA_NAME.fullmatch(name):
            raise ContractError("Schema names must be versioned identifiers")
        if custom and not name.startswith("custom."):
            raise ContractError("LLM schemas must use the custom. namespace")
        if custom:
            schema = json.loads(canonical(schema))
        expanded_nodes = _check_schema(schema, custom=custom)
        existing = self._schemas.get(name)
        if existing is not None and (digest(existing) != digest(schema)
                                    or (name in self._input_only) != input_only):
            raise ContractError("Schema version already has a different definition")
        # Stable ordering makes first-error repair identities restart-safe.
        self._schemas[name] = json.loads(canonical(schema)) if custom else deepcopy(schema)
        if custom:
            self._custom_work[name] = expanded_nodes
            self._custom_unique[name] = _unique_checks(schema)
        if input_only:
            self._input_only.add(name)
        return name

    def schema(self, name: str) -> dict:
        if name not in self._schemas:
            raise ContractError(f"Unregistered schema: {name}")
        return deepcopy(self._schemas[name])

    def fingerprint(self, name: str) -> str:
        return digest(self.schema(name))

    def validate(self, name: str, value: Any, *, output: bool = False) -> None:
        if output and name in self._input_only:
            raise ContractError("This schema can only be supplied by the task boundary")
        encoded = canonical(value)  # Reject non-JSON / NaN / Infinity, including unconstrained fields.
        schema = self.schema(name)
        if name in self._custom_work:
            nodes = _json_nodes(value)
            work = self._custom_work[name] * nodes + self._custom_unique[name] * nodes * nodes
            # Branch validators can interpolate repr(instance) into every error
            # even when the outer consumer requests only the first error.
            error_bytes = self._custom_work[name] * len(encoded.encode())
            if work > 2_000_000 or error_bytes > 16_000_000:
                raise ContractError("Custom schema/input pair exceeds bounded validation work")
        # Do not enumerate/sort an unbounded set of errors from generated schemas.
        error = next(Draft202012Validator(schema).iter_errors(value), None)
        if error is not None:
            path = "/" + "/".join(str(k) for k in error.absolute_path)
            # Do not echo a large result or input into repair messages.
            message = f"{error.validator} validation failed for schema {name}"
            raise ContractError(message, path=path, code="schema_validation")

    def compatible(self, source: str, destination: str) -> bool:
        # Deliberately nominal, not an unsound general JSON-Schema-subtyping claim.
        self.schema(source)
        self.schema(destination)
        return source == destination

    def catalog(self) -> dict[str, dict]:
        return {k: {"schema": self.schema(k), "sha256": self.fingerprint(k),
                    "input_only": k in self._input_only} for k in sorted(self._schemas)}
