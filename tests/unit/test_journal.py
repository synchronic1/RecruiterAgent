"""Durable operation-journal tests.

Authority: PRD sections 13.2 and 13.3, and AGENTS.md constraints 4, 5, 8.

These tests use synthetic data and a real temporary directory. They prove:

* the operation intent reaches disk durably before any move step is allowed;
* writes are atomic (temp file in the same directory, fsync, replace, dir fsync)
  and a failed replace leaves the previous journal intact;
* the ordered progression ``planned -> intent_recorded -> file_moved ->
  committed`` is enforced in code, out-of-order transitions are refused;
* a journal loads after a restart and a corrupt/truncated file is reported as
  ``needs_reconciliation`` rather than raised away or ignored;
* reconciliation treats the database as authoritative.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import (
    Journal,
    JournalOrderError,
    journal_dir,
    journal_path,
    load_journal,
    reconcile_with_database,
    reconcile_with_repo,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, Conflict
from resume_review.models import (
    ActionPlan,
    FileOperationRecord,
    OperationState,
    PendingIntent,
    PlannedOperation,
)
from resume_review.util import new_id


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


def make_operation(op_id: str = "op_1", document_id: str = "doc_1") -> PlannedOperation:
    return PlannedOperation(
        operation_id=op_id,
        document_id=document_id,
        kind=PendingIntent.MOVE_REJECTED,
        source="candidate-001.pdf",
        destination=f"Rejected/{document_id}/candidate-001.pdf",
        source_revision=1,
        expected_sha256="a" * 64,
        expected_size=128,
        decision_revision=2,
        intent_revision=1,
        location_version=0,
    )


def make_db_record(op_id: str, state: str) -> FileOperationRecord:
    return FileOperationRecord(
        id=op_id,
        batch_id="batch_x",
        document_id="doc_1",
        sequence=0,
        kind=PendingIntent.MOVE_REJECTED,
        source_rel_path="candidate-001.pdf",
        destination_rel_path="Rejected/doc_1/candidate-001.pdf",
        expected_sha256="a" * 64,
        expected_size=128,
        source_revision=1,
        decision_revision=2,
        intent_revision=1,
        location_version=0,
        state=OperationState(state),
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_journal_path_layout(root: Path) -> None:
    assert journal_dir(root) == root / ".review" / "journals"
    assert journal_path(root, "batch_abc").name == "batch_abc.json"
    assert journal_path(root, "batch_abc").parent == root / ".review" / "journals"


def test_journal_path_refuses_an_unsafe_batch_id(root: Path) -> None:
    for bad in ("../escape", "a/b", "..", "", "a\\b"):
        with pytest.raises(Conflict) as excinfo:
            journal_path(root, bad)
        assert excinfo.value.code == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Intent before the filesystem
# ---------------------------------------------------------------------------
def test_intent_is_durable_before_any_move_step(root: Path) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_order_test")
    journal.initialize([operation])

    observed: dict[str, str] = {}

    def executor_move_step() -> None:
        # A compliant executor reads the journal immediately before it touches a
        # file; the intent must already be on disk at that point.
        snapshot = load_journal(journal.path)
        observed["state_before_move"] = snapshot.states()[operation.operation_id]
        journal.record_file_moved(operation.operation_id)

    # Out of order: no intent recorded yet, so the move is refused.
    with pytest.raises(JournalOrderError) as excinfo:
        journal.record_file_moved(operation.operation_id)
    assert excinfo.value.code == Code.NEEDS_RECONCILIATION

    journal.record_intent(operation.operation_id)
    executor_move_step()
    journal.record_committed(operation.operation_id)

    assert observed["state_before_move"] == OperationState.INTENT_RECORDED.value
    on_disk = load_journal(journal.path)
    assert on_disk.states()[operation.operation_id] == OperationState.COMMITTED.value


def test_out_of_order_and_backwards_transitions_are_refused(root: Path) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_refuse")
    journal.initialize([operation])

    with pytest.raises(JournalOrderError):
        journal.record_committed(operation.operation_id)  # file_moved first
    with pytest.raises(JournalOrderError):
        journal.record_file_moved(operation.operation_id)  # intent first

    journal.record_intent(operation.operation_id)
    journal.record_file_moved(operation.operation_id)
    journal.record_committed(operation.operation_id)
    with pytest.raises(JournalOrderError):
        journal.record_intent(operation.operation_id)  # backwards from committed


def test_side_states_are_reachable_and_recovery_may_resume(root: Path) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_side")
    journal.initialize([operation])

    journal.record_needs_reconciliation(operation.operation_id, "both source and destination exist")
    assert journal.states()[operation.operation_id] == OperationState.NEEDS_RECONCILIATION.value

    # Recovery confirms a move happened and resumes the ordered progression.
    journal.record_file_moved(operation.operation_id, "destination identity verified")
    journal.record_committed(operation.operation_id)
    assert journal.states()[operation.operation_id] == OperationState.COMMITTED.value


def test_same_state_transition_is_idempotent(root: Path) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_idem")
    journal.initialize([operation])
    journal.record_intent(operation.operation_id)
    journal.record_intent(operation.operation_id)

    record = journal.get(operation.operation_id)
    assert record is not None
    assert record.state == OperationState.INTENT_RECORDED.value
    assert [h["state"] for h in record.history].count(OperationState.INTENT_RECORDED.value) == 1


def test_unknown_operation_is_not_found(root: Path) -> None:
    from resume_review.errors import NotFound

    journal = Journal(root, "batch_unknown")
    journal.initialize([make_operation()])
    with pytest.raises(NotFound):
        journal.record_intent("op_missing")


# ---------------------------------------------------------------------------
# Atomic and durable writes
# ---------------------------------------------------------------------------
def test_write_is_atomic_temp_fsync_then_replace(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def spy_replace(src: Any, dst: Any) -> None:
        calls.append("replace")
        real_replace(src, dst)

    def spy_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "fsync", spy_fsync)

    operation = make_operation()
    journal = Journal(root, "batch_atomic")
    journal.initialize([operation])

    assert calls and calls[0] == "fsync", "the temp file must be fsynced first"
    assert calls.index("fsync") < calls.index("replace"), "fsync must precede the replace"
    assert calls.count("replace") == 1

    # No leftover temp file after a successful replace.
    siblings = list(journal_dir(root).iterdir())
    assert siblings == [journal.path]
    # The final file is complete, parseable JSON.
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["batch_id"] == "batch_atomic"
    assert list(payload["operations"]) == [operation.operation_id]


def test_failed_replace_leaves_the_previous_journal_intact(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_crash")
    journal.initialize([operation])
    journal.record_intent(operation.operation_id)

    before = journal.path.read_bytes()

    def exploding_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated crash between temp write and replace")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError):
        journal.record_file_moved(operation.operation_id)

    after = journal.path.read_bytes()
    assert after == before, "a crash must never leave a half-written journal"
    # The temporary sibling is left in place; the journal module never deletes.
    assert any(name.startswith("batch_crash.json.tmp-") for name in os.listdir(journal_dir(root)))

    monkeypatch.undo()
    assert load_journal(journal.path).states()[operation.operation_id] == "intent_recorded"


# ---------------------------------------------------------------------------
# Loading after a restart
# ---------------------------------------------------------------------------
def test_journal_is_loadable_after_a_restart(root: Path) -> None:
    operation = make_operation()
    journal = Journal(root, "batch_restart")
    journal.initialize([operation], instance_id="inst_test", plan_hash="deadbeef")
    journal.record_intent(operation.operation_id)
    journal.record_file_moved(operation.operation_id)

    # A fresh object, as a new process would construct it, reconstructs state.
    reloaded = Journal.load(root, "batch_restart")
    record = reloaded.get(operation.operation_id)
    assert record is not None
    assert record.state == OperationState.FILE_MOVED.value
    snapshot = load_journal(journal.path)
    assert snapshot.instance_id == "inst_test"
    assert snapshot.plan_hash == "deadbeef"
    assert snapshot.by_id()[operation.operation_id].document_id == "doc_1"


def test_corrupt_journal_is_reported_not_raised(root: Path) -> None:
    path = journal_path(root, "batch_corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Truncated JSON.
    path.write_text('{"batch_id": "batch_corrupt", "operations": {"op_1": {"state": "int', encoding="utf-8")
    snapshot = load_journal(path)
    assert snapshot.exists is True
    assert snapshot.corrupt is True
    assert snapshot.needs_reconciliation is True
    assert snapshot.detail

    # Valid JSON but missing the operation map.
    path.write_text('{"batch_id": "batch_corrupt"}', encoding="utf-8")
    missing_map = load_journal(path)
    assert missing_map.corrupt is True
    assert missing_map.needs_reconciliation is True

    # Absent file is not corruption.
    absent = load_journal(journal_path(root, "batch_absent"))
    assert absent.exists is False
    assert absent.corrupt is False
    assert absent.needs_reconciliation is False

    # A journal object refuses to operate on a corrupt file rather than guessing.
    with pytest.raises(JournalOrderError):
        Journal.load(root, "batch_corrupt")


# ---------------------------------------------------------------------------
# Reconciliation: the database is authoritative
# ---------------------------------------------------------------------------
def test_reconcile_reports_disagreement_with_database_as_authoritative(root: Path) -> None:
    operation = make_operation("op_1")
    journal = Journal(root, "batch_reconcile")
    journal.initialize([operation])
    journal.record_intent("op_1")

    database_records = [
        make_db_record("op_1", OperationState.PLANNED.value),  # disagrees with journal
        make_db_record("op_2", OperationState.FILE_MOVED.value),  # only in the database
    ]

    report = reconcile_with_database(load_journal(journal.path), database_records)

    assert report.needs_reconciliation is True
    by_id = {item.operation_id: item for item in report.items}
    assert by_id["op_1"].journal_state == OperationState.INTENT_RECORDED.value
    assert by_id["op_1"].authoritative_state == OperationState.PLANNED.value
    assert by_id["op_1"].needs_reconciliation is True
    assert by_id["op_2"].journal_state is None
    assert by_id["op_2"].authoritative_state == OperationState.FILE_MOVED.value


def test_reconcile_flags_a_journal_operation_absent_from_the_database(root: Path) -> None:
    operation = make_operation("op_1")
    journal = Journal(root, "batch_absent_db")
    journal.initialize([operation])

    report = reconcile_with_database(load_journal(journal.path), [])

    assert report.needs_reconciliation is True
    assert report.items[0].operation_id == "op_1"
    assert report.items[0].authoritative_state is None
    assert "never committed" in report.items[0].reason


def test_reconcile_agreement_needs_no_reconciliation(root: Path) -> None:
    operation = make_operation("op_1")
    journal = Journal(root, "batch_agree")
    journal.initialize([operation])
    journal.record_intent("op_1")

    report = reconcile_with_database(
        load_journal(journal.path), [make_db_record("op_1", OperationState.INTENT_RECORDED.value)]
    )

    assert report.needs_reconciliation is False
    assert report.items[0].needs_reconciliation is False


def test_reconcile_with_repo_uses_the_durable_operation_rows(
    repo: Repository, root: Path
) -> None:
    document = repo.create_document(
        original_filename="candidate-001.pdf",
        rel_path="candidate-001.pdf",
        media_type="pdf",
        size_bytes=128,
        content_sha256="a" * 64,
        fs_identity=None,
    )
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document.id,
        kind=PendingIntent.MOVE_REJECTED,
        source="candidate-001.pdf",
        destination=f"Rejected/{document.id}/candidate-001.pdf",
        source_revision=0,
        expected_sha256="a" * 64,
        expected_size=128,
        decision_revision=0,
        intent_revision=0,
        location_version=0,
    )
    plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.db.instance_id,
        batch_id=new_id("batch"),
        criteria_version=1,
        operations=[operation],
    )
    repo.create_batch(plan, created_by="reviewer")
    repo.create_file_operations(plan.batch_id, plan.operations)

    journal = Journal(root, plan.batch_id)
    journal.initialize(plan.operations)
    journal.record_intent(operation.operation_id)

    # The database still says 'planned'; the journal claims the intent is recorded.
    report = reconcile_with_repo(load_journal(journal.path), repo)
    assert report.needs_reconciliation is True
    item = report.items[0]
    assert item.authoritative_state == OperationState.PLANNED.value
    assert item.journal_state == OperationState.INTENT_RECORDED.value
