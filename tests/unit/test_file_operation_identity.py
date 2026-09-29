"""Per-operation source identity: PRD 13.3 row 2 (source absent, destination verified).

Authority: PRD sections 13.2 and 13.3. This suite closes the row-2 gap left by
the 0001 schema: recovery now proves destination ownership against a source
identity bound to the operation's ``source_revision`` (written at plan time),
rather than only against the document's *current* identity.

The property under test is the one the filesystem guarantees and the PRD relies
on: a same-volume rename preserves the file's identity (its inode), so the
identity captured at plan time is exactly the identity the destination must
present after a genuine move. A copy placed by another actor has a new inode and
therefore cannot satisfy it -- which is precisely the "copied file mistaken for a
completed move" case 13.3 forbids.

All data is synthetic, the tests are offline and non-privileged, and no test
performs a managed move through the application: the "move" is simulated with a
plain rename, which is what a real executor's kernel move already did.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import plan_actions
from resume_review.actions.recovery import (
    Condition,
    Recovery,
    classify_operation,
    plan_recovery,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations, current_version, discover_migrations
from resume_review.models import (
    REJECTED_DIR,
    ActionPlan,
    Location,
    MediaType,
    OperationState,
    PendingIntent,
    PlannedOperation,
)
from resume_review.storage.no_clobber import file_identity
from resume_review.util import new_id, sha256_file

MIGRATION_VERSION = 2


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


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return sha256_file(path)


def _simulate_move(root: Path, source_rel: str, destination_rel: str) -> None:
    """A same-volume rename, as the executor's kernel move already performed it."""
    destination_abs = root / destination_rel
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)


def _make_document(
    repo: Repository,
    rel_path: str,
    *,
    fs_identity: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
) -> object:
    return repo.create_document(
        original_filename=Path(rel_path).name,
        rel_path=rel_path,
        media_type=MediaType.PDF,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        fs_identity=fs_identity,
    )


def _plan_and_persist(repo: Repository, root: Path, document_id: str) -> ActionPlan:
    """Plan one reject-move through the real planner and persist its journal rows."""
    plan = plan_actions(
        repo,
        document_ids=[document_id],
        intent_by_document={document_id: PendingIntent.MOVE_REJECTED},
        requested_by="reviewer@example.test",
        criteria_version=1,
        root=root,
    )
    assert len(plan.operations) == 1, plan.to_dict()
    repo.create_batch(plan, created_by="reviewer@example.test")
    repo.create_file_operations(plan.batch_id, plan.operations)
    return plan


def _make_operation_without_identity(
    repo: Repository,
    *,
    document_id: str,
    source: str,
    destination: str,
    expected_sha256: str,
    expected_size: int,
) -> str:
    """A journal row as migration 0001 would have written it: no source identity."""
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document_id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source,
        destination=destination,
        source_revision=1,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        decision_revision=0,
        intent_revision=1,
        location_version=0,
    )
    plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.instance_id,
        batch_id=new_id("batch"),
        criteria_version=1,
        operations=[operation],
    )
    plan.plan_hash = plan.compute_hash()
    repo.create_batch(plan, created_by="reviewer@example.test")
    repo.create_file_operations(plan.batch_id, [operation])
    return plan.batch_id


