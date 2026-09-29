"""Adversarial verification of the per-operation source-identity migration and
the crash-recovery path.

Authority: PRD sections 13.2 and 13.3. This file tries to *break* the five claims
made about migration ``0002`` and recovery, not to demonstrate them. Where a
claim survives the attack the test passes; where it does not, the failure is the
reproducer.

The claims under attack:

1.  A genuine same-volume move is recovered and committed; a copy with identical
    bytes/size/sha256 but a different inode, a destination on a different volume,
    and a recycled inode must not be committed on identity alone.
2.  Failure is safe: at every crash point around intent persistence and the move,
    recovery never loses a file, never reports success falsely, and never deletes.
    ``actions/`` contains no unsanctioned deletion path (verified by AST sweep).
3.  The migration is safe: 0001 then 0002 on a populated database preserves rows
    with a NULL identity, recovery treats those rows fail-safe, the runner records
    a checksum and refuses a tampered file, and a downgrade is refused.
4.  Identity is never a sole authorization input: a commit on identity with a
    mismatching hash, a mismatching size, or a destination outside the
    operation-owned namespace is refused.
5.  The two prior fixes hold: a recorded identity with inode 0 yields
    ``None``/``IDENTITY_UNVERIFIED`` and never commits on size alone, and a Trash
    destination shaped ``Trash/<batch_id>/<document_id>/<filename>`` is
    namespace-owned.

All data is synthetic. Every test is offline, deterministic, and non-privileged.
"""

from __future__ import annotations

