"""End-to-end tests for the apply executor across a whole batch.

Authority: PRD sections 13.1 (plan -> approve -> apply, partial completion),
13.2 (filesystem safety) and 13.3 (idempotent replay).

These tests drive the real :func:`~resume_review.actions.executor.apply_batch`
against a real temporary workspace and a real database. They cover the three
properties that only show up once more than one operation is involved:

* a conflict that appears *during* execution stops the remaining work, leaves the
  completed operation on disk, and marks the batch partial with a remaining count;
* a fully-applied batch replays as a no-op (the tree is byte-identical);
* a Trash intent lands in the batch-scoped Trash namespace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import plan_actions
from resume_review.actions.executor import OpOutcome, apply_batch
from resume_review.actions.journal import journal_path
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code
from resume_review.models import (
    TRASH_DIR,
    ExecutionState,
    Location,
    MediaType,
    OperationState,
    PendingIntent,
    Principal,
    Role,
)
from resume_review.util import now_iso, seconds_from_now_iso, sha256_file

ACTOR = "reviewer@example.test"


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


def reviewer(repo: Repository) -> Principal:
    return Principal(
        actor_ref=ACTOR,
        role=Role.REVIEWER,
        session_id="sess_test",
        instance_id=repo.instance_id,
    )


def add_document(repo: Repository, root: Path, name: str, data: bytes) -> str:
    """Place bytes on disk, register the document, and mark it Reject."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=len(data),
        content_sha256=sha256_file(path),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor=ACTOR)
    return document.id


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Managed files under the workspace; the ``.review`` journal is excluded."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".review":
            continue
        if path.is_file():
            out[rel.as_posix()] = path.read_bytes()
    return out


def persist_and_approve(repo: Repository, plan) -> None:
    repo.create_batch(plan, created_by=ACTOR)
    repo.create_file_operations(plan.batch_id, plan.operations)
    repo.approve_batch(
        plan.batch_id, actor=ACTOR, plan_hash=plan.plan_hash, expires_at=seconds_from_now_iso(900)
    )


