# SPDX-License-Identifier: MIT
"""SQLite persistence, append-only audit events and idempotent budget accounting."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
import json

from proteinrsi.contracts import canonical, digest


class BudgetExceeded(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.connect() as con:
            con.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (namespace TEXT, key TEXT, value TEXT,
                PRIMARY KEY(namespace,key));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL, kind TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS limits (resource TEXT PRIMARY KEY, amount INTEGER);
            CREATE TABLE IF NOT EXISTS charges (key TEXT PRIMARY KEY, resource TEXT,
                amount INTEGER, fingerprint TEXT, state TEXT);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """One campaign writer at a time. POSIX/WSL; not an OS sandbox."""
        import fcntl
        with (self.root / ".campaign.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        with self.connect() as con:
            row = con.execute("SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, namespace: str, key: str, value: Any, *, immutable: bool = False) -> None:
        encoded = canonical(value)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)).fetchone()
            if immutable and row and row["value"] != encoded:
                raise Conflict(f"Cannot overwrite immutable {namespace}/{key}")
            con.execute("INSERT INTO kv VALUES (?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
                        (namespace, key, encoded))

    def all(self, namespace: str) -> dict[str, Any]:
        with self.connect() as con:
            return {row["key"]: json.loads(row["value"]) for row in con.execute(
                "SELECT key,value FROM kv WHERE namespace=? ORDER BY key", (namespace,))}

    def event(self, kind: str, payload: dict) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO events(timestamp,kind,payload) VALUES (?,?,?)",
                        (time.time(), kind, canonical(payload)))

    def events(self) -> list[dict]:
        with self.connect() as con:
            return [{"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])}
                    for row in con.execute("SELECT * FROM events ORDER BY id")]

    def configure_budget(self, resources: dict[str, int]) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = dict(con.execute("SELECT resource,amount FROM limits").fetchall())
            if existing and existing != resources:
                raise Conflict("Cannot reset an existing campaign budget")
            for key, amount in resources.items():
                if type(amount) is not int or amount < 0:
                    raise ValueError("Resource limits must be nonnegative integers")
                con.execute("INSERT OR IGNORE INTO limits VALUES (?,?)", (key, amount))

    def reserve(self, key: str, resource: str, amount: int, payload: Any) -> None:
        if type(amount) is not int or amount < 0:
            raise ValueError("Negative/fractional resource charge")
        fingerprint = digest(payload)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM charges WHERE key=?", (key,)).fetchone()
            if old:
                if (old["resource"], old["amount"], old["fingerprint"]) != (resource, amount, fingerprint):
                    raise Conflict("Idempotency key reused for a different request")
                if old["state"] == "released":
                    raise Conflict("Released reservations cannot be resurrected")
                return
            limit = con.execute("SELECT amount FROM limits WHERE resource=?", (resource,)).fetchone()
            if limit is None:
                raise ValueError(f"Unconfigured resource: {resource}")
            used = con.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE resource=? AND state!='released'",
                               (resource,)).fetchone()[0]
            if used + amount > limit[0]:
                raise BudgetExceeded(f"{resource}: {used}+{amount} exceeds {limit[0]}")
            con.execute("INSERT INTO charges VALUES (?,?,?,?,?)", (key, resource, amount, fingerprint, "reserved"))

    def settle(self, key: str, *, release: bool = False) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state FROM charges WHERE key=?", (key,)).fetchone()
            if not row:
                raise KeyError(key)
            state = "released" if release else "committed"
            if row[0] == state:
                return
            if row[0] != "reserved":
                raise Conflict("Committed resource usage cannot be rolled back")
            con.execute("UPDATE charges SET state=? WHERE key=?", (state, key))

    def remaining(self, resource: str) -> int:
        with self.connect() as con:
            limit = con.execute("SELECT amount FROM limits WHERE resource=?", (resource,)).fetchone()
            if limit is None:
                raise KeyError(resource)
            used = con.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE resource=? AND state!='released'",
                               (resource,)).fetchone()[0]
            return limit[0] - used

    def usage(self) -> dict[str, dict[str, int]]:
        with self.connect() as con:
            result = {}
            for row in con.execute("SELECT resource,amount FROM limits"):
                counts = {r[0]: r[1] for r in con.execute(
                    "SELECT state,SUM(amount) FROM charges WHERE resource=? GROUP BY state", (row[0],))}
                result[row[0]] = {"limit": row[1], "committed": counts.get("committed", 0),
                                  "reserved": counts.get("reserved", 0)}
            return result


class SponsoredStore(Store):
    """Private child caches, local comparison caps, but all actual resources charge a study sponsor.

    Reservation IDs are stable per evaluation arm. No refund after execution. This
    is trusted controller code, not a worker ability to choose its budget sponsor.
    """
    def __init__(self, directory, sponsor: Store, prefix: str):
        super().__init__(directory)
        self.sponsor, self.prefix = sponsor, prefix

    def reserve(self, key, resource, amount, payload):
        sponsor_key = "meta-" + self.prefix + "-" + key
        # Local reservation enforces equal per-case limits before using global budget.
        super().reserve(key, resource, amount, payload)
        try:
            self.sponsor.reserve(sponsor_key, resource, amount,
                                 {"evaluation_arm": self.prefix, "request": payload})
        except Exception:
            with self.connect() as con:
                state = con.execute("SELECT state FROM charges WHERE key=?", (key,)).fetchone()
            if state and state[0] == "reserved":
                super().settle(key, release=True)
            raise

    def settle(self, key, *, release=False):
        self.sponsor.settle("meta-" + self.prefix + "-" + key, release=release)
        super().settle(key, release=release)

    def remaining(self, resource):
        return min(super().remaining(resource), self.sponsor.remaining(resource))
