"""Crash-safe, durable per-operation journal on disk.

Authority: PRD sections 13.2 and 13.3.

Where the journal lives
-----------------------
``<root>/.review/journals/<batch-id>.json`` (see
``skill/references/workspace-layout.md``). It is a per-batch file holding one
record per planned operation with its durable step.

What the journal guarantees
---------------------------
* **Intent before the filesystem.** :meth:`Journal.record_intent` persists the
  operation's intent durably *before* any executor is allowed to touch a file
  (PRD 13.2). The ordered progression is ``planned -> intent_recorded ->
  file_moved -> committed``, with ``needs_reconciliation``/``failed``/``skipped``
  as side states. An out-of-order transition (for example ``file_moved`` before
  ``intent_recorded``) is refused in code with
  :class:`JournalOrderError`.
* **Atomic and durable writes.** Every write goes to a temporary file in the same
  directory, is flushed and ``fsync``-ed, then ``os.replace``-d over the journal,
  then the directory is ``fsync``-ed (best effort on Windows, which has no
  directory fsync). A crash can never leave a half-written journal.
* **Loadable after a restart.** :func:`load_journal` reconstructs the recorded
  operations and their states. A corrupt or truncated file is *reported* with
  ``corrupt=True`` and ``needs_reconciliation=True`` rather than raised away or
  silently ignored.

The journal is a recovery aid, never the authority
--------------------------------------------------
``.review/review.db`` is the authoritative record. The journal exists so that
after a crash the helper can compare what it *intended* against what the database
*committed* and what the filesystem *shows*. :func:`reconcile_with_database`
therefore always treats the database's ``file_operations`` rows as authoritative
and marks any disagreement as requiring reconciliation. This module never deletes
a file, and its only filesystem write is the replacement of its own journal file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import Code, Conflict, NotFound
from ..models import REVIEW_DIR, OperationState

JOURNAL_SCHEMA_VERSION = "1.0"

#: Subdirectory of ``.review`` that holds the per-batch journals.
_JOURNAL_SUBDIR = "journals"

#: The ordered progression. A journal may not skip a step or move backwards.
OPERATION_ORDER: tuple[OperationState, ...] = (
    OperationState.PLANNED,
    OperationState.INTENT_RECORDED,
    OperationState.FILE_MOVED,
    OperationState.COMMITTED,
)

#: Side states reachable from anywhere; recovery may resume the ordered path from one.
SIDE_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.NEEDS_RECONCILIATION,
        OperationState.FAILED,
        OperationState.SKIPPED,
    }
)

#: A move to ``file_moved`` is only legal directly from ``intent_recorded``; a move
#: to ``committed`` only from ``file_moved``. This is what makes the order enforced.
_ALLOWED_PREDECESSORS: Mapping[OperationState, frozenset[OperationState]] = {
    OperationState.PLANNED: frozenset({OperationState.PLANNED}),
    OperationState.INTENT_RECORDED: frozenset(
        {OperationState.PLANNED, OperationState.INTENT_RECORDED}
    ),
    OperationState.FILE_MOVED: frozenset(
        {OperationState.INTENT_RECORDED, OperationState.FILE_MOVED}
    ),
    OperationState.COMMITTED: frozenset(
        {OperationState.FILE_MOVED, OperationState.COMMITTED}
    ),
}

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


class JournalOrderError(Conflict):
    """An attempt to move an operation backwards or to skip a required step."""

    code = Code.NEEDS_RECONCILIATION


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JournalOperation:
    operation_id: str
    document_id: str
    kind: str | None
    source: str
    destination: str
    state: str
    detail: str = ""
    history: tuple[Mapping[str, Any], ...] = ()

    @property
    def state_enum(self) -> OperationState:
        return OperationState(self.state)


@dataclass(frozen=True)
class JournalSnapshot:
    """An immutable read of a journal file. Never authoritative on its own."""

    batch_id: str
    instance_id: str = ""
    plan_hash: str = ""
    schema_version: str = JOURNAL_SCHEMA_VERSION
    exists: bool = False
    corrupt: bool = False
    needs_reconciliation: bool = False
    detail: str = ""
    operations: tuple[JournalOperation, ...] = ()
    created_at: str | None = None
    updated_at: str | None = None

    def by_id(self) -> dict[str, JournalOperation]:
        return {op.operation_id: op for op in self.operations}

    def states(self) -> dict[str, str]:
        return {op.operation_id: op.state for op in self.operations}


@dataclass(frozen=True)
class ReconciliationItem:
    operation_id: str
    journal_state: str | None
    authoritative_state: str | None
    needs_reconciliation: bool
    reason: str


@dataclass(frozen=True)
class ReconciliationReport:
    """Result of comparing a journal snapshot against the authoritative database."""

    batch_id: str
    items: tuple[ReconciliationItem, ...] = ()
    needs_reconciliation: bool = False
    corrupt: bool = False

    @property
    def disagreeing(self) -> tuple[ReconciliationItem, ...]:
        return tuple(item for item in self.items if item.needs_reconciliation)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def journal_dir(root: str | os.PathLike[str]) -> Path:
    """The ``.review/journals`` directory for a registered root."""
    return Path(root) / REVIEW_DIR / _JOURNAL_SUBDIR


def journal_path(root: str | os.PathLike[str], batch_id: str) -> Path:
    """``<root>/.review/journals/<batch-id>.json``.

    The batch id must be a single safe path component; a request that would escape
    the journals directory is refused rather than sanitized into a different name.
    """
    if not _BATCH_ID_RE.match(str(batch_id)):
        raise Conflict(
            "The batch id is not a safe single path component.",
            code=Code.INVALID_INPUT,
            detail={"field": "batch_id"},
        )
    return journal_dir(root) / f"{batch_id}.json"


# ---------------------------------------------------------------------------
# Durable atomic write
# ---------------------------------------------------------------------------
def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so a rename survives a power loss.

    Windows offers no directory fsync; there the file fsync before ``os.replace``
    is the strongest guarantee available. Silently skipped where unsupported.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _durable_replace(path: Path, payload: bytes) -> None:
    """Atomically replace ``path`` with ``payload``, durably.

    Temp file in the same directory -> write -> flush -> ``fsync`` -> ``os.replace``
    -> directory ``fsync``. A crash before the replace leaves the previous journal
    intact; a crash after it leaves the new one complete. There is never a
    half-written journal at ``path``.

    On a failed write the temporary file is deliberately left in place: this module
    never deletes a file, and a stray ``.tmp-*`` sibling is harmless because
    :func:`load_journal` only ever reads the journal path itself.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(directory)


