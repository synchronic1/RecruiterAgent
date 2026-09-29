"""Assemble the review payload from the database.

Authority: PRD sections 8.1, 8.2, 8.5 and 9.3, and the frozen seam in
``docs/contracts/report-payload.md`` (section 2, "Snapshot payload").

This module is pure assembly: it reads committed state and emits a plain ``dict``
whose shape is fixed by the contract. It never renders HTML, never escapes for a
markup context (that is ``snapshot.py``'s job) and never performs inference.

Contract rules encoded here:

* Only *approved* criteria appear. A proposal (``approved_at IS NULL``) is not part
  of the review surface yet and never reaches the report.
* Unknown is ``null``, never ``0`` or ``""``. An absent display name is unknown; an
  absent summary is unknown; a criterion with no assessment has ``result: null``.
* The whole-instance count and the filtered count are both always present, so a
  filtered view can never be mistaken for the total population.
* No aggregate suitability score, rank, or inferred-quality ordering is emitted
  anywhere (PRD section 10 and the contract's "Rules the renderer must obey").

The database layer is reached only through the small read surface of
``db.connection.Database`` (``query``/``query_one``/``scalar``/``instance_id``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import quote

from ..models import (
    MediaType,
    ProcessingState,
    ReviewState,
    TaskState,
)
from ..util import now_iso

if TYPE_CHECKING:  # pragma: no cover - import cycle hygiene
    from ..db.connection import Database

__all__ = ["REPORT_SCHEMA_VERSION", "build_snapshot_payload"]

#: Contract version of the embedded payload (docs/contracts/report-payload.md).
REPORT_SCHEMA_VERSION = "1.0"

#: A document counts as "processed" once extraction/analysis has reached a terminal
#: state. ``stale`` is included because the document *was* processed and a
#: re-review, not a first pass, is what is owed for it.
_PROCESSED_STATES = frozenset({ProcessingState.READY.value, ProcessingState.STALE.value})

#: An intent is "pending" while it is waiting on a human decision or an approval.
#: Applied and cancelled intents are history, not pending work.
_PENDING_INTENT_STATES = frozenset({"saved", "planned", "blocked"})

#: Static, code-keyed warning messages. Messages are never assembled from stored
#: detail strings, because a pipeline detail could embed a path or applicant text
#: and error text must stay safe to display and to log (errors.py, module rules).
_WARNING_MESSAGES: dict[str, str] = {
    "SCAN_ONLY_DOCUMENT": "No extractable text was found; the document needs a human read.",
    "EXTRACTION_FAILED": "Processing failed for this document.",
    "UNSUPPORTED_FORMAT": "This file format is not supported for automated review.",
    "FILE_MISSING": "The file is no longer at its recorded location.",
    "NEEDS_RECONCILIATION": "The file's location is ambiguous and needs reconciliation.",
    "ANALYSIS_STALE_RESULT": "The stored assessment is stale because the input changed.",
    "DECISION_NEEDS_RECHECK": "A human decision exists for changed input and needs recheck.",
    "DUPLICATE_CONTENT": "Another submission has identical content; review them separately.",
}

#: ``DUPLICATE_CONTENT`` has no constant in ``errors.Code`` yet; it is a warning
#: label, not a raised error code, so it is declared locally rather than added to
#: the frozen error catalogue.
DUPLICATE_CONTENT_CODE = "DUPLICATE_CONTENT"


def _row(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from a ``sqlite3.Row`` without assuming it exists."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _as_bool(value: Any) -> bool:
    return bool(value)


def _parse_json(text: Any, default: Any) -> Any:
    import json

    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def document_link(rel_path: str | None) -> str:
    """A relative link to an original document, percent-encoded per segment.

    Each path segment is encoded on its own so the separators stay literal and the
    result cannot be read as a scheme, an authority, or a drive path. A ``..`` or
    ``.`` segment is additionally encoded, so a malformed stored path can never
    climb out of the workspace through the link.
    """
    if not rel_path:
        return "./"
    parts = rel_path.replace("\\", "/").split("/")
    encoded: list[str] = []
    for part in parts:
        if part in ("", ".", ".."):
            encoded.append(quote(part.replace(".", "%2E"), safe=""))
            continue
        encoded.append(quote(part, safe=""))
    return "./" + "/".join(encoded)


def _document_warnings(doc: Any, profile: Any) -> list[dict[str, str]]:
    """Derive the per-document warning list from committed state.

    Warnings are advisory and code-keyed. They explain a limitation; they are never
    an assessment of the applicant and never trigger an automatic decision.
    """
    warnings: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        if code in seen:
            return
        seen.add(code)
        warnings.append({"code": code, "message": _WARNING_MESSAGES[code]})

    if _row(doc, "location") == "missing":
        add("FILE_MISSING")
    elif _row(doc, "location") == "conflict":
        add("NEEDS_RECONCILIATION")

    if _row(doc, "media_type") == MediaType.UNSUPPORTED.value:
        add("UNSUPPORTED_FORMAT")

    state = _row(doc, "processing_state")
    if state == ProcessingState.ERROR.value:
        add("EXTRACTION_FAILED")
    elif state == ProcessingState.STALE.value:
        add("ANALYSIS_STALE_RESULT")
    elif state == ProcessingState.MANUAL_REVIEW.value:
        detail = str(_row(doc, "processing_detail") or "").lower()
        # A pipeline that found no text on a scanned page says so in its detail;
        # the code lets the renderer explain the limitation precisely.
        add("SCAN_ONLY_DOCUMENT" if "scan" in detail else "EXTRACTION_FAILED")

    if _as_bool(_row(doc, "duplicate_content")):
        add(DUPLICATE_CONTENT_CODE)

    if _as_bool(_row(doc, "decision_needs_recheck")):
        add("DECISION_NEEDS_RECHECK")

    if profile is not None and _as_bool(_row(profile, "stale")):
        add("ANALYSIS_STALE_RESULT")

    return warnings


def _evidence_for_criterion(rows: Sequence[Any]) -> tuple[Any, list[str]]:
    """Return the assessment result and supporting evidence ids for one criterion.

    The ``result`` is the first non-null model assessment recorded for the
    criterion; the ids enumerate every criterion-kind evidence row so a renderer can
    show invalid (quote-missing) evidence distinctly through its ``validation``.
    """
    result: Any = None
    ids: list[str] = []
    for row in rows:
        ids.append(str(_row(row, "evidence_key")))
        if result is None and _row(row, "result") is not None:
            result = _row(row, "result")
    return result, ids


def build_snapshot_payload(
    db: "Database",
    *,
    mode: str = "snapshot",
    generated_at: str | None = None,
    document_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the review payload defined by ``docs/contracts/report-payload.md``.

    ``document_ids`` restricts the ``documents`` array and every filtered count to
    an explicit, immutable set. ``counts.total`` always remains the whole-instance
    total, so an active filter is never mistaken for the full population. When it is
    ``None`` the whole instance is in scope and ``filtered == total``.
    """
    instance_id = db.instance_id
    generated = generated_at or now_iso()

    instance_row = db.query_one(
        "SELECT id, schema_version, app_version, state_revision, storage_mode "
        "FROM instances WHERE id = ?",
        (instance_id,),
    )
    job_row = db.query_one(
        "SELECT id, title, criteria_version FROM jobs WHERE instance_id = ? "
        "ORDER BY updated_at DESC, id DESC LIMIT 1",
        (instance_id,),
    )
    # Approved and not superseded: the only criteria the review surface may show.
    criteria_rows = db.query(
        "SELECT criterion_id, version, definition, label, rationale FROM criteria "
        "WHERE instance_id = ? AND approved_at IS NOT NULL AND superseded_at IS NULL "
        "ORDER BY criterion_id ASC, version ASC",
        (instance_id,),
    )
    last_analysis_at = db.scalar(
        "SELECT MAX(generated_at) FROM profiles WHERE instance_id = ?",
        (instance_id,),
        default=None,
    )

    approved_criteria: list[dict[str, Any]] = []
    approved_criterion_ids: list[str] = []
    for row in criteria_rows:
        criterion_id = str(_row(row, "criterion_id"))
        if criterion_id not in approved_criterion_ids:
            approved_criterion_ids.append(criterion_id)
        approved_criteria.append(
            {
                "criterion_id": criterion_id,
                "version": int(_row(row, "version") or 0),
                "definition": str(_row(row, "definition") or ""),
                "label": _row(row, "label"),
                "rationale": str(_row(row, "rationale") or ""),
            }
        )

    # The applied criteria version is the job's, which is bumped when a set is
    # approved; fall back to the highest approved definition version.
    if job_row is not None and _row(job_row, "criteria_version") is not None:
        criteria_version = int(_row(job_row, "criteria_version") or 0)
    elif approved_criteria:
        criteria_version = max(c["version"] for c in approved_criteria)
    else:
        criteria_version = 0

    doc_rows = db.query(
        "SELECT * FROM documents WHERE instance_id = ? AND archived_at IS NULL "
        "ORDER BY ingested_at ASC, id ASC",
        (instance_id,),
    )
    decision_map = {
        str(_row(r, "document_id")): r
        for r in db.query("SELECT * FROM decisions WHERE instance_id = ?", (instance_id,))
    }
    intent_map = {
        str(_row(r, "document_id")): r
        for r in db.query("SELECT * FROM action_intents WHERE instance_id = ?", (instance_id,))
    }
    profile_map = {
        str(_row(r, "document_id")): r
        for r in db.query(
            "SELECT * FROM profiles WHERE instance_id = ? AND is_current = 1",
            (instance_id,),
        )
    }

    task_map: dict[str, list[Any]] = {}
    for row in db.query("SELECT * FROM review_tasks WHERE instance_id = ?", (instance_id,)):
        task_map.setdefault(str(_row(row, "document_id")), []).append(row)

    note_map: dict[str, list[Any]] = {}
    for row in db.query(
        "SELECT * FROM notes WHERE instance_id = ? AND deleted_at IS NULL ORDER BY created_at ASC, id ASC",
        (instance_id,),
    ):
        note_map.setdefault(str(_row(row, "document_id")), []).append(row)

    evidence_map: dict[str, list[Any]] = {}
    for row in db.query(
        "SELECT e.* FROM evidence e JOIN profiles p ON e.profile_id = p.id "
        "WHERE e.instance_id = ? AND p.is_current = 1 ORDER BY e.id ASC",
        (instance_id,),
    ):
        evidence_map.setdefault(str(_row(row, "document_id")), []).append(row)

    file_map: dict[str, list[Any]] = {}
    for row in db.query(
        "SELECT batch_id, document_id, kind, state, destination_rel_path, updated_at, "
        "error_code FROM file_operations WHERE instance_id = ? "
        "ORDER BY created_at ASC, sequence ASC",
        (instance_id,),
    ):
        file_map.setdefault(str(_row(row, "document_id")), []).append(row)

    history_map: dict[str, list[Any]] = {}
    for row in db.query(
        "SELECT entity_id, actor, new_json, created_at, outcome FROM audit_events "
        "WHERE instance_id = ? AND entity_type = 'decision' ORDER BY seq ASC",
        (instance_id,),
    ):
        if _row(row, "outcome") != "ok":
            continue
        new_value = _parse_json(_row(row, "new_json"), None)
        if not isinstance(new_value, dict) or "disposition" not in new_value:
            continue
        history_map.setdefault(str(_row(row, "entity_id")), []).append(row)

    scope_rows = doc_rows
    if document_ids is not None:
        wanted = set(document_ids)
        scope_rows = [d for d in doc_rows if str(_row(d, "id")) in wanted]

    documents = [
        _document_payload(
            doc,
            decision=decision_map.get(str(_row(doc, "id"))),
            intent=intent_map.get(str(_row(doc, "id"))),
            profile=profile_map.get(str(_row(doc, "id"))),
            tasks=task_map.get(str(_row(doc, "id")), ()),
            notes=note_map.get(str(_row(doc, "id")), ()),
            evidence=evidence_map.get(str(_row(doc, "id")), ()),
            file_actions=file_map.get(str(_row(doc, "id")), ()),
            history=history_map.get(str(_row(doc, "id")), ()),
            approved_criterion_ids=approved_criterion_ids,
        )
        for doc in scope_rows
    ]

    counts = _counts(
        scope_rows,
        total=len(doc_rows),
        decision_map=decision_map,
        intent_map=intent_map,
        task_map=task_map,
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "generated_at": generated,
        "instance": {
            "instance_id": str(_row(instance_row, "id", instance_id)),
            "job_title": str(_row(job_row, "title") or "") if job_row is not None else None,
            "app_version": _row(instance_row, "app_version"),
            "schema_version": int(_row(instance_row, "schema_version") or 0),
            "state_revision": int(_row(instance_row, "state_revision") or 0),
            "last_analysis_at": last_analysis_at,
            "storage_mode": _row(instance_row, "storage_mode"),
            "criteria_version": criteria_version,
            "criteria": approved_criteria,
        },
        "counts": counts,
        "documents": documents,
    }


