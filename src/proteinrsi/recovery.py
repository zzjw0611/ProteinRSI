# SPDX-License-Identifier: MIT
"""Explicit, audited retry allowance for known failed provider requests."""
from proteinrsi.llm import JSONLLM
from proteinrsi.storage import Conflict, BudgetExceeded


def authorize_retry(store, request_key, *, operator, reason, attempts=1):
    if not operator.strip() or not reason.strip() or type(attempts) is not int or not 1 <= attempts <= 4:
        raise ValueError("Provide operator/reason and 1–4 additional attempts")
    with store.lock(), store.transaction():
        call = store.get("llm", request_key)
        if not call or call.get("state") != "failed" or not JSONLLM._retryable(call):
            raise Conflict("Only known retryable failed LLM calls can be authorized; uncertain started calls require reconciliation")
        previous = store.get("llm_retry_authorizations", request_key, {})
        used = call.get("attempt", 1)
        if previous.get("attempt_limit", 0) > used:
            raise Conflict("This request already has an unused retry allowance")
        if attempts > store.remaining("llm_calls"):
            raise BudgetExceeded("Additional attempts must fit the remaining LLM budget")
        record = {"request_key": request_key, "attempt_limit": used + attempts,
            "after_attempt": used, "additional_attempts": attempts,
            "operator": operator, "reason": reason,
            "notice": "No call is sent or refunded; repeat the original operation with its checkpoints."}
        store.put("llm_retry_authorizations", request_key, record)
        store.event("llm_retry_authorized", record)
        return record
