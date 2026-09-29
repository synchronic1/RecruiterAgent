"""Read endpoints: status, the document list, document detail, and the original stream.

Authority: PRD section 12.1 (endpoint surface), section 12.2 (mutation rules),
section 10 (the five independent state dimensions), section 11.2 (stable
pagination with a deterministic ID tie-breaker), section 8.2 (table controls), and
acceptance tests AT-15, AT-17. The document payload follows the frozen seam in
``docs/contracts/report-payload.md`` so the connected page and the file snapshot
render the same fields.

Registration contract (``api/app.py``): this module exposes ``register(router)``.
It also composes the ``review`` and ``tasks`` endpoint modules, which are not in
``DEFAULT_ROUTE_MODULES``; they are registered from here so a single discovery
entry publishes the whole review surface. The imports are deliberately lazy so no
import cycle forms with the helpers those modules import back from this one.

Two rules this module enforces rather than trusts:

* **The original stream is resolved from the document id only.** The caller's id
  selects a database row; the row's ``current_rel_path`` is re-validated with
  :mod:`resume_review.storage.paths` and confined to the workspace root. No request
  input is ever turned into a path, and no filesystem path appears in a response.
* **The five state dimensions stay separate.** ``processing_state``, the review
  disposition, ``location``, ``pending_intent``, and the file-operation state are
  distinct fields. Nothing here collapses them into one "status".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from fastapi import Depends, Query
from fastapi.responses import FileResponse

from .. import SCHEMA_VERSION, __version__ as APP_VERSION
from ..errors import Code, NotFound, ValidationFailed
from ..models import (
    DocumentRecord,
    Location,
    MediaType,
    OperationState,
    ProcessingState,
    REPORT_FILENAME,
    REVIEW_DIR,
    ReviewState,
    Role,
)
from ..reporting import REPORT_SCHEMA_VERSION
from ..storage import (
    assert_no_reparse_traversal,
    assert_within_root,
    normalize_rel_path,
    safe_final_component,
)
from ..util import parse_iso
from .deps import (
    ApiRuntime,
    InstanceContext,
    get_request_id,
    get_runtime,
    require_operation,
    resolve_document,
)
from .envelope import ok_response

__all__ = [
    "register",
    "workspace_root",
    "resolve_original_path",
    "serialize_document",
    "serialize_note",
    "serialize_task",
    "MEDIA_CONTENT_TYPES",
]

#: The only content types we ever hand a browser for a stored original. Anything
#: else is served as opaque bytes, never sniffed into a rendering type.
MEDIA_CONTENT_TYPES: dict[str, str] = {
    MediaType.PDF.value: "application/pdf",
    MediaType.DOCX.value: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    MediaType.TXT.value: "text/plain; charset=utf-8",
    MediaType.UNSUPPORTED.value: "application/octet-stream",
    MediaType.UNKNOWN.value: "application/octet-stream",
}

_MEDIA_VALUES = {m.value for m in MediaType}
_PROCESSING_VALUES = {p.value for p in ProcessingState}
_LOCATION_VALUES = {loc.value for loc in Location}
_REVIEW_VALUES = {r.value for r in ReviewState}

#: Sort aliases the table offers, mapped onto the repository's whitelisted keys.
_SORT_ALIASES: dict[str, str] = {
    "name": "display_name",
    "filename": "original_filename",
    "decision": "review_state",
    "task_count": "open_task_count",
    "path": "current_rel_path",
    "document": "document_id",
}

#: Fields a caller may filter on through the ``filter`` JSON parameter. Kept to
#: scalar document columns; criterion/evidence predicates belong to the criteria
#: surface and are refused here rather than silently ignored.
_FILTER_FIELDS = {
    "processing_state",
    "location",
    "review_state",
    "media_type",
    "search",
    "duplicate_content",
    "decision_needs_recheck",
    "document_ids",
}

_MAX_SEARCH_LENGTH = 200
_MAX_FILTER_IDS = 5000


# ---------------------------------------------------------------------------
# Workspace and original-file resolution
# ---------------------------------------------------------------------------
def workspace_root(ctx: InstanceContext) -> Path:
    """The workspace root for the instance served by ``ctx``.

    The frozen layout (PRD section 4) places the live database at
    ``<root>/.review/review.db``, so the root is the database file's grandparent. A
    database not under a ``.review`` directory is treated as living at the root
    itself; containment checks below then apply to that directory. This derives the
    root from the database the API was already given, never from a request.
    """
    db_file = Path(ctx.db.path)
    review = db_file.parent
    if review.name == REVIEW_DIR:
        return review.parent
    return review


def resolve_original_path(root: str | Path, rel_path: str) -> Path:
    """Resolve a stored relative path to an existing file inside ``root``.

    Raises :class:`~resume_review.storage.paths.PathEscape` (422 ``PATH_ESCAPE``)
    for a traversal, absolute, drive-relative, UNC, device-namespace, ADS, or
    reserved-name path; :class:`~resume_review.storage.paths.SymlinkEscape`
    (422 ``SYMLINK_REJECTED``) when any component is a symlink or reparse point;
    and a 404 ``FILE_MISSING`` when the validated path is absent or not a regular
    file. It never returns a path outside the root and never reveals one.
    """
    norm = normalize_rel_path(rel_path)
    candidate = assert_no_reparse_traversal(root, norm, allow_final_absent=True)
    assert_within_root(root, candidate)
    if not candidate.is_file():
        raise NotFound(
            "The original document is not available in this workspace.",
            code=Code.FILE_MISSING,
            detail={"entity": "document", "reason": "absent"},
        )
    return candidate


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def _document_link(rel_path: str) -> str:
    """A relative, safely encoded link to the stored original."""
    return "./" + quote(str(rel_path), safe="/")


def _warnings_for(
    record: DocumentRecord,
    decision_needs_recheck: bool,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if record.duplicate_content:
        warnings.append(
            {
                "code": "DUPLICATE_CONTENT",
                "message": "Identical bytes are registered at more than one path.",
            }
        )
    if decision_needs_recheck:
        warnings.append(
            {
                "code": "DECISION_NEEDS_RECHECK",
                "message": "Material inputs changed after this decision; review it again.",
            }
        )
    if record.processing_state is ProcessingState.MANUAL_REVIEW:
        warnings.append(
            {
                "code": "MANUAL_REVIEW",
                "message": "This document needs manual review before it can be assessed.",
            }
        )
    if record.processing_state is ProcessingState.STALE:
        warnings.append(
            {
                "code": "ANALYSIS_STALE_RESULT",
                "message": "A relevant input changed; the stored assessment is stale.",
            }
        )
    return warnings


def serialize_document(
    record: DocumentRecord,
    *,
    decision: Any,
    intent: Any,
    tasks: Sequence[Any] = (),
    profile: Any = None,
    file_actions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Render one document with its independent state dimensions kept apart.

    ``decision`` and ``intent`` are the reviewer-owned records; ``tasks`` are the
    review tasks for the document; ``file_actions`` is the action-execution
    dimension. Absence of a decision is ``unreviewed``, which is a value, not a
    zero or a negative judgement.
    """
    open_tasks = [t for t in tasks if str(getattr(t.state, "value", t.state)) == "open"]
    task_warning = any(str(getattr(t, "severity", "normal")) == "attention" for t in open_tasks)
    needs_recheck = bool(
        record.decision_needs_recheck or getattr(decision, "needs_recheck", False)
    )
    return {
        "document_id": record.id,
        "display_name": record.display_name,
        "original_filename": record.original_filename,
        "current_rel_path": record.current_rel_path,
        "media_type": record.media_type.value,
        "size_bytes": record.size_bytes,
        "content_sha256": record.content_sha256,
        "current_revision": record.current_revision,
        "ingested_at": record.ingested_at,
        "submitted_at": record.submitted_at,
        "processing_state": record.processing_state.value,
        "processing_detail": record.processing_detail,
        "location": record.location.value,
        "location_version": record.location_version,
        "review_state": decision.disposition.value,
        "decision_revision": decision.decision_revision,
        "decision_actor": decision.actor,
        "decided_at": decision.decided_at,
        "decision_needs_recheck": needs_recheck,
        "recheck_reason": record.recheck_reason,
        "disposition_frozen": bool(decision.disposition_frozen),
        "pending_intent": intent.intent.value,
        "intent_revision": intent.intent_revision,
        "intent_state": intent.state,
        "duplicate_content": bool(record.duplicate_content),
        "duplicate_of": record.duplicate_of,
        "open_task_count": len(open_tasks),
        "task_warning": task_warning,
        "summary_text": getattr(profile, "summary_text", None) if profile else None,
        "summary_stale": bool(getattr(profile, "stale", False)) if profile else False,
        "file_actions": list(file_actions),
        "document_link": _document_link(record.current_rel_path),
        "warnings": _warnings_for(record, needs_recheck),
    }


