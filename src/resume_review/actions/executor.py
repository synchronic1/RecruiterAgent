"""Plan -> approve -> apply executor (PRD sections 13.1, 13.2, 13.3).

Authority: PRD 13.1 ("Plan -> approve -> apply"), 13.2 ("Filesystem safety"), 10.1
(file behavior after approval) and 13.3 (recovery contract). AGENTS.md constraints
3, 4 and 5 are the ones this module exists to enforce.

What this module does
---------------------
:func:`apply_batch` executes an **already approved** :class:`ActionPlan` for one
batch. It is the only place in the system that moves a managed file, and it moves
one only through
:func:`~resume_review.storage.no_clobber.atomic_no_clobber_move`.

The rules it encodes, in order of application:

1.  **A command line cannot manufacture approval.** Execution requires an approval
    record already bound to this batch *and* to the plan hash being executed. A
    reviewer's sorting, filtering, a chat message, or the mere fact that a document
    is marked Reject is not approval. A batch whose documents are all Reject but
    which has no approval record refuses and moves nothing.
2.  **Identity, not assumption.** The caller supplies an authenticated
    :class:`~resume_review.models.Principal`. A principal without the reviewer role
    is refused with ``ROLE_INSUFFICIENT``; this module never substitutes a different
    identity to get past that.
3.  **Revalidate everything, twice.** The whole plan is revalidated before the first
    operation, then each remaining operation is revalidated immediately before it
    runs, at the operation boundary -- containment and file identity included. A
    changed source, decision, intent, location version, criteria version,
    destination, or root binding invalidates the authorization. If the initial
    validation fails, nothing moves.
4.  **Intent before the file.** The operation's intent is recorded durably (journal
    and database row) *before* the move. Database updates and the filesystem move
    are not one transaction, so the design assumes a crash between them.
5.  **No fallback.** There is no copy-and-delete branch anywhere -- not even as an
    ``except`` handler. ``CROSS_VOLUME`` and ``UNSUPPORTED`` stop the batch and are
    reported; they are never degraded into something that "works".
6.  **Partial completion is recorded, never undone.** A conflict stops the remaining
    work, marks the batch ``partial``, and reports the count left for a revised plan.
    A completed operation is never silently rolled back.
7.  **Replay is idempotent.** Re-applying a batch does not repeat a completed
    operation. A move that happened before a crash is *reconciled and committed*,
    not repeated; that decision is delegated to
    :func:`~resume_review.actions.recovery.classify_operation` rather than
    reimplemented here.

``dry_run=True`` validates and reports but writes nothing: no journal file, no
database row, no batch transition, and no filesystem change.

Layer rules (AGENTS.md): this module imports ``models``, ``errors``, ``util``,
``storage``, ``db`` and the sibling ``journal``/``recovery`` modules. It imports no
HTTP layer and no model-adapter layer, never invokes a shell, and never renames,
copies, or deletes a file by any means other than the no-clobber primitive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import (
    Code,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    ResumeReviewError,
)
from ..models import (
    ActionPlan,
    ApprovalRecord,
    ExecutionState,
    FileOperationRecord,
    Location,
    OperationState,
    PendingIntent,
    PlannedOperation,
    Principal,
    Role,
    SkippedOperation,
    jsonable,
)
from ..storage.no_clobber import (
    MoveOutcome,
    atomic_no_clobber_move,
    ensure_directory,
    sha256_file,
)
from ..storage.paths import (
    assert_no_reparse_traversal,
    assert_within_root,
    normalize_rel_path,
)
from .journal import Journal, JournalOrderError
from .recovery import Condition, Recovery, classify_operation

__all__ = [
    "APPROVAL_LIFETIME_SECONDS",
    "OpOutcome",
    "OperationOutcome",
    "ApplyOutcome",
    "apply_batch",
]

#: Proposed approval lifetime (PRD 13.1). The approval record already carries the
#: expiry that ``approve_batch`` computed; this constant is the documented default
#: a caller should use when granting one, not something the executor applies itself.
APPROVAL_LIFETIME_SECONDS = 900

#: Batch states from which no further execution is possible without a new plan.
_TERMINAL_BATCH_STATES = frozenset(
    {
        ExecutionState.COMPLETED.value,
        ExecutionState.PARTIAL.value,
        ExecutionState.BLOCKED.value,
        ExecutionState.CANCELED.value,
    }
)

#: Finished operation states. A replay must treat these as done (PRD 13.3).
_FINISHED_OPERATION_STATES = frozenset(
    {OperationState.COMMITTED.value, OperationState.SKIPPED.value}
)

#: Location a committed move of each kind produces (PRD 10.1).
_LOCATION_FOR_KIND: Mapping[PendingIntent, Location] = {
    PendingIntent.MOVE_REJECTED: Location.REJECTED,
    PendingIntent.MOVE_TRASH: Location.TRASH,
    PendingIntent.RESTORE_ACTIVE: Location.ACTIVE,
    PendingIntent.RESTORE_PREVIOUS: Location.ACTIVE,
}

#: Move outcome -> machine-readable error code for the operation record.
_MOVE_OUTCOME_CODE: Mapping[str, str] = {
    MoveOutcome.DESTINATION_EXISTS: Code.DESTINATION_COLLISION,
    MoveOutcome.SOURCE_MISSING: Code.SOURCE_MISSING,
    MoveOutcome.SOURCE_CHANGED: Code.SOURCE_CHANGED,
    MoveOutcome.SOURCE_IS_REPARSE: Code.SYMLINK_REJECTED,
    MoveOutcome.DESTINATION_IS_REPARSE: Code.SYMLINK_REJECTED,
    MoveOutcome.NOT_A_REGULAR_FILE: Code.INVALID_INPUT,
    MoveOutcome.CROSS_VOLUME: Code.CROSS_VOLUME_MOVE,
    MoveOutcome.UNSUPPORTED: Code.NO_CLOBBER_UNSUPPORTED,
}


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------
class OpOutcome:
    """What happened to one operation. Stable strings; callers switch on these."""

    #: The move was performed, the destination verified, and the location committed.
    MOVED = "moved"
    #: A move performed before a crash was verified and committed. No second move.
    RECONCILED = "reconciled"
    #: The operation was already committed before this call; a replay is a no-op.
    ALREADY_COMPLETED = "already_completed"
    #: A conflict, an unverifiable destination, or a failed move stopped the batch here.
    BLOCKED = "blocked"
    #: The move failed in a way that is not a conflict (unexpected outcome).
    FAILED = "failed"
    #: Execution stopped before this operation was reached; the file is untouched.
    NOT_ATTEMPTED = "not_attempted"
    #: The row is in a side state (``skipped``) and is not executable.
    SKIPPED = "skipped"


@dataclass
class OperationOutcome:
    """Per-operation outcome. Carries the paths so a report is auditable."""

    operation_id: str
    document_id: str
    sequence: int
    kind: str
    source: str
    destination: str
    outcome: str
    state: str
    detail: str = ""
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)  # type: ignore[return-value]


@dataclass
class ApplyOutcome:
    """The structured result of one apply request (PRD 5.3 machine-readable shape)."""

    batch_id: str
    root: str
    dry_run: bool
    ok: bool
    state: str
    code: str = Code.OK
    message: str = ""
    approval_actor: str | None = None
    execution_revision: int | None = None
    remaining: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    operations: list[OperationOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def apply_batch(
    repo: Any,
    *,
    batch_id: str,
    actor: Principal,
    root: str | os.PathLike[str],
    now: str,
    dry_run: bool = False,
) -> ApplyOutcome:
    """Execute an already-approved batch of file operations.

    Parameters
    ----------
    repo:
        The instance-scoped :class:`~resume_review.db.Repository`. Every mutation
        goes through it so the state revision and audit row are written in the same
        transaction (PRD 12.2).
    batch_id:
        The batch to apply. Its stored plan and approval are authoritative; nothing
        about the plan is accepted from the caller.
    actor:
        The authenticated principal triggering execution. It must carry at least the
        reviewer role. A caller-supplied ``actor`` string is not accepted: the role
        gate exists precisely so identity cannot be asserted by request data.
    root:
        The registered workspace root. Every path is revalidated to stay inside it,
        at the operation boundary as well as at plan time.
    now:
        The current time as an ISO-8601 string. Injected rather than read so the
        15-minute approval lifetime is testable and deterministic.
    dry_run:
        Validate and report only. No file, journal, database, or batch-state change.

    Raises
    ------
    NotFound
        The batch does not exist.
    InvalidInput
        ``actor`` is not an authenticated :class:`Principal`.
    Forbidden
        The principal lacks the reviewer role, or belongs to another instance.
    Conflict
        ``APPROVAL_REQUIRED`` when no approval record exists for this batch and plan
        hash; ``APPROVAL_EXPIRED`` when the approval lapsed; ``REVISION_CONFLICT``
        when a concurrent writer moved the batch first.
    """
    root_path = Path(root)
    _require_principal(repo, actor)

    batch = repo.get_batch(str(batch_id))
    if batch is None:
        raise NotFound(
            "That action batch does not exist.",
            code=Code.NOT_FOUND,
            detail={"entity": "batch"},
        )

    plan = _plan_from_batch(batch)
    rows = _rows_for_batch(repo, batch_id)

    state = str(batch.get("execution_state") or ExecutionState.PLANNED.value)
    revision = int(batch.get("execution_revision") or 0)

    if state in _TERMINAL_BATCH_STATES:
        # A finished batch is reported, never re-executed. This is what makes a
        # replayed apply request a no-op (PRD 13.3) and what keeps a partial batch
        # from resuming without the revised plan it now requires (PRD 13.1).
        return _report_terminal(
            batch,
            plan,
            rows,
            root_path=root_path,
            dry_run=dry_run,
        )

    # ---- approval gate (PRD 13.1) -------------------------------------
    approval = _authorize_approval(batch, plan, now)

    if dry_run:
        return _dry_run(
            repo,
            plan,
            rows,
            root_path=root_path,
            approval=approval,
            state=state,
            revision=revision,
            batch_id=str(batch_id),
        )

    # ---- begin execution ----------------------------------------------
    if state != ExecutionState.APPLYING.value:
        revision = repo.set_batch_state(
            str(batch_id),
            ExecutionState.APPLYING.value,
            expected_execution_revision=revision,
        )
        state = ExecutionState.APPLYING.value

    journal, journal_warning = _open_journal(root_path, plan)
    outcomes = _new_outcome_map(rows, plan)
    warnings: list[str] = []
    if journal_warning:
        warnings.append(journal_warning)

    # ---- revalidate the whole plan before the first operation ---------
    blocking = _validate_plan(repo, plan, rows, root_path)
    if journal is None:
        blocking.append(
            "The operation journal for this batch could not be opened, so no move "
            "may begin."
        )
    if blocking:
        revision = repo.set_batch_state(
            str(batch_id),
            ExecutionState.BLOCKED.value,
            expected_execution_revision=revision,
            error_code=Code.PLAN_STALE,
            error_detail="; ".join(blocking[:5]),
        )
        warnings.extend(blocking)
        report = _build_report(
            batch_id=str(batch_id),
            root_path=root_path,
            dry_run=False,
            state=ExecutionState.BLOCKED.value,
            code=Code.PLAN_STALE,
            message="The plan failed revalidation; nothing was moved.",
            approval=approval,
            revision=revision,
            rows=rows,
            outcomes=outcomes,
            warnings=warnings,
            failed_validation=True,
        )
        return report

    # ---- execute, in plan order ---------------------------------------
    assert journal is not None  # guaranteed by the journal_warning check above
    stopped = _run_operations(
        repo,
        plan=plan,
        rows=rows,
        outcomes=outcomes,
        journal=journal,
        root_path=root_path,
        warnings=warnings,
    )

    final_state, code, message = _final_state(rows, outcomes, stopped, warnings)
    revision = repo.set_batch_state(
        str(batch_id),
        final_state,
        expected_execution_revision=revision,
        error_code=None if code == Code.OK else code,
        error_detail=message if code != Code.OK else None,
    )

    return _build_report(
        batch_id=str(batch_id),
        root_path=root_path,
        dry_run=False,
        state=final_state,
        code=code,
        message=message,
        approval=approval,
        revision=revision,
        rows=rows,
        outcomes=outcomes,
        warnings=warnings,
        failed_validation=False,
    )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def _require_principal(repo: Any, actor: Any) -> None:
    """Refuse anything but an authenticated reviewer principal (PRD 13.1)."""
    if not isinstance(actor, Principal):
        raise InvalidInput(
            "Applying a batch requires an authenticated principal, not a "
            "caller-supplied actor string.",
            code=Code.INVALID_INPUT,
            detail={"field": "actor"},
        )
    owned = str(getattr(actor, "instance_id", "") or "")
    if owned and owned != str(repo.instance_id):
        raise Forbidden(
            "This principal is not authenticated for this instance.",
            code=Code.FORBIDDEN,
            detail={"reason": "instance_mismatch"},
        )
    # Raises Forbidden(ROLE_INSUFFICIENT) for a viewer, and never substitutes a
    # different identity to get past it.
    actor.require(Role.REVIEWER)


def _authorize_approval(
    batch: Mapping[str, Any], plan: ActionPlan, now: str
) -> ApprovalRecord:
    """Return the approval record, or refuse.

    Approval is a stored record bound to a principal, a plan hash, a time and an
    expiry. Its absence, a hash mismatch, or a lapse is a refusal -- never an
    invitation to infer consent from the documents' current review states.
    """
    approval_actor = batch.get("approval_actor")
    approved_at = batch.get("approval_time")
    expires_at = batch.get("approval_expires_at")

    stored_hash = str(batch.get("plan_hash") or "")
    recomputed = plan.compute_hash()

    if not approval_actor or not approved_at or not expires_at:
        raise Conflict(
            "This batch has no recorded human approval. Sorting, filtering, or a "
            "document being marked Reject is not approval.",
            code=Code.APPROVAL_REQUIRED,
            detail={"batch_id": str(batch.get("id") or plan.batch_id)},
        )

    if not stored_hash or recomputed != stored_hash:
        # The plan being executed is not the plan that was approved.
        raise Conflict(
            "The batch's plan does not match the plan the approval was bound to.",
            code=Code.APPROVAL_REQUIRED,
            detail={
                "batch_id": str(batch.get("id") or plan.batch_id),
                "reason": "plan_hash_mismatch",
            },
        )

    approval = ApprovalRecord(
        actor=str(approval_actor),
        plan_hash=stored_hash,
        approved_at=str(approved_at),
        expires_at=str(expires_at),
    )
    if approval.is_expired(str(now)):
        raise Conflict(
            "The approval for this batch has expired; the plan must be approved "
            "again before it can be applied.",
            code=Code.APPROVAL_EXPIRED,
            detail={
                "batch_id": str(batch.get("id") or plan.batch_id),
                "expires_at": approval.expires_at,
            },
        )
    return approval


# ---------------------------------------------------------------------------
# Plan reconstruction and row binding
# ---------------------------------------------------------------------------
def _plan_from_batch(batch: Mapping[str, Any]) -> ActionPlan:
    """Rehydrate the stored plan. The stored plan hash is revalidated against it."""
    raw = batch.get("plan")
    if not isinstance(raw, Mapping):
        raise Conflict(
            "The batch's stored plan could not be read.",
            code=Code.NEEDS_RECONCILIATION,
            detail={"entity": "batch"},
        )
    operations: list[PlannedOperation] = []
    for item in raw.get("operations") or []:
        if not isinstance(item, Mapping):
            raise Conflict(
                "The batch's stored plan contains an unreadable operation.",
                code=Code.NEEDS_RECONCILIATION,
                detail={"reason": "operation_unreadable"},
            )
        try:
            kind = PendingIntent(str(item.get("kind")))
        except ValueError as exc:
            raise Conflict(
                "The batch's stored plan contains an unsupported operation kind.",
                code=Code.NEEDS_RECONCILIATION,
                detail={"reason": "unsupported_kind"},
            ) from exc
        operations.append(
            PlannedOperation(
                operation_id=str(item.get("operation_id")),
                document_id=str(item.get("document_id")),
                kind=kind,
                source=str(item.get("source")),
                destination=str(item.get("destination")),
                source_revision=int(item.get("source_revision") or 0),
                expected_sha256=str(item.get("expected_sha256") or ""),
                expected_size=(
                    None if item.get("expected_size") is None else int(item.get("expected_size"))
                ),
                decision_revision=int(item.get("decision_revision") or 0),
                intent_revision=int(item.get("intent_revision") or 0),
                location_version=int(item.get("location_version") or 0),
                expected_previous_location=(
                    None
                    if item.get("expected_previous_location") is None
                    else str(item.get("expected_previous_location"))
                ),
                origin_batch_id=(
                    None if item.get("origin_batch_id") is None else str(item.get("origin_batch_id"))
                ),
            )
        )
    skipped = [
        SkippedOperation(
            document_id=str(item.get("document_id")),
            reason=str(item.get("reason")),
            kind=None if item.get("kind") is None else str(item.get("kind")),
        )
        for item in (raw.get("skipped") or [])
        if isinstance(item, Mapping)
    ]
    return ActionPlan(
        schema_version=str(raw.get("schema_version") or "1.0"),
        instance_id=str(raw.get("instance_id") or batch.get("instance_id") or ""),
        batch_id=str(raw.get("batch_id") or batch.get("id") or ""),
        criteria_version=int(raw.get("criteria_version") or 0),
        operations=operations,
        plan_hash=str(raw.get("plan_hash") or batch.get("plan_hash") or ""),
        created_at=raw.get("created_at"),
        requested_by=raw.get("requested_by"),
        skipped=skipped,
        warnings=[str(w) for w in (raw.get("warnings") or [])],
        counts=dict(raw.get("counts") or {}),
    )


def _rows_for_batch(repo: Any, batch_id: str) -> list[FileOperationRecord]:
    """The durable per-operation rows, in sequence order."""
    return list(repo.list_file_operations(str(batch_id)))


def _rows_by_id(rows: Sequence[FileOperationRecord]) -> dict[str, FileOperationRecord]:
    return {str(row.id): row for row in rows}


def _state_of(row: FileOperationRecord) -> str:
    return row.state.value if isinstance(row.state, OperationState) else str(row.state)


# ---------------------------------------------------------------------------
# Revalidation
# ---------------------------------------------------------------------------
def _validate_plan(
    repo: Any,
    plan: ActionPlan,
    rows: Sequence[FileOperationRecord],
    root_path: Path,
) -> list[str]:
    """Blocking reasons for the whole plan, checked before the first operation.

    The authorization inputs -- source path and revision, decision and intent
    revisions, location version, criteria version, destination, and root binding --
    are revalidated here for every operation that is not already finished. A
    started operation (intent recorded or file moved) has already reached the
    filesystem, so its source presence is left to recovery rather than mistaken for
    a stale plan; its authorization inputs are still checked.
    """
    reasons: list[str] = []

    active_criteria = int(repo.active_criteria_version())
    if active_criteria != int(plan.criteria_version):
        reasons.append(
            f"The active criteria version is {active_criteria}, but this plan was "
            f"built from version {plan.criteria_version}."
        )

    by_id = _rows_by_id(rows)
    for operation in plan.operations:
        row = by_id.get(str(operation.operation_id))
        if row is None:
            reasons.append(
                f"Operation {operation.operation_id} has no durable record; the "
                f"journal and the database disagree."
            )
            continue
        state = _state_of(row)
        if state in _FINISHED_OPERATION_STATES:
            continue
        reasons.extend(_validate_operation(repo, operation, row, root_path))
    return reasons


def _validate_operation(
    repo: Any,
    operation: PlannedOperation,
    row: FileOperationRecord,
    root_path: Path,
) -> list[str]:
    """Blocking reasons for one operation, revalidated at the operation boundary.

    Reads only. The same checks run for the whole-plan validation and again
    immediately before the operation's move.
    """
    reasons: list[str] = []
    label = f"Operation {operation.operation_id}"

    document = repo.get_document(operation.document_id)
    if document is None:
        return [f"{label}: the document is no longer registered in this instance."]

    if str(document.current_rel_path) != str(operation.source):
        reasons.append(f"{label}: the recorded source path changed.")
    if int(document.current_revision) != int(operation.source_revision):
        reasons.append(
            f"{label}: the source revision is {document.current_revision}, not the "
            f"{operation.source_revision} the plan recorded."
        )
    if int(document.location_version) != int(operation.location_version):
        reasons.append(
            f"{label}: the location version is {document.location_version}, not the "
            f"{operation.location_version} the plan recorded."
        )

    decision = repo.get_decision(operation.document_id)
    if int(decision.decision_revision) != int(operation.decision_revision):
        reasons.append(
            f"{label}: the decision changed since the plan was built."
        )
    if bool(getattr(document, "decision_needs_recheck", False)) or bool(
        getattr(decision, "needs_recheck", False)
    ):
        reasons.append(
            f"{label}: the decision is flagged for re-check and must be reconfirmed."
        )

    intent = repo.get_intent(operation.document_id)
    if int(intent.intent_revision) != int(operation.intent_revision):
        reasons.append(f"{label}: the pending intent changed since the plan was built.")

    # Root binding and containment, re-proven now rather than trusted from plan time.
    try:
        _resolve_boundary(root_path, operation.source)
        _resolve_boundary(root_path, operation.destination)
    except ResumeReviewError as exc:
        reasons.append(f"{label}: a path failed containment validation ({exc.code}).")

    return reasons


def _resolve_boundary(root_path: Path, rel: str) -> Path:
    """Normalize, prove no reparse traversal, and prove containment. Then return it.

    Called at the operation boundary for both ends of every move. Raises
    ``PathEscape``/``SymlinkEscape`` rather than returning an unusable path.
    """
    norm = normalize_rel_path(rel)
    absolute = assert_no_reparse_traversal(root_path, norm, allow_final_absent=True)
    assert_within_root(root_path, absolute)
    return absolute


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _open_journal(
    root_path: Path, plan: ActionPlan
) -> tuple[Journal | None, str | None]:
    """Load and initialize the durable journal, or report why it could not be.

    ``initialize`` is idempotent: after a crash it re-opens the existing journal and
    preserves the recorded states, which is what lets a replay resume rather than
    restart. A corrupt journal, or one bound to a different plan, blocks execution.
    """
    try:
        journal = Journal.load(root_path, plan.batch_id)
        journal.initialize(
            plan.operations,
            instance_id=plan.instance_id or None,
            plan_hash=plan.plan_hash or None,
        )
    except JournalOrderError as exc:
        return None, f"The operation journal requires reconciliation: {exc.message}"
    except Conflict as exc:
        return None, f"The operation journal could not be opened: {exc.message}"
    except ResumeReviewError as exc:
        return None, f"The operation journal could not be written ({exc.code})."
    return journal, None


def _run_operations(
    repo: Any,
    *,
    plan: ActionPlan,
    rows: Sequence[FileOperationRecord],
    outcomes: dict[str, OperationOutcome],
    journal: Journal,
    root_path: Path,
    warnings: list[str],
) -> bool:
    """Run the plan's operations in order. Returns True when execution stopped early.

    An operation is executed only when its own boundary revalidation passes *and*
    recovery classifies reality as "resume". Anything else stops the remaining work.
    """
    by_id = _rows_by_id(rows)
    for operation in plan.operations:
        row = by_id.get(str(operation.operation_id))
        if row is None:
            warnings.append(
                f"Operation {operation.operation_id} has no durable record; stopping."
            )
            return True

        if _state_of(row) in _FINISHED_OPERATION_STATES:
            # Already committed (or recorded skipped) before this call. A replay
            # must not repeat it (PRD 13.3).
            _mark(
                outcomes,
                row,
                operation,
                outcome=(
                    OpOutcome.ALREADY_COMPLETED
                    if _state_of(row) == OperationState.COMMITTED.value
                    else OpOutcome.SKIPPED
                ),
                detail="This operation was already finished before this call.",
            )
            continue

        # Revalidate this operation immediately before it runs.
        reasons = _validate_operation(repo, operation, row, root_path)
        if reasons:
            warnings.extend(reasons)
            _mark_blocked(repo, journal, row, operation, code=Code.PLAN_STALE, detail=reasons[0])
            outcomes[str(operation.operation_id)] = _outcome_of(
                row, operation, OpOutcome.BLOCKED, reasons[0], code=Code.PLAN_STALE
            )
            return True

        result = _execute_one(
            repo,
            operation=operation,
            row=row,
            journal=journal,
            root_path=root_path,
            warnings=warnings,
        )
        outcomes[str(operation.operation_id)] = result
        if result.outcome in (OpOutcome.BLOCKED, OpOutcome.FAILED):
            return True
    return False


def _execute_one(
    repo: Any,
    *,
    operation: PlannedOperation,
    row: FileOperationRecord,
    journal: Journal,
    root_path: Path,
    warnings: list[str],
) -> OperationOutcome:
    """Execute one operation: reconcile a prior move, or perform a new one.

    Every path out of this function is either a committed operation or a blocked
    one; there is no branch that deletes, copies, or overwrites anything.
    """

    def blocked(code: str, detail: str) -> OperationOutcome:
        """Record the operation as needing reconciliation and return its outcome."""
        _mark_blocked(repo, journal, row, operation, code=code, detail=detail)
        return _outcome_of(row, operation, OpOutcome.BLOCKED, detail, code=code)

    # Reality decides first. A move that happened before a crash is committed, not
    # repeated; an ambiguous state stops the batch. classify_operation owns that
    # decision so recovery and execution cannot diverge.
    diagnosis = classify_operation(repo, operation=row, root=root_path)

    if diagnosis.condition == Condition.ALREADY_COMMITTED:
        return _outcome_of(row, operation, OpOutcome.ALREADY_COMPLETED, diagnosis.detail)

    if diagnosis.recovery == Recovery.COMMIT:
        # Crash after the move, before the commit: the file is already there. Commit
        # the recorded reality without a second move.
        _record_intent(repo, journal, row, operation)
        _record_file_moved(repo, journal, row, detail=diagnosis.detail)
        return _commit(
            repo,
            operation=operation,
            row=row,
            journal=journal,
            root_path=root_path,
            reconciled=True,
        )

    if diagnosis.recovery != Recovery.RESUME:
        warnings.append(f"Operation {operation.operation_id}: {diagnosis.detail}")
        return blocked(_recovery_code(diagnosis), diagnosis.detail)

    # Resume: the source is exactly what the plan recorded and the destination is
    # free. Re-prove containment and identity at this boundary, then move.
    try:
        source_abs = _resolve_boundary(root_path, operation.source)
        destination_abs = _resolve_boundary(root_path, operation.destination)
    except ResumeReviewError as exc:
        return blocked(
            exc.code,
            f"A path failed containment validation at the operation boundary ({exc.code}).",
        )

    if not os.path.isfile(source_abs):
        return blocked(
            Code.SOURCE_MISSING,
            "The source is no longer a regular file at its recorded path.",
        )

    # Durable intent, journal then database, before the file is touched (PRD 13.2).
    _record_intent(repo, journal, row, operation)

    try:
        ensure_directory(destination_abs.parent)
    except ResumeReviewError as exc:
        return blocked(
            exc.code, f"The destination directory could not be prepared ({exc.code})."
        )

    result = atomic_no_clobber_move(
        source_abs,
        destination_abs,
        expected_sha256=operation.expected_sha256 or None,
        expected_size=operation.expected_size,
        create_parents=True,
    )

    if result.outcome != MoveOutcome.MOVED:
        # An unsupported or cross-volume move can never be degraded into something
        # that "works"; it stops here. Every other outcome is a conflict to
        # reconcile. There is no copy-and-delete fallback in either case.
        code = _MOVE_OUTCOME_CODE.get(result.outcome, Code.INTERNAL_ERROR)
        return blocked(code, result.detail)

    _record_file_moved(repo, journal, row, detail=result.detail)

    # Verify the destination before committing the location (PRD 13.2).
    verified, verify_detail = _verify_destination(destination_abs, operation)
    if not verified:
        warnings.append(f"Operation {operation.operation_id}: {verify_detail}")
        return blocked(Code.NEEDS_RECONCILIATION, verify_detail)

    return _commit(
        repo,
        operation=operation,
        row=row,
        journal=journal,
        root_path=root_path,
        reconciled=False,
    )


def _verify_destination(
    destination_abs: Path, operation: PlannedOperation
) -> tuple[bool, str]:
    """Confirm the moved file exists at the destination with the recorded content."""
    if not os.path.isfile(destination_abs):
        return False, "The move reported success but the destination is not a regular file."
    expected = operation.expected_sha256
    if expected:
        try:
            actual = sha256_file(destination_abs)
        except OSError:
            return False, "The destination could not be read back for verification."
        if actual != expected:
            return False, "The destination content does not match the recorded revision."
    return True, ""


def _commit(
    repo: Any,
    *,
    operation: PlannedOperation,
    row: FileOperationRecord,
    journal: Journal,
    root_path: Path,
    reconciled: bool,
) -> OperationOutcome:
    """Commit a verified move: reconcile the location, then the operation state."""
    location = _LOCATION_FOR_KIND.get(operation.kind, Location.ACTIVE)
    try:
        repo.set_document_location(
            operation.document_id,
            operation.destination,
            location.value,
            expected_location_version=operation.location_version,
        )
    except Conflict as exc:
        # The file moved but the document was concurrently relocated. Do not pretend
        # the operation is committed; record the ambiguity for reconciliation.
        detail = (
            "The move completed but the document location changed concurrently "
            f"({exc.code}); the operation needs reconciliation."
        )
        _mark_blocked(repo, journal, row, operation, code=exc.code, detail=detail)
        return _outcome_of(row, operation, OpOutcome.BLOCKED, detail, code=exc.code)

    repo.update_file_operation(
        row.id,
        OperationState.COMMITTED.value,
        observed_source_state="absent",
        observed_destination_state="verified",
    )
    # Keep the in-memory row in step with the durable one. ``remaining`` and the
    # per-operation report are computed from these rows, so a committed operation
    # must not still look unfinished to the same call that committed it.
    row.state = OperationState.COMMITTED
    _best_effort(
        lambda: journal.record_committed(
            row.id, "committed after a verified move" if not reconciled else "reconciled a prior move"
        )
    )
    return _outcome_of(
        row,
        operation,
        OpOutcome.RECONCILED if reconciled else OpOutcome.MOVED,
        (
            "The move had already happened; its location and operation state were "
            "reconciled and committed without repeating the move."
            if reconciled
            else "Moved without clobbering and committed."
        ),
    )


# ---------------------------------------------------------------------------
# Journal and operation-state helpers
# ---------------------------------------------------------------------------
def _record_intent(
    repo: Any, journal: Journal, row: FileOperationRecord, operation: PlannedOperation
) -> None:
    """Persist the intent durably before the file is touched (PRD 13.2).

    The journal write comes first: it is the crash-recovery record. The database row
    follows. Both precede the move. The transition is only ever forward: a row that
    is already past ``planned`` is left alone, so a replay cannot move durable state
    backwards.
    """
    if _state_of(row) != OperationState.PLANNED.value:
        return
    _best_effort(
        lambda: journal.record_intent(row.id, "intent recorded before the move")
    )
    repo.update_file_operation(
        row.id, OperationState.INTENT_RECORDED.value, observed_source_state="present"
    )
    row.state = OperationState.INTENT_RECORDED


def _record_file_moved(
    repo: Any, journal: Journal, row: FileOperationRecord, *, detail: str
) -> None:
    """Advance the durable step to ``file_moved``. Forward-only, like intent."""
    state = _state_of(row)
    if state == OperationState.FILE_MOVED.value:
        return
    if state != OperationState.INTENT_RECORDED.value:
        # Intent must be recorded first; refuse to skip a required journal step.
        return
    _best_effort(lambda: journal.record_file_moved(row.id, detail))
    repo.update_file_operation(
        row.id,
        OperationState.FILE_MOVED.value,
        observed_source_state="absent",
        observed_destination_state="present",
    )
    row.state = OperationState.FILE_MOVED


def _mark_blocked(
    repo: Any,
    journal: Journal,
    row: FileOperationRecord,
    operation: PlannedOperation,
    *,
    code: str,
    detail: str,
) -> None:
    """Mark one operation as needing reconciliation and record why.

    No file is touched here. The operation's row moves to ``needs_reconciliation``
    so a later reconciliation pass (not this executor) decides what the truth is.
    A reconciliation task points a human at the document, deduplicated against the
    planner's own task for the same revision. This is what keeps a conflict from
    being "resolved" by deleting or overwriting anything.
    """
    try:
        repo.update_file_operation(
            row.id,
            OperationState.NEEDS_RECONCILIATION.value,
            error_code=code,
            error_detail=detail,
        )
    except ResumeReviewError:
        # The batch's own failure is what the caller reports; a bookkeeping failure
        # here must not mask it or trigger further filesystem action.
        pass
    _best_effort(lambda: journal.record_needs_reconciliation(row.id, detail))
    _best_effort(
        lambda: repo.upsert_task(
            document_id=operation.document_id,
            task_type="reconciliation",
            title="Reconcile an action that stopped before it completed",
            source_revision=int(operation.source_revision),
            origin="system",
            detail=detail,
            severity="attention",
        )
    )
    row.state = OperationState.NEEDS_RECONCILIATION


def _best_effort(action: Any) -> None:
    """Run a recovery-aid write, ignoring a failure the primary record already covers.

    The journal and the reconciliation task are aids; the ``file_operations`` row is
    authoritative. An aid that cannot be written must not abort an otherwise-safe
    operation, and it must never be the reason a file moves twice.
    """
    try:
        action()
    except ResumeReviewError:
        return


def _recovery_code(diagnosis: Any) -> str:
    if diagnosis.recovery == Recovery.MARK_MISSING:
        return Code.SOURCE_MISSING
    return Code.NEEDS_RECONCILIATION


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def _new_outcome_map(
    rows: Sequence[FileOperationRecord], plan: ActionPlan
) -> dict[str, OperationOutcome]:
    """Seed one NOT_ATTEMPTED outcome per plan operation, in plan order."""
    by_id = _rows_by_id(rows)
    outcomes: dict[str, OperationOutcome] = {}
    for operation in plan.operations:
        row = by_id.get(str(operation.operation_id))
        if row is None:
            continue
        outcomes[str(operation.operation_id)] = _outcome_of(
            row, operation, OpOutcome.NOT_ATTEMPTED, "Execution has not reached this operation."
        )
    return outcomes


def _outcome_of(
    row: FileOperationRecord,
    operation: PlannedOperation,
    outcome: str,
    detail: str,
    code: str | None = None,
) -> OperationOutcome:
    return OperationOutcome(
        operation_id=str(operation.operation_id),
        document_id=str(operation.document_id),
        sequence=int(row.sequence),
        kind=str(operation.kind.value if isinstance(operation.kind, PendingIntent) else operation.kind),
        source=str(operation.source),
        destination=str(operation.destination),
        outcome=outcome,
        state=_state_of(row),
        detail=detail,
        error_code=code,
    )


def _mark(
    outcomes: dict[str, OperationOutcome],
    row: FileOperationRecord,
    operation: PlannedOperation,
    *,
    outcome: str,
    detail: str,
    code: str | None = None,
) -> None:
    outcomes[str(operation.operation_id)] = _outcome_of(
        row, operation, outcome, detail, code=code
    )


def _final_state(
    rows: Sequence[FileOperationRecord],
    outcomes: Mapping[str, OperationOutcome],
    stopped: bool,
    warnings: list[str],
) -> tuple[str, str, str]:
    """The batch's terminal state, error code, and message."""
    blocked = [
        o for o in outcomes.values() if o.outcome in (OpOutcome.BLOCKED, OpOutcome.FAILED)
    ]
    completed = [
        o
        for o in outcomes.values()
        if o.outcome in (OpOutcome.MOVED, OpOutcome.RECONCILED, OpOutcome.ALREADY_COMPLETED)
    ]
    remaining = _remaining_count(rows)

    if not stopped and not blocked:
        return (
            ExecutionState.COMPLETED.value,
            Code.OK,
            "Every operation in the batch completed.",
        )

    if completed:
        return (
            ExecutionState.PARTIAL.value,
            Code.BATCH_PARTIAL,
            (
                f"The batch stopped with {len(completed)} operation(s) completed; "
                f"{remaining} operation(s) remain and require a revised plan."
            ),
        )

    failed = [o for o in blocked if o.outcome == OpOutcome.FAILED]
    code = failed[0].error_code if failed and failed[0].error_code else (
        blocked[0].error_code if blocked and blocked[0].error_code else Code.PLAN_STALE
    )
    message = (
        "No operation completed; the batch is blocked and needs attention before a "
        f"revised plan can run. {warnings[-1] if warnings else ''}"
    ).strip()
    return ExecutionState.BLOCKED.value, code, message


