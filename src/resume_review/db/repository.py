"""Core data access: instance, documents, human state, actions and the durable queue.

Authority: PRD section 8.3 ("Decisions and save behavior"), section 10 ("State
model and action semantics"), section 11 ("Database and persistence contracts"),
section 12.2 ("Mutation rules") and section 13 ("Safe file actions, approvals,
and recovery").

One class, :class:`Repository`, is the chokepoint every other module reads and
writes through. It composes :class:`~resume_review.db.repository_analysis.\
AnalysisRepositoryMixin` so criteria, profiles, evidence, idempotency and audit
methods share the same facade and the same ``Database``.

Invariants enforced here
------------------------

* Every mutating method runs inside ``Database.write(...)``. That context bumps
  ``instances.state_revision`` and writes the ``audit_events`` row in the same
  transaction as the mutation, which is the entire reason ``Database.write``
  exists. There is no method that updates a row outside it.
* Versioned writes compare ``expected_*`` and raise :class:`RevisionConflict`
  carrying the current value and revision, so the API can render the PRD 15.3
  conflict display. Last-writer-wins is never applied.
* Saving a decision or an intent never moves a file. Only the actions module,
  through an approved batch, touches the filesystem.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from ..errors import Code, Conflict, Forbidden, NotFound, ValidationFailed
from ..models import (
    ActionPlan,
    DecisionRecord,
    DocumentRecord,
    ExecutionState,
    FileOperationRecord,
    IntentRecord,
    Location,
    MediaType,
    NoteRecord,
    OperationState,
    PendingIntent,
    PlannedOperation,
    ProcessingState,
    ReviewState,
    StorageMode,
    TaskOrigin,
    TaskRecord,
    TaskState,
    task_dedupe_key,
)
from ..storage.paths import normalize_rel_path
from ..util import new_id, new_token, now_iso, seconds_from_now_iso
from .connection import Database, dump_row, encode_json, json_column
from .repository_analysis import AnalysisRepositoryMixin, coerce_enum

__all__ = [
    "Repository",
    "RevisionConflict",
    "document_from_row",
    "decision_from_row",
    "note_from_row",
    "task_from_row",
    "intent_from_row",
    "file_operation_from_row",
]


#: Sort keys the document table offers. The values are SQL fragments, never
#: caller-supplied text, so an unrecognized key can never reach the query.
_DOCUMENT_SORT: dict[str, str] = {
    "ingested_at": "d.ingested_at",
    "original_filename": "d.original_filename",
    "display_name": "d.display_name",
    "processing_state": "d.processing_state",
    "current_rel_path": "d.current_rel_path",
    "document_id": "d.id",
    "size_bytes": "d.size_bytes",
    "submitted_at": "d.submitted_at",
    "review_state": "COALESCE(dec.disposition, 'unreviewed')",
    "open_task_count": (
        "(SELECT COUNT(*) FROM review_tasks t WHERE t.document_id = d.id AND t.state = 'open')"
    ),
}

_BATCH_TERMINAL_STATES = (
    ExecutionState.COMPLETED.value,
    ExecutionState.PARTIAL.value,
    ExecutionState.BLOCKED.value,
    ExecutionState.CANCELED.value,
)

_JOB_KINDS = ("scan", "extraction", "analysis", "snapshot", "chat")


class RevisionConflict(Conflict):
    """A versioned write lost against newer committed state.

    Carries the current value and revision so the caller can show the reviewer what
    changed and who changed it (PRD section 15.3). It is deliberately a distinct
    subclass of :class:`~resume_review.errors.Conflict` so callers can catch the
    optimistic-concurrency case without catching every conflict.
    """

    code = Code.REVISION_CONFLICT

    def __init__(
        self,
        message: str,
        *,
        current_value: Any = None,
        current_revision: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(detail or {})
        if current_value is not None:
            merged.setdefault("current_value", current_value)
        if current_revision is not None:
            merged.setdefault("current_revision", int(current_revision))
        super().__init__(message, code=Code.REVISION_CONFLICT, detail=merged)


# ---------------------------------------------------------------------------
# Row mappers (never leak a raw sqlite3.Row past this module)
# ---------------------------------------------------------------------------
def document_from_row(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        id=str(row["id"]),
        instance_id=str(row["instance_id"]),
        original_filename=str(row["original_filename"]),
        current_rel_path=str(row["current_rel_path"]),
        first_seen_rel_path=str(row["first_seen_rel_path"]),
        display_name=row["display_name"],
        media_type=coerce_enum(MediaType, row["media_type"], MediaType.UNKNOWN),
        size_bytes=row["size_bytes"],
        content_sha256=row["content_sha256"],
        current_revision=int(row["current_revision"]),
        fs_identity=row["fs_identity"],
        processing_state=coerce_enum(ProcessingState, row["processing_state"], ProcessingState.DISCOVERED),
        processing_detail=row["processing_detail"],
        location=coerce_enum(Location, row["location"], Location.ACTIVE),
        location_version=int(row["location_version"]),
        decision_needs_recheck=bool(row["decision_needs_recheck"]),
        recheck_reason=row["recheck_reason"],
        duplicate_content=bool(row["duplicate_content"]),
        duplicate_of=row["duplicate_of"],
        submitted_at=row["submitted_at"],
        ingested_at=str(row["ingested_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
    )


def decision_from_row(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        document_id=str(row["document_id"]),
        disposition=coerce_enum(ReviewState, row["disposition"], ReviewState.UNREVIEWED),
        decision_revision=int(row["decision_revision"]),
        actor=str(row["actor"]),
        decided_at=row["decided_at"],
        needs_recheck=bool(row["needs_recheck"]),
        disposition_frozen=bool(row["disposition_frozen"]),
    )


def note_from_row(row: sqlite3.Row) -> NoteRecord:
    return NoteRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        body=str(row["body"]),
        author=str(row["author"]),
        note_revision=int(row["note_revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        title=str(row["title"]),
        task_type=str(row["task_type"]),
        criterion_id=row["criterion_id"],
        source_revision=row["source_revision"],
        dedupe_key=str(row["dedupe_key"]),
        origin=coerce_enum(TaskOrigin, row["origin"], TaskOrigin.HUMAN),
        detail=str(row["detail"]),
        state=coerce_enum(TaskState, row["state"], TaskState.OPEN),
        severity=str(row["severity"]),
        resolution=row["resolution"],
        resolution_note=row["resolution_note"],
        closed_by=row["closed_by"],
        closed_at=row["closed_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def intent_from_row(row: sqlite3.Row) -> IntentRecord:
    return IntentRecord(
        document_id=str(row["document_id"]),
        intent=coerce_enum(PendingIntent, row["intent"], PendingIntent.NONE),
        intent_revision=int(row["intent_revision"]),
        state=str(row["state"]),
        requester=str(row["requester"]),
        origin_batch_id=row["origin_batch_id"],
        note=row["note"],
    )


def file_operation_from_row(row: sqlite3.Row) -> FileOperationRecord:
    return FileOperationRecord(
        id=str(row["id"]),
        batch_id=str(row["batch_id"]),
        document_id=str(row["document_id"]),
        sequence=int(row["sequence"]),
        kind=coerce_enum(PendingIntent, row["kind"], PendingIntent.NONE),
        source_rel_path=str(row["source_rel_path"]),
        destination_rel_path=str(row["destination_rel_path"]),
        expected_sha256=str(row["expected_sha256"]),
        expected_size=row["expected_size"],
        source_revision=int(row["source_revision"]),
        decision_revision=int(row["decision_revision"]),
        intent_revision=int(row["intent_revision"]),
        location_version=int(row["location_version"]),
        state=coerce_enum(OperationState, row["state"], OperationState.PLANNED),
        observed_source_state=row["observed_source_state"],
        observed_destination_state=row["observed_destination_state"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
class Repository(AnalysisRepositoryMixin):
    """Data access over the frozen schema, wrapped around one ``Database``."""

    def __init__(self, db: Database) -> None:
        self.db = db
        #: Source identities captured by the planner at plan time, keyed by the
        #: operation id it minted. ``create_file_operations`` consumes these when
        #: it writes the durable journal rows, so the identity the plan verified
        #: against is the one the journal records (PRD section 13.3). This is
        #: deliberately in-memory: planning is documented as read-only, and it
        #: writes no batch, operation, or audit row.
        self._planned_source_identities: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal guards
    # ------------------------------------------------------------------
    @staticmethod
    def _require_document(conn: sqlite3.Connection, document_id: str) -> None:
        """Refuse a foreign-keyed write against a document that does not exist.

        Checking explicitly turns what would be an opaque ``IntegrityError`` into a
        clean 404 with a stable code, and it never leaks the caller's path.
        """
        row = conn.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise NotFound(
                "That document does not exist.",
                code=Code.NOT_FOUND,
                detail={"entity": "document"},
            )

    # ==================================================================
    # Instance and census (PRD 11.1, 8.1)
    # ==================================================================
    def create_instance(
        self,
        instance_id: str,
        app_version: str,
        schema_version: int,
        storage_mode: str = StorageMode.LOCAL.value,
        host_label: str | None = None,
    ) -> dict[str, Any]:
        """Insert the one instance row for this database.

        This is the genesis write: there is no prior ``state_revision`` to bump and
        no instance to attribute an audit row to, so it deliberately does not go
        through ``Database.write``. Every later mutation does.
        """
        mode = str(storage_mode)
        if mode not in (StorageMode.LOCAL.value, StorageMode.SHARED_HOST_LOCAL.value):
            raise ValidationFailed(
                "Unsupported storage mode.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "storage_mode"},
            )
        with self.db.bare_transaction() as conn:
            existing = conn.execute("SELECT id FROM instances LIMIT 1").fetchone()
            if existing is not None:
                raise Conflict(
                    "This database already has an instance.",
                    code=Code.INSTANCE_ALREADY_EXISTS,
                    detail={"reason": "instance_present"},
                )
            now = now_iso()
            conn.execute(
                "INSERT INTO instances (id, schema_version, app_version, state_revision, "
                "storage_mode, host_label, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                (instance_id, int(schema_version), app_version, mode, host_label, now, now),
            )
        return self.get_instance() or {}

    def get_instance(self) -> dict[str, Any] | None:
        return dump_row(self.db.query_one("SELECT * FROM instances LIMIT 1"))

    def status_counts(self) -> dict[str, int]:
        """Whole-instance census for the status strip.

        Documents without a decision row count as ``unreviewed``; a missing decision
        is ``unreviewed``, never a negative assessment (PRD sections 8.1, 10).
        """
        total = int(self.db.scalar("SELECT COUNT(*) FROM documents", (), default=0) or 0)
        processed = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM documents WHERE processing_state IN "
                "('ready','manual_review','stale','error')",
                (),
                default=0,
            )
            or 0
        )
        manual_review = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM documents WHERE processing_state = 'manual_review'",
                (),
                default=0,
            )
            or 0
        )
        disposition_rows = self.db.query(
            "SELECT disposition, COUNT(*) AS n FROM decisions GROUP BY disposition"
        )
        by_disposition = {str(r["disposition"]): int(r["n"]) for r in disposition_rows}
        keep = by_disposition.get(ReviewState.KEEP.value, 0)
        reject = by_disposition.get(ReviewState.REJECT.value, 0)
        hold = by_disposition.get(ReviewState.HOLD.value, 0)
        pending_action = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM action_intents WHERE intent != 'none' "
                "AND state IN ('saved','planned')",
                (),
                default=0,
            )
            or 0
        )
        needs_recheck = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM documents d LEFT JOIN decisions dec "
                "ON dec.document_id = d.id "
                "WHERE d.decision_needs_recheck = 1 OR COALESCE(dec.needs_recheck, 0) = 1",
                (),
                default=0,
            )
            or 0
        )
        open_tasks = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM review_tasks WHERE state = 'open'", (), default=0
            )
            or 0
        )
        return {
            "total": total,
            "processed": processed,
            "unreviewed": max(0, total - (keep + reject + hold)),
            "keep": keep,
            "reject": reject,
            "hold": hold,
            "manual_review": manual_review,
            "pending_action": pending_action,
            "needs_recheck": needs_recheck,
            "open_tasks": open_tasks,
        }

    # ==================================================================
    # Documents (PRD 4, 6.2, 10)
    # ==================================================================
    def create_document(
        self,
        original_filename: str,
        rel_path: str,
        media_type: str | MediaType,
        size_bytes: int | None,
        content_sha256: str | None,
        fs_identity: str | None,
        submitted_at: str | None = None,
    ) -> DocumentRecord:
        """Register a submission and mint its stable document ID.

        The ID is opaque and never derived from the filename, path, or content hash
        (PRD section 4). ``submitted_at`` stays ``None`` when unknown; a file's
        modification time is never substituted for it.
        """
        norm = normalize_rel_path(rel_path)
        media = coerce_enum(MediaType, media_type, MediaType.UNKNOWN)
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="document.create",
            entity_type="document",
        ) as conn:
            clash = conn.execute(
                "SELECT id FROM documents WHERE instance_id = ? AND current_rel_path = ?",
                (self.instance_id, norm),
            ).fetchone()
            if clash is not None:
                raise Conflict(
                    "A document is already registered at that path.",
                    code=Code.SETUP_COLLISION,
                    detail={"reason": "path_registered"},
                )
            document_id = new_id("document")
            now = now_iso()
            conn.execute(
                "INSERT INTO documents (id, instance_id, original_filename, current_rel_path, "
                "first_seen_rel_path, media_type, size_bytes, content_sha256, current_revision, "
                "fs_identity, processing_state, location, location_version, submitted_at, "
                "ingested_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, "
                "'discovered', 'active', 0, ?, ?, ?, ?)",
                (
                    document_id,
                    self.instance_id,
                    original_filename,
                    norm,
                    norm,
                    media.value,
                    size_bytes,
                    content_sha256,
                    fs_identity,
                    submitted_at,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            return document_from_row(row)

    def get_document(self, document_id: str) -> DocumentRecord | None:
        row = self.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        return document_from_row(row) if row else None

    def get_document_by_path(self, rel_path: str) -> DocumentRecord | None:
        row = self.db.query_one(
            "SELECT * FROM documents WHERE instance_id = ? AND current_rel_path = ?",
            (self.instance_id, normalize_rel_path(rel_path)),
        )
        return document_from_row(row) if row else None

    def list_documents(
        self,
        *,
        sort: str = "ingested_at",
        direction: str = "asc",
        processing_state: str | None = None,
        location: str | None = None,
        review_state: str | None = None,
        document_ids: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """List submissions with a whitelisted sort and a stable ID tie-breaker.

        The tie-breaker on ``document_id`` is what makes pagination reproducible when
        two rows share a sort value (PRD section 11.2).
        """
        column = _DOCUMENT_SORT.get(sort)
        if column is None:
            raise ValidationFailed(
                "Unsupported sort key.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "sort"},
            )
        order_dir = "DESC" if str(direction).lower() == "desc" else "ASC"
        clauses = ["d.instance_id = ?"]
        params: list[Any] = [self.instance_id]
        if processing_state is not None:
            clauses.append("d.processing_state = ?")
            params.append(str(processing_state))
        if location is not None:
            clauses.append("d.location = ?")
            params.append(str(location))
        if review_state is not None:
            clauses.append("COALESCE(dec.disposition, 'unreviewed') = ?")
            params.append(str(review_state))
        if document_ids is not None:
            ids = list(document_ids)
            if not ids:
                return []
            clauses.append("d.id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)

        order = f"{column} {order_dir}"
        if sort != "document_id":
            order += ", d.id ASC"
        sql = (
            "SELECT d.* FROM documents d LEFT JOIN decisions dec ON dec.document_id = d.id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY " + order
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        return [document_from_row(r) for r in self.db.query(sql, tuple(params))]

    def set_document_processing(
        self, document_id: str, state: str, detail: str | None = None
    ) -> None:
        """Advance the pipeline's processing state. Independent of any decision."""
        state_value = str(state)
        if state_value not in {s.value for s in ProcessingState}:
            raise ValidationFailed(
                "Unsupported processing state.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "state"},
            )
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="document.processing",
            entity_type="document",
            entity_id=document_id,
            new={"processing_state": state_value},
        ) as conn:
            cur = conn.execute(
                "UPDATE documents SET processing_state = ?, processing_detail = ?, updated_at = ? "
                "WHERE id = ?",
                (state_value, detail, now_iso(), document_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That document does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document"},
                )

    def set_document_location(
        self,
        document_id: str,
        rel_path: str,
        location: str,
        expected_location_version: int,
    ) -> int:
        """Record a reconciled filesystem location and bump ``location_version``.

        Optimistic: a stale ``expected_location_version`` is refused with the current
        value so the caller never silently overwrites a concurrent move.
        """
        location_value = str(location)
        if location_value not in {loc.value for loc in Location}:
            raise ValidationFailed(
                "Unsupported location.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "location"},
            )
        norm = normalize_rel_path(rel_path)
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="document.location",
            entity_type="document",
            entity_id=document_id,
        ) as conn:
            row = conn.execute(
                "SELECT location, location_version, current_rel_path FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise NotFound(
                    "That document does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document"},
                )
            current_revision = int(row["location_version"])
            if current_revision != int(expected_location_version):
                raise RevisionConflict(
                    "The document's location changed since it was read.",
                    current_value={
                        "relative_path": row["current_rel_path"],
                        "location": row["location"],
                    },
                    current_revision=current_revision,
                )
            new_revision = current_revision + 1
            conn.execute(
                "UPDATE documents SET current_rel_path = ?, location = ?, location_version = ?, "
                "updated_at = ? WHERE id = ?",
                (norm, location_value, new_revision, now_iso(), document_id),
            )
            return new_revision

    def set_duplicate_flags(
        self, document_id: str, duplicate_content: bool, duplicate_of: str | None = None
    ) -> None:
        """Flag identical bytes at two paths. Never merges, deletes, or moves."""
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="document.duplicate_flag",
            entity_type="document",
            entity_id=document_id,
        ) as conn:
            cur = conn.execute(
                "UPDATE documents SET duplicate_content = ?, duplicate_of = ?, updated_at = ? "
                "WHERE id = ?",
                (1 if duplicate_content else 0, duplicate_of, now_iso(), document_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That document does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document"},
                )

    def flag_decision_needs_recheck(
        self, document_id: str, reason: str | None = None, clear: bool = False
    ) -> None:
        """Flag (or clear) a decision for reconsideration after material change.

        The flag never overwrites the existing disposition; it asks a human to look
        again (PRD sections 6.3, 13.1).
        """
        value = 0 if clear else 1
        stored_reason = None if clear else reason
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="document.recheck",
            entity_type="document",
            entity_id=document_id,
            new={"needs_recheck": bool(value)},
        ) as conn:
            cur = conn.execute(
                "UPDATE documents SET decision_needs_recheck = ?, recheck_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (value, stored_reason, now_iso(), document_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That document does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document"},
                )
            conn.execute(
                "UPDATE decisions SET needs_recheck = ?, updated_at = ? WHERE document_id = ?",
                (value, now_iso(), document_id),
            )

    # ==================================================================
    # Revisions and extraction cache (PRD 6.3)
    # ==================================================================
    def add_revision(
        self,
        document_id: str,
        content_sha256: str,
        size_bytes: int,
        rel_path: str,
        parser_name: str | None = None,
        parser_version: str | None = None,
    ) -> int:
        """Append an immutable revision for a parsed byte-sequence.

        Revisions are never edited. The document's ``current_revision`` and cached
        hash are advanced in the same transaction.
        """
        norm = normalize_rel_path(rel_path)
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="revision.add",
            entity_type="document",
            entity_id=document_id,
        ) as conn:
            document = conn.execute(
                "SELECT id FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if document is None:
                raise NotFound(
                    "That document does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document"},
                )
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM document_revisions "
                    "WHERE document_id = ?",
                    (document_id,),
                ).fetchone()[0]
            )
            now = now_iso()
            conn.execute(
                "INSERT INTO document_revisions (document_id, instance_id, revision, content_sha256, "
                "size_bytes, rel_path, parser_name, parser_version, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    self.instance_id,
                    revision,
                    content_sha256,
                    int(size_bytes),
                    norm,
                    parser_name,
                    parser_version,
                    now,
                ),
            )
            conn.execute(
                "UPDATE documents SET current_revision = ?, content_sha256 = ?, size_bytes = ?, "
                "updated_at = ? WHERE id = ?",
                (revision, content_sha256, int(size_bytes), now, document_id),
            )
            return revision

    def get_revision(self, document_id: str, revision: int) -> dict[str, Any] | None:
        return dump_row(
            self.db.query_one(
                "SELECT * FROM document_revisions WHERE document_id = ? AND revision = ?",
                (document_id, int(revision)),
            )
        )

    def latest_revision(self, document_id: str) -> dict[str, Any] | None:
        return dump_row(
            self.db.query_one(
                "SELECT * FROM document_revisions WHERE document_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (document_id,),
            )
        )

    def set_revision_extraction(
        self,
        document_id: str,
        revision: int,
        state: str,
        detail: str | None,
        span_ref: str | None,
        span_count: int,
        char_count: int,
        page_count: int | None,
    ) -> None:
        """Record the extraction outcome for one revision."""
        if str(state) not in ("pending", "ok", "partial", "failed", "unsupported"):
            raise ValidationFailed(
                "Unsupported extraction state.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "state"},
            )
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="revision.extraction",
            entity_type="document",
            entity_id=document_id,
        ) as conn:
            cur = conn.execute(
                "UPDATE document_revisions SET extraction_state = ?, extraction_detail = ?, "
                "span_ref = ?, span_count = ?, char_count = ?, page_count = ? "
                "WHERE document_id = ? AND revision = ?",
                (
                    str(state),
                    detail,
                    span_ref,
                    int(span_count),
                    int(char_count),
                    page_count,
                    document_id,
                    int(revision),
                ),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That document revision does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "document_revision"},
                )

    def cache_extraction(
        self,
        content_sha256: str,
        parser_name: str,
        parser_version: str,
        payload: Any,
        span_count: int,
        char_count: int,
        page_count: int | None,
    ) -> None:
        """Cache extracted spans by (content hash, parser name, parser version).

        Unchanged bytes are never reparsed and a retry cannot create duplicate spans
        (PRD section 6.3).
        """
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="extraction.cache",
            entity_type="document",
        ) as conn:
            now = now_iso()
            conn.execute(
                "INSERT INTO extraction_cache (instance_id, content_sha256, parser_name, "
                "parser_version, payload_json, span_count, char_count, page_count, created_at, "
                "last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, content_sha256, parser_name, parser_version) DO UPDATE SET "
                "payload_json = excluded.payload_json, span_count = excluded.span_count, "
                "char_count = excluded.char_count, page_count = excluded.page_count, "
                "last_used_at = excluded.last_used_at",
                (
                    self.instance_id,
                    content_sha256,
                    parser_name,
                    parser_version,
                    encode_json(payload),
                    int(span_count),
                    int(char_count),
                    page_count,
                    now,
                    now,
                ),
            )

    def get_cached_extraction(
        self, content_sha256: str, parser_name: str, parser_version: str
    ) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM extraction_cache WHERE instance_id = ? AND content_sha256 = ? "
            "AND parser_name = ? AND parser_version = ?",
            (self.instance_id, content_sha256, parser_name, parser_version),
        )
        if row is None:
            return None
        item = dump_row(row) or {}
        item["payload"] = json_column(row["payload_json"], default=None)
        return item

    # ==================================================================
    # Decisions (PRD 8.3, 10)
    # ==================================================================
    def get_decision(self, document_id: str) -> DecisionRecord:
        """Return the decision, synthesizing an unreviewed default when absent.

        Absence of a row *is* ``unreviewed``; the default is never a positive or
        negative assessment (PRD section 10).
        """
        row = self.db.query_one("SELECT * FROM decisions WHERE document_id = ?", (document_id,))
        if row is not None:
            return decision_from_row(row)
        return DecisionRecord(document_id=document_id)

    def set_decision(
        self,
        document_id: str,
        disposition: str,
        expected_revision: int,
        actor: str,
        request_id: str | None = None,
    ) -> DecisionRecord:
        """Set the human disposition with an optimistic revision check.

        Saves only the review record; it never moves a file, even for ``reject``
        (PRD sections 8.3, 10.1). While ``disposition_frozen`` is set — a Trash
        request is pending or the file is in Trash — the write is refused.
        """
        disposition_value = str(disposition)
        if disposition_value not in {d.value for d in ReviewState}:
            raise ValidationFailed(
                "Unsupported disposition.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "disposition"},
            )
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="decision.set",
            entity_type="document",
            entity_id=document_id,
            request_id=request_id,
        ) as conn:
            self._require_document(conn, document_id)
            row = conn.execute(
                "SELECT * FROM decisions WHERE document_id = ?", (document_id,)
            ).fetchone()
            current_revision = int(row["decision_revision"]) if row else 0
            if row is not None and int(row["disposition_frozen"]):
                raise Conflict(
                    "Disposition controls are frozen while a Trash request is pending or the "
                    "file is in Trash.",
                    code=Code.INTENT_FROZEN,
                    detail={"document_id": document_id},
                )
            if current_revision != int(expected_revision):
                raise RevisionConflict(
                    "The decision changed since it was read.",
                    current_value=str(row["disposition"]) if row else ReviewState.UNREVIEWED.value,
                    current_revision=current_revision,
                    detail={"actor": str(row["actor"]) if row else ""},
                )
            new_revision = current_revision + 1
            now = now_iso()
            if row is None:
                conn.execute(
                    "INSERT INTO decisions (document_id, instance_id, disposition, decision_revision, "
                    "actor, decided_at, needs_recheck, disposition_frozen, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (document_id, self.instance_id, disposition_value, new_revision, actor, now, now, now),
                )
            else:
                conn.execute(
                    "UPDATE decisions SET disposition = ?, decision_revision = ?, actor = ?, "
                    "decided_at = ?, updated_at = ? WHERE document_id = ?",
                    (disposition_value, new_revision, actor, now, now, document_id),
                )
            updated = conn.execute(
                "SELECT * FROM decisions WHERE document_id = ?", (document_id,)
            ).fetchone()
            return decision_from_row(updated)

    def bulk_set_decisions(
        self,
        items: Sequence[Mapping[str, Any]],
        actor: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply many explicit dispositions atomically (PRD section 8.3).

        Every item's ``expected_revision`` is validated *before* any row is written.
        One stale item and the whole set is refused with the conflicts named, so a
        bulk action can never half-apply.
        """
        entries: list[tuple[str, str, int]] = []
        for item in items:
            document_id = str(item["document_id"])
            disposition_value = str(item["disposition"])
            if disposition_value not in {d.value for d in ReviewState}:
                raise ValidationFailed(
                    "Unsupported disposition.",
                    code=Code.VALIDATION_FAILED,
                    detail={"field": "disposition"},
                )
            entries.append((document_id, disposition_value, int(item["expected_revision"])))

        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="decision.bulk_set",
            entity_type="document",
            affected_ids=[e[0] for e in entries],
            request_id=request_id,
        ) as conn:
            conflicts: list[dict[str, Any]] = []
            for document_id, _disposition, expected in entries:
                row = conn.execute(
                    "SELECT * FROM decisions WHERE document_id = ?", (document_id,)
                ).fetchone()
                current_revision = int(row["decision_revision"]) if row else 0
                if row is not None and int(row["disposition_frozen"]):
                    conflicts.append(
                        {"document_id": document_id, "reason": "frozen"}
                    )
                    continue
                if current_revision != expected:
                    conflicts.append(
                        {
                            "document_id": document_id,
                            "reason": "revision_conflict",
                            "expected_revision": expected,
                            "current_revision": current_revision,
                            "current_disposition": (
                                str(row["disposition"]) if row else ReviewState.UNREVIEWED.value
                            ),
                        }
                    )
            if conflicts:
                raise RevisionConflict(
                    "One or more decisions changed since they were read; nothing was applied.",
                    detail={"conflicts": conflicts},
                )

            now = now_iso()
            document_ids: list[str] = []
            for document_id, disposition_value, expected in entries:
                row = conn.execute(
                    "SELECT * FROM decisions WHERE document_id = ?", (document_id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO decisions (document_id, instance_id, disposition, "
                        "decision_revision, actor, decided_at, needs_recheck, disposition_frozen, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                        (
                            document_id,
                            self.instance_id,
                            disposition_value,
                            expected + 1,
                            actor,
                            now,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE decisions SET disposition = ?, decision_revision = ?, actor = ?, "
                        "decided_at = ?, updated_at = ? WHERE document_id = ?",
                        (disposition_value, int(row["decision_revision"]) + 1, actor, now, now, document_id),
                    )
                document_ids.append(document_id)

        return {
            "updated": len(entries),
            "document_ids": document_ids,
            "state_revision": self.db.state_revision(),
        }

    def set_disposition_frozen(self, document_id: str, frozen: bool) -> None:
        """Freeze or unfreeze disposition controls for the Trash lifecycle."""
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="decision.freeze",
            entity_type="document",
            entity_id=document_id,
            new={"disposition_frozen": bool(frozen)},
        ) as conn:
            self._require_document(conn, document_id)
            now = now_iso()
            cur = conn.execute(
                "UPDATE decisions SET disposition_frozen = ?, updated_at = ? WHERE document_id = ?",
                (1 if frozen else 0, now, document_id),
            )
            if not cur.rowcount:
                conn.execute(
                    "INSERT INTO decisions (document_id, instance_id, disposition, decision_revision, "
                    "actor, decided_at, needs_recheck, disposition_frozen, created_at, updated_at) "
                    "VALUES (?, ?, 'unreviewed', 0, '', NULL, 0, ?, ?, ?)",
                    (document_id, self.instance_id, 1 if frozen else 0, now, now),
                )

    # ==================================================================
    # Notes (PRD 8.4)
    # ==================================================================
    def add_note(self, document_id: str, body: str, author: str) -> NoteRecord:
        note_id = new_id("note")
        with self.db.write(
            actor=author,
            actor_kind="human",
            event="note.add",
            entity_type="note",
            entity_id=note_id,
        ) as conn:
            self._require_document(conn, document_id)
            now = now_iso()
            conn.execute(
                "INSERT INTO notes (id, instance_id, document_id, body, author, note_revision, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (note_id, self.instance_id, document_id, body, author, now, now),
            )
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            return note_from_row(row)

    def update_note(
        self, note_id: str, body: str, expected_revision: int, actor: str
    ) -> NoteRecord:
        """Edit a note with its own revision counter, separate from the decision."""
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="note.update",
            entity_type="note",
            entity_id=note_id,
        ) as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NotFound(
                    "That note does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "note"},
                )
            current_revision = int(row["note_revision"])
            if current_revision != int(expected_revision):
                raise RevisionConflict(
                    "The note changed since it was read.",
                    current_value=str(row["body"]),
                    current_revision=current_revision,
                )
            conn.execute(
                "UPDATE notes SET body = ?, note_revision = ?, updated_at = ? WHERE id = ?",
                (body, current_revision + 1, now_iso(), note_id),
            )
            updated = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            return note_from_row(updated)

    def list_notes(self, document_id: str) -> list[NoteRecord]:
        rows = self.db.query(
            "SELECT * FROM notes WHERE document_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at ASC, id ASC",
            (document_id,),
        )
        return [note_from_row(r) for r in rows]

    def soft_delete_note(self, note_id: str, actor: str) -> None:
        """Soft-delete a note. Notes are never physically removed."""
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="note.delete",
            entity_type="note",
            entity_id=note_id,
        ) as conn:
            cur = conn.execute(
                "UPDATE notes SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now_iso(), now_iso(), note_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That note does not exist or was already removed.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "note"},
                )

    # ==================================================================
    # Tasks (PRD 8.4)
    # ==================================================================
    def upsert_task(
        self,
        document_id: str,
        task_type: str,
        title: str,
        criterion_id: str | None = None,
        source_revision: int | None = None,
        origin: str = TaskOrigin.HUMAN.value,
        detail: str = "",
        severity: str = "normal",
    ) -> tuple[TaskRecord, bool]:
        """Create a task, or return the existing one with ``created=False``.

        Deduplicated on ``UNIQUE(instance_id, dedupe_key)``. A second call with the
        same key is idempotent and, critically, does not reopen a task a human
        already closed (PRD section 8.4). A genuinely new source revision is a new
        key and therefore a new linked task.
        """
        origin_value = str(origin)
        if origin_value not in {o.value for o in TaskOrigin}:
            raise ValidationFailed(
                "Unsupported task origin.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "origin"},
            )
        if str(severity) not in ("info", "normal", "attention"):
            raise ValidationFailed(
                "Unsupported task severity.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "severity"},
            )
        dedupe_key = task_dedupe_key(task_type, document_id, criterion_id, source_revision)
        existing = self.db.query_one(
            "SELECT * FROM review_tasks WHERE instance_id = ? AND dedupe_key = ?",
            (self.instance_id, dedupe_key),
        )
        if existing is not None:
            return task_from_row(existing), False

        with self.db.write(
            actor=origin_value,
            actor_kind=origin_value if origin_value in ("human", "agent", "system") else "helper",
            event="task.upsert",
            entity_type="task",
        ) as conn:
            again = conn.execute(
                "SELECT * FROM review_tasks WHERE instance_id = ? AND dedupe_key = ?",
                (self.instance_id, dedupe_key),
            ).fetchone()
            if again is not None:
                return task_from_row(again), False
            self._require_document(conn, document_id)
            task_id = new_id("task")
            now = now_iso()
            conn.execute(
                "INSERT INTO review_tasks (id, instance_id, document_id, task_type, criterion_id, "
                "source_revision, dedupe_key, origin, title, detail, state, severity, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (
                    task_id,
                    self.instance_id,
                    document_id,
                    task_type,
                    criterion_id,
                    source_revision,
                    dedupe_key,
                    origin_value,
                    title,
                    detail,
                    str(severity),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
            return task_from_row(row), True

    def list_tasks(self, document_id: str | None = None, state: str | None = None) -> list[TaskRecord]:
        clauses = ["instance_id = ?"]
        params: list[Any] = [self.instance_id]
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state))
        rows = self.db.query(
            "SELECT * FROM review_tasks WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC, id ASC",
            tuple(params),
        )
        return [task_from_row(r) for r in rows]

    def open_task_counts(self, document_ids: Iterable[str]) -> dict[str, int]:
        """Open task counts per document, with explicit zeros for the rest."""
        ids = list(document_ids)
        counts: dict[str, int] = {document_id: 0 for document_id in ids}
        if not ids:
            return counts
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query(
            "SELECT document_id, COUNT(*) AS n FROM review_tasks WHERE instance_id = ? "
            "AND state = 'open' AND document_id IN (" + placeholders + ") GROUP BY document_id",
            tuple([self.instance_id, *ids]),
        )
        for row in rows:
            counts[str(row["document_id"])] = int(row["n"])
        return counts

    def close_task(
        self, task_id: str, resolution: str, actor: str, note: str | None = None
    ) -> TaskRecord:
        """Close a task, recording who closed it and why."""
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="task.close",
            entity_type="task",
            entity_id=task_id,
        ) as conn:
            now = now_iso()
            cur = conn.execute(
                "UPDATE review_tasks SET state = 'closed', resolution = ?, resolution_note = ?, "
                "closed_by = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (resolution, note, actor, now, now, task_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That task does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "task"},
                )
            row = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
            return task_from_row(row)

    # ==================================================================
    # Pending intent (PRD 10, 13.1)
    # ==================================================================
    def get_intent(self, document_id: str) -> IntentRecord:
        """Return the pending intent, defaulting to ``none`` when absent."""
        row = self.db.query_one("SELECT * FROM action_intents WHERE document_id = ?", (document_id,))
        return intent_from_row(row) if row is not None else IntentRecord(document_id=document_id)

    def set_intent(
        self,
        document_id: str,
        intent: str,
        requester: str,
        expected_revision: int,
        origin_batch_id: str | None = None,
        note: str | None = None,
    ) -> IntentRecord:
        """Save or cancel a pending intent with an optimistic revision check.

        A saved intent moves nothing; it is a request awaiting a plan and approval
        (PRD section 10).
        """
        intent_value = str(intent)
        if intent_value not in {p.value for p in PendingIntent}:
            raise ValidationFailed(
                "Unsupported intent.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "intent"},
            )
        with self.db.write(
            actor=requester,
            actor_kind="human",
            event="intent.set",
            entity_type="document",
            entity_id=document_id,
        ) as conn:
            self._require_document(conn, document_id)
            row = conn.execute(
                "SELECT * FROM action_intents WHERE document_id = ?", (document_id,)
            ).fetchone()
            current_revision = int(row["intent_revision"]) if row else 0
            if current_revision != int(expected_revision):
                raise RevisionConflict(
                    "The pending intent changed since it was read.",
                    current_value=str(row["intent"]) if row else PendingIntent.NONE.value,
                    current_revision=current_revision,
                )
            state = "cancelled" if intent_value == PendingIntent.NONE.value else "saved"
            new_revision = current_revision + 1
            now = now_iso()
            if row is None:
                conn.execute(
                    "INSERT INTO action_intents (document_id, instance_id, intent, intent_revision, "
                    "state, requester, origin_batch_id, note, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        self.instance_id,
                        intent_value,
                        new_revision,
                        state,
                        requester,
                        origin_batch_id,
                        note,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE action_intents SET intent = ?, intent_revision = ?, state = ?, "
                    "requester = ?, origin_batch_id = ?, note = ?, updated_at = ? "
                    "WHERE document_id = ?",
                    (
                        intent_value,
                        new_revision,
                        state,
                        requester,
                        origin_batch_id,
                        note,
                        now,
                        document_id,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM action_intents WHERE document_id = ?", (document_id,)
            ).fetchone()
            return intent_from_row(updated)

    # ==================================================================
    # Action batches and operations (PRD 13.1, 13.3)
    # ==================================================================
    def create_batch(self, plan: ActionPlan, created_by: str) -> str:
        """Persist an immutable plan as a batch in the ``planned`` state."""
        plan_hash = plan.plan_hash or plan.compute_hash()
        batch_id = plan.batch_id
        with self.db.write(
            actor=created_by,
            actor_kind="human",
            event="batch.create",
            entity_type="batch",
            entity_id=batch_id,
            new={"plan_hash": plan_hash, "operations": len(plan.operations)},
        ) as conn:
            now = now_iso()
            conn.execute(
                "INSERT INTO action_batches (id, instance_id, plan_json, plan_hash, "
                "criteria_version, execution_state, execution_revision, created_by, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 'planned', 0, ?, ?, ?)",
                (
                    batch_id,
                    self.instance_id,
                    encode_json(plan),
                    plan_hash,
                    int(plan.criteria_version),
                    created_by,
                    now,
                    now,
                ),
            )
        return batch_id

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM action_batches WHERE id = ?", (batch_id,))
        if row is None:
            return None
        item = dump_row(row) or {}
        item["plan"] = json_column(row["plan_json"], default=None)
        return item

    def list_batches(self, state: str | None = None) -> list[dict[str, Any]]:
        clauses = ["instance_id = ?"]
        params: list[Any] = [self.instance_id]
        if state is not None:
            clauses.append("execution_state = ?")
            params.append(str(state))
        rows = self.db.query(
            "SELECT * FROM action_batches WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC",
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dump_row(row) or {}
            item["plan"] = json_column(row["plan_json"], default=None)
            out.append(item)
        return out

    def approve_batch(
        self,
        batch_id: str,
        actor: str,
        plan_hash: str,
        expires_at: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record plan-bound human authorization.

        The stored ``plan_hash`` must match the approved one: a changed plan is a
        different plan. Model and worker identities can never approve (PRD section 13.1).
        """
        if actor.startswith("agent:") or actor.startswith("worker:"):
            raise Forbidden(
                "Only an authenticated human reviewer can approve a plan.",
                code=Code.APPROVAL_MUST_BE_HUMAN,
                detail={"reason": "non_human_approver"},
            )
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="batch.approve",
            entity_type="batch",
            entity_id=batch_id,
            request_id=request_id,
        ) as conn:
            row = conn.execute(
                "SELECT * FROM action_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFound(
                    "That action batch does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "batch"},
                )
            if str(row["plan_hash"]) != str(plan_hash):
                raise Conflict(
                    "The approved plan hash does not match the stored plan.",
                    code=Code.PLAN_HASH_MISMATCH,
                    detail={"batch_id": batch_id},
                )
            if str(row["execution_state"]) != ExecutionState.PLANNED.value:
                raise Conflict(
                    "This batch has already moved past planning.",
                    code=Code.BATCH_ALREADY_STARTED,
                    detail={"batch_id": batch_id, "state": str(row["execution_state"])},
                )
            now = now_iso()
            conn.execute(
                "UPDATE action_batches SET approval_actor = ?, approval_time = ?, "
                "approval_expires_at = ?, execution_state = 'approved', updated_at = ? WHERE id = ?",
                (actor, now, expires_at, now, batch_id),
            )
            updated = conn.execute(
                "SELECT * FROM action_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            item = dump_row(updated) or {}
            item["plan"] = json_column(updated["plan_json"], default=None)
            return item

    def set_batch_state(
        self,
        batch_id: str,
        state: str,
        expected_execution_revision: int,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> int:
        """Transition a batch, bumping ``execution_revision`` under an optimistic check.

        The bumped revision is what makes a replayed apply request unable to repeat
        committed work (PRD sections 13.1, 13.3).
        """
        state_value = str(state)
        if state_value not in {s.value for s in ExecutionState}:
            raise ValidationFailed(
                "Unsupported batch state.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "state"},
            )
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="batch.state",
            entity_type="batch",
            entity_id=batch_id,
            new={"execution_state": state_value},
        ) as conn:
            row = conn.execute(
                "SELECT * FROM action_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFound(
                    "That action batch does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "batch"},
                )
            current_revision = int(row["execution_revision"])
            if current_revision != int(expected_execution_revision):
                raise RevisionConflict(
                    "The batch execution state changed since it was read.",
                    current_value=str(row["execution_state"]),
                    current_revision=current_revision,
                )
            now = now_iso()
            started_at = row["started_at"] or (now if state_value == ExecutionState.APPLYING.value else None)
            finished_at = row["finished_at"] or (now if state_value in _BATCH_TERMINAL_STATES else None)
            new_revision = current_revision + 1
            conn.execute(
                "UPDATE action_batches SET execution_state = ?, execution_revision = ?, "
                "error_code = ?, error_detail = ?, started_at = ?, finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    state_value,
                    new_revision,
                    error_code,
                    error_detail,
                    started_at,
                    finished_at,
                    now,
                    batch_id,
                ),
            )
            return new_revision

    def record_planned_source_identity(self, operation_id: str, source_identity: str | None) -> None:
        """Capture a planned operation's source identity for the journal (PRD 13.3).

        Called by the planner while it builds each operation, before any batch or
        operation row exists. The value is held in memory and written to
        ``file_operations.source_identity`` by :meth:`create_file_operations` when
        the durable rows are created, so the identity recorded is the one the plan
        actually verified against -- not one re-observed later.

        This writes nothing to the database: planning must remain read-only
        (PRD 12.3). ``None`` records nothing, which recovery treats as
        "identity unavailable" and never papers over.
        """
        if source_identity:
            self._planned_source_identities[str(operation_id)] = str(source_identity)

    def set_file_operation_source_identity(self, operation_id: str, source_identity: str | None) -> None:
        """Set the operation-bound source identity on an existing journal row.

        The audited path for recording an identity after the row exists (for
        example when repairing a journal written before migration 0002). The
        column is a corroborating diagnostic, not an authorization input.
        """
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="operation.source_identity",
            entity_type="file_operation",
            entity_id=operation_id,
            new={"source_identity": source_identity},
        ) as conn:
            cur = conn.execute(
                "UPDATE file_operations SET source_identity = ?, updated_at = ? WHERE id = ?",
                (source_identity, now_iso(), operation_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That file operation does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "file_operation"},
                )

    def get_file_operation_source_identity(self, operation_id: str) -> str | None:
        """Read the operation-bound source identity, or ``None`` when unrecorded.

        ``FileOperationRecord`` predates migration 0002 and deliberately does not
        carry the column, so recovery resolves it through this method rather than
        widening a frozen model.
        """
        row = self.db.query_one(
            "SELECT source_identity FROM file_operations WHERE id = ?", (operation_id,)
        )
        if row is None:
            return None
        return row["source_identity"]

    def create_file_operations(
        self,
        batch_id: str,
        operations: Sequence[PlannedOperation],
        source_identities: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Write the durable journal rows for a batch before any file is touched.

        ``source_identities`` may supply the per-operation source identity
        explicitly; otherwise the value captured by the planner through
        :meth:`record_planned_source_identity` is used. Either way the identity
        binds the journal row to the physical file the plan was built from, so
        recovery can tell a completed move from a copy (PRD section 13.3).
        """
        explicit = source_identities or {}
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="batch.operations",
            entity_type="batch",
            entity_id=batch_id,
            affected_ids=[op.operation_id for op in operations],
        ) as conn:
            now = now_iso()
            ids: list[str] = []
            for sequence, op in enumerate(operations):
                identity = explicit.get(op.operation_id)
                if identity is None:
                    identity = self._planned_source_identities.pop(op.operation_id, None)
                conn.execute(
                    "INSERT INTO file_operations (id, instance_id, batch_id, document_id, sequence, "
                    "kind, source_rel_path, destination_rel_path, expected_sha256, expected_size, "
                    "source_revision, decision_revision, intent_revision, location_version, "
                    "source_identity, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                    (
                        op.operation_id,
                        self.instance_id,
                        batch_id,
                        op.document_id,
                        sequence,
                        op.kind.value if isinstance(op.kind, PendingIntent) else str(op.kind),
                        op.source,
                        op.destination,
                        op.expected_sha256,
                        op.expected_size,
                        int(op.source_revision),
                        int(op.decision_revision),
                        int(op.intent_revision),
                        int(op.location_version),
                        identity,
                        now,
                        now,
                    ),
                )
                ids.append(op.operation_id)
            return ids

    def update_file_operation(
        self,
        operation_id: str,
        state: str,
        observed_source_state: str | None = None,
        observed_destination_state: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Advance one operation's durable step and record observed filesystem state."""
        state_value = str(state)
        if state_value not in {s.value for s in OperationState}:
            raise ValidationFailed(
                "Unsupported operation state.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "state"},
            )
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="operation.update",
            entity_type="file_operation",
            entity_id=operation_id,
            new={"state": state_value},
        ) as conn:
            cur = conn.execute(
                "UPDATE file_operations SET state = ?, observed_source_state = ?, "
                "observed_destination_state = ?, error_code = ?, error_detail = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    state_value,
                    observed_source_state,
                    observed_destination_state,
                    error_code,
                    error_detail,
                    now_iso(),
                    operation_id,
                ),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That file operation does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "file_operation"},
                )

    def list_file_operations(self, batch_id: str) -> list[FileOperationRecord]:
        rows = self.db.query(
            "SELECT * FROM file_operations WHERE batch_id = ? ORDER BY sequence ASC",
            (batch_id,),
        )
        return [file_operation_from_row(r) for r in rows]

    def find_operations_in_state(self, states: Sequence[str]) -> list[FileOperationRecord]:
        """Operations currently in any of ``states``. Used by crash reconciliation."""
        values = [str(s) for s in states]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        rows = self.db.query(
            "SELECT * FROM file_operations WHERE instance_id = ? AND state IN ("
            + placeholders
            + ") ORDER BY created_at ASC, id ASC",
            tuple([self.instance_id, *values]),
        )
        return [file_operation_from_row(r) for r in rows]

    # ==================================================================
    # Durable queue (PRD 6.4)
    # ==================================================================
    def enqueue_job(
        self,
        job_key: str,
        kind: str,
        document_id: str | None = None,
        input_versions: Mapping[str, Any] | None = None,
        priority: int = 100,
    ) -> str:
        """Enqueue durable work, idempotent on ``(instance_id, job_key)``.

        A replay or retry returns the existing job id rather than creating a second
        job for the same inputs (PRD sections 6.4, 12.2).
        """
        if str(kind) not in _JOB_KINDS:
            raise ValidationFailed(
                "Unsupported job kind.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "kind"},
            )
        existing = self.db.query_one(
            "SELECT id FROM processing_jobs WHERE instance_id = ? AND job_key = ?",
            (self.instance_id, job_key),
        )
        if existing is not None:
            return str(existing["id"])
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="job.enqueue",
            entity_type="job",
            entity_id=document_id,
        ) as conn:
            again = conn.execute(
                "SELECT id FROM processing_jobs WHERE instance_id = ? AND job_key = ?",
                (self.instance_id, job_key),
            ).fetchone()
            if again is not None:
                return str(again["id"])
            job_id = new_id("job")
            now = now_iso()
            conn.execute(
                "INSERT INTO processing_jobs (id, instance_id, job_key, kind, document_id, "
                "input_versions, state, priority, attempts, max_attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, 3, ?, ?)",
                (
                    job_id,
                    self.instance_id,
                    job_key,
                    str(kind),
                    document_id,
                    encode_json(dict(input_versions or {})),
                    int(priority),
                    now,
                    now,
                ),
            )
            return job_id

    def _claimable_clause(self) -> str:
        return (
            "cancel_requested = 0 AND attempts < max_attempts AND "
            "(state = 'queued' OR "
            "(state IN ('leased','running') AND lease_expires_at IS NOT NULL AND lease_expires_at < ?))"
        )

    def claim_job(
        self, lease_owner: str, lease_seconds: float, kinds: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        """Atomically lease the next runnable job, skipping live leases.

        The selection and the lease update happen in one write transaction, so two
        workers cannot both believe they own the same job.
        """
        now = now_iso()
        params: list[Any] = [self.instance_id, now]
        kind_clause = ""
        if kinds:
            values = [str(k) for k in kinds]
            kind_clause = " AND kind IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        candidate = self.db.query_one(
            "SELECT id FROM processing_jobs WHERE instance_id = ? AND " + self._claimable_clause()
            + kind_clause + " ORDER BY priority ASC, created_at ASC LIMIT 1",
            tuple(params),
        )
        if candidate is None:
            return None
        candidate_id = str(candidate["id"])

        with self.db.write(
            actor=lease_owner,
            actor_kind="helper",
            event="job.claim",
            entity_type="job",
            entity_id=candidate_id,
        ) as conn:
            fresh_now = now_iso()
            row = conn.execute(
                "SELECT * FROM processing_jobs WHERE id = ? AND " + self._claimable_clause(),
                (candidate_id, fresh_now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE processing_jobs SET state = 'leased', lease_token = ?, lease_expires_at = ?, "
                "lease_owner = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (
                    new_token(),
                    seconds_from_now_iso(lease_seconds),
                    lease_owner,
                    fresh_now,
                    candidate_id,
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (candidate_id,)
            ).fetchone()
            return dump_row(claimed)

    def complete_job(self, job_id: str, result_ref: str | None = None) -> None:
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="job.complete",
            entity_type="job",
            entity_id=job_id,
        ) as conn:
            now = now_iso()
            cur = conn.execute(
                "UPDATE processing_jobs SET state = 'succeeded', result_ref = ?, "
                "lease_token = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                (result_ref, now, now, job_id),
            )
            if not cur.rowcount:
                raise NotFound(
                    "That job does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "job"},
                )

    def fail_job(self, job_id: str, error_code: str, error_detail: str) -> bool:
        """Record a failure. Returns ``True`` when the job will be retried.

        ``attempts`` was incremented at claim time, so the retry decision compares it
        against ``max_attempts`` (PRD section 6.4).
        """
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="job.fail",
            entity_type="job",
            entity_id=job_id,
            code=error_code,
        ) as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(
                    "That job does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "job"},
                )
            retry = int(row["attempts"]) < int(row["max_attempts"])
            now = now_iso()
            conn.execute(
                "UPDATE processing_jobs SET state = ?, error_code = ?, error_detail = ?, "
                "lease_token = NULL, lease_expires_at = NULL, lease_owner = NULL, "
                "finished_at = ?, updated_at = ? WHERE id = ?",
                (
                    "queued" if retry else "failed",
                    error_code,
                    error_detail,
                    None if retry else now,
                    now,
                    job_id,
                ),
            )
            return retry

    def cancel_job(self, job_id: str) -> None:
        """Cancel work not yet started; a running job is asked to stop instead."""
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="job.cancel",
            entity_type="job",
            entity_id=job_id,
        ) as conn:
            row = conn.execute(
                "SELECT state FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(
                    "That job does not exist.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "job"},
                )
            now = now_iso()
            if str(row["state"]) in ("queued", "leased"):
                conn.execute(
                    "UPDATE processing_jobs SET state = 'canceled', cancel_requested = 1, "
                    "lease_token = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (now, now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE processing_jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (now, job_id),
                )

    def reap_expired_leases(self) -> int:
        """Return expired leases to ``queued`` so a restart reclaims the work.

        A lease whose attempts are already exhausted is marked ``failed`` instead:
        re-queuing it would create a job that can never be claimed, which looks like
        a silent stall rather than the terminal failure it is (PRD section 6.4).
        """
        now = now_iso()
        expiring = (
            "instance_id = ? AND state IN ('leased','running') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?"
        )
        pending = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM processing_jobs WHERE " + expiring,
                (self.instance_id, now),
                default=0,
            )
            or 0
        )
        if pending == 0:
            return 0
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="job.reap_leases",
        ) as conn:
            retried = conn.execute(
                "UPDATE processing_jobs SET state = 'queued', lease_token = NULL, "
                "lease_expires_at = NULL, lease_owner = NULL, updated_at = ? "
                "WHERE " + expiring + " AND attempts < max_attempts",
                (now, self.instance_id, now),
            ).rowcount
            exhausted = conn.execute(
                "UPDATE processing_jobs SET state = 'failed', lease_token = NULL, "
                "lease_expires_at = NULL, lease_owner = NULL, error_code = ?, "
                "updated_at = ?, finished_at = ? "
                "WHERE " + expiring + " AND attempts >= max_attempts",
                (Code.LEASE_HELD, now, now, self.instance_id, now),
            ).rowcount
            return int(retried or 0) + int(exhausted or 0)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return dump_row(self.db.query_one("SELECT * FROM processing_jobs WHERE id = ?", (job_id,)))

    def list_jobs(
        self, state: str | None = None, document_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["instance_id = ?"]
        params: list[Any] = [self.instance_id]
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state))
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        rows = self.db.query(
            "SELECT * FROM processing_jobs WHERE " + " AND ".join(clauses)
            + " ORDER BY priority ASC, created_at ASC",
            tuple(params),
        )
        return [dump_row(r) or {} for r in rows]
