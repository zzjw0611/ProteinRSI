# SPDX-License-Identifier: MIT
"""SQLite persistence, append-only audit events and idempotent budget accounting."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import threading
import logging
from typing import Any, Iterator
import json

from proteinrsi.contracts import canonical, digest


class BudgetExceeded(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, directory: str | Path):
        self.event_sink = None
        self._transaction = threading.local()
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
            CREATE TABLE IF NOT EXISTS allocations (allocation_id TEXT, resource TEXT,
                amount INTEGER NOT NULL, state TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(allocation_id, resource));
            CREATE TABLE IF NOT EXISTS charge_allocations (key TEXT PRIMARY KEY, allocation_id TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._transaction, "connection", None)
        if active is not None:
            yield active
            return
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomically persist controller state and its audit; never hold across remote calls.

        Nested transactions use savepoints. Events reach UI sinks only after the
        outer commit, so a failed transition cannot appear as a published version.
        This capability is deliberately absent from the research worker RPC.
        """
        active = getattr(self._transaction, "connection", None)
        if active is not None:
            depth = self._transaction.depth + 1
            name = f"controller_{depth}"
            count = len(self._transaction.events)
            self._transaction.depth = depth
            active.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                active.execute(f"ROLLBACK TO {name}")
                del self._transaction.events[count:]
                raise
            finally:
                active.execute(f"RELEASE {name}")
                self._transaction.depth -= 1
            return
        events = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._transaction.connection = con
            self._transaction.events = events
            self._transaction.depth = 0
            try:
                yield
            finally:
                del self._transaction.connection
                del self._transaction.events
                del self._transaction.depth
        for event in events:
            self._emit_event(event)

    @staticmethod
    def _begin(con: sqlite3.Connection) -> None:
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")

    def _emit_event(self, event: dict) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                # A display failure must not make a committed action look retryable.
                logging.getLogger(__name__).exception("Audit sink failed after database commit")

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
            self._begin(con)
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
        timestamp = time.time()
        with self.connect() as con:
            cursor = con.execute("INSERT INTO events(timestamp,kind,payload) VALUES (?,?,?)",
                                 (timestamp, kind, canonical(payload)))
            event_id = cursor.lastrowid
        event = {"id": event_id, "timestamp": timestamp, "kind": kind, "payload": payload}
        if getattr(self._transaction, "connection", None) is not None:
            self._transaction.events.append(event)
        else:
            self._emit_event(event)

    def events(self) -> list[dict]:
        with self.connect() as con:
            return [{"id": row["id"], "timestamp": row["timestamp"], "kind": row["kind"], "payload": json.loads(row["payload"])}
                    for row in con.execute("SELECT * FROM events ORDER BY id")]

    def configure_budget(self, resources: dict[str, int | None]) -> None:
        with self.connect() as con:
            self._begin(con)
            existing = dict(con.execute("SELECT resource,amount FROM limits").fetchall())
            if existing and existing != resources:
                raise Conflict("Cannot reset an existing campaign budget")
            for key, amount in resources.items():
                if not (amount is None and key in {"llm_calls", "tool_calls"}) and (type(amount) is not int or amount < 0):
                    raise ValueError("Resource limits must be nonnegative integers; only LLM/tool limits may be null")
                con.execute("INSERT OR IGNORE INTO limits VALUES (?,?)", (key, amount))

    def reserve(self, key: str, resource: str, amount: int, payload: Any) -> None:
        if type(amount) is not int or amount < 0:
            raise ValueError("Negative/fractional resource charge")
        fingerprint = digest(payload)
        with self.connect() as con:
            self._begin(con)
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
            held = self._held(con, resource)
            if limit[0] is not None and used + held + amount > limit[0]:
                raise BudgetExceeded(f"{resource}: {used}+{held} held+{amount} exceeds {limit[0]}")
            con.execute("INSERT INTO charges VALUES (?,?,?,?,?)", (key, resource, amount, fingerprint, "reserved"))

    def settle(self, key: str, *, release: bool = False) -> None:
        with self.connect() as con:
            self._begin(con)
            row = con.execute("SELECT state FROM charges WHERE key=?", (key,)).fetchone()
            if not row:
                raise KeyError(key)
            state = "released" if release else "committed"
            if row[0] == state:
                return
            if row[0] != "reserved":
                raise Conflict("Committed resource usage cannot be rolled back")
            con.execute("UPDATE charges SET state=? WHERE key=?", (state, key))

    def remaining(self, resource: str) -> int | None:
        with self.connect() as con:
            limit = con.execute("SELECT amount FROM limits WHERE resource=?", (resource,)).fetchone()
            if limit is None:
                raise KeyError(resource)
            used = con.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE resource=? AND state!='released'",
                               (resource,)).fetchone()[0]
            return None if limit[0] is None else limit[0] - used - self._held(con, resource)

    def usage(self) -> dict[str, dict[str, int | None]]:
        with self.connect() as con:
            result = {}
            for row in con.execute("SELECT resource,amount FROM limits"):
                counts = {r[0]: r[1] for r in con.execute(
                    "SELECT state,SUM(amount) FROM charges WHERE resource=? GROUP BY state", (row[0],))}
                result[row[0]] = {"limit": row[1], "committed": counts.get("committed", 0),
                                  "reserved": counts.get("reserved", 0)}
                held = self._held(con, row[0])
                if held:
                    result[row[0]]["allocated_unspent"] = held
            return result


    @staticmethod
    def _allocation_used(con, allocation_id, resource):
        return con.execute("""SELECT COALESCE(SUM(c.amount),0) FROM charges c
            JOIN charge_allocations a ON c.key=a.key
            WHERE a.allocation_id=? AND c.resource=? AND c.state!='released'""",
            (allocation_id, resource)).fetchone()[0]

    @classmethod
    def _held(cls, con, resource):
        # Actual charges already count in usage; hold only the unspent allocation.
        return sum(row["amount"] - cls._allocation_used(con, row["allocation_id"], resource)
                   for row in con.execute("SELECT * FROM allocations WHERE resource=? AND state='active'",
                                          (resource,)))

    def reserve_allocations(self, group_id: str, allocations: dict[str, dict[str, int]],
                            *, keep_free: dict[str, int] | None = None) -> None:
        """Atomically hold both arms' finite caps before either arm can execute.

        This is controller-only. Repeating the identical group is idempotent;
        changing it or resurrecting a released group is forbidden.
        """
        if not group_id or not allocations:
            raise ValueError("Nonempty allocation group required")
        keep_free = keep_free or {}
        record = {"allocations": allocations, "keep_free": keep_free}
        if any(type(v) is not int or v < 0 for limits in allocations.values() for v in limits.values()):
            raise ValueError("Allocations require finite nonnegative integer caps")
        if any(type(v) is not int or v < 0 for v in keep_free.values()):
            raise ValueError("Invalid main-study headroom")
        with self.transaction():
            saved = self.get("budget_allocation_groups", group_id)
            if saved is not None:
                if saved != record:
                    raise Conflict("Evaluation allocation changed after freezing")
                with self.connect() as con:
                    for name, limits in allocations.items():
                        for resource, amount in limits.items():
                            row = con.execute("SELECT * FROM allocations WHERE allocation_id=? AND resource=?",
                                              (name, resource)).fetchone()
                            if not row or row["state"] != "active" or row["amount"] != amount:
                                raise Conflict("Allocation was released or changed")
                return
            resources = set(keep_free) | {r for caps in allocations.values() for r in caps}
            for resource in resources:
                required = sum(caps.get(resource, 0) for caps in allocations.values())
                available = self.remaining(resource)
                if available is not None and required + keep_free.get(resource, 0) > available:
                    raise BudgetExceeded(f"{resource}: paired allocation plus main headroom exceeds available budget")
            with self.connect() as con:
                for name, limits in allocations.items():
                    for resource, amount in limits.items():
                        if con.execute("SELECT 1 FROM allocations WHERE allocation_id=? AND resource=?",
                                       (name, resource)).fetchone():
                            raise Conflict("Allocation ID belongs to another group")
                        con.execute("INSERT INTO allocations VALUES (?,?,?,?,?)",
                                    (name, resource, amount, "active", digest(record)))
            self.put("budget_allocation_groups", group_id, record, immutable=True)
            self.event("evaluation_budget_reserved", {"group_id": group_id, **record})

    def allocation_remaining(self, allocation_id, resource):
        with self.connect() as con:
            row = con.execute("SELECT * FROM allocations WHERE allocation_id=? AND resource=?",
                              (allocation_id, resource)).fetchone()
            if row is None:
                raise KeyError((allocation_id, resource))
            if row["state"] != "active":
                return 0
            return row["amount"] - self._allocation_used(con, allocation_id, resource)

    def reserve_allocated(self, allocation_id, key, resource, amount, payload):
        """Convert held capacity to an idempotent real-call reservation, without double counting."""
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid allocated charge")
        with self.transaction(), self.connect() as con:
            old = con.execute("SELECT * FROM charges WHERE key=?", (key,)).fetchone()
            member = con.execute("SELECT allocation_id FROM charge_allocations WHERE key=?", (key,)).fetchone()
            if old:
                if (old["resource"], old["amount"], old["fingerprint"]) != (resource, amount, digest(payload)) or not member or member[0] != allocation_id:
                    raise Conflict("Allocated idempotency key reused for another request")
                if old["state"] == "released":
                    raise Conflict("Released call cannot be repeated under the same key")
                return
            if self.allocation_remaining(allocation_id, resource) < amount:
                raise BudgetExceeded(f"{resource}: branch allocation exhausted")
            # Allocation existence/state is checked explicitly even for zero-cost calls.
            row = con.execute("SELECT state FROM allocations WHERE allocation_id=? AND resource=?",
                              (allocation_id, resource)).fetchone()
            if row is None or row[0] != "active":
                raise Conflict("No active allocation")
            con.execute("INSERT INTO charges VALUES (?,?,?,?,?)",
                        (key, resource, amount, digest(payload), "reserved"))
            con.execute("INSERT INTO charge_allocations VALUES (?,?)", (key, allocation_id))

    def release_allocations(self, group_id):
        """Release unused headroom only; never refund committed or uncertain external work."""
        with self.transaction():
            record = self.get("budget_allocation_groups", group_id)
            if record is None:
                raise KeyError(group_id)
            with self.connect() as con:
                for name in record["allocations"]:
                    pending = con.execute("""SELECT 1 FROM charges c JOIN charge_allocations a ON c.key=a.key
                        WHERE a.allocation_id=? AND c.state='reserved' LIMIT 1""", (name,)).fetchone()
                    if pending:
                        raise Conflict("Reconcile reserved external work before releasing allocation")
                changed = 0
                for name in record["allocations"]:
                    changed += con.execute("UPDATE allocations SET state='released' WHERE allocation_id=? AND state='active'", (name,)).rowcount
            if changed:
                self.event("evaluation_budget_released", {"group_id": group_id, "committed_costs_preserved": True})


