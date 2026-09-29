"""Action-planner tests.

Authority: PRD sections 10, 10.1, 12.3 and 13.1, and AGENTS.md constraints 3, 4, 9.

These tests use synthetic data and real files inside a temporary root. They prove:

* building a plan never touches the filesystem and is not approval;
* each PRD 10.1 destination rule produces exactly the specified operation;
* a missing source blocks and creates a reconciliation task;
* an occupied destination is reported as a collision, never planned over;
* a ``decision_needs_recheck`` document is not silently included;
* every operation carries its revision and hash preconditions;
* ``plan_hash`` excludes the volatile fields and covers the operations;
* adversarial filenames cannot escape the root.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import SkipReason, plan_actions
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.models import (
    ActionPlan,
    Location,
    MediaType,
    PendingIntent,
    PlannedOperation,
    ReviewState,
)
from resume_review.util import new_id

DATA = b"%PDF-1.4\nsynthetic test resume bytes\n"


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
    size_bytes: int | None = None,
) -> Any:
    if content_sha256 == "auto":
        content_sha256 = digest()
    return repo.create_document(
        original_filename=name,
        rel_path=rel_path or name,
        media_type=MediaType.PDF,
        size_bytes=len(DATA) if size_bytes is None else size_bytes,
        content_sha256=content_sha256,
        fs_identity=None,
    )


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


def make_batch_with_committed_op(
    repo: Repository, document: Any, source: str, destination: str
) -> str:
    """Record one already-committed operation, as a prior move would have."""
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
    repo.update_file_operation(operation_ids[0], "committed")
    return batch_id


# ---------------------------------------------------------------------------
# Planning moves nothing and is not approval
# ---------------------------------------------------------------------------
def test_planning_moves_nothing_and_is_not_approval(repo: Repository, root: Path) -> None:
    keep = create_doc(repo, "keep-me.pdf", rel_path="keep-me.pdf")
    reject = create_doc(repo, "reject-me.pdf", rel_path="reject-me.pdf")
    place_file(root, "keep-me.pdf")
    place_file(root, "reject-me.pdf")
    repo.set_decision(keep.id, ReviewState.KEEP.value, 0, "reviewer")
    repo.set_decision(reject.id, ReviewState.REJECT.value, 0, "reviewer")

    before = tree_snapshot(root)
    plan = plan_actions(
        repo,
        document_ids=[keep.id, reject.id],
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )
    after = tree_snapshot(root)

    assert before == after, "planning must be byte-for-byte filesystem read-only"
    assert len(plan.operations) == 1
    assert plan.operations[0].document_id == reject.id
    # Not approval: no batch row and no operation rows were persisted.
    assert repo.list_batches() == []
    assert repo.find_operations_in_state(["planned", "intent_recorded"]) == []


# ---------------------------------------------------------------------------
# PRD 10.1 destination rules
# ---------------------------------------------------------------------------
def test_keep_active_emits_no_operation(repo: Repository, root: Path) -> None:
    doc = create_doc(repo)
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.KEEP.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.NO_OP_KEEP_ACTIVE


def test_hold_and_unreviewed_emit_no_operation(repo: Repository, root: Path) -> None:
    held = create_doc(repo, "held.pdf", rel_path="held.pdf")
    fresh = create_doc(repo, "fresh.pdf", rel_path="fresh.pdf")
    place_file(root, "held.pdf")
    place_file(root, "fresh.pdf")
    repo.set_decision(held.id, ReviewState.HOLD.value, 0, "reviewer")

    plan = plan_actions(
        repo,
        document_ids=[held.id, fresh.id],
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, held.id) == SkipReason.NO_OP_HOLD
    assert reason_of(plan, fresh.id) == SkipReason.NO_OP_UNREVIEWED


def test_reject_proposes_rejected_destination(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "candidate-001.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.MOVE_REJECTED
    assert operation.source == "candidate-001.pdf"
    assert operation.destination == f"Rejected/{doc.id}/candidate-001.pdf"


def test_keep_in_rejected_proposes_recorded_active_path(repo: Repository, root: Path) -> None:
    name = "candidate-007.pdf"
    doc = create_doc(repo, name)
    rejected_rel = f"Rejected/{doc.id}/{name}"
    place_file(root, rejected_rel)
    repo.set_document_location(doc.id, rejected_rel, Location.REJECTED.value, 0)
    repo.set_decision(doc.id, ReviewState.KEEP.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.RESTORE_ACTIVE
    assert operation.source == rejected_rel
    assert operation.destination == name
    assert operation.expected_previous_location == rejected_rel


def test_reject_while_already_in_rejected_is_a_no_op(repo: Repository, root: Path) -> None:
    name = "candidate-008.pdf"
    doc = create_doc(repo, name)
    rejected_rel = f"Rejected/{doc.id}/{name}"
    place_file(root, rejected_rel)
    repo.set_document_location(doc.id, rejected_rel, Location.REJECTED.value, 0)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.NO_OP_LOCATION


def test_move_trash_uses_batch_scoped_destination(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "trash-me.docx")
    place_file(root, doc.current_rel_path)
    repo.set_intent(doc.id, PendingIntent.MOVE_TRASH.value, "reviewer", 0)

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.MOVE_TRASH
    assert operation.destination == f"Trash/{plan.batch_id}/{doc.id}/trash-me.docx"


def test_restore_previous_uses_recorded_source_path(repo: Repository, root: Path) -> None:
    name = "candidate-009.pdf"
    doc = create_doc(repo, name)
    trash_rel = f"Trash/batch_prior/{doc.id}/{name}"
    make_batch_with_committed_op(repo, doc, source=name, destination=trash_rel)
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    repo.set_intent(doc.id, PendingIntent.RESTORE_PREVIOUS.value, "reviewer", 0)

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.RESTORE_PREVIOUS
    assert operation.source == trash_rel
    assert operation.destination == name


def test_restore_previous_without_a_recorded_path_is_skipped(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "candidate-010.pdf")
    name = doc.current_rel_path
    trash_rel = f"Trash/batch_none/{doc.id}/{name}"
    place_file(root, trash_rel)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    repo.set_intent(doc.id, PendingIntent.RESTORE_PREVIOUS.value, "reviewer", 0)

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.PREVIOUS_LOCATION_UNKNOWN


# ---------------------------------------------------------------------------
# Blocking conditions
# ---------------------------------------------------------------------------
def test_missing_source_blocks_and_creates_reconciliation_task(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "vanished.pdf")  # deliberately never written to disk
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.MISSING_SOURCE
    tasks = repo.list_tasks(document_id=doc.id)
    assert [t.task_type for t in tasks] == ["reconciliation"]


def test_destination_collision_is_reported_not_planned_over(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "candidate-011.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")
    occupied = root / f"Rejected/{doc.id}/candidate-011.pdf"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(b"an unrelated file")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.DESTINATION_COLLISION
    # The unrelated file is untouched.
    assert occupied.read_bytes() == b"an unrelated file"


def test_two_documents_may_not_share_a_destination(repo: Repository, root: Path) -> None:
    first = create_doc(repo, "same-name.pdf", rel_path="a/same-name.pdf")
    second = create_doc(repo, "same-name.pdf", rel_path="b/same-name.pdf")
    place_file(root, "a/same-name.pdf")
    place_file(root, "b/same-name.pdf")
    repo.set_decision(first.id, ReviewState.REJECT.value, 0, "reviewer")
    repo.set_decision(second.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo,
        document_ids=[first.id, second.id],
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    # Distinct document IDs give distinct destinations.
    destinations = {op.destination for op in plan.operations}
    assert destinations == {
        f"Rejected/{first.id}/same-name.pdf",
        f"Rejected/{second.id}/same-name.pdf",
    }


def test_decision_needs_recheck_requires_an_override(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "recheck.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")
    repo.flag_decision_needs_recheck(doc.id, "criteria version changed")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=2, root=root
    )
    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.DECISION_NEEDS_RECHECK

    overridden = plan_actions(
        repo,
        document_ids=[doc.id],
        requested_by="reviewer",
        criteria_version=2,
        root=root,
        overrides={doc.id: {"override_reason": "reviewer reconfirmed against the source"}},
    )
    assert len(overridden.operations) == 1
    assert any("reconfirmation" in warning for warning in overridden.warnings)


def test_an_empty_override_reason_does_not_satisfy_the_recheck(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "recheck-2.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")
    repo.flag_decision_needs_recheck(doc.id, "source changed")

    plan = plan_actions(
        repo,
        document_ids=[doc.id],
        requested_by="reviewer",
        criteria_version=1,
        root=root,
        overrides={doc.id: {"override_reason": "   "}},
    )
    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.DECISION_NEEDS_RECHECK


def test_missing_content_hash_blocks_a_move(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "no-hash.pdf", content_sha256=None)
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.SOURCE_IDENTITY_UNKNOWN


def test_unregistered_document_is_skipped_not_raised(repo: Repository, root: Path) -> None:
    plan = plan_actions(
        repo,
        document_ids=["doc_does_not_exist"],
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )
    assert plan.operations == []
    assert reason_of(plan, "doc_does_not_exist") == SkipReason.DOCUMENT_NOT_FOUND


# ---------------------------------------------------------------------------
# Plan contents
# ---------------------------------------------------------------------------
def test_operation_carries_revision_and_hash_preconditions(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "preconditions.pdf", size_bytes=len(DATA))
    place_file(root, doc.current_rel_path)
    decision = repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")
    repo.set_intent(doc.id, PendingIntent.MOVE_REJECTED.value, "reviewer", 0)
    intent_revision = repo.get_intent(doc.id).intent_revision

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=3, root=root
    )

    assert plan.criteria_version == 3
    operation = plan.operations[0]
    assert operation.source_revision == doc.current_revision
    assert operation.expected_sha256 == doc.content_sha256
    assert operation.expected_size == len(DATA)
    assert operation.decision_revision == decision.decision_revision
    assert operation.intent_revision == intent_revision
    assert operation.location_version == doc.location_version
    assert plan.plan_hash == plan.compute_hash()
    assert len(plan.plan_hash) == 64


def test_explicit_intent_mapping_wins_over_a_saved_intent(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "switch.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_intent(doc.id, PendingIntent.MOVE_REJECTED.value, "reviewer", 0)

    plan = plan_actions(
        repo,
        document_ids=[doc.id],
        intent_by_document={doc.id: PendingIntent.MOVE_TRASH},
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    assert plan.operations[0].kind == PendingIntent.MOVE_TRASH


def test_invalid_intent_is_skipped(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "bad-intent.pdf")
    place_file(root, doc.current_rel_path)

    plan = plan_actions(
        repo,
        document_ids=[doc.id],
        intent_by_document={doc.id: "not_a_real_intent"},
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    assert plan.operations == []
    assert reason_of(plan, doc.id) == SkipReason.INVALID_INTENT


def test_plan_hash_excludes_volatile_fields_and_covers_operations(
    repo: Repository, root: Path
) -> None:
    doc = create_doc(repo, "hashed.pdf")
    place_file(root, doc.current_rel_path)
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    volatile = dataclasses.replace(
        plan,
        plan_hash="",
        created_at="1999-01-01T00:00:00+00:00",
        requested_by="someone-else",
        counts={"operations": 99, "skipped": 3},
    )
    assert volatile.compute_hash() == plan.compute_hash()

    retargeted = dataclasses.replace(
        plan,
        operations=[dataclasses.replace(plan.operations[0], destination="Rejected/x/y.pdf")],
        plan_hash="",
    )
    assert retargeted.compute_hash() != plan.compute_hash()


def test_adversarial_filename_cannot_escape_the_root(repo: Repository, root: Path) -> None:
    doc = create_doc(repo, "..\\..\\CON.pdf", rel_path="weird.pdf")
    place_file(root, "weird.pdf")
    repo.set_decision(doc.id, ReviewState.REJECT.value, 0, "reviewer")

    plan = plan_actions(
        repo, document_ids=[doc.id], requested_by="reviewer", criteria_version=1, root=root
    )

    assert len(plan.operations) == 1
    destination = plan.operations[0].destination
    assert ".." not in destination.split("/")
    assert destination.startswith("Rejected/")
    assert (root / destination).resolve().is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# Hard rules (AGENTS.md)
# ---------------------------------------------------------------------------
def test_planner_source_contains_no_filesystem_mutation_or_forbidden_imports() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "resume_review"
        / "actions"
        / "planner.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "atomic_no_clobber_move",
        "shutil",
        "os.replace",
        "os.rename",
        "os.remove",
        "os.unlink",
        "rmtree",
        "openclaw_adapter",
        "resume_review.api",
    ):
        assert forbidden not in source, f"planner must not reference {forbidden!r}"


def test_journal_source_never_deletes_a_file_or_moves_documents() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "resume_review"
        / "actions"
        / "journal.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "atomic_no_clobber_move",
        "shutil",
        "os.rename",
        "os.remove",
        "os.unlink",
        "rmtree",
        "openclaw_adapter",
        "resume_review.api",
    ):
        assert forbidden not in source, f"journal must not reference {forbidden!r}"
    # The only rename it performs is the atomic replacement of its own journal file.
    assert "os.replace(tmp, path)" in source