import ast
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
from resume_review.db.migrations import (
    MigrationError,
    apply_migrations,
    assert_downgrade_allowed,
    current_version,
    discover_migrations,
)
from resume_review.models import (
    REJECTED_DIR,
    TRASH_DIR,
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
REVIEWER = "reviewer@example.test"


# ---------------------------------------------------------------------------
# Fixtures
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
    repository.create_instance("inst_adv_id", __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "Job - Operations Manager"
    workspace.mkdir(parents=True)
    return workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return sha256_file(path)


def _make_document(
    repo: Repository,
    rel_path: str,
    *,
    fs_identity: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
):
    return repo.create_document(
        original_filename=Path(rel_path).name,
        rel_path=rel_path,
        media_type=MediaType.PDF,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        fs_identity=fs_identity,
    )


def _persist(
    repo: Repository,
    *,
    document_id: str,
    kind: PendingIntent,
    source: str,
    destination: str,
    expected_sha256: str | None,
    expected_size: int | None,
    source_identity: str | None,
    batch_id: str | None = None,
) -> tuple[str, str]:
    """Write one durable journal row, optionally binding a source identity.

    Returns ``(batch_id, operation_id)``. Passing ``source_identity=None`` models
    a row written by migration 0001 (no identity column value), not a bug in the
    planner.
    """
    batch = batch_id or new_id("batch")
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document_id,
        kind=kind,
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
        batch_id=batch,
        criteria_version=1,
        operations=[operation],
    )
    plan.plan_hash = plan.compute_hash()
    repo.create_batch(plan, created_by=REVIEWER)
    explicit = {operation.operation_id: source_identity} if source_identity is not None else None
    repo.create_file_operations(batch, [operation], source_identities=explicit)
    return batch, operation.operation_id


def _plan_and_persist(repo: Repository, root: Path, document_id: str):
    """Plan one reject-move through the real planner and persist its journal row."""
    plan = plan_actions(
        repo,
        document_ids=[document_id],
        intent_by_document={document_id: PendingIntent.MOVE_REJECTED},
        requested_by=REVIEWER,
        criteria_version=1,
        root=root,
    )
    assert len(plan.operations) == 1, plan.to_dict()
    repo.create_batch(plan, created_by=REVIEWER)
    repo.create_file_operations(plan.batch_id, plan.operations)
    return plan


def _diagnose(repo: Repository, batch_id: str, root: Path, index: int = 0):
    record = repo.list_file_operations(batch_id)[index]
    return record, classify_operation(repo, operation=record, root=root)


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ===========================================================================
# Claim 1 -- a genuine same-volume move commits; a copy never does
# ===========================================================================
def test_genuine_same_volume_rename_commits_on_operation_identity(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"genuine bytes moved by the kernel"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]
    recorded = repo.get_file_operation_source_identity(operation.operation_id)
    assert recorded, "the planner must bind a source identity to the operation"

    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    repo.update_file_operation(operation.operation_id, OperationState.FILE_MOVED.value)

    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED
    assert diagnosis.recovery == Recovery.COMMIT
    assert diagnosis.evidence["identity_matches"] is True
    repair = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert repair.applied == 1
    assert repo.get_document(document.id).location is Location.REJECTED
    assert destination_abs.read_bytes() == payload


def test_copy_with_identical_bytes_size_and_sha_but_new_inode_never_commits(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"byte-for-byte identical copy"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    source_identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]

    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    destination_abs.write_bytes(payload)
    assert file_identity(destination_abs).digest_hint() != source_identity
    (root / source_rel).unlink()

    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is False
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False

    repair = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert repair.counts.get(Recovery.COMMIT, 0) == 0
    assert repo.list_file_operations(plan.batch_id)[0].state is OperationState.NEEDS_RECONCILIATION
    assert destination_abs.read_bytes() == payload


def test_a_destination_on_a_different_volume_is_not_committed(
    repo: Repository, root: Path
) -> None:
    """The recorded volume is part of the identity; a foreign volume must not match.

    A cross-volume copy always yields a new inode *and* a new volume, but this
    isolates the volume component: the destination presents the recorded inode on
    a different volume, which must still fail.
    """
    source_rel = "candidate-001.pdf"
    destination_rel = f"{REJECTED_DIR}/doc_volume/candidate-001.pdf"
    payload = b"same content, different volume"
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    observed = file_identity(root / destination_rel)
    assert observed is not None and observed.inode != 0
    # Same inode and size, different volume: a fabricated cross-volume identity.
    forged = f"{observed.volume + 1}:{observed.inode}:{observed.size}:{observed.mtime_ns}"
    document = _make_document(repo, source_rel, fs_identity=None)
    batch, operation_id = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity=forged,
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["identity_matches"] is False
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False
    repair = plan_recovery(repo, root=root, batch_id=batch, dry_run=False)
    assert repair.counts.get(Recovery.COMMIT, 0) == 0


def test_different_volume_and_different_inode_copy_is_blocked(
    repo: Repository, root: Path
) -> None:
    """A plain cross-volume copy (new inode, new volume) also fails."""
    source_rel = "candidate-001.pdf"
    destination_rel = f"{REJECTED_DIR}/doc_vol2/candidate-001.pdf"
    payload = b"copied across a volume boundary"
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    document = _make_document(repo, source_rel, fs_identity=None)
    batch, _ = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity="999999:123456:7:8",  # a foreign volume+inode
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE


def test_a_matching_document_identity_does_not_override_the_operation_identity(
    repo: Repository, root: Path
) -> None:
    """A copy at the destination never commits, even if the document's *current*
    identity was (wrongly) pointed at the copy.

    This mirrors the crash scenario in ``test_executor.py``: the destination is a
    fresh inode and the document identity names it. The operation-bound identity is
    bound to the source revision and must win, so the copy is refused rather than
    accepted as the performed move (PRD 13.3).
    """
    source_rel = "candidate-001.pdf"
    payload = b"the destination is a copy, not the moved source"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    source_identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]

    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    destination_abs.write_bytes(payload)  # a copy: new inode
    copy_identity = file_identity(destination_abs).digest_hint()
    assert copy_identity != source_identity
    with repo.db.write(actor="helper", actor_kind="helper", event="test.copy_identity") as conn:
        conn.execute(
            "UPDATE documents SET fs_identity = ? WHERE id = ?", (copy_identity, document.id)
        )
    (root / source_rel).unlink()
    repo.update_file_operation(operation.operation_id, OperationState.FILE_MOVED.value)

    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.evidence["document_fs_identity"] == copy_identity
    assert diagnosis.evidence["recorded_identity"] == source_identity
    assert diagnosis.evidence["identity_source"] == "operation"
    assert diagnosis.evidence["identity_matches"] is False
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False


# ===========================================================================
# Claim 2 -- failure is safe at every crash point; no deletion path
# ===========================================================================
def _crash_operation(repo: Repository, root: Path, *, state: str):
    """Plan a reject-move, persist it, force a journal state, return ids.

    No file is moved here; each caller arranges the filesystem around the intent.
    """
    source_rel = "candidate-001.pdf"
    payload = b"crash-point payload"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]
    if state != OperationState.PLANNED.value:
        repo.update_file_operation(operation.operation_id, state)
    return document, plan, source_rel, operation, payload


