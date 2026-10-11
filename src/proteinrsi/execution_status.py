# SPDX-License-Identifier: MIT
"""Audit-derived execution health, distinct from campaign phase and process liveness."""
import json


def execution_status(connection, phase):
    row = connection.execute("""SELECT id,kind,payload FROM events WHERE kind IN
        ('run_started','run_failed','research_blocked','team_completed',
         'waiting_for_laboratory') ORDER BY id DESC LIMIT 1""").fetchone()
    if row is None:
        return {"state": "complete" if phase == "complete" else "not_started"}
    event_id, kind, payload = row
    detail = json.loads(payload)
    state = ("blocked" if kind in ("run_failed", "research_blocked") else
             "waiting_for_laboratory" if kind == "waiting_for_laboratory" else
             "complete" if phase == "complete" else "in_progress_or_interrupted")
    result = {"state": state, "event_id": event_id, "event": kind,
              "error_type": detail.get("error_type"), "liveness": "not_checked"}
    if state == "blocked":
        step = connection.execute("""SELECT kind,payload FROM events WHERE id <= ?
            AND kind IN ('run_started','research_step_started','research_step_completed')
            ORDER BY id DESC LIMIT 1""", (event_id,)).fetchone()
        if step and step[0] == "research_step_started":
            payload = json.loads(step[1])
            result["unfinished_step"] = payload.get("step_id")
            result["operation"] = payload.get("operation")
    return result
