"""Build an immutable, canonical action plan. Planning moves nothing.

Authority: PRD sections 10, 10.1, 12.3 and 13.1.

What this module does
---------------------
:func:`plan_actions` takes an **explicit set of document IDs** and resolves each
one into a concrete, fully-specified move (or a recorded reason it is not moved).
It produces an :class:`~resume_review.models.ActionPlan` whose ``plan_hash`` comes
from :meth:`~resume_review.models.ActionPlan.compute_hash`.

What this module deliberately does **not** do
---------------------------------------------
* It never moves, renames, deletes, copies, or writes a document, and it imports
  no file-mutation utility. A plan is a description, not an act.
* It is not approval. Building a plan is not authorization; the approval record is
  created later, by a human, in the API layer (PRD 13.1).
* It never accepts a plan payload from a caller or a model as authoritative. The
  helper constructs every destination from primitives. Callers may supply only an
  *intent* per document and a *reconfirmation / override reason* for a decision
  flagged ``decision_needs_recheck``. Destinations are never caller-supplied.

Destination rules (PRD 10.1)
----------------------------
===============================  ==================================================
Keep, currently active           no operation emitted
Keep while in Rejected/          return to the recorded active path
Reject (in active)               ``Rejected/<document-id>/<original-filename>``
Hold or Unreviewed               preserve current location; no implicit restore
Move to Trash                    ``Trash/<batch-id>/<document-id>/<filename>``
Restore from Trash               the previous recorded path
Missing source                   block the move; create a reconciliation task
===============================  ==================================================

Every destination is validated through
:func:`~resume_review.storage.paths.normalize_rel_path` and confirmed to stay
inside the registered root; a destination that cannot be validated, or that is
already occupied, is reported (``PATH_INVALID`` / ``DESTINATION_COLLISION``)
rather than planned over.

``kind`` on each operation records the pending-intent dimension only. The review
decision and the verified location are separate dimensions and are never
collapsed into it (see ``skill/references/state-model.md``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import Code, ResumeReviewError
from ..models import (
    REJECTED_DIR,
    TRASH_DIR,
    ActionPlan,
    Location,
    PendingIntent,
    PlannedOperation,
    ReviewState,
    SkippedOperation,
)
from ..storage.no_clobber import file_identity
from ..storage.paths import (
    assert_no_reparse_traversal,
    assert_within_root,
    join_rel,
    normalize_rel_path,
    safe_final_component,
    to_absolute,
)
from ..util import new_id, now_iso

#: Schema version of the serialized action plan (PRD 12.3).
PLAN_SCHEMA_VERSION = "1.0"


class SkipReason:
    """Stable reason codes for a document that produced no operation.

    ``no_op_*`` and ``already_at_destination`` are benign: the requested state
    already holds. The remaining codes are blocking conditions the reviewer or a
    reconciliation pass must resolve.
    """

    # Benign no-ops: the requested end state already holds.
    NO_OP_KEEP_ACTIVE = "no_op_keep_active"
    NO_OP_HOLD = "no_op_hold"
    NO_OP_UNREVIEWED = "no_op_unreviewed"
    NO_OP_LOCATION = "no_op_location"
    ALREADY_AT_DESTINATION = "already_at_destination"

    # Blocking conditions. No move is emitted for any of these.
    DOCUMENT_NOT_FOUND = "document_not_found"
    DECISION_NEEDS_RECHECK = "decision_needs_recheck"
    MISSING_SOURCE = "missing_source"
    # Matches errors.Code.DESTINATION_COLLISION so the reason is machine-actionable.
    DESTINATION_COLLISION = Code.DESTINATION_COLLISION
    SOURCE_IDENTITY_UNKNOWN = "source_identity_unknown"
    PREVIOUS_LOCATION_UNKNOWN = "previous_location_unknown"
    PATH_INVALID = "path_invalid"
    INVALID_INTENT = "invalid_intent"


#: Reasons whose cause is a benign "nothing to do".
_NO_OP_REASONS = frozenset(
    {
        SkipReason.NO_OP_KEEP_ACTIVE,
        SkipReason.NO_OP_HOLD,
        SkipReason.NO_OP_UNREVIEWED,
        SkipReason.NO_OP_LOCATION,
        SkipReason.ALREADY_AT_DESTINATION,
    }
)

#: Reasons that block and need a human or a reconciliation pass.
_BLOCKING_REASONS = frozenset(
    {
        SkipReason.MISSING_SOURCE,
        SkipReason.DESTINATION_COLLISION,
        SkipReason.SOURCE_IDENTITY_UNKNOWN,
        SkipReason.PREVIOUS_LOCATION_UNKNOWN,
        SkipReason.PATH_INVALID,
    }
)


def plan_actions(
    repo: Any,
    *,
    document_ids: Sequence[str],
    intent_by_document: Mapping[str, Any] | None = None,
    requested_by: str,
    criteria_version: int,
    root: str | os.PathLike[str],
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ActionPlan:
    """Resolve an explicit set of documents into one concrete, immutable plan.

    Parameters
    ----------
    repo:
        The :class:`~resume_review.db.repository.Repository`. Read methods resolve
        documents, decisions, intents, and any prior recorded location;
        ``upsert_task`` records a reconciliation task when a source is missing. No
        batch or operation row is written here.
    document_ids:
        The exact selected set. Duplicates are collapsed; order is preserved.
    intent_by_document:
        Optional per-document requested intent (a :class:`PendingIntent` or its
        string value). A document absent from this mapping falls back to its saved
        ``action_intents`` row. This is a *request*, never an approval.
    requested_by:
        Recorded actor reference for the plan. The caller must obtain this from
        the authenticated session, never from a request body (PRD 12.2).
    criteria_version:
        The criteria version the plan was built from. A later criteria change
        invalidates the authorization (PRD 13.1).
    root:
        The registered workspace root. Every path is validated to stay inside it.
    overrides:
        ``{document_id: {...}}``. Only two keys are honored:
        ``decision_reconfirmed`` (bool) and ``override_reason`` (non-empty str).
        Either satisfies the ``decision_needs_recheck`` requirement (PRD 13.1). A
        caller-supplied *destination* is never accepted.
    """
    root_path = Path(root)
    batch_id = new_id("batch")
    instance_id = str(repo.db.instance_id)

    requested_ids: list[str] = []
    for raw in document_ids:
        document_id = str(raw)
        if document_id not in requested_ids:
            requested_ids.append(document_id)

    operations: list[PlannedOperation] = []
    skipped: list[SkippedOperation] = []
    warnings: list[str] = []
    seen_destinations: dict[str, str] = {}

    for document_id in requested_ids:
        document = repo.get_document(document_id)
        if document is None:
            skipped.append(
                SkippedOperation(document_id=document_id, reason=SkipReason.DOCUMENT_NOT_FOUND)
            )
            warnings.append(
                f"Document {document_id} is not registered in this instance and was skipped."
            )
            continue

        decision = repo.get_decision(document.id)
        override = _override_for(overrides, document.id)

        intent, intent_error = _resolve_intent(repo, document.id, intent_by_document)
        if intent_error is not None:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=SkipReason.INVALID_INTENT,
                    kind=None,
                )
            )
            warnings.append(intent_error)
            continue

        recheck = bool(document.decision_needs_recheck or decision.needs_recheck)
        if recheck and not _reconfirmed(override):
            reason = document.recheck_reason or "This decision is flagged for re-check."
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=SkipReason.DECISION_NEEDS_RECHECK,
                    kind=_kind_value(intent),
                )
            )
            warnings.append(
                f"Document {document.id} was skipped because its decision needs re-check "
                f"({reason}); reconfirm it or record an explicit override reason."
            )
            continue
        if recheck:
            warnings.append(
                f"Document {document.id} was included under a recorded reconfirmation "
                f"({_override_reason(override) or 'reviewer reconfirmed'})."
            )

        effective = intent if intent != PendingIntent.NONE else _infer_intent(document, decision)

        if effective == PendingIntent.NONE:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=_no_op_reason(document, decision),
                    kind=None,
                )
            )
            continue

        try:
            source_rel = _validate_relative(root_path, document.current_rel_path)
        except ResumeReviewError as exc:
            _skip_path_invalid(skipped, warnings, document.id, effective, exc)
            continue

        destination_rel, destination_error = _build_destination(
            repo, document, effective, batch_id
        )
        if destination_error is not None:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=destination_error,
                    kind=_kind_value(effective),
                )
            )
            warnings.append(
                f"Document {document.id} was skipped: no recorded previous location is "
                f"available to restore it to."
            )
            continue

        try:
            destination_rel = _validate_relative(root_path, destination_rel)
        except ResumeReviewError as exc:
            _skip_path_invalid(skipped, warnings, document.id, effective, exc)
            continue

        if destination_rel == source_rel:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=SkipReason.ALREADY_AT_DESTINATION,
                    kind=_kind_value(effective),
                )
            )
            continue

        if document.location in (Location.MISSING, Location.CONFLICT):
            _record_missing_source(repo, document, skipped, warnings, effective)
            continue

        source_abs = to_absolute(root_path, source_rel)
        if not os.path.isfile(source_abs):
            _record_missing_source(repo, document, skipped, warnings, effective)
            continue

        if not document.content_sha256:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=SkipReason.SOURCE_IDENTITY_UNKNOWN,
                    kind=_kind_value(effective),
                )
            )
            warnings.append(
                f"Document {document.id} has no recorded content hash; a move cannot be "
                f"verified against the source and was not planned."
            )
            continue

        collision = _destination_collision(
            repo, root_path, destination_rel, seen_destinations, document.id
        )
        if collision is not None:
            skipped.append(
                SkippedOperation(
                    document_id=document.id,
                    reason=SkipReason.DESTINATION_COLLISION,
                    kind=_kind_value(effective),
                )
            )
            warnings.append(
                f"Document {document.id} was not planned: {collision} Never overwriting."
            )
            continue

        seen_destinations[destination_rel] = document.id
        operation_id = new_id("operation")
        operations.append(
            PlannedOperation(
                operation_id=operation_id,
                document_id=document.id,
                kind=effective,
                source=source_rel,
                destination=destination_rel,
                source_revision=int(document.current_revision),
                expected_sha256=str(document.content_sha256),
                expected_size=document.size_bytes,
                decision_revision=int(decision.decision_revision),
                intent_revision=int(repo.get_intent(document.id).intent_revision),
                location_version=int(document.location_version),
                expected_previous_location=(
                    source_rel
                    if effective in (PendingIntent.RESTORE_ACTIVE, PendingIntent.RESTORE_PREVIOUS)
                    else None
                ),
            )
        )
        # Capture the source's physical identity for this source revision now, while
        # the file is still at its recorded location. A same-volume rename preserves
        # this identity, so recovery can require the destination to present it and
        # reject a copy another actor placed there (PRD section 13.3). The value is
        # staged on the repository and written to the journal with the operation
        # rows; planning itself persists nothing.
        repo.record_planned_source_identity(operation_id, _capture_source_identity(source_abs))

    if not operations:
        warnings.append("The plan contains no operations; there is nothing to approve.")

    plan = ActionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        instance_id=instance_id,
        batch_id=batch_id,
        criteria_version=int(criteria_version),
        operations=operations,
        created_at=now_iso(),
        requested_by=requested_by,
        skipped=skipped,
        warnings=warnings,
        counts={
            "requested": len(requested_ids),
            "operations": len(operations),
            "skipped": len(skipped),
            "no_op": sum(1 for s in skipped if s.reason in _NO_OP_REASONS),
            "blocked": sum(1 for s in skipped if s.reason in _BLOCKING_REASONS),
        },
    )
    # Canonicalized by the frozen hash, which excludes plan_hash itself,
    # created_at, requested_by, and counts (PRD 12.3).
    plan.plan_hash = plan.compute_hash()
    return plan


# ---------------------------------------------------------------------------
# Intent resolution
# ---------------------------------------------------------------------------
def _resolve_intent(
    repo: Any,
    document_id: str,
    intent_by_document: Mapping[str, Any] | None,
) -> tuple[PendingIntent, str | None]:
    """Return the requested intent for one document, and an error message.

    An explicit caller-supplied intent takes precedence over a saved intent row.
    """
    if intent_by_document is not None and document_id in intent_by_document:
        return _coerce_intent(intent_by_document[document_id], document_id)
    record = repo.get_intent(document_id)
    if str(record.state) == "saved" and record.intent != PendingIntent.NONE:
        return record.intent, None
    return PendingIntent.NONE, None


def _coerce_intent(value: Any, document_id: str) -> tuple[PendingIntent, str | None]:
    if isinstance(value, PendingIntent):
        return value, None
    try:
        return PendingIntent(str(value)), None
    except ValueError:
        return (
            PendingIntent.NONE,
            f"Document {document_id} was skipped: the requested intent is not a "
            f"supported pending intent.",
        )


def _infer_intent(document: Any, decision: Any) -> PendingIntent:
    """Infer the operation a decision implies when no explicit intent is saved.

    A saved intent always wins; this only covers the direct decision -> file
    behaviour in PRD 10.1. It never infers a Trash move or a previous-location
    restore, which require an explicit intent.
    """
    disposition = decision.disposition
    if document.location == Location.ACTIVE and disposition == ReviewState.REJECT:
        return PendingIntent.MOVE_REJECTED
    if document.location == Location.REJECTED and disposition == ReviewState.KEEP:
        return PendingIntent.RESTORE_ACTIVE
    return PendingIntent.NONE


def _no_op_reason(document: Any, decision: Any) -> str:
    if document.location in (Location.TRASH, Location.REJECTED):
        return SkipReason.NO_OP_LOCATION
    if decision.disposition == ReviewState.KEEP:
        return SkipReason.NO_OP_KEEP_ACTIVE
    if decision.disposition == ReviewState.HOLD:
        return SkipReason.NO_OP_HOLD
    if decision.disposition == ReviewState.UNREVIEWED:
        return SkipReason.NO_OP_UNREVIEWED
    return SkipReason.NO_OP_LOCATION


# ---------------------------------------------------------------------------
# Destinations (built from primitives only)
# ---------------------------------------------------------------------------
def _build_destination(
    repo: Any,
    document: Any,
    intent: PendingIntent,
    batch_id: str,
) -> tuple[str, str | None]:
    """Return ``(destination, error_reason)``; only one is meaningful."""
    if intent == PendingIntent.MOVE_REJECTED:
        return (
            join_rel(REJECTED_DIR, document.id, safe_final_component(document.original_filename)),
            None,
        )
    if intent == PendingIntent.MOVE_TRASH:
        return (
            join_rel(
                TRASH_DIR,
                batch_id,
                document.id,
                safe_final_component(document.original_filename),
            ),
            None,
        )
    if intent == PendingIntent.RESTORE_ACTIVE:
        # The path recorded at first discovery is the recorded active path.
        return str(document.first_seen_rel_path), None
    if intent == PendingIntent.RESTORE_PREVIOUS:
        previous = _previous_recorded_path(repo, document.id, document.current_rel_path)
        if previous is None:
            return "", SkipReason.PREVIOUS_LOCATION_UNKNOWN
        return previous, None
    return "", SkipReason.INVALID_INTENT


def _previous_recorded_path(repo: Any, document_id: str, current_rel_path: str) -> str | None:
    """The most recent recorded source path for this document's own operations.

    Read from the durable ``file_operations`` journal, which is the authoritative
    record of what the helper actually did. Returns ``None`` when nothing is
    recorded, so the caller refuses rather than guessing an active path.
    """
    try:
        records = repo.find_operations_in_state(["committed", "file_moved", "intent_recorded"])
    except Exception:  # pragma: no cover - a read failure must not invent a path
        return None
    candidates = [
        record.source_rel_path
        for record in records
        if str(record.document_id) == str(document_id)
        and str(record.source_rel_path) != str(current_rel_path)
    ]
    return candidates[-1] if candidates else None


def _destination_collision(
    repo: Any,
    root_path: Path,
    destination_rel: str,
    seen_destinations: Mapping[str, str],
    document_id: str,
) -> str | None:
    """Return a human-readable collision message, or ``None`` when the path is free."""
    other = seen_destinations.get(destination_rel)
    if other is not None and other != document_id:
        return f"another document in this plan ({other}) already targets that destination."
    if os.path.lexists(to_absolute(root_path, destination_rel)):
        return "the destination already exists on disk."
    occupant = repo.get_document_by_path(destination_rel)
    if occupant is not None and occupant.id != document_id:
        return f"the destination is the registered path of document {occupant.id}."
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _capture_source_identity(source_abs: str) -> str | None:
    """The source file's physical identity, or ``None`` when it cannot be read.

    The value is the ``volume:inode:size:mtime_ns`` hint from
    :class:`~resume_review.storage.no_clobber.FileIdentity`. It is a read-only
    observation; planning still imports no file-mutation utility.
    """
    identity = file_identity(source_abs)
    return identity.digest_hint() if identity is not None else None


def _validate_relative(root_path: Path, rel: str) -> str:
    """Normalize ``rel`` and prove it stays inside ``root_path``.

    Raises a ``PathEscape``/``SymlinkEscape`` when the path is not strictly
    relative and contained.
    """
    norm = normalize_rel_path(rel)
    absolute = assert_no_reparse_traversal(root_path, norm, allow_final_absent=True)
    assert_within_root(root_path, absolute)
    return norm


def _skip_path_invalid(
    skipped: list[SkippedOperation],
    warnings: list[str],
    document_id: str,
    intent: PendingIntent,
    error: ResumeReviewError,
) -> None:
    skipped.append(
        SkippedOperation(
            document_id=document_id,
            reason=SkipReason.PATH_INVALID,
            kind=_kind_value(intent),
        )
    )
    warnings.append(
        f"Document {document_id} was skipped: a path failed containment validation "
        f"({error.code})."
    )


def _record_missing_source(
    repo: Any,
    document: Any,
    skipped: list[SkippedOperation],
    warnings: list[str],
    intent: PendingIntent,
) -> None:
    """Block the move and record a reconciliation task (PRD 10.1, 13.3)."""
    skipped.append(
        SkippedOperation(
            document_id=document.id,
            reason=SkipReason.MISSING_SOURCE,
            kind=_kind_value(intent),
        )
    )
    warnings.append(
        f"Document {document.id} was blocked: the source is not present at its recorded "
        f"location. A reconciliation task was created."
    )
    try:
        repo.upsert_task(
            document_id=document.id,
            task_type="reconciliation",
            title="Reconcile a missing source before any file action",
            source_revision=int(document.current_revision),
            origin="system",
            detail=(
                "The planner could not find the document at its recorded relative path. "
                "No move was planned; investigate the source and its location before "
                "approving any action."
            ),
            severity="attention",
        )
    except Exception as exc:  # pragma: no cover - task bookkeeping must not abort planning
        warnings.append(
            f"The reconciliation task for {document.id} could not be recorded "
            f"({type(exc).__name__})."
        )


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------
def _override_for(
    overrides: Mapping[str, Mapping[str, Any]] | None, document_id: str
) -> Mapping[str, Any]:
    if not overrides:
        return {}
    value = overrides.get(document_id)
    return value if isinstance(value, Mapping) else {}


def _reconfirmed(override: Mapping[str, Any]) -> bool:
    """True when the reviewer reconfirmed or gave an explicit override reason."""
    if override.get("decision_reconfirmed") is True:
        return True
    reason = override.get("override_reason")
    return isinstance(reason, str) and reason.strip() != ""


def _override_reason(override: Mapping[str, Any]) -> str:
    reason = override.get("override_reason")
    return reason.strip() if isinstance(reason, str) else ""


def _kind_value(intent: PendingIntent) -> str:
    return intent.value if isinstance(intent, PendingIntent) else str(intent)


# Deliberately no filesystem mutation helpers live in this module. Planning is a
# read-only description; execution is a separate, approval-gated concern.
