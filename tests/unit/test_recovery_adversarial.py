"""Adversarial falsification of the planner, journal, recovery, and restore claims.

Authority: PRD sections 10, 10.1 and 13.3, and AGENTS.md constraints 4, 5 and 8.

This file exists to *try to break* the four modules:

* the catastrophe hunted for is a crash-recovery path that DELETES a file, so the
  both-exist row and the repair path are attacked with byte snapshots and an AST
  sweep for any deletion or rename primitive;
* the whole 13.3 table is exercised row by row and the mapping asserted exactly;
* destination ownership is attacked with a copy that has matching bytes and a
  recorded identity that cannot distinguish it;
* path escape through ``plan_actions`` is attacked with crafted document ids,
  filenames and recorded previous paths.

All data is synthetic. Every assertion is over real code and real temporary files.
"""

from __future__ import annotations

import ast
import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import Journal, JournalOrderError, SkipReason, plan_actions
from resume_review.actions import journal as journal_module
from resume_review.actions import planner as planner_module
from resume_review.actions import recovery as recovery_module
from resume_review.actions import restore as restore_module
from resume_review.actions.recovery import (
    ACTIVE_OPERATION_STATES,
    Condition,
    Recovery,
    classify_operation,
    plan_recovery,
)
from resume_review.actions.restore import RestoreSkipReason, plan_restore
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code
from resume_review.models import (
    REJECTED_DIR,
    TRASH_DIR,
    ActionPlan,
    Location,
    MediaType,
    OperationState,
    PendingIntent,
    PlannedOperation,
    SkippedOperation,
)
from resume_review.storage.no_clobber import file_identity
from resume_review.util import new_id, sha256_file


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


def _make_document(
    repo: Repository,
    rel_path: str,
    *,
    fs_identity: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    original_filename: str | None = None,
) -> Any:
    return repo.create_document(
        original_filename=original_filename or Path(rel_path).name,
        rel_path=rel_path,
        media_type=MediaType.PDF,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        fs_identity=fs_identity,
    )


def _set_identity(repo: Repository, document_id: str, identity: str) -> None:
    with repo.db.write(
        actor="helper",
        actor_kind="helper",
        event="document.identity",
        entity_type="document",
        entity_id=document_id,
    ) as conn:
        conn.execute("UPDATE documents SET fs_identity = ? WHERE id = ?", (identity, document_id))


def _record_content(repo: Repository, document_id: str, path: Path) -> None:
    """Record a document's current content hash/size, as discovery would."""
    with repo.db.write(
        actor="helper",
        actor_kind="helper",
        event="document.content",
        entity_type="document",
        entity_id=document_id,
    ) as conn:
        conn.execute(
            "UPDATE documents SET content_sha256 = ?, size_bytes = ? WHERE id = ?",
            (sha256_file(path), path.stat().st_size, document_id),
        )


def _make_operation(
    repo: Repository,
    *,
    document_id: str,
    source: str,
    destination: str,
    expected_sha256: str,
    expected_size: int,
    kind: PendingIntent = PendingIntent.MOVE_REJECTED,
    location_version: int = 0,
    state: str = OperationState.PLANNED.value,
) -> tuple[str, Any]:
    """Create a batch + file_operations row and return (batch_id, record)."""
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document_id,
        kind=kind,
        source=source,
        destination=destination,
        source_revision=1,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        decision_revision=1,
        intent_revision=1,
        location_version=location_version,
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
    if state != OperationState.PLANNED.value:
        repo.update_file_operation(operation.operation_id, state)
    return plan.batch_id, repo.list_file_operations(plan.batch_id)[0]


def _record_move(repo: Repository, *, document_id: str, source: str, destination: str, digest: str,
                 size: int, kind: PendingIntent = PendingIntent.MOVE_TRASH) -> str:
    """Record one already-committed move, as a prior apply would have."""
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document_id,
        kind=kind,
        source=source,
        destination=destination,
        source_revision=0,
        expected_sha256=digest,
        expected_size=size,
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
    repo.create_batch(plan, created_by="reviewer")
    ids = repo.create_file_operations(plan.batch_id, plan.operations)
    repo.update_file_operation(ids[0], OperationState.COMMITTED.value)
    return plan.batch_id


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _db_snapshot(repo: Repository) -> dict[str, Any]:
    tables = (
        "documents",
        "decisions",
        "action_intents",
        "action_batches",
        "file_operations",
        "review_tasks",
    )
    snapshot: dict[str, Any] = {
        name: [dict(r) for r in repo.db.query(f"SELECT * FROM {name} ORDER BY rowid")]
        for name in tables
    }
    snapshot["state_revision"] = repo.db.state_revision()
    snapshot["audit_count"] = repo.db.scalar("SELECT COUNT(*) FROM audit_events")
    return snapshot


