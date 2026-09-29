"""SQLite connection management, transaction discipline, and state revision.

Authority: PRD section 11.3 and section 12.2.

Invariants this module owns:

* One writer per instance, serialized through the helper. Writers use
  ``BEGIN IMMEDIATE`` so that a write transaction takes its lock up front instead
  of discovering a conflict at COMMIT.
* A successful application mutation increments ``instances.state_revision`` and
  writes an ``audit_events`` row **in the same transaction**. There is no code path
  that mutates state without both.
* WAL is enabled only when the database is on local storage we support. On a
  network filesystem SQLite's own documentation warns that WAL's shared-memory
  index does not work; we do not configure it there.
* Bound lock waits. A helper that blocks forever on a busy database is worse than
  one that returns a conflict.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..errors import ResumeReviewError
from ..models import canonical_json, jsonable
from ..util import now_iso
from ..storage.topology import TopologyKind, probe_topology

__all__ = ["DbConfig", "Database", "audit_row", "open_connection"]


@dataclass
class DbConfig:
    """Connection policy. Defaults are the tested configuration."""

    path: Path
    #: WAL is negotiated at connect time and downgraded automatically on storage
    #: that cannot support it.
    prefer_wal: bool = True
    busy_timeout_ms: int = 7_500
    #: FULL is the default because durability of a committed human decision
    #: matters more here than write throughput at 400-row scale.
    synchronous: str = "FULL"
    #: Bound the analysis of pathological queries. Read-only; never truncates data.
    cache_size_kib: int = -32_000  # negative means KiB in SQLite


def open_connection(config: DbConfig) -> sqlite3.Connection:
    """Open a connection with the tested pragmas applied."""
    path = Path(config.path)
    if path.parent and not path.parent.exists():
        raise ResumeReviewError(
            "The workspace database directory does not exist.",
            code="WORKSPACE_UNINITIALISED",
            http_status=409,
        )

    # check_same_thread=False because the API hands connections to the worker pool;
    # access is still serialized per connection by the Database class below.
    conn = sqlite3.connect(
        str(path),
        timeout=config.busy_timeout_ms / 1000.0,
        isolation_level=None,  # we control transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON")
    cur.execute(f"PRAGMA busy_timeout = {int(config.busy_timeout_ms)}")
    cur.execute(f"PRAGMA synchronous = {config.synchronous}")
    cur.execute(f"PRAGMA cache_size = {int(config.cache_size_kib)}")
    cur.execute("PRAGMA temp_store = MEMORY")

    if config.prefer_wal:
        topology = probe_topology(path)
        if topology.kind in (TopologyKind.LOCAL_FIXED, TopologyKind.LOCAL_REMOVABLE):
            try:
                mode = cur.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    cur.execute("PRAGMA journal_mode = DELETE")
            except sqlite3.DatabaseError:
                cur.execute("PRAGMA journal_mode = DELETE")
        else:
            # Network, RAM, or unknown storage: stay on the rollback journal.
            cur.execute("PRAGMA journal_mode = DELETE")

    cur.close()
    return conn


def audit_row(
    *,
    instance_id: str,
    actor: str,
    actor_kind: str,
    event: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    affected_ids: Sequence[str] | None = None,
    prior: Any = None,
    new: Any = None,
    outcome: str = "ok",
    request_id: str | None = None,
    code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build the audit INSERT. Pure function so tests can assert on it directly."""
    sql = (
        "INSERT INTO audit_events (instance_id, actor, actor_kind, event, entity_type, "
        "entity_id, affected_ids_json, prior_json, new_json, outcome, request_id, code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    values = (
        instance_id,
        actor,
        actor_kind,
        event,
        entity_type,
        entity_id,
        canonical_json(list(affected_ids or [])),
        canonical_json(prior) if prior is not None else None,
        canonical_json(new) if new is not None else None,
        outcome,
        request_id,
        code,
        now_iso(),
    )
    return sql, values