def _remaining_count(rows: Sequence[FileOperationRecord]) -> int:
    return sum(
        1
        for row in rows
        if _state_of(row) not in (OperationState.COMMITTED.value, OperationState.SKIPPED.value)
    )


def _counts(rows: Sequence[FileOperationRecord], outcomes: Mapping[str, OperationOutcome]) -> dict[str, int]:
    counts = {
        "total": len(rows),
        "moved": 0,
        "reconciled": 0,
        "already_completed": 0,
        "blocked": 0,
        "failed": 0,
        "not_attempted": 0,
        "skipped": 0,
    }
    for outcome in outcomes.values():
        key = outcome.outcome
        if key in counts:
            counts[key] += 1
    counts["completed"] = counts["moved"] + counts["reconciled"] + counts["already_completed"]
    counts["remaining"] = _remaining_count(rows)
    return counts


def _build_report(
    *,
    batch_id: str,
    root_path: Path,
    dry_run: bool,
    state: str,
    code: str,
    message: str,
    approval: ApprovalRecord | None,
    revision: int | None,
    rows: Sequence[FileOperationRecord],
    outcomes: Mapping[str, OperationOutcome],
    warnings: list[str],
    failed_validation: bool,
) -> ApplyOutcome:
    ordered = sorted(outcomes.values(), key=lambda o: o.sequence)
    ok = state == ExecutionState.COMPLETED.value
    return ApplyOutcome(
        batch_id=batch_id,
        root=str(root_path),
        dry_run=dry_run,
        ok=ok,
        state=state,
        code=code,
        message=message,
        approval_actor=approval.actor if approval is not None else None,
        execution_revision=revision,
        remaining=_remaining_count(rows),
        counts=_counts(rows, outcomes),
        operations=ordered,
        warnings=list(warnings),
    )