# ---------------------------------------------------------------------------
# Attack 1: DELETION -- there must be no deletion path, and repair must not tidy up
# ---------------------------------------------------------------------------
def test_no_module_contains_a_deletion_or_document_move_path() -> None:
    """AST proof over every owned module: nothing removes, renames, or moves a file.

    ``journal.py`` is allowed exactly one rename, of its own journal file. Any other
    call that could remove a managed file is a critical defect.
    """
    modules = {
        "planner": planner_module,
        "journal": journal_module,
        "recovery": recovery_module,
        "restore": restore_module,
    }

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{dotted(node.value)}.{node.attr}"
        return ""

    forbidden = {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.rename",
        "os.replace",
        "shutil.rmtree",
        "shutil.move",
        "atomic_no_clobber_move",
    }
    findings: dict[str, list[str]] = {}
    for name, module in modules.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = dotted(node.func)
                if target in forbidden or target.endswith(".unlink") or target.endswith(".rmdir"):
                    calls.append(target)
        if calls:
            findings[name] = calls

    # The only permitted mutation call anywhere is journal.py's os.replace of its
    # own journal file, and it must replace a temp path inside the journal dir.
    assert findings == {"journal": ["os.replace"]}, findings
    journal_source = Path(journal_module.__file__).read_text(encoding="utf-8")
    assert "os.replace(tmp, path)" in journal_source

    # Sanity: the sweep would have caught a planted deletion.
    probe = ast.parse("import os\nos.unlink('x')\n")
    caught = [
        dotted(n.func)
        for n in ast.walk(probe)
        if isinstance(n, ast.Call) and dotted(n.func) in forbidden
    ]
    assert caught == ["os.unlink"]


def test_both_present_repair_leaves_both_files_byte_identical(repo: Repository, root: Path) -> None:
    """PRD 13.3 row 3 under a real repair: two files, matching bytes, zero deletion."""
    source_rel = "candidate-001.pdf"
    payload = b"identical bytes at both names -- a lazy clean-up would delete one"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / source_rel, payload)
    _write(root / destination_rel, payload)

    before = _tree(root)
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.BOTH_PRESENT
    assert diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION
    assert diagnosis.evidence["same_inode"] in (True, False)

    # Dry run then the real repair: neither may touch a file.
    plan_recovery(repo, root=root, batch_id=batch_id, dry_run=True)
    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 1

    after = _tree(root)
    assert after == before, "a repair must not add, remove, or alter any file"
    assert (root / source_rel).read_bytes() == payload
    assert (root / destination_rel).read_bytes() == payload
    assert repo.get_document(document.id).location is Location.CONFLICT
    assert repo.list_file_operations(batch_id)[0].state is OperationState.NEEDS_RECONCILIATION


def test_both_present_same_inode_still_stops_and_deletes_nothing(repo: Repository, root: Path) -> None:
    """A POSIX link-then-unlink window (one inode, two names) must still stop."""
    source_rel = "linked.pdf"
    payload = b"one file, two names"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    source_path = root / source_rel
    digest = _write(source_path, payload)
    dest_path = root / destination_rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_path, dest_path)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not supported on this filesystem")

    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.BOTH_PRESENT
    assert diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION

    before = _tree(root)
    plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert _tree(root) == before
    assert source_path.exists() and dest_path.exists()


def test_repair_never_removes_the_source_as_a_tidy_up(repo: Repository, root: Path) -> None:
    """Row 1 repair is a no-op on the filesystem: the source must survive it."""
    source_rel = "keep-source.pdf"
    payload = b"the source must still be here after a repair pass"
    digest = _write(root / source_rel, payload)
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT
    assert diagnosis.recovery == Recovery.RESUME

    before = _tree(root)
    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 0  # resumption is the executor's job, not reconciliation's
    assert _tree(root) == before
    assert (root / source_rel).read_bytes() == payload
    assert not (root / destination_rel).exists()
    # The operation is left exactly where it was for the executor to resume.
    assert repo.list_file_operations(batch_id)[0].state is operation.state


