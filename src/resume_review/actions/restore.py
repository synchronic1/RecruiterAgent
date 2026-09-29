"""Plan a restore/undo as a first-class intent. Planning moves nothing.

Authority: PRD sections 10, 10.1 and 13.3, and ``skill/references/state-model.md``.

Why this module exists
----------------------
"Restore previous location" and "return to the active folder" are **different
intents** and must never be collapsed into one ambiguous Undo button (PRD 10.1).
The planner in :mod:`resume_review.actions.planner` already knows how to resolve
either intent into a destination, but nothing names the distinction or refuses a
caller that passes neither. This module does exactly that:

* :func:`plan_restore` accepts one explicit restore intent -- ``restore_active``
  (return to the recorded active path) or ``restore_previous`` (return to the
  previously recorded location, which for a file rejected while already in
  ``Rejected/`` legitimately means ``Rejected/`` again). Any other intent is
  refused with an :class:`~resume_review.errors.InvalidInput`; the module never
  guesses.
* :func:`plan_restore_from_batch` plans the undo of a batch of already-applied
  moves. An undo returns each file to the location it was moved from, which is
  precisely the ``restore_previous`` intent -- never ``restore_active``.

What this module deliberately does **not** do
---------------------------------------------
* It never moves, renames, deletes, copies, overwrites, or writes a document. It
  imports no file-mutation utility. A restore is a *new plan* that travels the
  same plan -> approve -> apply path as any other move; there is no separate undo
  command that executes directly.
* It is not approval. Building a restore plan is not authorization; the approval
  record is created later, by a human, in the API layer.
* It never changes a review decision. Restoring a rejected file does not silently
  reset its decision to ``unreviewed`` (PRD 10.1): the decision dimension is
  independent of the pending-intent and location dimensions.

Safety additions over the raw planner
-------------------------------------
* Every destination is revalidated through
  :func:`~resume_review.storage.paths.normalize_rel_path` and proven to stay
  inside the registered root before it is returned.
* A restore proposes a move onto a destination that must be free. An occupied
  destination is reported as a collision and never planned over; a source whose
  content no longer matches the recorded hash is reported as a content conflict
  and never planned. Both require a fresh, human-approved plan.
* A restore whose source location is missing is blocked and produces a
  reconciliation task, matching the "Missing source" row of PRD 10.1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..errors import Code, InvalidInput, NotFound, ResumeReviewError
from ..models import (
    ActionPlan,
    OperationState,
    PendingIntent,
    PlannedOperation,
    SkippedOperation,
)
from ..storage.paths import (
    assert_no_reparse_traversal,
    assert_within_root,
    normalize_rel_path,
    to_absolute,
)
from ..util import sha256_file
from .planner import SkipReason, plan_actions

#: The only two intents a restore plan may carry. Everything else is refused.
RESTORE_INTENTS: tuple[PendingIntent, ...] = (
    PendingIntent.RESTORE_ACTIVE,
    PendingIntent.RESTORE_PREVIOUS,
)


class RestoreSkipReason:
    """Restore-specific blocking reasons.

    The base planner reasons live in
    :class:`~resume_review.actions.planner.SkipReason`. This adds one code for the
    case a restore introduces: the source file's content no longer matches the
    recorded hash, so the plan's ``expected_sha256`` precondition is false.
    """

    CONTENT_CHANGED = "content_changed"


#: Operation journal states that mean the file may actually have moved. Undoing a
#: batch only makes sense for these; a merely ``planned`` operation is not undone,
#: it is canceled.
_APPLIED_OPERATION_STATES = frozenset(
    {
        OperationState.FILE_MOVED.value,
        OperationState.COMMITTED.value,
        OperationState.NEEDS_RECONCILIATION.value,
    }
)

#: Benign "nothing to do" reasons, mirroring the planner's classifier.
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
        RestoreSkipReason.CONTENT_CHANGED,
    }
)


def plan_restore(
    repo: Any,
    *,
    document_ids: Sequence[str],
    intent: PendingIntent | str,
    requested_by: str,
    root: str | os.PathLike[str],
) -> ActionPlan:
    """Resolve an explicit restore intent into a new, immutable plan.

    Parameters
    ----------
    repo:
        The :class:`~resume_review.db.repository.Repository`. Read methods resolve
        documents, decisions, intents, and the recorded previous location;
        ``upsert_task`` records a reconciliation task when a source is missing or
        its content changed. Nothing else is written: this function creates no
        batch and no operation row.
    document_ids:
        The exact selected set. Duplicates are collapsed; order is preserved.
    intent:
        ``restore_active`` (return to the recorded active path) or
        ``restore_previous`` (return to the previously recorded location). Any
        other intent -- including ``none``, ``move_rejected`` and ``move_trash`` --
        raises :class:`~resume_review.errors.InvalidInput`.
    requested_by:
        Recorded actor reference for the plan. The caller must obtain this from
        the authenticated session, never from a request body.
    root:
        The registered workspace root. Every path is validated to stay inside it.

    Returns
    -------
    ActionPlan
        A plan with a fresh ``batch_id`` and ``plan_hash`` -- a new plan and a new
        audit trail. It is **not** approval and it moves nothing. Blocked or
        conflicting documents appear in ``skipped`` with a blocking reason.
    """
    restore_intent = coerce_restore_intent(intent)
    root_path = Path(root)
    requested_ids = _dedupe(document_ids)

    plan = plan_actions(
        repo,
        document_ids=requested_ids,
        intent_by_document={document_id: restore_intent for document_id in requested_ids},
        requested_by=requested_by,
        criteria_version=int(repo.active_criteria_version()),
        root=root_path,
    )

    # Belt and braces: prove every destination the planner produced is a canonical
    # relative path that stays inside the root before it is handed to a caller.
    _assert_destinations_contained(root_path, plan.operations)
    return _apply_content_guard(repo, root_path, plan)


def plan_restore_from_batch(
    repo: Any,
    *,
    batch_id: str,
    requested_by: str,
    root: str | os.PathLike[str],
) -> ActionPlan:
    """Plan the undo of an already-applied batch as a new ``restore_previous`` plan.

    An undo sends each affected file back to the location it was moved from. That
    is the ``restore_previous`` intent for every operation, whatever the original
    move was: undoing a rejection returns the file to its recorded previous
    location (the active path it came from), undoing a Trash move returns it to
    the pre-Trash path, and undoing an earlier restore returns it to the location
    that restore moved it out of. It is deliberately **not** ``restore_active``,
    which is a different, explicit reviewer intent.

    Only operations that may actually have moved a file are considered
    (``file_moved``, ``committed``, ``needs_reconciliation``); a merely planned
    operation is not undone here, it is canceled. If the batch is unknown, a
    :class:`~resume_review.errors.NotFound` is raised. If it has no applied
    operations, an empty plan is returned with an explanatory warning.

    The result is a new plan with a new ``batch_id``; it never reuses the original
    batch and it moves nothing.
    """
    if repo.get_batch(batch_id) is None:
        raise NotFound(
            "That action batch does not exist.",
            code=Code.NOT_FOUND,
            detail={"entity": "batch"},
        )

    operations = repo.list_file_operations(batch_id)
    document_ids: list[str] = []
    for operation in operations:
        document_id = str(operation.document_id)
        if str(operation.state) in _APPLIED_OPERATION_STATES and document_id not in document_ids:
            document_ids.append(document_id)

    plan = plan_restore(
        repo,
        document_ids=document_ids,
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by=requested_by,
        root=root,
    )
    if not document_ids:
        return _rebuild(
            plan,
            operations=plan.operations,
            skipped=plan.skipped,
            warnings=[
                *plan.warnings,
                "The batch has no applied file operations to restore; nothing was planned.",
            ],
        )
    return plan


# ---------------------------------------------------------------------------
# Intent handling
# ---------------------------------------------------------------------------
def coerce_restore_intent(intent: PendingIntent | str) -> PendingIntent:
    """Return the restore intent, or raise for anything that is not one.

    Accepts a :class:`~resume_review.models.PendingIntent` or its string value. An
    unrecognized value and a recognized-but-not-a-restore value (``none``,
    ``move_rejected``, ``move_trash``) are both refused -- the module never guesses
    which restore the caller meant.
    """
    raw = intent if isinstance(intent, PendingIntent) else str(intent)
    try:
        value = PendingIntent(raw)
    except ValueError as exc:
        raise InvalidInput(
            "The requested intent is not a supported pending intent.",
            code=Code.INVALID_INPUT,
            detail={"intent": str(intent)},
        ) from exc
    if value not in RESTORE_INTENTS:
        raise InvalidInput(
            "A restore plan supports only the restore_active and restore_previous "
            "intents. The requested intent is a different operation and was refused "
            "rather than guessed.",
            code=Code.INVALID_INPUT,
            detail={"intent": value.value},
        )
    return value


# ---------------------------------------------------------------------------
# Content-identity guard (PRD 13.3)
# ---------------------------------------------------------------------------
def _apply_content_guard(repo: Any, root_path: Path, plan: ActionPlan) -> ActionPlan:
    """Refuse to plan a restore whose source no longer matches its recorded hash.

    The planner checks that the source exists and that a hash was recorded, but it
    does not read the file. A restore is a move onto a destination that must be
    free, so a source whose content changed externally must surface as a conflict
    and require new approval rather than being moved on stale preconditions
    (PRD 13.3).
    """
    if not plan.operations:
        return plan

    kept: list[PlannedOperation] = []
    conflicts: list[PlannedOperation] = []
    for operation in plan.operations:
        actual = _hash_source(root_path, operation.source)
        if actual is not None and actual == operation.expected_sha256:
            kept.append(operation)
        else:
            conflicts.append(operation)

    if not conflicts:
        return plan

    skipped = list(plan.skipped)
    warnings = list(plan.warnings)
    for operation in conflicts:
        skipped.append(
            SkippedOperation(
                document_id=operation.document_id,
                reason=RestoreSkipReason.CONTENT_CHANGED,
                kind=_kind_value(operation.kind),
            )
        )
        warnings.append(
            f"Document {operation.document_id} was not planned for restore: the file at "
            f"its recorded location could not be verified against the recorded content hash. "
            f"No destination was overwritten; the conflict requires new approval."
        )
        _record_conflict_task(repo, operation, warnings)

    return _rebuild(plan, operations=kept, skipped=skipped, warnings=warnings)


def _hash_source(root_path: Path, rel: str) -> str | None:
    """The current sha256 of a root-relative source file, or ``None`` if unreadable."""
    try:
        absolute = to_absolute(root_path, rel)
    except ResumeReviewError:
        return None
    try:
        if not os.path.isfile(absolute):
            return None
        return sha256_file(absolute)
    except OSError:  # pragma: no cover - a read failure must not invent identity
        return None


def _record_conflict_task(
    repo: Any, operation: PlannedOperation, warnings: list[str]
) -> None:
    """Preserve evidence of the conflict as a reconciliation task (PRD 13.3)."""
    try:
        repo.upsert_task(
            document_id=operation.document_id,
            task_type="reconciliation",
            title="Reconcile a changed source before restoring it",
            source_revision=int(operation.source_revision),
            origin="system",
            detail=(
                "The file at its recorded location no longer matches the recorded "
                "revision and content hash. No restore was planned and no destination "
                "was overwritten. Re-verify the source and approve a new plan."
            ),
            severity="attention",
        )
    except Exception as exc:  # pragma: no cover - task bookkeeping must not abort planning
        warnings.append(
            f"The conflict task for {operation.document_id} could not be recorded "
            f"({type(exc).__name__})."
        )


# ---------------------------------------------------------------------------
# Destination containment
# ---------------------------------------------------------------------------
def _assert_destinations_contained(
    root_path: Path, operations: Iterable[PlannedOperation]
) -> None:
    """Normalize and prove each destination stays inside the registered root.

    Raises :class:`~resume_review.errors.InvalidInput` for a destination that is
    not canonical, that escapes the root, or whose path traverses a link.
    """
    for operation in operations:
        try:
            normalized = normalize_rel_path(operation.destination)
            absolute = assert_no_reparse_traversal(
                root_path, normalized, allow_final_absent=True
            )
            assert_within_root(root_path, absolute)
        except ResumeReviewError as exc:
            raise InvalidInput(
                "A restore destination failed containment validation and was refused.",
                code=Code.INVALID_INPUT,
                detail={"document_id": operation.document_id, "reason": exc.code},
            ) from exc
        if normalized != operation.destination:
            raise InvalidInput(
                "A restore destination was not in canonical relative form and was refused.",
                code=Code.INVALID_INPUT,
                detail={"document_id": operation.document_id},
            )


# ---------------------------------------------------------------------------
# Plan rebuilding
# ---------------------------------------------------------------------------
def _rebuild(
    plan: ActionPlan,
    *,
    operations: Sequence[PlannedOperation],
    skipped: Sequence[SkippedOperation],
    warnings: Sequence[str],
) -> ActionPlan:
    """Return a fresh plan with the same identity and a recomputed hash.

    The batch id is preserved (it is already a new, unsaved identity); the hash is
    recomputed because ``plan_hash`` must cover the operations actually returned.
    """
    counts = {
        "requested": int(plan.counts.get("requested", 0)),
        "operations": len(operations),
        "skipped": len(skipped),
        "no_op": sum(1 for item in skipped if item.reason in _NO_OP_REASONS),
        "blocked": sum(1 for item in skipped if item.reason in _BLOCKING_REASONS),
    }
    rebuilt = ActionPlan(
        schema_version=plan.schema_version,
        instance_id=plan.instance_id,
        batch_id=plan.batch_id,
        criteria_version=plan.criteria_version,
        operations=list(operations),
        created_at=plan.created_at,
        requested_by=plan.requested_by,
        skipped=list(skipped),
        warnings=list(warnings),
        counts=counts,
    )
    rebuilt.plan_hash = rebuilt.compute_hash()
    return rebuilt


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        value = str(raw)
        if value not in out:
            out.append(value)
    return out


def _kind_value(intent: PendingIntent | str) -> str:
    return intent.value if isinstance(intent, PendingIntent) else str(intent)


# Deliberately no filesystem mutation helpers live in this module. Building a
# restore plan is a read-only description; execution is a separate,
# approval-gated concern handled by the executor over the returned ActionPlan.