def _encode(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse_operations(raw: Any) -> tuple[tuple[JournalOperation, ...], bool]:
    """Parse the operation map. Returns ``(operations, corrupt)``."""
    if not isinstance(raw, Mapping):
        return (), True
    operations: list[JournalOperation] = []
    corrupt = False
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            corrupt = True
            continue
        state = str(value.get("state", ""))
        operation_id = str(value.get("operation_id") or key)
        if state not in {s.value for s in OperationState}:
            corrupt = True
            continue
        history = value.get("history")
        operations.append(
            JournalOperation(
                operation_id=operation_id,
                document_id=str(value.get("document_id", "")),
                kind=(None if value.get("kind") is None else str(value.get("kind"))),
                source=str(value.get("source", "")),
                destination=str(value.get("destination", "")),
                state=state,
                detail=str(value.get("detail", "") or ""),
                history=tuple(h for h in history if isinstance(h, Mapping))
                if isinstance(history, list)
                else (),
            )
        )
    operations.sort(key=lambda op: op.operation_id)
    return tuple(operations), corrupt


def load_journal(path: str | os.PathLike[str]) -> JournalSnapshot:
    """Reconstruct a recorded batch from disk without ever raising on corruption.

    A missing file is ``exists=False``. A corrupt or truncated file is returned
    with ``corrupt=True`` and ``needs_reconciliation=True`` so the caller can
    surface it as a recovery condition (PRD 13.3) instead of losing it to an
    exception or treating it as "no operations were ever planned".
    """
    journal = Path(path)
    batch_id = journal.stem
    try:
        raw = journal.read_bytes()
    except FileNotFoundError:
        return JournalSnapshot(batch_id=batch_id, exists=False)
    except OSError as exc:
        return JournalSnapshot(
            batch_id=batch_id,
            exists=True,
            corrupt=True,
            needs_reconciliation=True,
            detail=f"The journal could not be read: {type(exc).__name__}.",
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JournalSnapshot(
            batch_id=batch_id,
            exists=True,
            corrupt=True,
            needs_reconciliation=True,
            detail=f"The journal is corrupt or truncated and cannot be parsed: {type(exc).__name__}.",
        )

    if not isinstance(data, Mapping) or not isinstance(data.get("operations"), Mapping):
        return JournalSnapshot(
            batch_id=batch_id,
            exists=True,
            corrupt=True,
            needs_reconciliation=True,
            detail="The journal is missing its operation map.",
        )

    operations, corrupt = _parse_operations(data["operations"])
    return JournalSnapshot(
        batch_id=str(data.get("batch_id") or batch_id),
        instance_id=str(data.get("instance_id", "") or ""),
        plan_hash=str(data.get("plan_hash", "") or ""),
        schema_version=str(data.get("schema_version", JOURNAL_SCHEMA_VERSION) or ""),
        exists=True,
        corrupt=corrupt,
        needs_reconciliation=corrupt,
        detail="One or more operation records were unreadable." if corrupt else "",
        operations=operations,
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
class Journal:
    """A durable, per-batch journal backed by one file under ``.review/journals``.

    This is state bookkeeping, not an executor. It exposes no method that touches a
    managed document: it never moves, renames, copies, or deletes one. The executor
    is a later concern; this class only records what that executor is allowed to do
    and what it has done.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        batch_id: str,
        *,
        instance_id: str = "",
        plan_hash: str = "",
    ) -> None:
        self.root = Path(root)
        self.batch_id = str(batch_id)
        self.path = journal_path(self.root, self.batch_id)
        self._instance_id = str(instance_id)
        self._plan_hash = str(plan_hash)
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    # -- constructors --------------------------------------------------
    @classmethod
    def load(cls, root: str | os.PathLike[str], batch_id: str) -> "Journal":
        """Load an existing journal. Raises on a corrupt file so the caller can act."""
        journal = cls(root, batch_id)
        with journal._lock:
            snapshot = load_journal(journal.path)
            if snapshot.corrupt:
                raise JournalOrderError(
                    "The journal is corrupt and requires reconciliation before use.",
                    code=Code.NEEDS_RECONCILIATION,
                    detail={"batch_id": journal.batch_id, "reason": snapshot.detail},
                )
            if snapshot.exists:
                journal._data = journal._from_snapshot(snapshot)
        return journal

    def initialize(
        self,
        operations: Sequence[Any],
        *,
        instance_id: str | None = None,
        plan_hash: str | None = None,
    ) -> "Journal":
        """Write the initial ``planned`` records for ``operations``, durably.

        Idempotent for a repeated identical call; re-initializing an existing
        journal under a different plan hash is refused (the plan is immutable).
        """
        with self._lock:
            if instance_id is not None:
                self._instance_id = str(instance_id)
            if plan_hash is not None:
                self._plan_hash = str(plan_hash)

            if self.path.exists():
                snapshot = load_journal(self.path)
                if snapshot.corrupt:
                    raise JournalOrderError(
                        "The journal is corrupt and requires reconciliation before use.",
                        code=Code.NEEDS_RECONCILIATION,
                        detail={"batch_id": self.batch_id, "reason": snapshot.detail},
                    )
                if plan_hash and snapshot.plan_hash and snapshot.plan_hash != plan_hash:
                    raise Conflict(
                        "The journal was initialized from a different plan hash.",
                        code=Code.PLAN_HASH_MISMATCH,
                        detail={"batch_id": self.batch_id},
                    )
                self._data = self._from_snapshot(snapshot)
            else:
                self._data = self._empty()

            for operation in operations:
                self._add_operation(operation)
            self._persist()
            return self

    # -- reads ---------------------------------------------------------
    def get(self, operation_id: str) -> JournalOperation | None:
        with self._lock:
            data = self._ensure_loaded()
            record = data["operations"].get(str(operation_id))
            if record is None:
                return None
            return _operation_from_record(record)

    def states(self) -> dict[str, str]:
        with self._lock:
            data = self._ensure_loaded()
            return {key: str(value.get("state")) for key, value in data["operations"].items()}

    def snapshot(self) -> JournalSnapshot:
        with self._lock:
            data = self._ensure_loaded()
            operations, corrupt = _parse_operations(data["operations"])
            return JournalSnapshot(
                batch_id=str(data.get("batch_id", self.batch_id)),
                instance_id=str(data.get("instance_id", "")),
                plan_hash=str(data.get("plan_hash", "")),
                exists=self.path.exists(),
                corrupt=corrupt,
                needs_reconciliation=corrupt,
                operations=operations,
                created_at=data.get("created_at"),
                updated_at=data.get("updated_at"),
            )

    # -- durable step transitions -------------------------------------
    def record_intent(self, operation_id: str, detail: str | None = None) -> str:
        """Record ``intent_recorded`` durably.

        This must complete before any executor touches the source file (PRD 13.2).
        """
        return self._transition(operation_id, OperationState.INTENT_RECORDED, detail)

    def record_file_moved(self, operation_id: str, detail: str | None = None) -> str:
        return self._transition(operation_id, OperationState.FILE_MOVED, detail)

    def record_committed(self, operation_id: str, detail: str | None = None) -> str:
        return self._transition(operation_id, OperationState.COMMITTED, detail)

    def record_needs_reconciliation(self, operation_id: str, reason: str) -> str:
        return self._transition(operation_id, OperationState.NEEDS_RECONCILIATION, reason)

    def record_failed(self, operation_id: str, detail: str | None = None) -> str:
        return self._transition(operation_id, OperationState.FAILED, detail)

    def record_skipped(self, operation_id: str, reason: str | None = None) -> str:
        return self._transition(operation_id, OperationState.SKIPPED, reason)

    def _transition(self, operation_id: str, target: OperationState, detail: str | None) -> str:
        with self._lock:
            data = self._ensure_loaded()
            record = data["operations"].get(str(operation_id))
            if record is None:
                raise NotFound(
                    "That operation is not recorded in this journal.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "journal_operation", "batch_id": self.batch_id},
                )
            current = OperationState(str(record["state"]))
            _assert_transition_allowed(current, target)
            if current == target:
                return current.value  # idempotent: a replay must not repeat work
            previous_detail = record.get("detail")
            history = record.setdefault("history", [])
            previous_history_length = len(history)
            record["state"] = target.value
            record["detail"] = "" if detail is None else str(detail)
            history.append(
                {"state": target.value, "detail": record["detail"], "at": _now_iso_stable()}
            )
            try:
                self._persist()
            except Exception:
                # The write never reached disk; leave memory matching the file so a
                # caught failure cannot make the journal lie about what is durable.
                record["state"] = current.value
                record["detail"] = previous_detail
                del history[previous_history_length:]
                raise
            return target.value

    # -- internals -----------------------------------------------------
    def _empty(self) -> dict[str, Any]:
        now = _now_iso_stable()
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "instance_id": self._instance_id,
            "plan_hash": self._plan_hash,
            "created_at": now,
            "updated_at": now,
            "operations": {},
        }

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._data is None:
            snapshot = load_journal(self.path)
            if snapshot.corrupt:
                raise JournalOrderError(
                    "The journal is corrupt and requires reconciliation before use.",
                    code=Code.NEEDS_RECONCILIATION,
                    detail={"batch_id": self.batch_id, "reason": snapshot.detail},
                )
            self._data = self._from_snapshot(snapshot) if snapshot.exists else self._empty()
        return self._data

    def _from_snapshot(self, snapshot: JournalSnapshot) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        data = json.loads(raw)
        data.setdefault("batch_id", snapshot.batch_id)
        data.setdefault("instance_id", snapshot.instance_id)
        data.setdefault("plan_hash", snapshot.plan_hash)
        data.setdefault("schema_version", snapshot.schema_version)
        data.setdefault("operations", {})
        return data

    def _add_operation(self, operation: Any) -> None:
        if isinstance(operation, Mapping):
            operation_id = str(operation["operation_id"])
            fields = {
                "document_id": str(operation.get("document_id", "")),
                "kind": _kind_value(operation.get("kind")),
                "source": str(operation.get("source", "")),
                "destination": str(operation.get("destination", "")),
            }
        else:
            operation_id = str(getattr(operation, "operation_id"))
            fields = {
                "document_id": str(getattr(operation, "document_id", "")),
                "kind": _kind_value(getattr(operation, "kind", None)),
                "source": str(getattr(operation, "source", "")),
                "destination": str(getattr(operation, "destination", "")),
            }
        data = self._ensure_loaded()
        existing = data["operations"].get(operation_id)
        if existing is not None:
            return  # idempotent initialization keeps the recorded state
        data["operations"][operation_id] = {
            "operation_id": operation_id,
            **fields,
            "state": OperationState.PLANNED.value,
            "detail": "",
            "history": [
                {
                    "state": OperationState.PLANNED.value,
                    "detail": "",
                    "at": _now_iso_stable(),
                }
            ],
        }

    def _persist(self) -> None:
        data = self._ensure_loaded()
        data["updated_at"] = _now_iso_stable()
        _durable_replace(self.path, _encode(data))


def _operation_from_record(record: Mapping[str, Any]) -> JournalOperation:
    history = record.get("history")
    return JournalOperation(
        operation_id=str(record.get("operation_id", "")),
        document_id=str(record.get("document_id", "")),
        kind=(None if record.get("kind") is None else str(record.get("kind"))),
        source=str(record.get("source", "")),
        destination=str(record.get("destination", "")),
        state=str(record.get("state", "")),
        detail=str(record.get("detail", "") or ""),
        history=tuple(h for h in history if isinstance(h, Mapping))
        if isinstance(history, list)
        else (),
    )


def _assert_transition_allowed(current: OperationState, target: OperationState) -> None:
    """Refuse a backwards or step-skipping transition; allow recovery from a side state."""
    if current == target:
        return
    if target in SIDE_STATES:
        return
    allowed = _ALLOWED_PREDECESSORS.get(target)
    if allowed is None:  # pragma: no cover - every ordered state is mapped
        raise JournalOrderError(
            f"Unknown operation state {target.value!r}.",
            code=Code.NEEDS_RECONCILIATION,
        )
    if current in allowed or current in SIDE_STATES:
        return
    raise JournalOrderError(
        f"Refused an out-of-order journal transition from {current.value!r} to {target.value!r}.",
        code=Code.NEEDS_RECONCILIATION,
        detail={"current": current.value, "target": target.value},
    )


# ---------------------------------------------------------------------------
# Reconciliation against the authoritative database
# ---------------------------------------------------------------------------
def reconcile_with_database(
    snapshot: JournalSnapshot,
    database_operations: Sequence[Any],
) -> ReconciliationReport:
    """Compare a journal snapshot against ``file_operations`` rows.

    The database is authoritative. Every disagreement, and every operation present
    in only one side, is reported as needing reconciliation; nothing is silently
    accepted from the journal alone (PRD 13.3).
    """
    journal_by_id = snapshot.by_id()
    items: list[ReconciliationItem] = []
    needs = bool(snapshot.needs_reconciliation or snapshot.corrupt)
    db_ids: set[str] = set()

    for record in database_operations:
        operation_id = str(getattr(record, "id"))
        db_ids.add(operation_id)
        authoritative = _state_value(getattr(record, "state", None))
        journal_op = journal_by_id.get(operation_id)
        if journal_op is None:
            items.append(
                ReconciliationItem(
                    operation_id=operation_id,
                    journal_state=None,
                    authoritative_state=authoritative,
                    needs_reconciliation=True,
                    reason=(
                        "The database records this operation but the journal does not; "
                        "the database is authoritative."
                    ),
                )
            )
            needs = True
        elif journal_op.state != authoritative:
            items.append(
                ReconciliationItem(
                    operation_id=operation_id,
                    journal_state=journal_op.state,
                    authoritative_state=authoritative,
                    needs_reconciliation=True,
                    reason=(
                        "The journal and the database disagree; the database is "
                        "authoritative and the difference must be reconciled."
                    ),
                )
            )
            needs = True
        else:
            items.append(
                ReconciliationItem(
                    operation_id=operation_id,
                    journal_state=journal_op.state,
                    authoritative_state=authoritative,
                    needs_reconciliation=False,
                    reason="The journal and the database agree.",
                )
            )

    for journal_op in snapshot.operations:
        if journal_op.operation_id in db_ids:
            continue
        items.append(
            ReconciliationItem(
                operation_id=journal_op.operation_id,
                journal_state=journal_op.state,
                authoritative_state=None,
                needs_reconciliation=True,
                reason=(
                    "The journal records an operation the database does not; treat it as "
                    "never committed and reconcile."
                ),
            )
        )
        needs = True

    return ReconciliationReport(
        batch_id=snapshot.batch_id,
        items=tuple(items),
        needs_reconciliation=needs,
        corrupt=snapshot.corrupt,
    )


def reconcile_with_repo(snapshot: JournalSnapshot, repo: Any) -> ReconciliationReport:
    """Reconcile a snapshot against ``repo``'s durable ``file_operations`` rows."""
    operations: Iterable[Any] = ()
    if snapshot.batch_id:
        try:
            operations = repo.list_file_operations(snapshot.batch_id)
        except Exception:  # pragma: no cover - a read failure is a reconciliation condition
            operations = ()
    return reconcile_with_database(snapshot, list(operations))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _state_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, OperationState):
        return value.value
    return str(value)


def _kind_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _now_iso_stable() -> str:
    from ..util import now_iso

    return now_iso()