# ---------------------------------------------------------------------------
# Attack 2: the whole 13.3 table, asserted row by row
# ---------------------------------------------------------------------------
def test_prd_13_3_table_row_source_present_destination_absent(repo: Repository, root: Path) -> None:
    source_rel = "row1.pdf"
    payload = b"row one"
    digest = _write(root / source_rel, payload)
    document = _make_document(repo, source_rel)
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=f"{REJECTED_DIR}/{document.id}/{source_rel}",
        expected_sha256=digest,
        expected_size=len(payload),
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT
    assert diagnosis.recovery == Recovery.RESUME
    assert diagnosis.safe_to_proceed_without_human is True
    assert diagnosis.requires_policy_recheck is True


def test_prd_13_3_table_row_source_absent_destination_verified(repo: Repository, root: Path) -> None:
    source_rel = "row2.pdf"
    payload = b"row two"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, payload)
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED
    assert diagnosis.recovery == Recovery.COMMIT
    assert diagnosis.safe_to_proceed_without_human is True


def test_prd_13_3_table_row_source_absent_trash_destination_verified(
    repo: Repository, root: Path
) -> None:
    """Row 2 for a Trash move: ownership is ``Trash/<batch>/<document>/<file>``.

    Regression: a previous implementation compared the document id to the *batch*
    segment, so every legitimately completed Trash move was blocked as
    "not operation-owned" instead of being committed (PRD 13.3 row 2).
    """
    source_rel = "row2-trash.pdf"
    payload = b"row two, trashed"
    document = _make_document(repo, source_rel)
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination="",  # re-pointed below at the planner's real batch-scoped path
        expected_sha256="0" * 64,
        expected_size=len(payload),
        kind=PendingIntent.MOVE_TRASH,
    )
    destination_rel = f"{TRASH_DIR}/{batch_id}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, payload)
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    with repo.db.write(
        actor="helper",
        actor_kind="helper",
        event="operation.update",
        entity_type="file_operation",
        entity_id=operation.id,
    ) as conn:
        conn.execute(
            "UPDATE file_operations SET destination_rel_path = ?, expected_sha256 = ? WHERE id = ?",
            (destination_rel, digest, operation.id),
        )
    operation = repo.list_file_operations(batch_id)[0]

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.evidence["namespace_owned"] is True
    assert diagnosis.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED
    assert diagnosis.recovery == Recovery.COMMIT

    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 1
    assert repo.get_document(document.id).location is Location.TRASH
    assert (root / destination_rel).read_bytes() == payload  # untouched by reconciliation


def test_prd_13_3_table_row_both_present(repo: Repository, root: Path) -> None:
    source_rel = "row3.pdf"
    payload = b"row three"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / source_rel, payload)
    _write(root / destination_rel, payload)
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.BOTH_PRESENT
    assert diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.requires_human_investigation is True


def test_prd_13_3_table_row_neither_present(repo: Repository, root: Path) -> None:
    source_rel = "row4.pdf"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256="a" * 64,
        expected_size=17,
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.NEITHER_PRESENT
    assert diagnosis.recovery == Recovery.MARK_MISSING
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.requires_human_investigation is True

    plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    after = repo.get_document(document.id)
    assert after.location is Location.MISSING
    # It must not guess a location: the recorded path is left as it was, un-moved.
    assert after.current_rel_path == source_rel
    assert not (root / destination_rel).exists()


def test_prd_13_3_table_row_five_content_and_ownership_conflicts(
    repo: Repository, root: Path
) -> None:
    # Content differs at the destination.
    source_rel = "row5.pdf"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    _write(root / destination_rel, b"wrong")
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256="f" * 64,
        expected_size=None,
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.requires_human_investigation is True

    # A destination outside the document's own namespace.
    doc2 = _make_document(repo, "row5b.pdf")
    other_ns = f"{REJECTED_DIR}/someone-else/row5b.pdf"
    digest2 = _write(root / other_ns, b"bytes")
    _set_identity(repo, doc2.id, file_identity(root / other_ns).digest_hint())
    _, op2 = _make_operation(
        repo,
        document_id=doc2.id,
        source="row5b.pdf",
        destination=other_ns,
        expected_sha256=digest2,
        expected_size=len(b"bytes"),
    )
    d2 = classify_operation(repo, operation=op2, root=root)
    assert d2.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert d2.evidence["namespace_owned"] is False
    assert d2.evidence["reason"] == "destination_path_not_operation_owned"