def serialize_note(note: Any) -> dict[str, Any]:
    return {
        "id": note.id,
        "document_id": note.document_id,
        "body": note.body,
        "author": note.author,
        "note_revision": note.note_revision,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def serialize_task(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "document_id": task.document_id,
        "title": task.title,
        "task_type": task.task_type,
        "criterion_id": task.criterion_id,
        "source_revision": task.source_revision,
        "origin": task.origin.value,
        "detail": task.detail,
        "state": task.state.value,
        "severity": task.severity,
        "resolution": task.resolution,
        "resolution_note": task.resolution_note,
        "closed_by": task.closed_by,
        "closed_at": task.closed_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _operations_by_document(ctx: InstanceContext) -> dict[str, list[dict[str, Any]]]:
    """File-operation outcomes per document, from one query.

    This is the "action execution" dimension, kept separate from the pending
    intent: an intent is what a human asked for, an operation state is what the
    executor actually did.
    """
    states = [s.value for s in OperationState]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for op in ctx.repository.find_operations_in_state(states):
        grouped.setdefault(op.document_id, []).append(
            {
                "batch_id": op.batch_id,
                "kind": op.kind.value,
                "state": op.state.value,
                "sequence": op.sequence,
                "source": op.source_rel_path,
                "destination": op.destination_rel_path,
                "error_code": op.error_code,
            }
        )
    return grouped


def _serialize_row(
    ctx: InstanceContext,
    record: DocumentRecord,
    file_actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    decision = ctx.repository.get_decision(record.id)
    intent = ctx.repository.get_intent(record.id)
    tasks = ctx.repository.list_tasks(document_id=record.id)
    profile = ctx.repository.current_profile(record.id)
    return serialize_document(
        record,
        decision=decision,
        intent=intent,
        tasks=tasks,
        profile=profile,
        file_actions=file_actions,
    )


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------
def _one_of(value: Any, allowed: set[str], field: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text not in allowed:
        raise ValidationFailed(
            "The filter value is not recognized.",
            code=Code.FILTER_UNKNOWN_VALUE,
            detail={"field": field},
        )
    return text


def _parse_filter_json(raw: str | None) -> dict[str, Any]:
    """Parse the ``filter`` query parameter into an allowed scalar-filter object.

    A flat object of registered scalar fields only. A criterion/evidence predicate
    is refused with ``FILTER_FIELD_NOT_ALLOWED`` rather than silently ignored, so a
    caller never believes an unapplied filter was applied.
    """
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValidationFailed(
            "The filter parameter is not valid JSON.",
            code=Code.FILTER_UNKNOWN_VALUE,
            detail={"field": "filter"},
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ValidationFailed(
            "The filter parameter must be a JSON object.",
            code=Code.FILTER_UNKNOWN_VALUE,
            detail={"field": "filter"},
        )
    out: dict[str, Any] = {}
    for key, value in parsed.items():
        name = str(key)
        if name.startswith("criterion:") or name not in _FILTER_FIELDS:
            raise ValidationFailed(
                "The filter names a field this endpoint does not support.",
                code=Code.FILTER_FIELD_NOT_ALLOWED,
                detail={"field": name},
            )
        out[name] = value
    return out


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValidationFailed(
        "The filter value is not recognized.",
        code=Code.FILTER_UNKNOWN_VALUE,
        detail={"field": field},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def register(router: Any) -> None:
    """Attach the read endpoints, then the review and task endpoints."""

    @router.get("/status", name="status")
    def status(
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        request_id: str = Depends(get_request_id),
    ):
        """Health, versions, counts, queue depth, and snapshot status (PRD 12.1)."""
        instance = ctx.repository.get_instance() or {}
        diagnostics = ctx.db.diagnostics()
        counts = ctx.repository.status_counts()
        jobs = ctx.repository.list_jobs()
        by_state: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for job in jobs:
            state_name = str(job.get("state", "unknown"))
            kind_name = str(job.get("kind", "unknown"))
            by_state[state_name] = by_state.get(state_name, 0) + 1
            by_kind[kind_name] = by_kind.get(kind_name, 0) + 1

        data = {
            "instance_id": ctx.instance_id,
            "health": {"ok": True, "backend": diagnostics.get("backend", "sqlite3")},
            "versions": {
                "app_version": str(instance.get("app_version") or APP_VERSION),
                "schema_version": int(instance.get("schema_version") or SCHEMA_VERSION),
                "sqlite_version": diagnostics.get("sqlite_version"),
                "report_schema_version": REPORT_SCHEMA_VERSION,
            },
            "counts": dict(counts),
            "queue": {
                "total": len(jobs),
                "by_state": by_state,
                "by_kind": by_kind,
                "pending": (
                    by_state.get("queued", 0)
                    + by_state.get("leased", 0)
                    + by_state.get("running", 0)
                ),
            },
            "snapshot": _snapshot_status(ctx),
        }
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.get("/documents", name="list_documents")
    def list_documents(
        page: int = Query(1, ge=1),
        page_size: int | None = Query(None, ge=1),
        sort: str = Query("ingested_at"),
        direction: str = Query("asc"),
        processing_state: str | None = Query(None),
        location: str | None = Query(None),
        review_state: str | None = Query(None),
        search: str | None = Query(None),
        document_ids: list[str] | None = Query(None),
        filter: str | None = Query(None),
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        runtime: ApiRuntime = Depends(get_runtime),
        request_id: str = Depends(get_request_id),
    ):
        """A paginated, filtered, stably ordered document list (AT-15, AT-17)."""
        cfg = runtime.config
        size = page_size if page_size is not None else cfg.default_page_size
        if size > cfg.max_page_size:
            raise ValidationFailed(
                "The requested page size is larger than this helper allows.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "page_size", "max": cfg.max_page_size},
            )
        if str(direction).lower() not in ("asc", "desc"):
            raise ValidationFailed(
                "Unsupported sort direction.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "direction"},
            )
        direction_value = str(direction).lower()

        parsed = _parse_filter_json(filter)
        ps = _one_of(
            processing_state if processing_state is not None else parsed.get("processing_state"),
            _PROCESSING_VALUES,
            "processing_state",
        )
        loc = _one_of(
            location if location is not None else parsed.get("location"),
            _LOCATION_VALUES,
            "location",
        )
        rev = _one_of(
            review_state if review_state is not None else parsed.get("review_state"),
            _REVIEW_VALUES,
            "review_state",
        )
        media = _one_of(parsed.get("media_type"), _MEDIA_VALUES, "media_type")

        search_term = search if search is not None else parsed.get("search")
        if search_term is not None:
            search_term = str(search_term)
            if len(search_term) > _MAX_SEARCH_LENGTH:
                raise ValidationFailed(
                    "The search term is too long.",
                    code=Code.INVALID_INPUT,
                    detail={"field": "search", "max": _MAX_SEARCH_LENGTH},
                )

        ids: list[str] | None = list(document_ids) if document_ids else None
        if ids is None and parsed.get("document_ids") is not None:
            raw_ids = parsed["document_ids"]
            if not isinstance(raw_ids, (list, tuple)):
                raise ValidationFailed(
                    "The filter value is not recognized.",
                    code=Code.FILTER_UNKNOWN_VALUE,
                    detail={"field": "document_ids"},
                )
            ids = [str(i) for i in raw_ids]
        if ids is not None and len(ids) > _MAX_FILTER_IDS:
            raise ValidationFailed(
                "The explicit document set is too large.",
                code=Code.INVALID_INPUT,
                detail={"field": "document_ids", "max": _MAX_FILTER_IDS},
            )

        duplicate_flag = (
            _as_bool(parsed["duplicate_content"], "duplicate_content")
            if "duplicate_content" in parsed
            else None
        )
        recheck_flag = (
            _as_bool(parsed["decision_needs_recheck"], "decision_needs_recheck")
            if "decision_needs_recheck" in parsed
            else None
        )

        sort_key = _SORT_ALIASES.get(str(sort), str(sort))
        # The repository applies its whitelist and appends the document_id
        # tie-breaker, which is what makes paging reproducible (PRD 11.2).
        rows = ctx.repository.list_documents(
            sort=sort_key,
            direction=direction_value,
            processing_state=ps,
            location=loc,
            review_state=rev,
            document_ids=ids,
        )
        rows = [
            r
            for r in rows
            if r.archived_at is None
            and (media is None or r.media_type.value == media)
            and (duplicate_flag is None or bool(r.duplicate_content) is duplicate_flag)
            and (recheck_flag is None or bool(r.decision_needs_recheck) is recheck_flag)
            and (search_term is None or _matches_search(r, search_term))
        ]

        total = len(rows)
        page_count = max(1, (total + size - 1) // size)
        start = (page - 1) * size
        page_rows = rows[start : start + size]

        ops_map = _operations_by_document(ctx)
        documents = [
            _serialize_row(ctx, record, ops_map.get(record.id, ()))
            for record in page_rows
        ]

        counts = ctx.repository.status_counts()
        counts["filtered"] = total
        data = {
            "documents": documents,
            "page": page,
            "page_size": size,
            "total": total,
            "page_count": page_count,
            "has_more": page * size < total,
            "sort": sort_key,
            "direction": direction_value,
            "counts": counts,
        }
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.get("/documents/{document_id}", name="document_detail")
    def document_detail(
        record: DocumentRecord = Depends(resolve_document),
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        request_id: str = Depends(get_request_id),
    ):
        """One document with its evidence, assessments, notes, and tasks."""
        decision = ctx.repository.get_decision(record.id)
        intent = ctx.repository.get_intent(record.id)
        tasks = ctx.repository.list_tasks(document_id=record.id)
        profile = ctx.repository.current_profile(record.id)
        ops_map = _operations_by_document(ctx)

        evidence: list[dict[str, Any]] = []
        criteria: list[dict[str, Any]] = []
        if profile is not None:
            for item in ctx.repository.evidence_for_profile(profile.id):
                evidence.append(
                    {
                        "id": item.get("evidence_key") or item.get("id"),
                        "criterion_id": item.get("criterion_id"),
                        "span_id": item.get("span_id"),
                        "quote": item.get("quote"),
                        "locator": item.get("locator") or {},
                        "result": item.get("result"),
                        "claim_kind": item.get("claim_kind"),
                        "validation": item.get("validation"),
                    }
                )
            by_criterion: dict[str, dict[str, Any]] = {}
            for item in evidence:
                criterion_id = item.get("criterion_id")
                if not criterion_id:
                    continue
                entry = by_criterion.setdefault(
                    str(criterion_id),
                    {
                        "criterion_id": str(criterion_id),
                        "result": item.get("result"),
                        "explanation": "",
                        "evidence_ids": [],
                    },
                )
                if item.get("id"):
                    entry["evidence_ids"].append(item["id"])
            criteria = list(by_criterion.values())

        data = serialize_document(
            record,
            decision=decision,
            intent=intent,
            tasks=tasks,
            profile=profile,
            file_actions=ops_map.get(record.id, ()),
        )
        data["criteria"] = criteria
        data["evidence"] = evidence
        data["tasks"] = [serialize_task(t) for t in tasks]
        data["notes"] = [serialize_note(n) for n in ctx.repository.list_notes(record.id)]

        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.get("/documents/{document_id}/original", name="document_original")
    def document_original(
        record: DocumentRecord = Depends(resolve_document),
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
    ):
        """Stream the stored original, resolved only from the document id.

        The path is the row's validated ``current_rel_path`` confined to the
        workspace root; the caller's id is never turned into a path and the
        response carries no filesystem path. Served as an attachment so an
        applicant-supplied document is never rendered in the helper's origin.
        """
        root = workspace_root(ctx)
        path = resolve_original_path(root, record.current_rel_path)
        download_name = safe_final_component(
            record.display_name or record.original_filename or record.id
        )
        content_type = MEDIA_CONTENT_TYPES.get(
            record.media_type.value, "application/octet-stream"
        )
        return FileResponse(
            path=str(path),
            media_type=content_type,
            filename=download_name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    # The review and task endpoints share this module's helpers, so they are
    # registered here rather than discovered separately. Imported lazily to keep
    # the module import graph acyclic.
    from . import review as review_module
    from . import tasks as tasks_module

    review_module.register(router)
    tasks_module.register(router)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _matches_search(record: DocumentRecord, term: str) -> bool:
    needle = term.casefold()
    for value in (record.display_name, record.original_filename, record.current_rel_path):
        if value and needle in str(value).casefold():
            return True
    return False


def _snapshot_status(ctx: InstanceContext) -> dict[str, Any]:
    """Report the snapshot report's presence and staleness, never its path.

    Staleness compares the report file's modification time against the instance's
    last committed mutation time; a report older than the latest mutation does not
    include current state and is reported stale.
    """
    root = workspace_root(ctx)
    report = root / REPORT_FILENAME
    info: dict[str, Any] = {
        "name": REPORT_FILENAME,
        "exists": False,
        "byte_size": 0,
        "modified_at": None,
        "stale": False,
    }
    try:
        stat = report.stat()
    except OSError:
        return info
    info["exists"] = True
    info["byte_size"] = int(stat.st_size)
    modified_at = _mtime_iso(stat.st_mtime)
    info["modified_at"] = modified_at
    instance = ctx.repository.get_instance() or {}
    updated_at = instance.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            info["stale"] = parse_iso(updated_at) > parse_iso(modified_at)
        except (ValueError, TypeError):
            info["stale"] = True
    return info


def _mtime_iso(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