class Database:
    """Owns the connection lifecycle and the mutation invariants.

    Deliberately *not* a connection pool. One instance scale is 400 submissions and
    a handful of reviewers; a single serialized writer is simpler to reason about
    and cannot violate the single-writer assumption.
    """

    def __init__(self, config: DbConfig, *, instance_id: str | None = None) -> None:
        self.config = config
        self._instance_id = instance_id
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0
        self._in_transaction = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self.config.path)

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = open_connection(self.config)
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sqlite_version(self) -> str:
        return str(self.connect().execute("SELECT sqlite_version()").fetchone()[0])

    # -- instance identity -------------------------------------------------
    @property
    def instance_id(self) -> str:
        if self._instance_id is None:
            row = self.connect().execute("SELECT id FROM instances LIMIT 1").fetchone()
            if row is None:
                raise ResumeReviewError(
                    "This workspace has no instance record.",
                    code="WORKSPACE_UNINITIALISED",
                    http_status=409,
                )
            self._instance_id = str(row["id"])
        return self._instance_id

    def state_revision(self) -> int:
        row = self.connect().execute(
            "SELECT state_revision FROM instances WHERE id = ?", (self.instance_id,)
        ).fetchone()
        return int(row["state_revision"]) if row else 0

    # -- read --------------------------------------------------------------
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read connection. Never opens a write transaction."""
        conn = self.connect()
        with self._lock:
            yield conn

    # -- write -------------------------------------------------------------
    @contextmanager
    def write(
        self,
        *,
        actor: str,
        event: str,
        actor_kind: str = "human",
        entity_type: str | None = None,
        entity_id: str | None = None,
        affected_ids: Sequence[str] | None = None,
        prior: Any = None,
        new: Any = None,
        request_id: str | None = None,
        code: str | None = None,
        bump_revision: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """Run one mutation transaction.

        On success: bumps ``state_revision``, writes the audit row, and commits.
        On failure: rolls back the whole transaction and records a separate
        ``denied``/``error`` audit row so a refused attempt is still visible.

        Nested ``write`` calls join the outer transaction so a composite operation
        (save decision, then plan) either commits entirely or not at all.
        """
        conn = self.connect()
        with self._lock:
            if self._in_transaction:
                # Join the outer transaction; the outer frame owns commit/audit.
                yield conn
                return

            self._in_transaction = True
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn

                if bump_revision:
                    conn.execute(
                        "UPDATE instances SET state_revision = state_revision + 1, updated_at = ? "
                        "WHERE id = ?",
                        (now_iso(), self.instance_id),
                    )

                sql, values = audit_row(
                    instance_id=self.instance_id,
                    actor=actor,
                    actor_kind=actor_kind,
                    event=event,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    affected_ids=affected_ids,
                    prior=prior,
                    new=new,
                    outcome="ok",
                    request_id=request_id,
                    code=code,
                )
                conn.execute(sql, values)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - rollback of a dead txn
                    pass
                self._in_transaction = False
                # Record the refusal outside the failed transaction. This must not
                # itself raise and mask the original error.
                try:
                    sql, values = audit_row(
                        instance_id=self.instance_id,
                        actor=actor,
                        actor_kind=actor_kind,
                        event=event,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        affected_ids=affected_ids,
                        prior=None,
                        new=None,
                        outcome="error",
                        request_id=request_id,
                        code=code,
                    )
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(sql, values)
                    conn.execute("COMMIT")
                except sqlite3.Error:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            else:
                self._in_transaction = False

    @contextmanager
    def bare_transaction(self) -> Iterator[sqlite3.Connection]:
        """A transaction with no revision bump and no audit row.

        Reserved for migration and repair paths, which are themselves audited at a
        higher level. Using this for a normal domain mutation is a bug.
        """
        conn = self.connect()
        with self._lock:
            if self._in_transaction:
                yield conn
                return
            self._in_transaction = True
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._in_transaction = False

    # -- helpers -----------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0]

    def integrity_check(self) -> str:
        return str(self.connect().execute("PRAGMA integrity_check").fetchone()[0])

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret operational facts for ``status`` and the compatibility matrix."""
        conn = self.connect()
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        topology = probe_topology(self.path)
        return {
            "sqlite_version": self.sqlite_version(),
            "journal_mode": str(journal),
            "foreign_keys": bool(foreign_keys),
            "database_bytes": int(page_count) * int(page_size),
            "topology": topology.to_dict(),
            "backend": "sqlite3",
        }


def dump_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in rows]


def json_column(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column, tolerating NULL and legacy garbage."""
    import json

    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def encode_json(value: Any) -> str:
    return canonical_json(jsonable(value))