# ---------------------------------------------------------------------------
# Attack 3: destination ownership -- a copy must not be mistaken for a move
# ---------------------------------------------------------------------------
def test_identical_copy_with_no_metadata_is_not_accepted(repo: Repository, root: Path) -> None:
    """Identical bytes at the recorded destination, no recorded identity at all."""
    source_rel = "copy.pdf"
    payload = b"the exact bytes the record expects"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, payload)  # a copy, not the recorded file

    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is None
    not_established = diagnosis.evidence["not_established"]
    assert any("another actor" in item or "identity" in item for item in not_established)

    # A repair must not commit it on hash alone.
    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 1
    assert repo.list_file_operations(batch_id)[0].state is OperationState.NEEDS_RECONCILIATION
    assert repo.get_document(document.id).location is Location.CONFLICT
    assert (root / destination_rel).read_bytes() == payload  # copy preserved


def test_size_only_identity_cannot_authorize_a_commit(repo: Repository, root: Path) -> None:
    """When no inode was recorded, size+hash must NOT be accepted as ownership.

    Regression: a recorded identity of ``volume:0:size:mtime`` (a filesystem that
    reported no file index) previously fell back to a size comparison, so a copy
    with matching size and hash was committed as a verified move -- the exact
    "copied file from another actor" case PRD 13.3 forbids.
    """
    source_rel = "no-inode.pdf"
    payload = b"same size, copied by another actor"
    document = _make_document(repo, source_rel)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, payload)  # a distinct file (new inode)
    _set_identity(repo, document.id, f"1:0:{len(payload)}:0")

    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=len(payload),
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is None
    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    assert (root / destination_rel).read_bytes() == payload  # the copy is preserved


def test_identical_copy_with_a_different_recorded_inode_is_blocked(
    repo: Repository, root: Path
) -> None:
    source_rel = "copy2.pdf"
    payload = b"bytes copied, identity is the original"
    original = root / "incoming" / source_rel
    _write(original, payload)
    original_identity = file_identity(original).digest_hint()
    document = _make_document(repo, source_rel, fs_identity=original_identity)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    _write(root / destination_rel, payload)  # a copy with a different inode

    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=sha256_file(original),
        expected_size=len(payload),
    )
    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.evidence["identity_matches"] is False
    assert diagnosis.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE


# ---------------------------------------------------------------------------
# Attack 4: journal crash-safety
# ---------------------------------------------------------------------------
def _planned(op_id: str = "op_x", doc_id: str = "doc_x") -> PlannedOperation:
    return PlannedOperation(
        operation_id=op_id,
        document_id=doc_id,
        kind=PendingIntent.MOVE_REJECTED,
        source="candidate.pdf",
        destination=f"Rejected/{doc_id}/candidate.pdf",
        source_revision=1,
        expected_sha256="a" * 64,
        expected_size=10,
        decision_revision=1,
        intent_revision=1,
        location_version=0,
    )


def test_intent_is_on_disk_before_the_executor_can_touch_the_file(root: Path) -> None:
    operation = _planned("op_durable", "doc_durable")
    journal = Journal(root, "batch_durable")
    journal.initialize([operation])

    # The executor may not record a move until the intent is durable.
    with pytest.raises(JournalOrderError):
        journal.record_file_moved(operation.operation_id)

    journal.record_intent(operation.operation_id)

    # Read the raw bytes back from disk, as a fresh process would, *before* any
    # move step: the intent must already be there.
    on_disk = journal.path.read_bytes()
    snapshot = journal_module.load_journal(journal.path)
    assert snapshot.states()[operation.operation_id] == OperationState.INTENT_RECORDED.value
    assert OperationState.INTENT_RECORDED.value.encode() in on_disk

    journal.record_file_moved(operation.operation_id)
    journal.record_committed(operation.operation_id)
    assert journal_module.load_journal(journal.path).states()[operation.operation_id] == "committed"


