# SPDX-License-Identifier: MIT
"""Durable bounded repair of a response contract, never of a scientific verdict."""
from pydantic import ValidationError

from .resources import NAMESPACE
from .schema import ContractError


def validated_response(store, llm, *, key, role, instructions, context, schema,
                       validate, max_repairs=2):
    errors = []
    previous = None
    for attempt in range(max_repairs + 1):
        attempt_key = f"{key}/response/{attempt}"
        receipt = store.get(NAMESPACE, attempt_key)
        if receipt is None:
            request = dict(context)
            if errors:
                request["response_repair"] = {"attempt": attempt, "errors": errors,
                    "previous_response": previous}
            raw = llm.complete(role, instructions, request, schema)
            receipt = {"status": "returned", "raw": raw}
            store.put(NAMESPACE, attempt_key, receipt)
        raw = receipt["raw"]
        try:
            result = validate(raw)
        except (ValidationError, ContractError) as exc:
            errors = ([exc.detail()] if isinstance(exc, ContractError) else
                      exc.errors(include_url=False, include_context=False, include_input=False))
            previous = raw
            store.put(NAMESPACE, attempt_key, {**receipt, "status": "invalid", "errors": errors})
        else:
            store.put(NAMESPACE, attempt_key, {**receipt, "status": "validated"})
            return result
    raise ContractError("Response exhausted its contract repair limit", code="response_invalid")