def test_crash_before_intent_source_present_destination_absent_resumes(
    repo: Repository, root: Path
) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.PLANNED.value
    )
    before = _tree(root)
    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT
    assert diagnosis.recovery == Recovery.RESUME

    result = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    # Row 1 is the executor's job; reconciliation touches nothing.
    assert result.counts.get(Recovery.RESUME, 0) == 1
    assert result.applied == 0
    assert _tree(root) == before
    assert (root / source_rel).read_bytes() == payload


def test_crash_after_intent_before_move_resumes_without_mutation(
    repo: Repository, root: Path
) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.INTENT_RECORDED.value
    )
    before = _tree(root)
    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.recovery == Recovery.RESUME
    plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert _tree(root) == before


def test_crash_after_move_source_absent_destination_present_commits_without_touching_files(
    repo: Repository, root: Path
) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.FILE_MOVED.value
    )
    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    before = _tree(root)

    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.recovery == Recovery.COMMIT
    plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    # Reconciliation wrote DB state only; the filesystem is byte-identical.
    assert _tree(root) == before
    assert destination_abs.read_bytes() == payload
    assert not (root / source_rel).exists()


def test_crash_both_present_never_deletes_a_file(repo: Repository, root: Path) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.FILE_MOVED.value
    )
    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    destination_abs.write_bytes(payload)  # both names now exist
    before = _tree(root)

    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.condition == Condition.BOTH_PRESENT
    assert diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION
    result = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert result.counts.get(Recovery.COMMIT, 0) == 0
    # Neither file was removed; both bytes survive.
    assert _tree(root) == before
    assert (root / source_rel).read_bytes() == payload
    assert destination_abs.read_bytes() == payload


def test_crash_neither_present_marks_missing_without_deleting(
    repo: Repository, root: Path
) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.FILE_MOVED.value
    )
    (root / source_rel).unlink()
    before = _tree(root)
    _, diagnosis = _diagnose(repo, plan.batch_id, root)
    assert diagnosis.condition == Condition.NEITHER_PRESENT
    assert diagnosis.recovery == Recovery.MARK_MISSING
    plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert _tree(root) == before
    assert repo.get_document(document.id).location is Location.MISSING


def test_recovery_never_reports_success_for_a_copy(repo: Repository, root: Path) -> None:
    """A copy at the destination makes no commit claim and moves no file."""
    source_rel = "candidate-001.pdf"
    payload = b"the destination is someone else's copy"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    plan = _plan_and_persist(repo, root, document.id)
    operation = plan.operations[0]
    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    destination_abs.write_bytes(payload)
    (root / source_rel).unlink()
    before = _tree(root)

    result = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    # "Applied" here means the block was journaled; it must never mean a commit.
    assert result.counts.get(Recovery.COMMIT, 0) == 0
    assert _tree(root) == before
    assert repo.list_file_operations(plan.batch_id)[0].state is OperationState.NEEDS_RECONCILIATION
    assert repo.get_document(document.id).location is not Location.REJECTED


def test_dry_run_reconciliation_mutates_nothing(repo: Repository, root: Path) -> None:
    document, plan, source_rel, operation, payload = _crash_operation(
        repo, root, state=OperationState.FILE_MOVED.value
    )
    destination_abs = root / operation.destination
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    before = _tree(root)
    revision_before = repo.db.state_revision()
    audit_before = repo.db.scalar("SELECT COUNT(*) FROM audit_events")
    state_before = repo.list_file_operations(plan.batch_id)[0].state

    result = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=True)
    assert result.mutating is False
    assert result.applied == 0
    assert _tree(root) == before
    assert repo.db.state_revision() == revision_before
    assert repo.db.scalar("SELECT COUNT(*) FROM audit_events") == audit_before
    assert repo.list_file_operations(plan.batch_id)[0].state is state_before


#: Calls that mutate or delete a filesystem entry. ``os.replace`` is allowed only
#: in the journal, which replaces its own temp file -- never a managed document.
_FORBIDDEN_CALLS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("shutil", "move"),
    ("shutil", "rmtree"),
    ("shutil", "copy"),
    ("shutil", "copy2"),
    ("shutil", "copyfile"),
    ("os", "rename"),
}
_FORBIDDEN_METHODS = {"unlink", "rmdir", "rmtree", "truncate", "replace", "rename", "write_bytes", "write_text"}


