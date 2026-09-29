"""Crash-reconciliation tests for :mod:`resume_review.actions.recovery`.

Authority: PRD sections 13.2 and 13.3. The five rows of the 13.3 table are
normative, so each has its own test here. The two properties that matter most and
are easiest to get lazily wrong are also asserted directly:

* the both-exist case must leave both files present and byte-identical, and the
  module must contain no deletion path at all;
* a destination that merely has matching content -- a copy placed by another
  actor -- must not be accepted as proof of a completed move.

All data is synthetic. No move is ever performed by the code under test: recovery
reads the filesystem and, when explicitly asked, writes journal/database state.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions.recovery import (
    Condition,
    Recovery,
    classify_operation,
    plan_recovery,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import NotFound
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


def _make_document(repo: Repository, rel_path: str, *, fs_identity: str | None):
    return repo.create_document(
        original_filename=Path(rel_path).name,
        rel_path=rel_path,
        media_type=MediaType.PDF,
        size_bytes=None,
        content_sha256=None,
        fs_identity=fs_identity,
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
) -> tuple[str, object]:
    """Create a batch + journal row and return (batch_id, operation record)."""
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
    record = repo.list_file_operations(plan.batch_id)[0]
    return plan.batch_id, record


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every regular file under the workspace, keyed by relative path."""
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ---------------------------------------------------------------------------
# Row 1: source exists as expected; destination absent
# ---------------------------------------------------------------------------
def test_source_present_destination_absent_is_resumable(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    digest = _write(root / source_rel, b"resume bytes v1")
    size = (root / source_rel).stat().st_size
    document = _make_document(repo, source_rel, fs_identity=file_identity(root / source_rel).digest_hint())
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=f"{REJECTED_DIR}/{document.id}/{source_rel}",
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.SOURCE_PRESENT_DESTINATION_ABSENT
    assert diagnosis.recovery == Recovery.RESUME
    assert diagnosis.safe_to_proceed_without_human is True
    assert diagnosis.requires_policy_recheck is True
    assert diagnosis.resumable is True
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["source"]["exists"] is True
    assert diagnosis.evidence["destination"]["exists"] is False


def test_source_present_but_content_changed_blocks(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    _write(root / source_rel, b"resume bytes v1")
    _write(root / source_rel, b"tampered bytes v2")
    document = _make_document(repo, source_rel, fs_identity=None)
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=f"{REJECTED_DIR}/{document.id}/{source_rel}",
        expected_sha256="0" * 64,  # does not match the current file
        expected_size=len(b"tampered bytes v2"),
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.evidence["content_matches"] is False
    # The source is left exactly where it was.
    assert (root / source_rel).read_bytes() == b"tampered bytes v2"


# ---------------------------------------------------------------------------
# Row 2: source absent; operation-owned destination verified
# ---------------------------------------------------------------------------
def test_source_absent_verified_destination_commits(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"resume bytes, moved by a rename"
    destination_rel = f"{REJECTED_DIR}/{{doc}}/{source_rel}"
    # Create the document first so its ID can appear in the namespace.
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = destination_rel.format(doc=document.id)
    digest = _write(root / destination_rel, payload)
    size = (root / destination_rel).stat().st_size
    # A real rename preserves the source's identity; record the destination's
    # identity as the document's recorded identity, which is what discovery does.
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.SOURCE_ABSENT_DESTINATION_VERIFIED
    assert diagnosis.recovery == Recovery.COMMIT
    assert diagnosis.safe_to_proceed_without_human is True
    assert diagnosis.committable is True
    assert diagnosis.evidence["namespace_owned"] is True
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is True


def _set_identity(repo: Repository, document_id: str, identity: str) -> None:
    """Record a file identity on a document, as discovery does.

    The frozen repository exposes no setter for ``fs_identity``; a direct update
    through the audited write path is the least invasive way to set up the
    scenario without touching a frozen file.
    """
    with repo.db.write(
        actor="helper",
        actor_kind="helper",
        event="document.identity",
        entity_type="document",
        entity_id=document_id,
    ) as conn:
        conn.execute("UPDATE documents SET fs_identity = ? WHERE id = ?", (identity, document_id))


def test_repair_commits_verified_move_and_bumps_revision(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, b"moved bytes")
    size = (root / destination_rel).stat().st_size
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
        location_version=0,
    )

    revision_before = repo.db.state_revision()
    audit_before = repo.db.scalar("SELECT COUNT(*) FROM audit_events")
    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)

    assert plan.mutating is True
    assert plan.applied == 1
    assert plan.counts[Recovery.COMMIT] == 1

    refreshed_op = repo.list_file_operations(batch_id)[0]
    assert refreshed_op.state is OperationState.COMMITTED
    assert refreshed_op.observed_source_state == "absent"

    document_after = repo.get_document(document.id)
    assert document_after is not None
    assert document_after.location is Location.REJECTED
    assert document_after.current_rel_path == destination_rel
    assert document_after.location_version == 1

    # Audited and revision-bumped: the mutation went through the Repository.
    assert repo.db.state_revision() > revision_before
    assert repo.db.scalar("SELECT COUNT(*) FROM audit_events") > audit_before
    # The file itself was untouched by reconciliation.
    assert (root / destination_rel).read_bytes() == b"moved bytes"


def test_replaying_does_not_repeat_committed_operations(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, b"moved bytes")
    size = (root / destination_rel).stat().st_size
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    batch_id, _ = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    first = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert first.applied == 1
    revision_after_first = repo.db.state_revision()

    second = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    # The committed operation is excluded from the active set: nothing repeats.
    assert second.diagnoses == []
    assert second.applied == 0
    assert repo.db.state_revision() == revision_after_first


# ---------------------------------------------------------------------------
# Row 3: both exist -- the destructive-if-lazy row
# ---------------------------------------------------------------------------
def test_both_exist_stops_for_reconciliation_and_deletes_nothing(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"identical resume bytes at both names"
    digest = _write(root / source_rel, payload)
    size = (root / source_rel).stat().st_size
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    _write(root / destination_rel, payload)  # an identical copy, deliberately
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.BOTH_PRESENT
    assert diagnosis.recovery == Recovery.STOP_FOR_RECONCILIATION
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.requires_human_investigation is True

    # Now actually run the repair path over the both-exist case.
    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 1

    # The decisive assertion: neither file was deleted or altered.
    assert (root / source_rel).exists()
    assert (root / destination_rel).exists()
    assert (root / source_rel).read_bytes() == payload
    assert (root / destination_rel).read_bytes() == payload
    assert (root / source_rel).read_bytes() == (root / destination_rel).read_bytes()

    refreshed = repo.list_file_operations(batch_id)[0]
    assert refreshed.state is OperationState.NEEDS_RECONCILIATION
    document_after = repo.get_document(document.id)
    assert document_after is not None
    assert document_after.location is Location.CONFLICT


def test_recovery_module_contains_no_deletion_path() -> None:
    """Static proof: the module never removes, unlinks, renames, or moves a file.

    Parsed as AST rather than grepped, so the guard list in ``recovery.py`` itself
    is not mistaken for a call.
    """
    import ast

    source = Path(__file__).resolve().parents[2] / "src" / "resume_review" / "actions" / "recovery.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

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
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name in forbidden or name.endswith(".unlink") or name.endswith(".rmdir"):
                called.append(name)

    assert called == [], f"recovery.py must not contain a deletion or move path: {called}"
    # Sanity: the check would catch a call if one were added.
    probe = ast.parse("import os\nos.remove('x')\n")
    probe_calls = [
        dotted(n.func) for n in ast.walk(probe) if isinstance(n, ast.Call) and dotted(n.func) in forbidden
    ]
    assert probe_calls == ["os.remove"]


# ---------------------------------------------------------------------------
# Row 5: content, path ownership, or identity differs
# ---------------------------------------------------------------------------
def test_destination_copied_by_another_actor_is_not_accepted(repo: Repository, root: Path) -> None:
    """A matching-bytes copy must not be mistaken for a completed move.

    An unrelated actor copies an identical-bytes file to the recorded destination
    path. The content and path are right but the file identity is not the recorded
    source, so recovery must block and surface both what it could and could not
    establish rather than silently commit.
    """
    source_rel = "candidate-001.pdf"
    payload = b"exactly the bytes the record expects"
    # The operation's source revision existed and was recorded with this identity.
    original = root / "incoming" / source_rel
    digest = _write(original, payload)
    original_identity = file_identity(original).digest_hint()
    size = original.stat().st_size

    document = _make_document(repo, source_rel, fs_identity=original_identity)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"

    # Another actor copies the same bytes to the recorded destination, then the
    # original source disappears. The destination is a *different* file.
    _write(root / destination_rel, payload)
    assert file_identity(root / destination_rel).digest_hint() != original_identity
    original.unlink()  # the actor's copy operation, not recovery, removed the original

    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    # What could be established ...
    assert diagnosis.evidence["namespace_owned"] is True
    assert diagnosis.evidence["content_matches"] is True
    # ... and what could not: the identity does not match the recorded source.
    assert diagnosis.evidence["identity_matches"] is False
    assert diagnosis.evidence["recorded_identity"] == original_identity
    # The copy is preserved for inspection.
    assert (root / destination_rel).read_bytes() == payload


def test_destination_content_differs_blocks(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    _write(root / destination_rel, b"wrong bytes")
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
    assert diagnosis.evidence["content_matches"] is False
    assert (root / destination_rel).read_bytes() == b"wrong bytes"


def test_destination_outside_document_namespace_blocks(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    # A destination that is not under this document's own namespace.
    destination_rel = f"{REJECTED_DIR}/someone-elses-document/{source_rel}"
    digest = _write(root / destination_rel, b"bytes")
    size = (root / destination_rel).stat().st_size
    _set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.IDENTITY_OR_CONTENT_DIFFERS
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.evidence["namespace_owned"] is False
    assert diagnosis.evidence["reason"] == "destination_path_not_operation_owned"


def test_unverifiable_destination_identity_blocks_and_surfaces_limits(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)  # no recorded identity
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    digest = _write(root / destination_rel, b"content matches exactly")
    size = (root / destination_rel).stat().st_size
    _, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)

    assert diagnosis.condition == Condition.IDENTITY_UNVERIFIED
    assert diagnosis.recovery == Recovery.BLOCK_PRESERVE_EVIDENCE
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.evidence["content_matches"] is True
    assert diagnosis.evidence["identity_matches"] is None
    established = diagnosis.evidence["established"]
    not_established = diagnosis.evidence["not_established"]
    assert any("namespace" in item for item in established)
    assert any("identity" in item for item in not_established)


# ---------------------------------------------------------------------------
# Row 4: neither exists
# ---------------------------------------------------------------------------
def test_neither_exists_marks_missing_and_requests_human(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256="a" * 64,
        expected_size=10,
    )

    diagnosis = classify_operation(repo, operation=operation, root=root)
    assert diagnosis.condition == Condition.NEITHER_PRESENT
    assert diagnosis.recovery == Recovery.MARK_MISSING
    assert diagnosis.safe_to_proceed_without_human is False
    assert diagnosis.requires_human_investigation is True

    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=False)
    assert plan.applied == 1
    document_after = repo.get_document(document.id)
    assert document_after is not None
    assert document_after.location is Location.MISSING
    refreshed = repo.list_file_operations(batch_id)[0]
    assert refreshed.state is OperationState.NEEDS_RECONCILIATION
    assert refreshed.error_code == "SOURCE_MISSING"


# ---------------------------------------------------------------------------
# Dry run mutates nothing
# ---------------------------------------------------------------------------
def test_dry_run_mutates_nothing(repo: Repository, root: Path) -> None:
    # A mix of conditions so the dry run exercises several branches.
    source_rel = "candidate-001.pdf"
    payload = b"kept bytes"
    digest = _write(root / source_rel, payload)
    size = len(payload)
    document = _make_document(repo, source_rel, fs_identity=None)
    destination_rel = f"{REJECTED_DIR}/{document.id}/{source_rel}"
    _write(root / destination_rel, payload)  # both exist
    batch_id, operation = _make_operation(
        repo,
        document_id=document.id,
        source=source_rel,
        destination=destination_rel,
        expected_sha256=digest,
        expected_size=size,
    )

    revision_before = repo.db.state_revision()
    audit_before = repo.db.scalar("SELECT COUNT(*) FROM audit_events")
    operations_before = repo.db.query("SELECT * FROM file_operations ORDER BY id")
    documents_before = repo.db.query("SELECT * FROM documents ORDER BY id")
    files_before = _snapshot(root)

    plan = plan_recovery(repo, root=root, batch_id=batch_id, dry_run=True)

    assert plan.dry_run is True
    assert plan.mutating is False
    assert plan.applied == 0
    assert all(action.applied is False for action in plan.actions)
    assert plan.counts[Recovery.STOP_FOR_RECONCILIATION] == 1

    # Journal, database, and filesystem are all byte-for-byte unchanged.
    assert repo.db.state_revision() == revision_before
    assert repo.db.scalar("SELECT COUNT(*) FROM audit_events") == audit_before
    assert [dict(r) for r in repo.db.query("SELECT * FROM file_operations ORDER BY id")] == [
        dict(r) for r in operations_before
    ]
    assert [dict(r) for r in repo.db.query("SELECT * FROM documents ORDER BY id")] == [
        dict(r) for r in documents_before
    ]
    assert _snapshot(root) == files_before
    # The operation stays exactly where it was.
    assert repo.list_file_operations(batch_id)[0].state is operation.state


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def test_batch_scope_limits_diagnoses(repo: Repository, root: Path) -> None:
    source_rel = "candidate-001.pdf"
    payload = b"bytes"
    digest = _write(root / source_rel, payload)
    size = len(payload)
    doc_a = _make_document(repo, source_rel, fs_identity=None)
    doc_b = _make_document(repo, "candidate-002.pdf", fs_identity=None)
    batch_a, _ = _make_operation(
        repo,
        document_id=doc_a.id,
        source=source_rel,
        destination=f"{REJECTED_DIR}/{doc_a.id}/{source_rel}",
        expected_sha256=digest,
        expected_size=size,
    )
    _make_operation(
        repo,
        document_id=doc_b.id,
        source="candidate-002.pdf",
        destination=f"{REJECTED_DIR}/{doc_b.id}/candidate-002.pdf",
        expected_sha256=digest,
        expected_size=size,
    )

    scoped = plan_recovery(repo, root=root, batch_id=batch_a, dry_run=True)
    assert len(scoped.diagnoses) == 1
    assert scoped.diagnoses[0].document_id == doc_a.id

    everything = plan_recovery(repo, root=root, dry_run=True)
    assert len(everything.diagnoses) == 2


def test_unknown_batch_raises_not_found(repo: Repository, root: Path) -> None:
    with pytest.raises(NotFound):
        plan_recovery(repo, root=root, batch_id="batch_does_not_exist", dry_run=True)
