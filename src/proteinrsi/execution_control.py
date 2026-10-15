# SPDX-License-Identifier: MIT
"""Operator-only process suspension; scientific records and receipts stay intact."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3

from proteinrsi.contracts import digest
from proteinrsi.storage import Conflict


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": int(pid), "parent": int(fields[1]), "start_ticks": int(fields[19]),
                "state": fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def register_controller(store):
    from proteinrsi.governance import _packaged_source
    identity = process_identity(os.getpid())
    if identity is None:
        raise Conflict("Process identity unavailable")
    with store.lock():
        old = store.get("execution_control", "controller")
        live = process_identity(old["pid"]) if old else None
        if (old and old.get("running") and live and live["state"] != "Z"
                and live["start_ticks"] == old["start_ticks"] and live["pid"] != os.getpid()):
            raise Conflict("Another controller is running this campaign")
        store.put("execution_control", "controller",
                  {**identity, "running": True, "source_ref": digest(_packaged_source())})


def unregister_controller(store):
    with store.lock():
        current = store.get("execution_control", "controller")
        if current and current["pid"] == os.getpid():
            store.put("execution_control", "controller", {**current, "running": False})


def _registry(directory):
    path = (Path(directory) / "state.sqlite3").resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        row = con.execute("SELECT value FROM kv WHERE namespace='execution_control' "
                          "AND key='controller'").fetchone()
    if not row:
        raise Conflict("No registered controller; inspect this historical run's operator pause record")
    return json.loads(row[0])


def _signal(identity, action):
    live = process_identity(identity["pid"])
    if live and live["start_ticks"] == identity["start_ticks"] and live["state"] != "Z":
        try:
            os.kill(identity["pid"], action)
            return True
        except ProcessLookupError:
            pass
    return False


def _save(directory, record):
    from proteinrsi.trace_export import _atomic_text
    with _atomic_text(Path(directory) / "operator_pause.json") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def pause_campaign(directory):
    root = _registry(directory)
    if root["pid"] == os.getpid() or not root.get("running"):
        raise Conflict("No external running controller to pause")
    stopped, pending = [], [root]
    try:
        while pending:
            current = pending.pop()
            if not _signal(current, signal.SIGSTOP):
                continue
            stopped.append(current)
            # Stop each parent before enumerating its children: it cannot start
            # additional workers while the subtree is being captured.
            for entry in Path("/proc").iterdir():
                if entry.name.isdigit():
                    child = process_identity(int(entry.name))
                    if child and child["parent"] == current["pid"]:
                        pending.append(child)
        if not stopped:
            raise Conflict("Registered controller has exited")
        record = {"mechanism": "SIGSTOP", "status": "paused", "processes": stopped,
                  "paused_at": datetime.now(timezone.utc).isoformat(), "source_ref": root["source_ref"]}
        _save(directory, record)
        return record
    except BaseException:
        for identity in reversed(stopped):
            _signal(identity, signal.SIGCONT)
        raise


def resume_campaign(directory):
    from proteinrsi.governance import _packaged_source
    record = json.loads((Path(directory) / "operator_pause.json").read_text())
    if record.get("status") != "paused" or record.get("source_ref") != digest(_packaged_source()):
        raise Conflict("Resume requires the paused run's original source and environment")
    resumed = sum(_signal(identity, signal.SIGCONT) for identity in reversed(record["processes"]))
    record.update(status="resumed", resumed_at=datetime.now(timezone.utc).isoformat(), resumed_processes=resumed)
    _save(directory, record)
    return record