def test_ast_sweep_actions_has_no_unsanctioned_deletion_or_rename() -> None:
    """The only mutating FS calls in ``actions/`` are sanctioned; nothing deletes.

    Scans every module under ``src/resume_review/actions`` for ``os.*``/``shutil.*``
    mutating calls and for path-object mutators. ``os.replace`` is permitted only
    in ``journal.py`` (temp file -> journal). Any deletion call anywhere fails.
    """
    import resume_review.actions as actions_pkg

    actions_dir = Path(actions_pkg.__file__).parent
    offenders: list[str] = []
    for module_path in sorted(actions_dir.glob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), str(module_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                base = func.value
                if isinstance(base, ast.Name) and base.id in ("os", "shutil"):
                    if (base.id, func.attr) in _FORBIDDEN_CALLS:
                        offenders.append(f"{module_path.name}:{node.lineno}:{base.id}.{func.attr}")
                    if (base.id, func.attr) == ("os", "replace") and module_path.name != "journal.py":
                        offenders.append(f"{module_path.name}:{node.lineno}:os.replace")
                if func.attr in _FORBIDDEN_METHODS and module_path.name != "journal.py":
                    # A path-object mutator (``path.unlink()`` etc.) is never
                    # sanctioned in this package.
                    if func.attr in {"unlink", "rmdir", "rmtree", "rename", "truncate"}:
                        offenders.append(f"{module_path.name}:{node.lineno}:{func.attr}()")
    assert offenders == [], f"unsanctioned filesystem mutation in actions/: {offenders}"


def test_no_journal_file_under_actions_deletes_a_managed_document() -> None:
    """``journal.py``'s os.replace target is its own temp file, never a document."""
    import resume_review.actions.journal as journal_module

    source = Path(journal_module.__file__).read_text(encoding="utf-8")
    # The replace must be from a sibling temp path over the journal path.
    assert "os.replace(" in source
    assert "shutil" not in source


# ===========================================================================
# Claim 3 -- migration safety
# ===========================================================================
def _copy_migrations(directory: Path, versions: tuple[int, ...]) -> None:
    """Copy the named migration files into ``directory`` byte-for-byte."""
    import shutil

    directory.mkdir(parents=True, exist_ok=True)
    real = Path(discover_migrations()[0].path).parent
    for migration in discover_migrations():
        if migration.version in versions:
            shutil.copy(real / migration.path.name, directory / migration.path.name)


def test_0001_then_0002_on_a_populated_database_preserves_rows_with_null_identity(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "migrations"
    _copy_migrations(stage, (1,))
    only_0001 = tmp_path / "only0001"
    only_0001.mkdir()
    _copy_migrations(only_0001, (1,))

    database = Database(DbConfig(path=tmp_path / "populated.db"))
    conn = database.connect()
    apply_migrations(conn, directory=only_0001)
    assert current_version(conn) == 1

    repo = Repository(database)
    repo.create_instance("inst_mig", __version__, 1)
    source_rel = "candidate-001.pdf"
    payload = b"a row that predates the identity column"
    (tmp_path / "candidate-001.pdf").write_bytes(payload)
    document = _make_document(
        repo,
        source_rel,
        fs_identity=None,
        content_sha256=sha256_file(tmp_path / "candidate-001.pdf"),
        size_bytes=len(payload),
    )
    # A journal row exactly as 0001 wrote it: the source_identity column does not
    # exist yet, so the row is inserted with the 0001 column set only.
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    batch = new_id("batch")
    operation_id = new_id("operation")
    digest = sha256_file(tmp_path / "candidate-001.pdf")
    plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.instance_id,
        batch_id=batch,
        criteria_version=1,
        operations=[
            PlannedOperation(
                operation_id=operation_id,
                document_id=document.id,
                kind=PendingIntent.MOVE_REJECTED,
                source=source_rel,
                destination=destination_rel,
                source_revision=1,
                expected_sha256=digest,
                expected_size=len(payload),
                decision_revision=0,
                intent_revision=1,
                location_version=0,
            )
        ],
    )
    plan.plan_hash = plan.compute_hash()
    repo.create_batch(plan, created_by=REVIEWER)
    now = "2020-01-01T00:00:00Z"
    with repo.db.write(actor="helper", actor_kind="helper", event="test.legacy_row") as conn:
        conn.execute(
            "INSERT INTO file_operations (id, instance_id, batch_id, document_id, sequence, "
            "kind, source_rel_path, destination_rel_path, expected_sha256, expected_size, "
            "source_revision, decision_revision, intent_revision, location_version, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 0, 'move_rejected', ?, ?, ?, ?, "
            "1, 0, 1, 0, 'planned', ?, ?)",
            (
                operation_id,
                repo.instance_id,
                batch,
                document.id,
                source_rel,
                destination_rel,
                digest,
                len(payload),
                now,
                now,
            ),
        )
    rows_before = repo.db.scalar("SELECT COUNT(*) FROM documents")

    # Now bring 0002 in and migrate.
    _copy_migrations(stage, (1, 2))
    result = apply_migrations(conn, directory=stage)
    assert result["applied"] == [2]
    assert current_version(conn) == MIGRATION_VERSION

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_operations)")}
    assert "source_identity" in columns
    # Existing rows survived, with a NULL identity.
    assert repo.db.scalar("SELECT COUNT(*) FROM documents") == rows_before
    assert repo.get_file_operation_source_identity(operation_id) is None
    assert repo.get_document(document.id) is not None
    database.close()


