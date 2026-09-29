"""Crash reconciliation for interrupted file operations.

Authority: PRD sections 13.2 and 13.3, and 11.2.

PRD 13.3 is the specification, and it is normative::

    +----------------------------------------------------+--------------------------------------+
    | Observed condition                                 | Recovery behavior                    |
    +====================================================+======================================+
    | Source exists as expected; destination absent      | Safe candidate to resume after       |
    |                                                    | approval/expiry policy checks        |
    | Source absent; expected operation-owned            | Reconcile the location and commit    |
    | destination verified                               | the already-performed move           |
    | Both exist                                         | Stop for reconciliation; never       |
    |                                                    | delete one merely because hashes     |
    |                                                    | match                                |
    | Neither exists                                     | Mark missing and request human       |
    |                                                    | investigation                        |
    | Content, path ownership, or identity differs       | Block and preserve evidence of the   |
    |                                                    | conflict                             |
    +----------------------------------------------------+--------------------------------------+

The module's job is to look at the journal plus the *actual* source and
destination state and decide which row applies. It never guesses and it never
repairs by touching files.

Two rules shape every line below.

1.  **There is no deletion path.** The third row is the one that destroys data
    when implemented lazily: hashes match, so "clean up the duplicate". PRD 13.3
    forbids exactly that. Nothing in this module removes, unlinks, renames, or
    trashes a file. A repair only ever *writes journal/database state* to match
    the filesystem; the filesystem itself is read-only here. If a future edit
    needs a move, it belongs in the executor (which uses
    ``storage.no_clobber``), not in reconciliation.

2.  **Destination ownership is stricter than a matching hash.** PRD 13.3:
    "Destination identity includes its operation/document-owned namespace and
    recorded metadata, not just a matching content hash. A copied file from
    another actor must not be mistaken for proof of a completed move." So
    "verified" requires all of: the recorded destination path, the document's own
    namespace, matching size and content hash, *and* a matching recorded file
    identity. Where identity cannot be established the destination is reported as
    unverified rather than accepted, and the evidence says what could and could
    not be established.

``plan_recovery`` is a dry run by default. It reports what reconciliation would
do and mutates nothing -- no journal write, no database write, no filesystem
change. An actual repair requires ``dry_run=False`` explicitly and goes through
:class:`~resume_review.db.Repository` mutations so every change is audited and
bumps ``instances.state_revision`` (PRD 12.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import Repository
from ..errors import Code, NotFound, ResumeReviewError
from ..models import (
    REJECTED_DIR,
    REVIEW_DIR,
    TRASH_DIR,
    FileOperationRecord,
    Location,
    OperationState,
    PendingIntent,
    jsonable,
)
from ..storage.no_clobber import FileIdentity, file_identity, same_inode, sha256_file
from ..storage.paths import containment_report

__all__ = [
    "Condition",
    "Recovery",
    "OperationDiagnosis",
    "RecoveryAction",
    "RecoveryPlan",
    "classify_operation",
    "plan_recovery",
    "ACTIVE_OPERATION_STATES",
]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class Condition:
    """The observed filesystem condition, named after the PRD 13.3 rows."""

    #: Row 1: the source is still where the journal says, the destination is free.
    SOURCE_PRESENT_DESTINATION_ABSENT = "source_present_destination_absent"
    #: Row 2: the source is gone and the operation-owned destination is verified.
    SOURCE_ABSENT_DESTINATION_VERIFIED = "source_absent_destination_verified"
    #: Row 3: both names exist. Ambiguous; a human must reconcile. Never delete.
    BOTH_PRESENT = "both_present"
    #: Row 4: neither name exists. The file is missing; a human must investigate.
    NEITHER_PRESENT = "neither_present"
    #: Row 5: content, path ownership, or identity differs from the record.
    IDENTITY_OR_CONTENT_DIFFERS = "identity_or_content_differs"
    #: Row 5 behavior, distinct cause: the destination exists and its content and
    #: path agree, but ownership identity could not be established either way. We
    #: refuse to treat "we cannot rule it out" as proof of a completed move.
    IDENTITY_UNVERIFIED = "identity_unverified"
    #: Not a PRD row: the journal already records this operation as finished.
    #: Kept separate so a replay is a no-op rather than a second reconciliation.
    ALREADY_COMMITTED = "already_committed"


class Recovery:
    """What recovery should do for a condition. One behavior per PRD 13.3 row."""

    #: Row 1: the executor may resume the move after approval/expiry checks.
    RESUME = "resume"
    #: Row 2: reconcile the document location and commit the performed move.
    COMMIT = "commit"
    #: Row 3: stop; a human reconciles two existing files. No automatic action.
    STOP_FOR_RECONCILIATION = "stop_for_reconciliation"
    #: Row 4: mark the document missing and ask a human to investigate.
    MARK_MISSING = "mark_missing"
    #: Row 5: block and preserve the evidence of the conflict.
    BLOCK_PRESERVE_EVIDENCE = "block_preserve_evidence"
    #: Already finished; nothing to do.
    NO_ACTION = "no_action"


#: States that represent an interruption recovery must look at. Terminal states
#: (``committed``, ``skipped``) are deliberately excluded: re-running recovery over
#: a finished operation is how a replay repeats completed work (PRD 13.3).
ACTIVE_OPERATION_STATES: tuple[str, ...] = (
    OperationState.PLANNED.value,
    OperationState.INTENT_RECORDED.value,
    OperationState.FILE_MOVED.value,
    OperationState.NEEDS_RECONCILIATION.value,
    OperationState.FAILED.value,
)

#: Location a committed move of each kind produces.
_LOCATION_FOR_KIND = {
    PendingIntent.MOVE_REJECTED: Location.REJECTED,
    PendingIntent.MOVE_TRASH: Location.TRASH,
    PendingIntent.RESTORE_ACTIVE: Location.ACTIVE,
    PendingIntent.RESTORE_PREVIOUS: Location.ACTIVE,
}


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------
@dataclass
class OperationDiagnosis:
    """What one interrupted operation looks like, and what recovery may do.

    ``evidence`` is a JSON-serializable record of exactly what was observed and
    what was relied on. It is written so that a reviewer can see not only the
    decision but the facts behind it -- including what could *not* be established.
    """

    operation_id: str
    document_id: str
    batch_id: str
    condition: str
    recovery: str
    detail: str
    safe_to_proceed_without_human: bool
    requires_policy_recheck: bool = False
    requires_human_investigation: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def resumable(self) -> bool:
        return self.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT

    @property
    def committable(self) -> bool:
        return self.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED

    @property
    def blocks(self) -> bool:
        return self.recovery in (Recovery.STOP_FOR_RECONCILIATION, Recovery.BLOCK_PRESERVE_EVIDENCE)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)  # type: ignore[return-value]


@dataclass
class RecoveryAction:
    """The reconciliation step taken (or reported) for one diagnosis."""

    operation_id: str
    document_id: str
    recovery: str
    applied: bool
    detail: str
    mutations: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)  # type: ignore[return-value]


@dataclass
class RecoveryPlan:
    """The result of one reconciliation pass. Never carries a file operation."""

    root: str
    dry_run: bool
    batch_id: str | None
    diagnoses: list[OperationDiagnosis] = field(default_factory=list)
    actions: list[RecoveryAction] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def mutating(self) -> bool:
        return not self.dry_run

    @property
    def applied(self) -> int:
        return sum(1 for action in self.actions if action.applied)

    @property
    def requires_human(self) -> list[OperationDiagnosis]:
        return [d for d in self.diagnoses if d.requires_human_investigation or not d.safe_to_proceed_without_human]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "dry_run": self.dry_run,
            "mutating": self.mutating,
            "batch_id": self.batch_id,
            "counts": dict(self.counts),
            "applied": self.applied,
            "diagnoses": [d.to_dict() for d in self.diagnoses],
            "actions": [a.to_dict() for a in self.actions],
        }


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------
def _parse_recorded_identity(recorded: str | None) -> tuple[int, int, int, int] | None:
    """Parse the ``volume:inode:size:mtime_ns`` string stored in ``documents``.

    ``None`` (or anything unparseable) means no identity was recorded, which is a
    fact recovery must surface rather than paper over.
    """
    if not recorded:
        return None
    parts = str(recorded).split(":")
    if len(parts) != 4:
        return None
    try:
        volume, inode, size, mtime_ns = (int(p) for p in parts)
    except (TypeError, ValueError):
        return None
    return volume, inode, size, mtime_ns


def _identity_agrees(recorded: str | None, observed: FileIdentity | None) -> bool | None:
    """Whether the observed file is plausibly the file the journal recorded.

    ``True``/``False`` when both sides carry a comparable identity; ``None`` when
    either is unavailable and the question therefore cannot be answered. Identity
    is a corroborating signal, never a sole authorization input; a mismatch is
    strong evidence *against* a completed move, which is why it blocks.

    A zero file index means a filesystem did not report one (some Windows
    configurations and filesystems). Size is *not* a substitute for identity: a
    copy placed by another actor has the same size and content hash, so accepting a
    size match would be exactly the "copied file mistaken for a completed move"
    case PRD 13.3 forbids. When either side lacks a usable inode the result is
    ``None`` -- unestablished -- so the caller routes to ``IDENTITY_UNVERIFIED``
    and says plainly what it could not prove, rather than committing on size.
    """
    parsed = _parse_recorded_identity(recorded)
    if parsed is None or observed is None:
        return None
    rec_volume, rec_inode, _rec_size, _ = parsed
    if rec_inode == 0 or observed.inode == 0:
        return None
    return (rec_volume, rec_inode) == (observed.volume, observed.inode)


def _namespace_owned(operation: FileOperationRecord, destination_rel: str) -> bool:
    """Whether the destination sits in the document's own operation-owned namespace.

    Managed moves put the file under ``Rejected/<document-id>/<filename>`` or
    ``Trash/<batch-id>/<document-id>/<filename>`` (the planner's
    ``_build_destination``); the document ID segment is the ownership proof that a
    hash alone cannot give. For Trash the document id is the *third* component --
    the batch id at index 1 scopes the move and is not the document. A restore
    writes back to the document's active path and is owned by the operation record
    itself, provided it does not land inside a managed directory.
    """
    parts = [p for p in destination_rel.split("/") if p]
    if not parts:
        return False
    if operation.kind is PendingIntent.MOVE_REJECTED:
        return len(parts) >= 3 and parts[0] == REJECTED_DIR and parts[1] == operation.document_id
    if operation.kind is PendingIntent.MOVE_TRASH:
        return (
            len(parts) >= 3
            and parts[0] == TRASH_DIR
            and parts[1] == operation.batch_id
            and parts[2] == operation.document_id
        )
    # Restore kinds: the destination is the document's own active path, which must
    # not be inside a managed or reserved directory.
    return parts[0] not in (REJECTED_DIR, TRASH_DIR, REVIEW_DIR)


def _describe_file(path: Path) -> dict[str, Any]:
    """Non-hashing facts about a path. Safe on absent paths."""
    identity = file_identity(path)
    return {
        "path": str(path),
        "exists": os.path.lexists(path),
        "identity": identity.digest_hint() if identity is not None else None,
        "size": identity.size if identity is not None else None,
        "is_reparse_point": _is_reparse(path),
        "is_regular_file": os.path.isfile(path),
    }


def _is_reparse(path: Path) -> bool:
    from ..storage.paths import is_reparse_point

    try:
        return is_reparse_point(path)
    except OSError:  # pragma: no cover - defensive; lstat failing is handled inside
        return False


def _content_matches(path: Path, *, expected_sha256: str | None, expected_size: int | None) -> bool | None:
    """Whether a real file matches the operation's recorded size and hash.

    ``None`` means the question could not be evaluated (no recorded hash, or the
    file is not readable); the caller treats that as "not established".
    """
    if not expected_sha256:
        return None
    if expected_size is not None:
        try:
            if os.path.getsize(path) != int(expected_size):
                return False
        except OSError:
            return None
    try:
        return sha256_file(path) == expected_sha256
    except OSError:
        return None


def _base_evidence(
    operation: FileOperationRecord,
    *,
    source_path: Path,
    destination_path: Path,
    source_containment: dict[str, Any],
    destination_containment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "document_id": operation.document_id,
        "batch_id": operation.batch_id,
        "kind": operation.kind.value if isinstance(operation.kind, PendingIntent) else str(operation.kind),
        "journal_state": operation.state.value
        if isinstance(operation.state, OperationState)
        else str(operation.state),
        "source_rel_path": operation.source_rel_path,
        "destination_rel_path": operation.destination_rel_path,
        "expected": {
            "sha256": operation.expected_sha256,
            "size": operation.expected_size,
            "source_revision": operation.source_revision,
            "location_version": operation.location_version,
        },
        "source": _describe_file(source_path),
        "destination": _describe_file(destination_path),
        "source_containment": source_containment,
        "destination_containment": destination_containment,
    }


def _blocked_evidence(
    operation: FileOperationRecord,
    *,
    source_path: Path,
    destination_path: Path,
    reason: str,
) -> dict[str, Any]:
    """Evidence for a path that could not even be resolved into the root."""
    return {
        "operation_id": operation.id,
        "document_id": operation.document_id,
        "batch_id": operation.batch_id,
        "source_rel_path": operation.source_rel_path,
        "destination_rel_path": operation.destination_rel_path,
        "source": {"path": str(source_path)},
        "destination": {"path": str(destination_path)},
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_operation(
    repo: Repository,
    *,
    operation: FileOperationRecord,
    root: str | os.PathLike[str],
) -> OperationDiagnosis:
    """Diagnose one journal operation against the actual filesystem.

    This function reads only. It performs no mutation, so it is safe to call in a
    dry run, from a report, or from a test. The five PRD 13.3 rows are each their
    own code path below; two extra conditions are named explicitly because a
    silent fall-through would be exactly the laziness the PRD warns against
    (``ALREADY_COMMITTED``) or would accept an unverifiable destination
    (``IDENTITY_UNVERIFIED``).
    """
    root_path = Path(root)

    # A finished operation is not re-reconciled. This is what makes a replayed
    # apply request unable to repeat completed work (PRD 13.3).
    journal_state = operation.state.value if isinstance(operation.state, OperationState) else str(operation.state)
    if journal_state == OperationState.COMMITTED.value:
        return OperationDiagnosis(
            operation_id=operation.id,
            document_id=operation.document_id,
            batch_id=operation.batch_id,
            condition=Condition.ALREADY_COMMITTED,
            recovery=Recovery.NO_ACTION,
            detail="The journal already records this move as committed; a replay is a no-op.",
            safe_to_proceed_without_human=True,
            evidence={"operation_id": operation.id, "journal_state": journal_state},
        )

    # Resolve both paths inside the root. A path that does not resolve is itself a
    # path-ownership conflict (row 5).
    try:
        source_path = _resolve(root_path, operation.source_rel_path)
        destination_path = _resolve(root_path, operation.destination_rel_path)
        source_containment = containment_report(root_path, operation.source_rel_path)
        destination_containment = containment_report(root_path, operation.destination_rel_path)
    except ResumeReviewError as exc:
        return OperationDiagnosis(
            operation_id=operation.id,
            document_id=operation.document_id,
            batch_id=operation.batch_id,
            condition=Condition.IDENTITY_OR_CONTENT_DIFFERS,
            recovery=Recovery.BLOCK_PRESERVE_EVIDENCE,
            detail=f"The recorded path could not be resolved inside the workspace root: {exc.message}",
            safe_to_proceed_without_human=False,
            requires_human_investigation=True,
            evidence=_blocked_evidence(
                operation,
                source_path=root_path / operation.source_rel_path,
                destination_path=root_path / operation.destination_rel_path,
                reason=exc.code,
            ),
        )

    if not source_containment.get("ok") or not destination_containment.get("ok"):
        return OperationDiagnosis(
            operation_id=operation.id,
            document_id=operation.document_id,
            batch_id=operation.batch_id,
            condition=Condition.IDENTITY_OR_CONTENT_DIFFERS,
            recovery=Recovery.BLOCK_PRESERVE_EVIDENCE,
            detail="A recorded path is outside the workspace root or escapes it through a link.",
            safe_to_proceed_without_human=False,
            requires_human_investigation=True,
            evidence=_base_evidence(
                operation,
                source_path=source_path,
                destination_path=destination_path,
                source_containment=source_containment,
                destination_containment=destination_containment,
            ),
        )

    source_exists = os.path.lexists(source_path)
    destination_exists = os.path.lexists(destination_path)

    if source_exists and destination_exists:
        return _both_present(operation, source_path=source_path, destination_path=destination_path, evidence=_base_evidence(
            operation,
            source_path=source_path,
            destination_path=destination_path,
            source_containment=source_containment,
            destination_containment=destination_containment,
        ))
    if source_exists and not destination_exists:
        return _source_only(
            operation,
            source_path=source_path,
            destination_path=destination_path,
            evidence=_base_evidence(
                operation,
                source_path=source_path,
                destination_path=destination_path,
                source_containment=source_containment,
                destination_containment=destination_containment,
            ),
        )
    if destination_exists and not source_exists:
        document = repo.get_document(operation.document_id)
        return _destination_only(
            repo,
            operation,
            operation_source_identity=repo.get_file_operation_source_identity(operation.id),
            document_fs_identity=document.fs_identity if document is not None else None,
            destination_path=destination_path,
            evidence=_base_evidence(
                operation,
                source_path=source_path,
                destination_path=destination_path,
                source_containment=source_containment,
                destination_containment=destination_containment,
            ),
        )
    return _neither(operation, evidence=_base_evidence(
        operation,
        source_path=source_path,
        destination_path=destination_path,
        source_containment=source_containment,
        destination_containment=destination_containment,
    ))


def _resolve(root: Path, rel: str) -> Path:
    """Resolve a recorded root-relative path without following links."""
    from ..storage.paths import to_absolute

    return to_absolute(root, rel)


def _both_present(
    operation: FileOperationRecord,
    *,
    source_path: Path,
    destination_path: Path,
    evidence: dict[str, Any],
) -> OperationDiagnosis:
    """Row 3: both names exist. Stop. Never delete one merely because hashes match.

    ``same_inode`` is recorded because on POSIX a ``link``-then-``unlink``
    interruption leaves two names for one file; that is useful for the human doing
    the reconciliation, but it does *not* license this module to clean anything up.
    There is no branch here that removes either path.
    """
    evidence = dict(evidence)
    evidence["same_inode"] = same_inode(source_path, destination_path)
    evidence["note"] = (
        "Both the source and the destination exist. Recovery never deletes either "
        "one, even when the content hashes match: a matching hash is not proof that "
        "the destination is this operation's file, and a deletion here is "
        "unrecoverable (PRD 13.3)."
    )
    return OperationDiagnosis(
        operation_id=operation.id,
        document_id=operation.document_id,
        batch_id=operation.batch_id,
        condition=Condition.BOTH_PRESENT,
        recovery=Recovery.STOP_FOR_RECONCILIATION,
        detail="Both the source and the destination exist; a human must reconcile them.",
        safe_to_proceed_without_human=False,
        requires_human_investigation=True,
        evidence=evidence,
    )


def _source_only(
    operation: FileOperationRecord,
    *,
    source_path: Path,
    destination_path: Path,
    evidence: dict[str, Any],
) -> OperationDiagnosis:
    """Row 1 (and its row-5 failure): source present, destination absent.

    Resumable only when the source is the file the plan recorded. A source whose
    content changed is not "as expected" and must not be moved.
    """
    evidence = dict(evidence)
    if _is_reparse(source_path):
        evidence["reason"] = "source_is_reparse_point"
        return _differ(operation, evidence, "The source is a link or reparse point, which is never a managed file.")
    if not os.path.isfile(source_path):
        evidence["reason"] = "source_not_regular_file"
        return _differ(operation, evidence, "The source is not a regular file.")

    content_matches = _content_matches(
        source_path,
        expected_sha256=operation.expected_sha256,
        expected_size=operation.expected_size,
    )
    evidence["content_matches"] = content_matches

    if content_matches is False:
        evidence["reason"] = "source_content_changed"
        return _differ(
            operation,
            evidence,
            "The source content no longer matches the size and hash the plan recorded.",
        )
    if content_matches is None:
        evidence["reason"] = "source_content_unverifiable"
        evidence["established"] = [
            "the source exists at the recorded path and is a regular file",
            "the destination is absent",
        ]
        evidence["not_established"] = [
            "that the source content matches the recorded revision "
            "(no usable recorded hash to compare against)",
        ]
        return OperationDiagnosis(
            operation_id=operation.id,
            document_id=operation.document_id,
            batch_id=operation.batch_id,
            condition=Condition.IDENTITY_UNVERIFIED,
            recovery=Recovery.BLOCK_PRESERVE_EVIDENCE,
            detail="The source exists but its content could not be verified against the recorded revision.",
            safe_to_proceed_without_human=False,
            requires_human_investigation=True,
            evidence=evidence,
        )

    evidence["reason"] = "source_matches_recorded_revision"
    return OperationDiagnosis(
        operation_id=operation.id,
        document_id=operation.document_id,
        batch_id=operation.batch_id,
        condition=Condition.SOURCE_PRESENT_DESTINATION_ABSENT,
        recovery=Recovery.RESUME,
        detail=(
            "The source exists as expected and the destination is absent; the move is a "
            "safe candidate to resume after approval and expiry policy checks."
        ),
        safe_to_proceed_without_human=True,
        requires_policy_recheck=True,
        evidence=evidence,
    )


def _destination_only(
    repo: Repository,
    operation: FileOperationRecord,
    *,
    operation_source_identity: str | None,
    document_fs_identity: str | None,
    destination_path: Path,
    evidence: dict[str, Any],
) -> OperationDiagnosis:
    """Row 2 and its row-5 failures: source absent, destination present.

    "Verified" is deliberately strict. It requires the recorded destination path,
    the document-owned namespace, matching size and hash, *and* a matching
    recorded identity. Anything less is a conflict or an unverified destination --
    a copied file from another actor must not be mistaken for a completed move.

    The identity used is the **operation-bound** source identity recorded at plan
    time (``file_operations.source_identity``). That value is tied to the
    operation's ``source_revision``; a same-volume rename preserves it, a copy
    does not. Only when the operation carries no identity (a journal row written
    before migration 0002) does this fall back to ``documents.fs_identity``, and
    the evidence names which one was used. Either way, an identity whose inode is
    unavailable (0) yields ``None`` -- unestablished -- so a size/hash match alone
    never commits.
    """
    evidence = dict(evidence)
    if _is_reparse(destination_path):
        evidence["reason"] = "destination_is_reparse_point"
        return _differ(
            operation,
            evidence,
            "The destination is a link or reparse point; refusing to treat it as an operation-owned file.",
        )
    if not os.path.isfile(destination_path):
        evidence["reason"] = "destination_not_regular_file"
        return _differ(operation, evidence, "The destination is not a regular file.")

    recorded_identity = operation_source_identity or document_fs_identity
    namespace_owned = _namespace_owned(operation, operation.destination_rel_path)
    content_matches = _content_matches(
        destination_path,
        expected_sha256=operation.expected_sha256,
        expected_size=operation.expected_size,
    )
    observed_identity = file_identity(destination_path)
    identity_matches = _identity_agrees(recorded_identity, observed_identity)

    evidence["namespace_owned"] = namespace_owned
    evidence["content_matches"] = content_matches
    evidence["recorded_identity"] = recorded_identity
    evidence["operation_source_identity"] = operation_source_identity
    evidence["document_fs_identity"] = document_fs_identity
    if operation_source_identity:
        evidence["identity_source"] = "operation"
    elif document_fs_identity:
        evidence["identity_source"] = "document"
    else:
        evidence["identity_source"] = None
    evidence["identity_matches"] = identity_matches

    if not namespace_owned:
        evidence["reason"] = "destination_path_not_operation_owned"
        return _differ(
            operation,
            evidence,
            "The destination is not inside this document's operation-owned namespace.",
        )
    if content_matches is False:
        evidence["reason"] = "destination_content_differs"
        return _differ(
            operation,
            evidence,
            "The destination content does not match the recorded size and hash of this operation's source.",
        )
    if identity_matches is False:
        evidence["reason"] = "destination_identity_differs"
        return _differ(
            operation,
            evidence,
            "The destination content matches but its file identity does not match the recorded source; "
            "this is not established as proof of this operation's move.",
        )
    if content_matches is None or identity_matches is None:
        evidence["reason"] = "destination_ownership_unverified"
        evidence["established"] = [
            "the destination exists at the recorded path",
            "the destination is inside the document's operation-owned namespace",
        ]
        if content_matches is True:
            evidence["established"].append("the destination size and content hash match the recorded revision")
        # ``not_established`` must exist before anything appends to it: an
        # unverifiable content check (no recorded hash, or an unreadable file) and
        # an unavailable identity can both hold at once, and the append below used
        # to run before the key was created, raising KeyError instead of blocking.
        not_established = evidence.setdefault("not_established", [])
        if content_matches is None:
            not_established.append("that the destination content matches the recorded revision")
        not_established.append(
            "that the destination is this operation's file rather than a copy placed here by another actor "
            "(no recorded file identity to compare)"
        )
        return OperationDiagnosis(
            operation_id=operation.id,
            document_id=operation.document_id,
            batch_id=operation.batch_id,
            condition=Condition.IDENTITY_UNVERIFIED,
            recovery=Recovery.BLOCK_PRESERVE_EVIDENCE,
            detail=(
                "The source is absent and a file is present at the recorded destination, but its ownership "
                "could not be verified; refusing to commit it without the ownership evidence."
            ),
            safe_to_proceed_without_human=False,
            requires_human_investigation=True,
            evidence=evidence,
        )

    evidence["reason"] = "destination_verified_as_operation_owned"
    return OperationDiagnosis(
        operation_id=operation.id,
        document_id=operation.document_id,
        batch_id=operation.batch_id,
        condition=Condition.SOURCE_ABSENT_DESTINATION_VERIFIED,
        recovery=Recovery.COMMIT,
        detail="The source is absent and the operation-owned destination is verified; commit the performed move.",
        safe_to_proceed_without_human=True,
        evidence=evidence,
    )


def _neither(
    operation: FileOperationRecord,
    *,
    evidence: dict[str, Any],
) -> OperationDiagnosis:
    """Row 4: neither name exists. Mark missing; a human investigates."""
    evidence = dict(evidence)
    evidence["reason"] = "source_and_destination_absent"
    evidence["note"] = (
        "Neither the recorded source nor the recorded destination exists. Recovery marks the document "
        "missing; it does not guess a location and it does not delete anything."
    )
    return OperationDiagnosis(
        operation_id=operation.id,
        document_id=operation.document_id,
        batch_id=operation.batch_id,
        condition=Condition.NEITHER_PRESENT,
        recovery=Recovery.MARK_MISSING,
        detail="Neither the source nor the destination exists; the file is missing and needs human investigation.",
        safe_to_proceed_without_human=False,
        requires_human_investigation=True,
        evidence=evidence,
    )


def _differ(
    operation: FileOperationRecord,
    evidence: dict[str, Any],
    detail: str,
) -> OperationDiagnosis:
    """Row 5: content, path ownership, or identity differs. Block; preserve evidence."""
    evidence = dict(evidence)
    evidence.setdefault(
        "note",
        "A conflict was detected. Recovery does not touch or remove either file; the evidence is "
        "preserved for a human to resolve (PRD 13.3).",
    )
    return OperationDiagnosis(
        operation_id=operation.id,
        document_id=operation.document_id,
        batch_id=operation.batch_id,
        condition=Condition.IDENTITY_OR_CONTENT_DIFFERS,
        recovery=Recovery.BLOCK_PRESERVE_EVIDENCE,
        detail=detail,
        safe_to_proceed_without_human=False,
        requires_human_investigation=True,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Plan / repair
# ---------------------------------------------------------------------------
def plan_recovery(
    repo: Repository,
    *,
    root: str | os.PathLike[str],
    batch_id: str | None = None,
    dry_run: bool = True,
) -> RecoveryPlan:
    """Reconcile interrupted operations. Dry run by default; mutates nothing.

    ``dry_run=False`` is the explicit opt-in for a repair. Even then, only journal
    and location state is written, through :class:`Repository` so each change is
    audited and bumps ``instances.state_revision``. No file is ever moved, deleted,
    or created here; resuming a move is the executor's job (row 1).
    """
    root_path = Path(root)
    if batch_id is not None:
        if repo.get_batch(batch_id) is None:
            raise NotFound(
                "That action batch does not exist.",
                code=Code.NOT_FOUND,
                detail={"entity": "batch"},
            )
        candidates = [
            op for op in repo.list_file_operations(batch_id) if _is_active(op)
        ]
    else:
        candidates = repo.find_operations_in_state(ACTIVE_OPERATION_STATES)

    diagnoses: list[OperationDiagnosis] = []
    actions: list[RecoveryAction] = []
    counts: dict[str, int] = {}

    for operation in candidates:
        diagnosis = classify_operation(repo, operation=operation, root=root_path)
        diagnoses.append(diagnosis)
        counts[diagnosis.recovery] = counts.get(diagnosis.recovery, 0) + 1
        actions.append(_reconcile(repo, operation=operation, diagnosis=diagnosis, dry_run=dry_run))

    return RecoveryPlan(
        root=str(root_path),
        dry_run=bool(dry_run),
        batch_id=batch_id,
        diagnoses=diagnoses,
        actions=actions,
        counts=counts,
    )


def _is_active(operation: FileOperationRecord) -> bool:
    state = operation.state.value if isinstance(operation.state, OperationState) else str(operation.state)
    return state in ACTIVE_OPERATION_STATES


def _reconcile(
    repo: Repository,
    *,
    operation: FileOperationRecord,
    diagnosis: OperationDiagnosis,
    dry_run: bool,
) -> RecoveryAction:
    """Report (dry run) or apply (repair) the reconciliation for one diagnosis."""
    if diagnosis.recovery in (Recovery.NO_ACTION, Recovery.RESUME):
        return RecoveryAction(
            operation_id=operation.id,
            document_id=operation.document_id,
            recovery=diagnosis.recovery,
            applied=False,
            detail=(
                "The executor resumes this move after approval and expiry checks; reconciliation "
                "itself changes nothing."
                if diagnosis.recovery == Recovery.RESUME
                else "No reconciliation is required."
            ),
        )

    if dry_run:
        return RecoveryAction(
            operation_id=operation.id,
            document_id=operation.document_id,
            recovery=diagnosis.recovery,
            applied=False,
            detail="Dry run: " + _would_do(diagnosis),
            mutations=[],
        )

    mutations: list[str] = []
    try:
        if diagnosis.recovery == Recovery.COMMIT:
            location = _LOCATION_FOR_KIND.get(operation.kind, Location.ACTIVE)
            _apply_location(repo, operation, operation.destination_rel_path, location)
            mutations.append(f"document.location={location.value}")
            repo.update_file_operation(
                operation.id,
                OperationState.COMMITTED.value,
                observed_source_state="absent",
                observed_destination_state="verified",
            )
            mutations.append("file_operation.state=committed")
        elif diagnosis.recovery == Recovery.MARK_MISSING:
            _apply_location(repo, operation, operation.source_rel_path, Location.MISSING)
            mutations.append("document.location=missing")
            repo.update_file_operation(
                operation.id,
                OperationState.NEEDS_RECONCILIATION.value,
                observed_source_state="absent",
                observed_destination_state="absent",
                error_code=Code.SOURCE_MISSING,
                error_detail=diagnosis.detail,
            )
            mutations.append("file_operation.state=needs_reconciliation")
        elif diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION:
            _apply_location(repo, operation, operation.destination_rel_path, Location.CONFLICT)
            mutations.append("document.location=conflict")
            repo.update_file_operation(
                operation.id,
                OperationState.NEEDS_RECONCILIATION.value,
                observed_source_state="present",
                observed_destination_state="present",
                error_code=Code.NEEDS_RECONCILIATION,
                error_detail=diagnosis.detail,
            )
            mutations.append("file_operation.state=needs_reconciliation")
        else:  # Recovery.BLOCK_PRESERVE_EVIDENCE
            _apply_location(repo, operation, operation.destination_rel_path, Location.CONFLICT)
            mutations.append("document.location=conflict")
            repo.update_file_operation(
                operation.id,
                OperationState.NEEDS_RECONCILIATION.value,
                observed_source_state=(
                    "present"
                    if (diagnosis.evidence.get("source", {}) or {}).get("exists")
                    else "absent"
                ),
                observed_destination_state=(
                    "present"
                    if (diagnosis.evidence.get("destination", {}) or {}).get("exists")
                    else "absent"
                ),
                error_code=Code.NEEDS_RECONCILIATION,
                error_detail=diagnosis.detail,
            )
            mutations.append("file_operation.state=needs_reconciliation")
    except ResumeReviewError as exc:
        return RecoveryAction(
            operation_id=operation.id,
            document_id=operation.document_id,
            recovery=diagnosis.recovery,
            applied=bool(mutations),
            detail=f"Reconciliation could not complete: {exc.message}",
            mutations=mutations,
            error=exc.code,
        )

    return RecoveryAction(
        operation_id=operation.id,
        document_id=operation.document_id,
        recovery=diagnosis.recovery,
        applied=True,
        detail=_would_do(diagnosis),
        mutations=mutations,
    )


def _apply_location(
    repo: Repository,
    operation: FileOperationRecord,
    rel_path: str,
    location: Location,
) -> None:
    """Set the document location under the operation's optimistic revision.

    A concurrent move raises ``RevisionConflict``; the caller records the partial
    result rather than overwriting newer state.
    """
    repo.set_document_location(
        operation.document_id,
        rel_path,
        location.value,
        expected_location_version=operation.location_version,
    )


def _would_do(diagnosis: OperationDiagnosis) -> str:
    return {
        Recovery.COMMIT: "record the verified move: commit the operation and reconcile the document location.",
        Recovery.MARK_MISSING: "mark the document missing and flag the operation for human investigation.",
        Recovery.STOP_FOR_RECONCILIATION: (
            "stop and flag the operation for reconciliation; both files are left untouched."
        ),
        Recovery.BLOCK_PRESERVE_EVIDENCE: (
            "block and flag the operation; every file is left in place so the conflict can be inspected."
        ),
        Recovery.NO_ACTION: "no action.",
        Recovery.RESUME: "leave the operation for the executor to resume.",
    }.get(diagnosis.recovery, "no action.")