class SponsoredStore(Store):
    """Private child caches, local comparison caps, but all actual resources charge a study sponsor.

    Reservation IDs are stable per evaluation arm. No refund after execution. This
    is trusted controller code, not a worker ability to choose its budget sponsor.
    """
    def __init__(self, directory, sponsor: Store, prefix: str, *, allocation_id: str | None = None):
        super().__init__(directory)
        self.sponsor, self.prefix = sponsor, prefix
        self.allocation_id = allocation_id

    def reserve(self, key, resource, amount, payload):
        sponsor_key = "meta-" + self.prefix + "-" + key
        # No cross-database transaction is claimed: sponsor-first side effects use
        # stable IDs, so a crash before local commit replays the same reservation.
        with self.transaction():
            super().reserve(key, resource, amount, payload)
            request = {"evaluation_arm": self.prefix, "request": payload}
            if self.allocation_id is None:
                self.sponsor.reserve(sponsor_key, resource, amount, request)
            else:
                self.sponsor.reserve_allocated(self.allocation_id, sponsor_key, resource, amount, request)

    def settle(self, key, *, release=False):
        self.sponsor.settle("meta-" + self.prefix + "-" + key, release=release)
        super().settle(key, release=release)

    def remaining(self, resource):
        sponsor_remaining = (self.sponsor.allocation_remaining(self.allocation_id, resource)
                             if self.allocation_id else self.sponsor.remaining(resource))
        finite = [n for n in (super().remaining(resource), sponsor_remaining) if n is not None]
        return min(finite) if finite else None

    def event(self, kind, payload):
        super().event(kind, payload)
        self.sponsor.event("validation_event", {"branch": self.prefix, "kind": kind, "payload": payload})

    def put(self, namespace, key, value, *, immutable=False):
        if namespace == "task_predictions":
            # Frozen main-campaign samples may reference a descendant's predictions.
            # Publish the exact artifact, not an audit wrapper or new prediction.
            # This is controller-only: worker RPC still cannot write this namespace.
            if not isinstance(value, dict) or key != digest(value):
                raise ValueError("Task predictions must use a content-addressed artifact key")
            # Check the sponsor first; never replace conflicting evidence in either store.
            self.sponsor.put(namespace, key, value, immutable=True)
            super().put(namespace, key, value, immutable=True)
            return
        super().put(namespace, key, value, immutable=immutable)
        # These namespaces are operator-only; the worker RPC cannot read them.
        if namespace in {"context_evidence", "context_sessions", "context_frames", "context_reads", "research_notes", "llm", "llm_attempts", "llm_preflights", "tool_jobs", "agent_snapshots", "research_runs", "research_step_outputs", "code_programs", "feedback_inputs", "feedback_results", "batches", "measurements", "task_reports", "task_loop_results"}:
            self.sponsor.put("validation_"+namespace, self.prefix+"/"+key,
                             {"branch": self.prefix, **(value if isinstance(value, dict) else {"value": value})}, immutable=immutable)


class AllocatedBudgetView:
    """Controller facade: same evaluation records, separately reserved E-call caps."""
    def __init__(self, store: Store, allocation_id: str):
        self.store, self.allocation_id = store, allocation_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    def reserve(self, key, resource, amount, payload):
        return self.store.reserve_allocated(self.allocation_id, key, resource, amount, payload)

    def remaining(self, resource):
        return self.store.allocation_remaining(self.allocation_id, resource)