def test_pre_migration_row_is_treated_fail_safe(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    payload = b"content matches but there is no recorded identity"
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    batch, _ = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity=None,
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False


def test_pre_migration_row_with_source_present_does_not_crash(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"source still where it was planned"
    digest = _write(root / source_rel, payload)
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=len(payload)
    )
    batch, _ = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=f"{REJECTED_DIR}/{document.id}/candidate-001.pdf",
        expected_sha256=digest,
        expected_size=len(payload),
        source_identity=None,
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT


def test_migration_runner_refuses_a_tampered_migration(tmp_path: Path) -> None:
    stage = tmp_path / "migrations"
    _copy_migrations(stage, (1, 2))
    database = Database(DbConfig(path=tmp_path / "tamper.db"))
    conn = database.connect()
    first = apply_migrations(conn, directory=stage)
    assert first["applied"] == [1, 2]

    target = stage / "0002_file_operation_source_identity.sql"
    target.write_text(target.read_text() + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(conn, directory=stage)
    assert excinfo.value.code == "MANIFEST_MISMATCH"
    database.close()


def test_downgrade_from_a_newer_schema_is_refused(db: Database) -> None:
    conn = db.connect()
    # Record a version this build does not understand.
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
        "VALUES (9999, 'future', '2020-01-01T00:00:00Z', 'x')"
    )
    conn.commit()
    assert current_version(conn) == 9999
    with pytest.raises(MigrationError) as excinfo:
        assert_downgrade_allowed(conn)
    assert excinfo.value.code == "DOWNGRADE_REFUSED"


def test_second_apply_of_an_up_to_date_database_is_idempotent(db: Database) -> None:
    """Migration 0002's version must be understood by this build.

    A build that applies migration 2 but reports ``SCHEMA_VERSION < 2`` treats its
    own up-to-date database as a downgrade and refuses to open it. This test used
    to mark that condition ``xfail`` while the constant was frozen at 1 by another
    concern; it now fails hard, because a silent xfail would hide exactly the
    regression the assertion exists to catch. tests/unit/test_migration_version.py
    guards the constant directly.
    """
    conn = db.connect()
    result = apply_migrations(conn)
    assert result["applied"] == []
    assert result["version"] == MIGRATION_VERSION
    assert MIGRATION_VERSION <= SCHEMA_VERSION


# ===========================================================================
# Claim 4 -- identity is never a sole authorization input
# ===========================================================================
def _owned_identity_operation(
    repo: Repository,
    root: Path,
    *,
    payload: bytes,
    tamper: str | None = None,
    destination_rel: str | None = None,
):
    """A genuine move whose destination identity matches, with one field tampered.

    ``tamper`` selects which recorded fact to corrupt: ``hash``, ``size``, or
    ``namespace``.
    """
    source_rel = "candidate-001.pdf"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    dest = destination_rel or f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    batch, operation_id = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=dest,
        expected_sha256="f" * 64 if tamper == "hash" else digest,
        expected_size=(size + 1) if tamper == "size" else size,
        source_identity=identity,
    )
    # A genuine same-volume move: the destination presents the recorded identity.
    destination_abs = root / dest
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    repo.update_file_operation(operation_id, OperationState.FILE_MOVED.value)
    return document, batch, destination_abs