def _counts(
    scope_rows: Sequence[Any],
    *,
    total: int,
    decision_map: Mapping[str, Any],
    intent_map: Mapping[str, Any],
    task_map: Mapping[str, Sequence[Any]],
) -> dict[str, int]:
    """Whole-instance total plus every count over the current scope."""
    processed = unreviewed = keep = reject = hold = 0
    manual_review = pending_action = needs_recheck = open_tasks = 0

    for doc in scope_rows:
        document_id = str(_row(doc, "id"))
        state = _row(doc, "processing_state")
        if state in _PROCESSED_STATES:
            processed += 1
        if state == ProcessingState.MANUAL_REVIEW.value:
            manual_review += 1

        decision = decision_map.get(document_id)
        disposition = _row(decision, "disposition") if decision is not None else ReviewState.UNREVIEWED.value
        if disposition == ReviewState.UNREVIEWED.value:
            unreviewed += 1
        elif disposition == ReviewState.KEEP.value:
            keep += 1
        elif disposition == ReviewState.REJECT.value:
            reject += 1
        elif disposition == ReviewState.HOLD.value:
            hold += 1

        if _as_bool(_row(doc, "decision_needs_recheck")) or _as_bool(
            _row(decision, "needs_recheck") if decision is not None else 0
        ):
            needs_recheck += 1

        intent = intent_map.get(document_id)
        if intent is not None and _row(intent, "intent") != "none" and _row(intent, "state") in _PENDING_INTENT_STATES:
            pending_action += 1

        for task in task_map.get(document_id, ()):  # type: ignore[arg-type]
            if _row(task, "state") == TaskState.OPEN.value:
                open_tasks += 1

    return {
        "total": total,
        "filtered": len(scope_rows),
        "processed": processed,
        "unreviewed": unreviewed,
        "keep": keep,
        "reject": reject,
        "hold": hold,
        "manual_review": manual_review,
        "pending_action": pending_action,
        "needs_recheck": needs_recheck,
        "open_tasks": open_tasks,
    }


