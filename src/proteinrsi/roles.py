# SPDX-License-Identifier: MIT
"""Logical role names without rewriting historical provider/cache identifiers."""
ROLE_NAMES = {"A": "Planner", "B": "Executor", "C": "Analyst", "M": "Improver", "E": "Evaluator"}
PROMPT_ALIASES = {"planner": "protocol_planner", "executor": "protocol_step",
                  "improver": "meta", "evaluator": "evaluation_verdict"}


def role_name(identifier: str) -> str:
    return ROLE_NAMES.get(identifier.split("-", 1)[0], identifier)