def test_identity_alone_with_a_mismatching_hash_is_refused(
    repo: Repository, root: Path
) -> None:
    document, batch, destination_abs = _owned_identity_operation(
        repo, root, payload=b"hash must be checked, not assumed", tamper="hash"
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["identity_matches"] is True
    assert diagnosis.evidence["content_matches"] is False
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False
    repair = plan_recovery(repo, root=root, batch_id=batch, dry_run=False)
    assert repair.counts.get(Recovery.COMMIT, 0) == 0
    assert destination_abs.exists()


def test_identity_alone_with_a_mismatching_size_is_refused(
    repo: Repository, root: Path
) -> None:
    document, batch, destination_abs = _owned_identity_operation(
        repo, root, payload=b"size must be checked too", tamper="size"
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["identity_matches"] is True
    assert diagnosis.recovery in (Recovery.BLOCK_PRESERVE_EVIDENCE, Recovery.STOP_FOR_RECONCILIATION)
    assert diagnosis.committable is False


def test_identity_alone_with_a_destination_outside_the_namespace_is_refused(
    repo: Repository, root: Path
) -> None:
    """Identity matches, content matches, but the path is not operation-owned."""
    foreign_dest = f"{REJECTED_DIR}/some-other-document/candidate-001.pdf"
    document, batch, destination_abs = _owned_identity_operation(
        repo, root, payload=b"identity right, namespace wrong", destination_rel=foreign_dest
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["namespace_owned"] is False
    assert diagnosis.evidence["identity_matches"] is True
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False
    assert destination_abs.exists()


def test_identity_match_with_no_recorded_hash_is_unverified_not_committed(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"no hash recorded to compare"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    identity = file_identity(root / destination_rel).digest_hint()
    batch, _ = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=destination_rel,
        expected_sha256="",  # 0001 requires NOT NULL; empty means "no usable hash"
        expected_size=size,
        source_identity=identity,
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["content_matches"] is None
    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    # The reconciliation pass must fail safe, not raise, on an unverifiable row.
    result = plan_recovery(repo, root=root, batch_id=batch, dry_run=False)
    assert result.counts.get(Recovery.COMMIT, 0) == 0
    assert repo.list_file_operations(batch)[0].state is OperationState.NEEDS_RECONCILIATION
    assert (root / destination_rel).read_bytes() == payload


# ===========================================================================
# Claim 5 -- the two prior fixes hold
# ===========================================================================
def test_recorded_identity_with_inode_zero_is_unverified_and_never_commits_on_size(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"size matches but the inode is unavailable"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    observed = file_identity(root / destination_rel)
    assert observed is not None
    forged = f"{observed.volume}:0:{size}:{observed.mtime_ns}"
    batch, _ = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity=forged,
    )
    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is None
    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.committable is False

    plan_recovery(repo, root=root, batch_id=batch, dry_run=False)
    assert repo.list_file_operations(batch)[0].state is OperationState.NEEDS_RECONCILIATION
    assert (root / destination_rel).read_bytes() == payload


def test_trash_destination_shape_is_recognised_as_namespace_owned(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"moved into the trash namespace"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    batch = new_id("batch")
    destination_rel = f"{TRASH_DIR}/{batch}/{document.id}/candidate-001.pdf"
    _, operation_id = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_TRASH,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity=identity,
        batch_id=batch,
    )
    destination_abs = root / destination_rel
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    repo.update_file_operation(operation_id, OperationState.FILE_MOVED.value)

    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["namespace_owned"] is True
    assert diagnosis.recovery == Recovery.COMMIT
    repair = plan_recovery(repo, root=root, batch_id=batch, dry_run=False)
    assert repair.applied == 1
    assert repo.get_document(document.id).location is Location.TRASH


def test_trash_destination_with_the_wrong_batch_or_document_is_not_owned(
    repo: Repository, root: Path
) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"trash path with the wrong owner segment"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    identity = file_identity(root / source_rel).digest_hint()
    document = _make_document(
        repo, source_rel, fs_identity=None, content_sha256=digest, size_bytes=size
    )
    batch = new_id("batch")
    # Wrong document segment: the batch and document IDs do not match the operation.
    destination_rel = f"{TRASH_DIR}/{batch}/some-other-document/candidate-001.pdf"
    _, operation_id = _persist(
        repo,
        document_id=document.id,
        kind=PendingIntent.MOVE_TRASH,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        source_identity=identity,
        batch_id=batch,
    )
    destination_abs = root / destination_rel
    destination_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / source_rel, destination_abs)
    repo.update_file_operation(operation_id, OperationState.FILE_MOVED.value)

    _, diagnosis = _diagnose(repo, batch, root)
    assert diagnosis.evidence["namespace_owned"] is False
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert destination_abs.exists()