def _document_payload(
    doc: Any,
    *,
    decision: Any,
    intent: Any,
    profile: Any,
    tasks: Sequence[Any],
    notes: Sequence[Any],
    evidence: Sequence[Any],
    file_actions: Sequence[Any],
    history: Sequence[Any],
    approved_criterion_ids: Sequence[str],
) -> dict[str, Any]:
    """One document entry, in the exact shape the contract fixes."""
    document_id = str(_row(doc, "id"))

    evidence_items = [
        {
            "id": str(_row(row, "evidence_key")),
            "criterion_id": _row(row, "criterion_id"),
            "span_id": str(_row(row, "span_id") or ""),
            "quote": str(_row(row, "quote") or ""),
            "locator": _parse_json(_row(row, "locator_json"), {}),
            "validation": str(_row(row, "validation") or "unchecked"),
        }
        for row in evidence
    ]

    # Criterion-kind evidence grouped by the criterion it supports. Summary
    # evidence has no criterion and is not part of any per-criterion assessment.
    by_criterion: dict[str, list[Any]] = {}
    for row in evidence:
        criterion_id = _row(row, "criterion_id")
        if criterion_id:
            by_criterion.setdefault(str(criterion_id), []).append(row)

    criterion_items: list[dict[str, Any]] = []
    for criterion_id in approved_criterion_ids:
        result, evidence_ids = _evidence_for_criterion(by_criterion.get(criterion_id, ()))
        criterion_items.append(
            {
                "criterion_id": criterion_id,
                # null means no assessment is stored for this criterion yet; it is
                # never rendered as "not established" or as a negative.
                "result": result,
                # The schema stores a result per evidence item but no separate
                # explanation text; unknown stays null rather than being invented.
                "explanation": None,
                "evidence_ids": evidence_ids,
            }
        )

    ordered_tasks = sorted(
        tasks,
        key=lambda t: (
            0 if _row(t, "state") == TaskState.OPEN.value else 1,
            str(_row(t, "created_at") or ""),
            str(_row(t, "id") or ""),
        ),
    )
    task_items = [
        {
            "id": str(_row(t, "id")),
            "title": str(_row(t, "title") or ""),
            "origin": str(_row(t, "origin") or "human"),
            "state": str(_row(t, "state") or "open"),
            "severity": str(_row(t, "severity") or "normal"),
            "criterion_id": _row(t, "criterion_id"),
            "detail": str(_row(t, "detail") or ""),
        }
        for t in ordered_tasks
    ]
    open_task_count = sum(1 for t in tasks if _row(t, "state") == TaskState.OPEN.value)
    task_warning = any(
        _row(t, "state") == TaskState.OPEN.value and _row(t, "severity") == "attention"
        for t in tasks
    )

    note_items = [
        {
            "id": str(_row(n, "id")),
            "body": str(_row(n, "body") or ""),
            "author": str(_row(n, "author") or ""),
            "updated_at": _row(n, "updated_at"),
        }
        for n in notes
    ]

    history_items: list[dict[str, Any]] = []
    for row in history:
        new_value = _parse_json(_row(row, "new_json"), {})
        history_items.append(
            {
                "disposition": new_value.get("disposition"),
                "actor": new_value.get("actor", _row(row, "actor")),
                "at": _row(row, "created_at"),
                "decision_revision": new_value.get("decision_revision"),
            }
        )

    file_items = [
        {
            "batch_id": str(_row(f, "batch_id")),
            "kind": str(_row(f, "kind") or ""),
            "state": str(_row(f, "state") or "planned"),
            "destination": str(_row(f, "destination_rel_path") or ""),
            "at": _row(f, "updated_at"),
            "error_code": _row(f, "error_code"),
        }
        for f in file_actions
    ]

    return {
        "document_id": document_id,
        "display_name": _row(doc, "display_name"),
        "original_filename": str(_row(doc, "original_filename") or ""),
        "current_rel_path": str(_row(doc, "current_rel_path") or ""),
        "media_type": str(_row(doc, "media_type") or MediaType.UNKNOWN.value),
        "size_bytes": _row(doc, "size_bytes"),
        "ingested_at": _row(doc, "ingested_at"),
        "submitted_at": _row(doc, "submitted_at"),
        "processing_state": str(_row(doc, "processing_state") or ""),
        "processing_detail": _row(doc, "processing_detail"),
        "location": str(_row(doc, "location") or ""),
        "location_version": int(_row(doc, "location_version") or 0),
        "review_state": _row(decision, "disposition") if decision is not None else ReviewState.UNREVIEWED.value,
        "decision_revision": int(_row(decision, "decision_revision") or 0) if decision is not None else 0,
        "decision_needs_recheck": _as_bool(_row(doc, "decision_needs_recheck")),
        "recheck_reason": _row(doc, "recheck_reason"),
        "disposition_frozen": _as_bool(_row(decision, "disposition_frozen") if decision is not None else 0),
        "pending_intent": _row(intent, "intent") if intent is not None else "none",
        "intent_revision": int(_row(intent, "intent_revision") or 0) if intent is not None else 0,
        "duplicate_content": _as_bool(_row(doc, "duplicate_content")),
        "duplicate_of": _row(doc, "duplicate_of"),
        "open_task_count": open_task_count,
        "task_warning": task_warning,
        # An absent profile is unknown, not an empty summary.
        "summary_text": _row(profile, "summary_text") if profile is not None else None,
        "summary_stale": _as_bool(_row(profile, "stale") if profile is not None else 0),
        "criteria": criterion_items,
        "evidence": evidence_items,
        "tasks": task_items,
        "notes": note_items,
        "decision_history": history_items,
        "file_actions": file_items,
        "document_link": document_link(str(_row(doc, "current_rel_path") or "")),
        "warnings": _document_warnings(doc, profile),
    }
