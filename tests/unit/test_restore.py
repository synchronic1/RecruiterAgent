"""Restore/undo planning tests.

Authority: PRD sections 10, 10.1 and 13.3, ``skill/references/state-model.md``,
and AGENTS.md constraints 2, 3, 4 and 5.

These tests use synthetic data and real files inside a temporary root. They prove:

* "restore previous location" and "return to the active folder" are distinct
  intents that produce *different* destinations for the same document -- the
  regression this module exists to prevent;
* any other intent is refused rather than guessed;
* a restore is a new plan with a new audit trail: planning moves nothing, writes
  no batch and no operation row, and is not approval;
* a restore preserves the earlier review decision, it never resets it;
* an occupied destination is reported as a collision and never overwritten;
* a source whose content changed externally is refused and requires new approval;
* a restore whose source is missing is blocked and creates a reconciliation task;
* every destination is canonical and stays inside the registered root.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import SkipReason
from resume_review.actions.restore import (
    RestoreSkipReason,
    coerce_restore_intent,
    plan_restore,
    plan_restore_from_batch,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, InvalidInput, NotFound
from resume_review.models import (
    ActionPlan,
    Location,
    MediaType,
    PendingIntent,
    PlannedOperation,
    ReviewState,
)
from resume_review.util import new_id

DATA = b"%PDF-1.4\nsynthetic restore test resume bytes\n"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "Job - Operations Manager"
    workspace.mkdir(parents=True)
    return workspace


def place_file(root: Path, rel_path: str, data: bytes = DATA) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def digest(data: bytes = DATA) -> str:
    return hashlib.sha256(data).hexdigest()


def create_doc(
    repo: Repository,
    name: str = "candidate-001.pdf",
    *,
    rel_path: str | None = None,
    content_sha256: str | None = "auto",
) -> Any:
    if content_sha256 == "auto":
        content_sha256 = digest()
    return repo.create_document(
        original_filename=name,
        rel_path=rel_path or name,
        media_type=MediaType.PDF,
        size_bytes=len(DATA),
        content_sha256=content_sha256,
        fs_identity=None,
    )


def make_move(
    repo: Repository,
    document: Any,
    *,
    source: str,
    destination: str,
    commit: bool = True,
) -> str:
    """Record one move in the durable journal, optionally committed.

    Mirrors what a prior apply would have written: a batch, one file_operations
    row, and (when ``commit``) the ``committed`` journal step. This is the
    authoritative record ``restore_previous`` reads for the previous location.
    """
    batch_id = new_id("batch")
    plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.db.instance_id,
        batch_id=batch_id,
        criteria_version=1,
        operations=[
            PlannedOperation(
                operation_id=new_id("operation"),
                document_id=document.id,
                kind=PendingIntent.MOVE_TRASH,
                source=source,
                destination=destination,
                source_revision=0,
                expected_sha256=document.content_sha256 or digest(),
                expected_size=document.size_bytes,
                decision_revision=0,
                intent_revision=1,
                location_version=0,
            )
        ],
    )
    repo.create_batch(plan, created_by="reviewer")
    operation_ids = repo.create_file_operations(batch_id, plan.operations)
    if commit:
        repo.update_file_operation(operation_ids[0], "committed")
    return batch_id


def reason_of(plan: ActionPlan, document_id: str) -> str | None:
    for skipped in plan.skipped:
        if skipped.document_id == document_id:
            return skipped.reason
    return None


def tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            snapshot[path.relative_to(root).as_posix()] = (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return snapshot


# ---------------------------------------------------------------------------
# The regression this module exists to prevent
# ---------------------------------------------------------------------------
def test_restore_active_and_restore_previous_have_different_destinations(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)  # first_seen_rel_path == name (the active path)
    rejected_rel = f"Rejected/{doc.id}/{name}"
    trash_rel = f"Trash/batch_old/{doc.id}/{name}"
    # History: it was rejected, then moved to Trash. Its previous recorded
    # location is Rejected/; its recorded active path is still the root file.
    make_move(repo, doc, source=rejected_rel, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    active_plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer",
        root=root,
    )
    previous_plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert len(active_plan.operations) == 1
    assert len(previous_plan.operations) == 1
    active_op = active_plan.operations[0]
    previous_op = previous_plan.operations[0]

    assert active_op.kind == PendingIntent.RESTORE_ACTIVE
    assert previous_op.kind == PendingIntent.RESTORE_PREVIOUS
    assert active_op.source == trash_rel
    assert previous_op.source == trash_rel

    assert active_op.destination == name
    assert previous_op.destination == rejected_rel
    assert active_op.destination != previous_op.destination

    # Two intents, two plans, two audit trails.
    assert active_plan.batch_id != previous_plan.batch_id
    assert active_plan.plan_hash != previous_plan.plan_hash

    # The regression, stated directly: one ambiguous "undo" would have had to pick
    # one of these and silently discard the other.
    assert active_op.destination != previous_op.destination


def test_restore_previous_may_legitimately_return_to_rejected(
    repo: Repository, root: Path
) -> None:
    """A file rejected while already in Rejected/ restores back to Rejected/."""
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    rejected_rel = f"Rejected/{doc.id}/{name}"
    trash_rel = f"Trash/batch_x/{doc.id}/{name}"
    make_move(repo, doc, source=rejected_rel, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations[0].destination == rejected_rel
    assert plan.operations[0].destination.startswith("Rejected/")


# ---------------------------------------------------------------------------
# Intent handling: refuse rather than guess
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_intent",
    [
        PendingIntent.MOVE_TRASH,
        PendingIntent.MOVE_REJECTED,
        PendingIntent.NONE,
        "move_trash",
        "none",
        "undo_everything",
        "",
    ],
)
def test_non_restore_intents_are_refused(
    repo: Repository, root: Path, bad_intent: Any
) -> None:
    doc = create_doc(repo, "candidate.pdf")
    place_file(root, doc.current_rel_path)

    with pytest.raises(InvalidInput) as excinfo:
        plan_restore(
            repo,
            document_ids=[doc.id],
            intent=bad_intent,
            requested_by="reviewer",
            root=root,
        )

    assert excinfo.value.code == Code.INVALID_INPUT
    # Refusing happens before any planning: nothing was persisted.
    assert repo.list_batches() == []


def test_coerce_restore_intent_accepts_string_and_enum() -> None:
    assert coerce_restore_intent("restore_active") == PendingIntent.RESTORE_ACTIVE
    assert coerce_restore_intent("restore_previous") == PendingIntent.RESTORE_PREVIOUS
    assert coerce_restore_intent(PendingIntent.RESTORE_ACTIVE) == PendingIntent.RESTORE_ACTIVE
    with pytest.raises(InvalidInput):
        coerce_restore_intent("restore")


# ---------------------------------------------------------------------------
# A restore is a new plan and a new audit trail; it moves nothing
# ---------------------------------------------------------------------------
def test_planning_a_restore_moves_nothing_and_is_not_approval(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_1/{doc.id}/{name}"
    make_move(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    before = tree_snapshot(root)
    decision_before = repo.get_decision(doc.id)
    intent_before = repo.get_intent(doc.id)
    batches_before = {batch["id"] for batch in repo.list_batches()}
    operations_before = {
        operation.id
        for operation in repo.find_operations_in_state(
            ["planned", "intent_recorded", "file_moved", "committed"]
        )
    }

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )
    after = tree_snapshot(root)

    assert before == after, "planning a restore must be filesystem read-only"
    # Not approval: no new batch and no new operation rows were persisted.
    assert {batch["id"] for batch in repo.list_batches()} == batches_before
    assert plan.batch_id not in batches_before
    assert {
        operation.id
        for operation in repo.find_operations_in_state(
            ["planned", "intent_recorded", "file_moved", "committed"]
        )
    } == operations_before
    assert repo.list_file_operations(plan.batch_id) == []

    # The review decision is preserved exactly; a restore never resets it.
    assert repo.get_decision(doc.id).disposition == ReviewState.REJECT
    assert repo.get_decision(doc.id).decision_revision == decision_before.decision_revision
    # No pending intent was written either; planning is not a saved request.
    assert repo.get_intent(doc.id).intent == PendingIntent.NONE
    assert repo.get_intent(doc.id).intent_revision == intent_before.intent_revision

    # The plan is fully specified and hashable.
    operation = plan.operations[0]
    assert operation.destination == name
    assert operation.expected_sha256 == digest()
    assert plan.plan_hash == plan.compute_hash()
    assert len(plan.plan_hash) == 64


def test_restore_from_trash_uses_previous_path_and_preserves_decision(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_2/{doc.id}/{name}"
    make_move(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.RESTORE_PREVIOUS
    assert operation.source == trash_rel
    assert operation.destination == name
    # Earlier decision survives the restore proposal (PRD 10.1).
    assert repo.get_decision(doc.id).disposition == ReviewState.REJECT


def test_restore_active_from_rejected_returns_to_recorded_active_path(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    rejected_rel = f"Rejected/{doc.id}/{name}"
    place_file(root, rejected_rel)
    repo.set_document_location(doc.id, rejected_rel, Location.REJECTED.value, 0)
    repo.set_decision(doc.id, ReviewState.KEEP.value, 0, "reviewer")

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent="restore_active",
        requested_by="reviewer",
        root=root,
    )

    assert len(plan.operations) == 1
    assert plan.operations[0].destination == name
    assert plan.operations[0].source == rejected_rel
    assert repo.get_decision(doc.id).disposition == ReviewState.KEEP


def test_restore_of_a_file_already_active_is_a_no_op(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "candidate.pdf")
    place_file(root, doc.current_rel_path)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.ALREADY_AT_DESTINATION


# ---------------------------------------------------------------------------
# Conflicts: never overwrite, never move on stale preconditions
# ---------------------------------------------------------------------------
def test_occupied_destination_is_reported_not_overwritten(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_3/{doc.id}/{name}"
    make_move(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    # The target of the restore is occupied by an unrelated file.
    occupant = place_file(root, name, b"an unrelated file")

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.DESTINATION_COLLISION
    # The unrelated file is untouched and no overwrite was planned.
    assert occupant.read_bytes() == b"an unrelated file"


def test_source_changed_externally_blocks_and_requires_new_approval(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)  # recorded content hash is sha256(DATA)
    trash_rel = f"Trash/batch_4/{doc.id}/{name}"
    make_move(repo, doc, source=name, destination=trash_rel)
    # The file at its recorded location no longer matches the recorded hash.
    place_file(root, trash_rel, b"tampered bytes that no longer match")
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == RestoreSkipReason.CONTENT_CHANGED
    assert plan.counts["blocked"] == 1
    # Evidence of the conflict is preserved as a reconciliation task.
    tasks = repo.list_tasks(document_id=doc.id)
    assert [t.task_type for t in tasks] == ["reconciliation"]
    # No destination was created or overwritten.
    assert not (root / name).exists()


def test_missing_source_location_is_blocked_with_reconciliation_task(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "candidate.pdf")
    # The document is recorded as missing; no file exists at any source location.
    repo.set_document_location(doc.id, "lost/candidate.pdf", Location.MISSING.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.MISSING_SOURCE
    assert [t.task_type for t in repo.list_tasks(document_id=doc.id)] == ["reconciliation"]


def test_restore_previous_without_a_recorded_path_is_skipped(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_5/{doc.id}/{name}"
    # The journal has no prior move, so there is no previous location to restore to.
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.PREVIOUS_LOCATION_UNKNOWN


def test_missing_content_hash_blocks_a_restore(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "candidate.pdf", content_sha256=None)
    name = doc.current_rel_path
    rejected_rel = f"Rejected/{doc.id}/{name}"
    place_file(root, rejected_rel)
    repo.set_document_location(doc.id, rejected_rel, Location.REJECTED.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer",
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.SOURCE_IDENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# Destinations stay inside the registered root
# ---------------------------------------------------------------------------
def test_destinations_are_canonical_and_contained(repo: Repository, root: Path) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_6/{doc.id}/{name}"
    make_move(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore(
        repo,
        document_ids=[doc.id],
        intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer",
        root=root,
    )

    destination = plan.operations[0].destination
    assert ".." not in destination.split("/")
    assert not destination.startswith("/")
    assert (root / destination).resolve().is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# Restore from a batch
# ---------------------------------------------------------------------------
def test_restore_from_batch_plans_the_undo_of_applied_operations(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_7/{doc.id}/{name}"
    original_batch = make_move(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore_from_batch(
        repo, batch_id=original_batch, requested_by="reviewer", root=root
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.RESTORE_PREVIOUS
    assert operation.source == trash_rel
    assert operation.destination == name
    # A new plan and a new audit trail; the original batch is not reused.
    assert plan.batch_id != original_batch
    assert [batch["id"] for batch in repo.list_batches()] == [original_batch]


def test_restore_from_batch_unknown_batch_raises(repo: Repository, root: Path) -> None:
    with pytest.raises(NotFound) as excinfo:
        plan_restore_from_batch(
            repo, batch_id="batch_does_not_exist", requested_by="reviewer", root=root
        )
    assert excinfo.value.code == Code.NOT_FOUND


def test_restore_from_batch_with_no_applied_operations_is_empty(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "candidate.pdf")
    unapplied = make_move(
        repo, doc, source="candidate.pdf", destination="Trash/batch_8/x/candidate.pdf",
        commit=False,
    )

    plan = plan_restore_from_batch(
        repo, batch_id=unapplied, requested_by="reviewer", root=root
    )

    assert plan.operations == []
    assert any("no applied file operations" in warning for warning in plan.warnings)


# ---------------------------------------------------------------------------
# Hard rules (AGENTS.md)
# ---------------------------------------------------------------------------
def test_restore_source_contains_no_filesystem_mutation_or_forbidden_imports() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "resume_review"
        / "actions"
        / "restore.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "atomic_no_clobber_move",
        "shutil",
        "os.replace",
        "os.rename",
        "os.remove",
        "os.unlink",
        "rmtree",
        "approve_batch",
        "create_batch",
        "openclaw_adapter",
        "resume_review.api",
    ):
        assert forbidden not in source, f"restore must not reference {forbidden!r}"