# ---------------------------------------------------------------------------
# (a) A genuine same-volume move recovers and commits
# ---------------------------------------------------------------------------
def test_genuine_move_is_committed_on_operation_bound_identity(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"resume bytes that were really moved"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    source_identity = file_identity(root / source_rel).digest_hint()
    # No document identity at all: only the operation-bound identity can verify this.
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )

    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]
    operation_id = operation.operation_id

    # The planner captured the source identity at plan time, bound to source_revision.
    recorded = repo.get_file_operation_source_identity(operation_id)
    assert recorded == source_identity, "the planner must bind the source identity to the operation"

    # Simulate the executor's same-volume rename, then a crash before the commit.
    _simulate_move(root, source_rel, operation.destination)
    repo.update_file_operation(operation_id, OperationState.FILE_MOVED.value)
    assert not (root / source_rel).exists()
    assert (root / operation.destination).read_bytes() == payload

    diagnosis = classify_operation(repo, operation=repo.list_file_operations(plan.batch_id)[0], root=root)
    assert diagnosis.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED
    assert diagnosis.recovery == Recovery.COMMIT
    assert diagnosis.safe_to_proceed_without_human is True
    assert diagnosis.evidence["identity_matches"] is True
    assert diagnosis.evidence["identity_source"] == "operation"
    assert diagnosis.evidence["recorded_identity"] == source_identity

    repair = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert repair.applied == 1
    assert repair.counts[Recovery.COMMIT] == 1

    document_after = repo.get_document(document.id)
    assert document_after is not None
    assert document_after.location is Location.REJECTED
    assert document_after.current_rel_path == operation.destination
    refreshed = repo.list_file_operations(plan.batch_id)[0]
    assert refreshed.state is OperationState.COMMITTED
    # The file was never touched by reconciliation.
    assert (root / operation.destination).read_bytes() == payload


# ---------------------------------------------------------------------------
# (b) A copy with identical bytes/size/hash but a different inode is not committed
# ---------------------------------------------------------------------------
def test_copy_with_identical_bytes_but_new_inode_is_not_committed(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"byte-identical copy must still be rejected"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    source_identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )

    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]

    # Another actor copies the same bytes to the recorded destination, then the
    # original source disappears. Content, size and hash all match; identity does not.
    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    destination_abs.write_bytes(payload)
    assert file_identity(destination_abs).digest_hint() != source_identity
    (root / source_rel).unlink()

    record = repo.list_file_operations(plan.batch_id)[0]
    diagnosis = classify_operation(repo, operation=record, root=root)

    # A documented non-commit outcome: identity is proven different, so block.
    assert diagnosis.condition in (Condition.IDENTITY_OR_CONTENT_DIFFERS, Condition.IDENTITY_UNVERIFIED)
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.committable is False
    assert diagnosis.evidence["namespace_owned"] is True
    assert diagnosis.evidence["content_matches"] is True  # bytes and hash agree ...
    assert diagnosis.evidence["identity_matches"] is False  # ... but it is not our file
    assert diagnosis.evidence["identity_source"] == "operation"

    # The repair path must not commit it either, and must leave the copy in place.
    repair = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert repair.counts.get(Recovery.COMMIT, 0) == 0
    assert repo.get_document(document.id).location is Location.CONFLICT
    assert repo.list_file_operations(plan.batch_id)[0].state is OperationState.NEEDS_RECONCILIATION
    assert destination_abs.read_bytes() == payload


# ---------------------------------------------------------------------------
# (c) An operation planned before the migration (no identity) fails safe
# ---------------------------------------------------------------------------
def test_operation_without_recorded_identity_fails_safe(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"content matches but ownership cannot be established"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size

    batch_id = _make_operation_without_identity(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )
    record = repo.list_file_operations(batch_id)[0]
    assert repo.get_file_operation_source_identity(record.id) is None

    diagnosis = classify_operation(repo, operation=record, root=root)

    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is None
    assert diagnosis.evidence["recorded_identity"] is None
    assert diagnosis.evidence["identity_source"] is None

    repair = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert repair.counts.get(Recovery.COMMIT, 0) == 0
    assert record.state is OperationState.PLANNED  # never advanced to committed
    assert repo.list_file_operations(batch_id)[0].state is OperationState.NEEDS_RECONCILIATION
    assert repo.get_document(document.id).location is Location.CONFLICT
    # Never committed on size or hash alone.
    assert (root / destination_rel).read_bytes() == payload


def test_preferred_identity_is_the_operation_value_not_the_document(
    repo: Repository, root: Path
) -> None:
    """When both exist, the operation-bound value is the one compared."""
    source_rel = "candidate-001.pdf"
    payload = b"the operation identity is authoritative"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    # A deliberately WRONG document identity must not be what verification uses.
    document = _make_document(
        repo,
        source_rel,
        fs_identity="1:999999999:1:1",
        content_sha256=digest,
        size_bytes=size,
    )

    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]
    _simulate_move(root, source_rel, operation.destination)
    repo.update_file_operation(operation.operation_id, OperationState.FILE_MOVED.value)

    diagnosis = classify_operation(repo, operation=repo.list_file_operations(plan.batch_id)[0], root=root)
    assert diagnosis.recovery == Recovery.COMMIT
    assert diagnosis.evidence["identity_source"] == "operation"
    assert diagnosis.evidence["recorded_identity"] != "1:999999999:1:1"
    assert diagnosis.evidence["document_fs_identity"] == "1:999999999:1:1"