# ---------------------------------------------------------------------------
# Partial completion
# ---------------------------------------------------------------------------
def test_a_mid_execution_collision_stops_the_batch_and_leaves_earlier_work(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = [
        add_document(repo, root, "alpha.pdf", b"%PDF-1.4\nalpha\n"),
        add_document(repo, root, "bravo.pdf", b"%PDF-1.4\nbravo\n"),
        add_document(repo, root, "charlie.pdf", b"%PDF-1.4\ncharlie\n"),
    ]
    plan = plan_actions(repo, document_ids=docs, requested_by=ACTOR, criteria_version=0, root=root)
    assert len(plan.operations) == 3
    persist_and_approve(repo, plan)

    first, second, third = plan.operations
    foreign = b"%PDF-1.4\nforeign file placed by another actor\n"

    # Race the executor: the instant operation 1 commits its location, an unrelated
    # file appears at operation 2's destination. This is a collision that exists
    # during execution, not at plan time.
    real_set_location = repo.set_document_location
    calls = {"n": 0}

    def racing_set_location(document_id, rel_path, location, expected_location_version):
        result = real_set_location(document_id, rel_path, location, expected_location_version)
        calls["n"] += 1
        if calls["n"] == 1:
            colliding = root / second.destination
            colliding.parent.mkdir(parents=True, exist_ok=True)
            colliding.write_bytes(foreign)
        return result

    monkeypatch.setattr(repo, "set_document_location", racing_set_location)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    # The batch stopped, is partial, and reports the remaining work.
    assert outcome.ok is False
    assert outcome.state == ExecutionState.PARTIAL.value
    assert outcome.code == Code.BATCH_PARTIAL
    assert outcome.remaining == 2
    assert outcome.counts["moved"] == 1
    assert outcome.counts["completed"] == 1

    # Operation 1 completed on disk and was not rolled back.
    assert not (root / first.source).exists()
    assert (root / first.destination).read_bytes() == b"%PDF-1.4\nalpha\n"

    # Operation 2 was stopped by the collision; neither file was touched.
    assert (root / second.source).read_bytes() == b"%PDF-1.4\nbravo\n"
    assert (root / second.destination).read_bytes() == foreign

    # Operation 3 was never reached; its source is untouched and no destination exists.
    assert (root / third.source).read_bytes() == b"%PDF-1.4\ncharlie\n"
    assert not (root / third.destination).exists()

    rows = {row.document_id: row.state for row in repo.list_file_operations(plan.batch_id)}
    assert rows[docs[0]] == OperationState.COMMITTED
    assert rows[docs[1]] == OperationState.NEEDS_RECONCILIATION
    assert rows[docs[2]] == OperationState.PLANNED

    assert repo.get_document(docs[0]).location == Location.REJECTED
    assert repo.get_document(docs[1]).location == Location.ACTIVE
    assert repo.get_document(docs[2]).location == Location.ACTIVE
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.PARTIAL.value

    # A blocked operation left a reconciliation task for a human.
    tasks = repo.list_tasks(document_id=docs[1], state="open")
    assert any(task.task_type == "reconciliation" for task in tasks)

    # And the durable journal exists for the batch.
    assert journal_path(root, plan.batch_id).is_file()


# ---------------------------------------------------------------------------
# Idempotent replay across a whole batch
# ---------------------------------------------------------------------------
def test_a_multi_document_batch_applies_and_replays_idempotently(
    repo: Repository, root: Path
) -> None:
    docs = [
        add_document(repo, root, "one.pdf", b"%PDF-1.4\none\n"),
        add_document(repo, root, "two.pdf", b"%PDF-1.4\ntwo\n"),
        add_document(repo, root, "three.pdf", b"%PDF-1.4\nthree\n"),
    ]
    plan = plan_actions(repo, document_ids=docs, requested_by=ACTOR, criteria_version=0, root=root)
    assert len(plan.operations) == 3
    persist_and_approve(repo, plan)

    first = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())
    assert first.ok is True
    assert first.state == ExecutionState.COMPLETED.value
    assert first.counts["moved"] == 3
    assert first.remaining == 0

    after_first = tree_snapshot(root)
    revision_after_first = repo.get_batch(plan.batch_id)["execution_revision"]

    second = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert second.ok is True
    assert second.state == ExecutionState.COMPLETED.value
    assert second.counts["moved"] == 0
    assert second.counts["already_completed"] == 3
    assert {op.outcome for op in second.operations} == {OpOutcome.ALREADY_COMPLETED}
    # The tree is byte-identical: no operation ran a second time.
    assert tree_snapshot(root) == after_first
    assert repo.get_batch(plan.batch_id)["execution_revision"] == revision_after_first


# ---------------------------------------------------------------------------
# Trash namespace
# ---------------------------------------------------------------------------
def test_a_trash_intent_moves_into_the_batch_scoped_trash_namespace(
    repo: Repository, root: Path
) -> None:
    path = root / "discard.pdf"
    path.write_bytes(b"%PDF-1.4\ndiscard\n")
    document = repo.create_document(
        original_filename="discard.pdf",
        rel_path="discard.pdf",
        media_type=MediaType.PDF,
        size_bytes=len(b"%PDF-1.4\ndiscard\n"),
        content_sha256=sha256_file(path),
        fs_identity=None,
    )
    # Trash is only ever entered on an explicit intent; nothing is inferred.
    repo.set_intent(document.id, PendingIntent.MOVE_TRASH.value, requester=ACTOR, expected_revision=0)

    plan = plan_actions(
        repo, document_ids=[document.id], requested_by=ACTOR, criteria_version=0, root=root
    )
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == PendingIntent.MOVE_TRASH
    expected_destination = f"{TRASH_DIR}/{plan.batch_id}/{document.id}/discard.pdf"
    assert operation.destination == expected_destination

    persist_and_approve(repo, plan)
    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.ok is True
    assert (root / expected_destination).read_bytes() == b"%PDF-1.4\ndiscard\n"
    assert not (root / "discard.pdf").exists()
    refreshed = repo.get_document(document.id)
    assert refreshed.location == Location.TRASH
    assert refreshed.current_rel_path == expected_destination