def _report_terminal(
    batch: Mapping[str, Any],
    plan: ActionPlan,
    rows: Sequence[FileOperationRecord],
    *,
    root_path: Path,
    dry_run: bool,
) -> ApplyOutcome:
    """Report a batch that has already reached a terminal state. Executes nothing.

    A replay of a completed batch returns the same completed outcome without
    repeating a move. A partial or blocked batch reports its remaining count; the
    remainder needs a revised plan, not another apply of this one (PRD 13.1, 13.3).
    """
    state = str(batch.get("execution_state"))
    outcomes: dict[str, OperationOutcome] = {}
    for operation in plan.operations:
        row = next((r for r in rows if str(r.id) == str(operation.operation_id)), None)
        if row is None:
            continue
        row_state = _state_of(row)
        if row_state == OperationState.COMMITTED.value:
            outcome, detail = OpOutcome.ALREADY_COMPLETED, "Already committed."
        elif row_state == OperationState.SKIPPED.value:
            outcome, detail = OpOutcome.SKIPPED, "Recorded skipped."
        else:
            outcome, detail = (
                OpOutcome.NOT_ATTEMPTED,
                "Left unexecuted; the batch is already in a terminal state.",
            )
        outcomes[str(operation.operation_id)] = _outcome_of(row, operation, outcome, detail)

    message = {
        ExecutionState.COMPLETED.value: "The batch is already completed; a replay performs no work.",
        ExecutionState.PARTIAL.value: (
            "The batch is partial; the remaining operations require a revised plan."
        ),
        ExecutionState.BLOCKED.value: (
            "The batch is blocked; it requires a revised plan before anything can run."
        ),
        ExecutionState.CANCELED.value: "The batch was canceled; nothing was executed.",
    }.get(state, "The batch is no longer executable.")

    code = Code.OK if state == ExecutionState.COMPLETED.value else (
        Code.BATCH_PARTIAL if state == ExecutionState.PARTIAL.value else Code.BATCH_ALREADY_STARTED
    )
    return _build_report(
        batch_id=str(batch.get("id") or plan.batch_id),
        root_path=root_path,
        dry_run=dry_run,
        state=state,
        code=code,
        message=message,
        approval=None,
        revision=int(batch.get("execution_revision") or 0),
        rows=rows,
        outcomes=outcomes,
        warnings=[message] if state != ExecutionState.COMPLETED.value else [],
        failed_validation=False,
    )