# ---------------------------------------------------------------------------
# The repository setter/getter that binds an identity to an existing operation
# ---------------------------------------------------------------------------
def test_set_file_operation_source_identity_round_trips(repo: Repository, root: Path) -> None:
    document = _make_document(repo, "candidate-001.pdf", fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    batch_id = _make_operation_without_identity(
        repo,
        document_id=document.id,
        source="candidate-001.pdf",
        destination=destination_rel,
        expected_sha256="a" * 64,
        expected_size=4,
    )
    operation_id = repo.list_file_operations(batch_id)[0].id
    assert repo.get_file_operation_source_identity(operation_id) is None

    revision_before = repo.db.state_revision()
    repo.set_file_operation_source_identity(operation_id, "7:42:4:99")
    assert repo.get_file_operation_source_identity(operation_id) == "7:42:4:99"
    # A durable, audited mutation like every other repository write.
    assert repo.db.state_revision() > revision_before


# ---------------------------------------------------------------------------
# (d) The migration applies on top of 0001 and is idempotent under the runner
# ---------------------------------------------------------------------------
def test_migration_0002_applies_and_is_idempotent(db: Database) -> None:
    conn = db.connect()
    # 0001 then 0002 were both applied in order.
    assert current_version(conn) == MIGRATION_VERSION
    assert [m.version for m in discover_migrations()] == [1, 2]

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_operations)")}
    assert "source_identity" in columns

    # Idempotent: a second run applies nothing and reports the same version.
    result = apply_migrations(conn)
    assert result["applied"] == []
    assert result["version"] == MIGRATION_VERSION


def test_migration_0002_is_additive_only() -> None:
    """The new migration touches only the new column; 0001 is not restructured."""
    migration = next(m for m in discover_migrations() if m.version == MIGRATION_VERSION)
    sql = migration.sql.lower()
    assert "alter table file_operations add column source_identity" in sql
    for forbidden in ("drop table", "drop column", "create table", "delete from"):
        assert forbidden not in sql


def test_planning_still_persists_nothing_with_identity_capture(repo: Repository, root: Path) -> None:
    """Capturing an identity is a read: planning writes no row and no audit event."""
    source_rel = "candidate-001.pdf"
    digest = _write(root / source_rel, b"plan me")
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=7
    )
    batches_before = repo.list_batches()
    audit_before = repo.db.scalar("SELECT COUNT(*) FROM audit_events")
    revision_before = repo.db.state_revision()

    plan = plan_actions(
        repo,
        document_ids=[document.id],
        intent_by_document={document.id: PendingIntent.MOVE_REJECTED},
        requested_by="reviewer@example.test",
        criteria_version=1,
        root=root,
    )
    assert len(plan.operations) == 1
    assert repo.list_batches() == batches_before
    assert repo.db.scalar("SELECT COUNT(*) FROM audit_events") == audit_before
    assert repo.db.state_revision() == revision_before
    assert repo.find_operations_in_state(["planned", "intent_recorded"]) == []