@pytest.mark.parametrize(
    "raw",
    [
        b"",  # zero bytes
        b'{"batch_id": "x", "operations": {"op": {"state": "int',  # truncated JSON
        b"null",  # valid JSON, not an object
        b'{"batch_id": "x"}',  # object with no operation map
        b'{"batch_id": "x", "operations": "not-a-map"}',
        b'{"batch_id": "x", "operations": {"op": 7}}',
        b'{"batch_id": "x", "operations": {"op": {"state": "not_a_state"}}}',
    ],
)
def test_corrupt_or_truncated_journal_is_reported(root: Path, raw: bytes) -> None:
    path = journal_module.journal_path(root, "batch_corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

    snapshot = journal_module.load_journal(path)
    assert snapshot.exists is True
    assert snapshot.corrupt is True
    assert snapshot.needs_reconciliation is True
    assert snapshot.detail  # a plain-language reason, never a silent empty

    # A Journal object must also refuse, with a reconciliation condition rather
    # than a generic error.
    with pytest.raises(JournalOrderError) as excinfo:
        Journal.load(root, "batch_corrupt")
    assert excinfo.value.code == Code.NEEDS_RECONCILIATION


def test_out_of_order_transitions_are_refused(root: Path) -> None:
    operation = _planned("op_order", "doc_order")
    journal = Journal(root, "batch_order")
    journal.initialize([operation])

    with pytest.raises(JournalOrderError):
        journal.record_file_moved(operation.operation_id)  # skips intent_recorded
    with pytest.raises(JournalOrderError):
        journal.record_committed(operation.operation_id)  # skips two steps

    journal.record_intent(operation.operation_id)
    with pytest.raises(JournalOrderError):
        journal.record_committed(operation.operation_id)  # skips file_moved

    journal.record_file_moved(operation.operation_id)
    journal.record_committed(operation.operation_id)
    with pytest.raises(JournalOrderError):
        journal.record_intent(operation.operation_id)  # backwards


def test_journal_never_authorizes_a_move_the_database_did_not_record(
    repo: Repository, root: Path
) -> None:
    """A journal claim with no database operation is flagged, never trusted."""
    operation = _planned("op_ghost", "doc_ghost")
    journal = Journal(root, "batch_ghost")
    journal.initialize([operation], instance_id=repo.instance_id, plan_hash="deadbeef")
    journal.record_intent(operation.operation_id)

    report = journal_module.reconcile_with_repo(journal_module.load_journal(journal.path), repo)
    assert report.needs_reconciliation is True
    item = report.items[0]
    assert item.authoritative_state is None
    assert "never committed" in item.reason


# ---------------------------------------------------------------------------
# Attack 5: plan_hash properties
# ---------------------------------------------------------------------------
def _sample_plan(**overrides: Any) -> ActionPlan:
    operation = PlannedOperation(
        operation_id="op_hash",
        document_id="doc_hash",
        kind=PendingIntent.MOVE_REJECTED,
        source="candidate.pdf",
        destination="Rejected/doc_hash/candidate.pdf",
        source_revision=2,
        expected_sha256="b" * 64,
        expected_size=128,
        decision_revision=3,
        intent_revision=1,
        location_version=0,
    )
    plan = ActionPlan(
        schema_version="1.0",
        instance_id="inst_hash",
        batch_id="batch_hash",
        criteria_version=1,
        operations=[operation],
        plan_hash="c" * 64,
        created_at="2000-01-01T00:00:00+00:00",
        requested_by="reviewer-a",
        counts={"operations": 1, "skipped": 0},
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def test_plan_hash_ignores_only_the_volatile_fields() -> None:
    base = _sample_plan()
    volatile = _sample_plan(
        plan_hash="",
        created_at="2031-12-31T23:59:59+00:00",
        requested_by="someone-else",
        counts={"operations": 999, "skipped": 7, "blocked": 4},
    )
    assert volatile.compute_hash() == base.compute_hash()

    assert len(base.compute_hash()) == 64
    changed_destination = dataclasses.replace(
        base,
        operations=[
            dataclasses.replace(base.operations[0], destination="Rejected/doc_hash/other.pdf")
        ],
    )
    assert changed_destination.compute_hash() != base.compute_hash()

    # It also covers the skipped set and the warnings.
    changed_skipped = dataclasses.replace(
        base, skipped=[SkippedOperation(document_id="doc_x", reason="missing_source")]
    )
    assert changed_skipped.compute_hash() != base.compute_hash()
    changed_warnings = dataclasses.replace(base, warnings=["something changed"])
    assert changed_warnings.compute_hash() != base.compute_hash()


# ---------------------------------------------------------------------------
# Attack 6: the planner moves nothing and writes nothing
# ---------------------------------------------------------------------------
def test_planning_every_intent_kind_moves_nothing_and_writes_nothing(
    repo: Repository, root: Path
) -> None:
    reject = _make_document(repo, "reject.pdf")
    reject_path = root / "reject.pdf"
    _write(reject_path, b"reject me")
    _record_content(repo, reject.id, reject_path)

    trash = _make_document(repo, "trash.pdf")
    trash_path = root / "trash.pdf"
    _write(trash_path, b"trash me")
    _record_content(repo, trash.id, trash_path)

    restore_active = _make_document(repo, "ra.pdf")
    ra_rel = f"{REJECTED_DIR}/{restore_active.id}/ra.pdf"
    ra_path = root / ra_rel
    _write(ra_path, b"restore me")
    _record_content(repo, restore_active.id, ra_path)
    repo.set_document_location(restore_active.id, ra_rel, Location.REJECTED.value, 0)

    restore_prev = _make_document(repo, "rp.pdf")
    rp_trash_rel = f"{TRASH_DIR}/batch_old/{restore_prev.id}/rp.pdf"
    rp_path = root / rp_trash_rel
    _write(rp_path, b"restore my previous location")
    _record_content(repo, restore_prev.id, rp_path)
    repo.set_document_location(restore_prev.id, rp_trash_rel, Location.TRASH.value, 0)
    _record_move(
        repo,
        document_id=restore_prev.id,
        source="rp.pdf",
        destination=rp_trash_rel,
        digest=sha256_file(rp_path),
        size=rp_path.stat().st_size,
    )

    keep = _make_document(repo, "keep.pdf")
    keep_path = root / "keep.pdf"
    _write(keep_path, b"keep me")
    _record_content(repo, keep.id, keep_path)

    tree_before = _tree(root)
    db_before = _db_snapshot(repo)

    plan = plan_actions(
        repo,
        document_ids=[reject.id, trash.id, restore_active.id, restore_prev.id, keep.id],
        intent_by_document={
            reject.id: PendingIntent.MOVE_REJECTED,
            trash.id: PendingIntent.MOVE_TRASH,
            restore_active.id: PendingIntent.RESTORE_ACTIVE,
            restore_prev.id: PendingIntent.RESTORE_PREVIOUS,
            keep.id: PendingIntent.NONE,
        },
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    assert {op.kind for op in plan.operations} == {
        PendingIntent.MOVE_REJECTED,
        PendingIntent.MOVE_TRASH,
        PendingIntent.RESTORE_ACTIVE,
        PendingIntent.RESTORE_PREVIOUS,
    }
    assert any(s.document_id == keep.id for s in plan.skipped)

    assert _tree(root) == tree_before, "planning must be byte-for-byte filesystem read-only"
    assert _db_snapshot(repo) == db_before, "planning must persist nothing to the database"


# ---------------------------------------------------------------------------
# Attack 7: restore intents
# ---------------------------------------------------------------------------
def test_restore_active_and_previous_differ_for_the_same_document(
    repo: Repository, root: Path
) -> None:
    name = "candidate.pdf"
    doc = _make_document(repo, name)
    rejected_rel = f"{REJECTED_DIR}/{doc.id}/{name}"
    trash_rel = f"{TRASH_DIR}/batch_old/{doc.id}/{name}"
    payload = b"restore bytes"
    trash_path = root / trash_rel
    digest = _write(trash_path, payload)
    _record_content(repo, doc.id, trash_path)
    # History: rejected first, then trashed. Previous recorded location is Rejected/.
    _record_move(repo, document_id=doc.id, source=rejected_rel, destination=trash_rel,
                 digest=digest, size=len(payload))
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    active = plan_restore(
        repo, document_ids=[doc.id], intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer", root=root,
    )
    previous = plan_restore(
        repo, document_ids=[doc.id], intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer", root=root,
    )
    assert len(active.operations) == 1 and len(previous.operations) == 1
    assert active.operations[0].destination == name
    assert previous.operations[0].destination == rejected_rel
    assert active.operations[0].destination != previous.operations[0].destination
    assert active.batch_id != previous.batch_id
    assert active.plan_hash != previous.plan_hash


def test_restore_will_not_plan_over_an_occupied_old_path(repo: Repository, root: Path) -> None:
    name = "candidate.pdf"
    doc = _make_document(repo, name)
    trash_rel = f"{TRASH_DIR}/batch_x/{doc.id}/{name}"
    trash_path = root / trash_rel
    _write(trash_path, b"the file in trash")
    _record_content(repo, doc.id, trash_path)
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)
    # The recorded active path is now occupied by an unrelated file.
    occupant = root / name
    occupant.write_bytes(b"unrelated occupant")

    plan = plan_restore(
        repo, document_ids=[doc.id], intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer", root=root,
    )
    assert plan.operations == []
    assert plan.skipped[0].reason == SkipReason.DESTINATION_COLLISION
    assert occupant.read_bytes() == b"unrelated occupant"


def test_restore_missing_source_blocks_and_creates_a_reconciliation_task(
    repo: Repository, root: Path
) -> None:
    doc = _make_document(repo, "candidate.pdf")
    repo.set_document_location(doc.id, "lost/candidate.pdf", Location.MISSING.value, 0)

    plan = plan_restore(
        repo, document_ids=[doc.id], intent=PendingIntent.RESTORE_ACTIVE,
        requested_by="reviewer", root=root,
    )
    assert plan.operations == []
    assert plan.skipped[0].reason == SkipReason.MISSING_SOURCE
    tasks = repo.list_tasks(document_id=doc.id)
    assert [t.task_type for t in tasks] == ["reconciliation"]


def test_restore_content_changed_blocks_with_new_approval(repo: Repository, root: Path) -> None:
    name = "candidate.pdf"
    original = b"the original recorded bytes"
    doc = _make_document(repo, name, content_sha256=sha256_file_bytes(original), size_bytes=len(original))
    rejected_rel = f"{REJECTED_DIR}/{doc.id}/{name}"
    trash_rel = f"{TRASH_DIR}/batch_4/{doc.id}/{name}"
    # History: rejected, then trashed. "Previous" resolves to Rejected/.
    _record_move(repo, document_id=doc.id, source=rejected_rel, destination=trash_rel,
                 digest=sha256_file_bytes(original), size=len(original))
    # The file at its recorded location no longer matches the recorded hash.
    _write(root / trash_rel, b"tampered bytes that no longer match the recorded revision")
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    plan = plan_restore(
        repo, document_ids=[doc.id], intent=PendingIntent.RESTORE_PREVIOUS,
        requested_by="reviewer", root=root,
    )
    assert plan.operations == []
    assert plan.skipped[0].reason == RestoreSkipReason.CONTENT_CHANGED
    assert plan.counts["blocked"] == 1
    assert [t.task_type for t in repo.list_tasks(document_id=doc.id)] == ["reconciliation"]


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Attack 8: path escape through plan_actions
# ---------------------------------------------------------------------------
def _craft_document_id(repo: Repository, crafted_id: str) -> Any:
    """Mint a normal document, then rewrite its id to an attacker-chosen value.

    Document ids are opaque and minted by the helper, so this models defence in
    depth: even a database row whose id is hostile must not become a path escape.
    """
    document = _make_document(repo, "crafted.pdf", original_filename="crafted.pdf")
    with repo.db.write(
        actor="helper",
        actor_kind="helper",
        event="document.rename_id",
        entity_type="document",
        entity_id=document.id,
    ) as conn:
        conn.execute("UPDATE documents SET id = ? WHERE id = ?", (crafted_id, document.id))
    return repo.get_document(crafted_id)


@pytest.mark.parametrize(
    "crafted_id",
    [
        "doc_../../escape",
        "doc_..\\..\\escape",
        "doc_C:/windows",
        "doc_bad:stream",
        "doc_trailing.",
    ],
)
def test_crafted_document_id_that_escapes_is_refused(
    repo: Repository, root: Path, crafted_id: str
) -> None:
    _write(root / "crafted.pdf", b"source bytes")
    document = _craft_document_id(repo, crafted_id)
    assert document is not None

    tree_before = _tree(root)
    plan = plan_actions(
        repo,
        document_ids=[crafted_id],
        intent_by_document={crafted_id: PendingIntent.MOVE_REJECTED},
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )

    assert plan.operations == []
    assert plan.skipped[0].reason == SkipReason.PATH_INVALID
    assert _tree(root) == tree_before


def test_crafted_document_id_with_a_slash_stays_contained(repo: Repository, root: Path) -> None:
    """A slash in an id must not escape; it may only nest inside Rejected/."""
    crafted_id = "doc_nested/sub"
    _write(root / "crafted.pdf", b"source bytes")
    document = _craft_document_id(repo, crafted_id)
    with repo.db.write(
        actor="helper", actor_kind="helper", event="document.content",
        entity_type="document", entity_id=crafted_id,
    ) as conn:
        conn.execute(
            "UPDATE documents SET content_sha256 = ?, size_bytes = ? WHERE id = ?",
            (sha256_file(root / "crafted.pdf"), (root / "crafted.pdf").stat().st_size, crafted_id),
        )
    plan = plan_actions(
        repo,
        document_ids=[crafted_id],
        intent_by_document={crafted_id: PendingIntent.MOVE_REJECTED},
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )
    assert len(plan.operations) == 1
    destination = plan.operations[0].destination
    assert (root / destination).resolve().is_relative_to(root.resolve())
    assert destination.startswith(f"{REJECTED_DIR}/")


def test_adversarial_original_filename_stays_inside_the_root(repo: Repository, root: Path) -> None:
    for index, name in enumerate(
        ("../../evil.pdf", "..\\..\\evil.pdf", "CON.pdf", "nul.txt", "a:b.pdf")
    ):
        document = _make_document(
            repo,
            rel_path=f"src-{index}.pdf",
            original_filename=name,
        )
        source = root / document.current_rel_path
        _write(source, b"source bytes")
        _record_content(repo, document.id, source)
        plan = plan_actions(
            repo,
            document_ids=[document.id],
            intent_by_document={document.id: PendingIntent.MOVE_REJECTED},
            requested_by="reviewer",
            criteria_version=1,
            root=root,
        )
        assert len(plan.operations) == 1, name
        destination = plan.operations[0].destination
        assert ".." not in destination.split("/"), destination
        assert ":" not in destination and "\\" not in destination, destination
        assert (root / destination).resolve().is_relative_to(root.resolve())
        assert destination.startswith(f"{REJECTED_DIR}/{document.id}/")


def test_crafted_previous_recorded_path_cannot_escape(repo: Repository, root: Path) -> None:
    """A hostile ``source_rel_path`` in the journal must not become a destination."""
    doc = _make_document(repo, "candidate.pdf")
    trash_rel = f"{TRASH_DIR}/batch_bad/{doc.id}/candidate.pdf"
    _write(root / trash_rel, b"source bytes")
    repo.set_document_location(doc.id, trash_rel, Location.TRASH.value, 0)

    hostile = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=doc.id,
        kind=PendingIntent.MOVE_TRASH,
        source="../../outside.pdf",
        destination=f"{TRASH_DIR}/batch_bad/{doc.id}/candidate.pdf",
        source_revision=0,
        expected_sha256="a" * 64,
        expected_size=12,
        decision_revision=0,
        intent_revision=1,
        location_version=0,
    )
    hostile_plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.instance_id,
        batch_id=new_id("batch"),
        criteria_version=1,
        operations=[hostile],
    )
    repo.create_batch(hostile_plan, created_by="reviewer")
    ids = repo.create_file_operations(hostile_plan.batch_id, hostile_plan.operations)
    repo.update_file_operation(ids[0], OperationState.COMMITTED.value)

    tree_before = _tree(root)
    plan = plan_actions(
        repo,
        document_ids=[doc.id],
        intent_by_document={doc.id: PendingIntent.RESTORE_PREVIOUS},
        requested_by="reviewer",
        criteria_version=1,
        root=root,
    )
    assert plan.operations == []
    assert plan.skipped[0].reason == SkipReason.PATH_INVALID
    assert _tree(root) == tree_before


def test_recovery_active_states_exclude_terminal_states() -> None:
    assert OperationState.PLANNED.value in ACTIVE_OPERATION_STATES
    assert OperationState.INTENT_RECORDED.value in ACTIVE_OPERATION_STATES
    assert OperationState.FILE_MOVED.value in ACTIVE_OPERATION_STATES
    assert OperationState.NEEDS_RECONCILIATION.value in ACTIVE_OPERATION_STATES
    assert OperationState.FAILED.value in ACTIVE_OPERATION_STATES
    assert OperationState.COMMITTED.value not in ACTIVE_OPERATION_STATES
    assert OperationState.SKIPPED.value not in ACTIVE_OPERATION_STATES