def _dry_run(
    repo: Any,
    plan: ActionPlan,
    rows: Sequence[FileOperationRecord],
    *,
    root_path: Path,
    approval: ApprovalRecord,
    state: str,
    revision: int,
    batch_id: str,
) -> ApplyOutcome:
    """Validate and report the projected outcome. Mutates nothing at all."""
    outcomes = _new_outcome_map(rows, plan)
    warnings: list[str] = []

    blocking = _validate_plan(repo, plan, rows, root_path)
    if blocking:
        warnings.extend(blocking)
        for operation in plan.operations:
            row = next((r for r in rows if str(r.id) == str(operation.operation_id)), None)
            if row is None:
                continue
            if _state_of(row) in _FINISHED_OPERATION_STATES:
                continue
            outcomes[str(operation.operation_id)] = _outcome_of(
                row, operation, OpOutcome.BLOCKED, blocking[0], code=Code.PLAN_STALE
            )
        return _build_report(
            batch_id=batch_id,
            root_path=root_path,
            dry_run=True,
            state=ExecutionState.BLOCKED.value,
            code=Code.PLAN_STALE,
            message="Dry run: the plan fails revalidation; nothing would be moved.",
            approval=approval,
            revision=revision,
            rows=rows,
            outcomes=outcomes,
            warnings=warnings,
            failed_validation=True,
        )

    by_id = _rows_by_id(rows)
    for operation in plan.operations:
        row = by_id.get(str(operation.operation_id))
        if row is None:
            continue
        if _state_of(row) in _FINISHED_OPERATION_STATES:
            outcomes[str(operation.operation_id)] = _outcome_of(
                row, operation, OpOutcome.ALREADY_COMPLETED, "Already finished."
            )
            continue
        diagnosis = classify_operation(repo, operation=row, root=root_path)
        if diagnosis.condition == Condition.ALREADY_COMMITTED:
            outcome = OpOutcome.ALREADY_COMPLETED
        elif diagnosis.recovery == Recovery.COMMIT:
            outcome = OpOutcome.RECONCILED
        elif diagnosis.recovery == Recovery.RESUME:
            outcome = OpOutcome.MOVED
        else:
            outcome = OpOutcome.BLOCKED
        outcomes[str(operation.operation_id)] = _outcome_of(
            row, operation, outcome, diagnosis.detail
        )

    projected = _projected_state(outcomes)
    return _build_report(
        batch_id=batch_id,
        root_path=root_path,
        dry_run=True,
        state=projected,
        code=Code.OK,
        message="Dry run: nothing was moved and nothing was written.",
        approval=approval,
        revision=revision,
        rows=rows,
        outcomes=outcomes,
        warnings=warnings,
        failed_validation=False,
    )


def _projected_state(outcomes: Mapping[str, OperationOutcome]) -> str:
    if any(o.outcome in (OpOutcome.BLOCKED, OpOutcome.FAILED) for o in outcomes.values()):
        return ExecutionState.PARTIAL.value
    return ExecutionState.COMPLETED.value
