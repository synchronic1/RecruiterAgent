"""Executor tests: approval gating, revalidation, and no-clobber moves.

Authority: PRD sections 13.1, 13.2, 13.3 and 10.1; AGENTS.md constraints 3, 4, 5.

Every test uses synthetic data and a real temporary workspace. The properties
asserted here are the ones the PRD calls out as easy to get lazily wrong:

* a command line cannot manufacture approval -- a batch whose documents are all
  marked Reject but which has no approval record refuses and moves nothing;
* an expired approval, a changed plan hash, and a viewer principal all refuse;
* if the whole-plan revalidation fails, nothing moves (asserted against a tree
  snapshot taken immediately before the call);
* a replayed apply repeats nothing;
* a crash after the move but before the commit is reconciled, not re-moved;
* a cross-volume or unsupported move is never degraded into a copy.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import plan_actions
from resume_review.actions.executor import (
    OpOutcome,
    apply_batch,
)
from resume_review.db import Repository, RevisionConflict
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, Conflict, Forbidden, InvalidInput
from resume_review.models import (
    REJECTED_DIR,
    ExecutionState,
    Location,
    MediaType,
    OperationState,
    Principal,
    Role,
)
from resume_review.storage.no_clobber import (
    FileIdentity,
    MoveOutcome,
    MoveResult,
    file_identity,
)
from resume_review.util import now_iso, seconds_from_now_iso, sha256_file

DATA = b"%PDF-1.4\nsynthetic test resume bytes\n"


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
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "Job - Operations Manager"
    workspace.mkdir(parents=True)
    return workspace


def reviewer(repo: Repository, role: Role = Role.REVIEWER) -> Principal:
    return Principal(
        actor_ref="reviewer@example.test",
        role=role,
        session_id="sess_test",
        instance_id=repo.instance_id,
    )


def place_file(root: Path, rel_path: str, data: bytes = DATA) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree_snapshot(root: Path, *, include_review: bool = False) -> dict[str, bytes]:
    """Every regular file under the workspace, keyed by relative path.

    The ``.review`` directory holds the durable operation journal, which the
    executor is *expected* to write; "the tree is unchanged" is a statement about
    the managed documents, so the journal is excluded unless asked for.
    """
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not include_review and rel.parts and rel.parts[0] == ".review":
            continue
        if path.is_file():
            out[rel.as_posix()] = path.read_bytes()
    return out


def make_reject_plan(
    repo: Repository,
    root: Path,
    *,
    name: str = "candidate-001.pdf",
    rel_path: str | None = None,
    intent: str | None = None,
):
    """Place a file, register it, mark it Reject, and build a real plan."""
    rel = rel_path or name
    place_file(root, rel)
    document = repo.create_document(
        original_filename=name,
        rel_path=rel,
        media_type=MediaType.PDF,
        size_bytes=len(DATA),
        content_sha256=sha256_file(root / rel),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor="reviewer@example.test")
    if intent is not None:
        repo.set_intent(
            document.id, intent, requester="reviewer@example.test", expected_revision=0
        )
    plan = plan_actions(
        repo,
        document_ids=[document.id],
        requested_by="reviewer@example.test",
        criteria_version=0,
        root=root,
    )
    assert plan.operations, "the fixture expected a concrete operation"
    return document, plan


def persist_batch(repo: Repository, plan) -> None:
    repo.create_batch(plan, created_by="reviewer@example.test")
    repo.create_file_operations(plan.batch_id, plan.operations)


def approve(repo: Repository, plan, *, expires_at: str | None = None) -> None:
    repo.approve_batch(
        plan.batch_id,
        actor="reviewer@example.test",
        plan_hash=plan.plan_hash,
        expires_at=expires_at or seconds_from_now_iso(900),
    )


def set_identity(repo: Repository, document_id: str, identity: str) -> None:
    """Record a file identity on a document, as discovery does.

    The frozen repository exposes no setter for ``fs_identity``; a direct update
    through the audited write path is the least invasive way to set up the crash
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


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_apply_moves_a_rejected_file_and_commits_the_location(
    repo: Repository, root: Path
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    outcome = apply_batch(
        repo,
        batch_id=plan.batch_id,
        actor=reviewer(repo),
        root=root,
        now=now_iso(),
    )

    destination = root / REJECTED_DIR / document.id / "candidate-001.pdf"
    assert outcome.ok is True
    assert outcome.state == ExecutionState.COMPLETED.value
    assert outcome.counts["moved"] == 1
    assert outcome.remaining == 0
    assert destination.is_file()
    assert destination.read_bytes() == DATA
    assert not (root / "candidate-001.pdf").exists()

    moved = outcome.operations[0]
    assert moved.outcome == OpOutcome.MOVED

    refreshed = repo.get_document(document.id)
    assert refreshed.location == Location.REJECTED
    assert refreshed.current_rel_path == f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    assert refreshed.location_version == 1

    rows = repo.list_file_operations(plan.batch_id)
    assert [r.state for r in rows] == [OperationState.COMMITTED]
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.COMPLETED.value


# ---------------------------------------------------------------------------
# Approval is not inferred
# ---------------------------------------------------------------------------
def test_all_reject_documents_without_approval_move_nothing(
    repo: Repository, root: Path
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    # Deliberately no approve() call: every document is Reject, but no human
    # approved this plan. Sorting, filtering, or a Reject decision is not approval.
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(
            repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
        )

    assert excinfo.value.code == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == before
    assert repo.get_document(document.id).location == Location.ACTIVE
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.PLANNED.value
    assert [r.state for r in repo.list_file_operations(plan.batch_id)] == [
        OperationState.PLANNED
    ]


def test_expired_approval_moves_nothing(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan, expires_at=seconds_from_now_iso(-1))
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(
            repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
        )

    assert excinfo.value.code == Code.APPROVAL_EXPIRED
    assert tree_snapshot(root) == before
    assert repo.get_document(document.id).location == Location.ACTIVE


def test_plan_hash_mismatch_is_refused(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    # Tamper with the stored plan after approval: the plan being executed is no
    # longer the plan the approval was bound to.
    with repo.db.write(
        actor="attacker",
        event="test.tamper",
    ) as conn:
        conn.execute(
            "UPDATE action_batches SET plan_json = replace(plan_json, ?, ?) WHERE id = ?",
            ("candidate-001.pdf", "candidate-999.pdf", plan.batch_id),
        )
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(
            repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
        )

    assert excinfo.value.code == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == before


def test_a_viewer_principal_is_refused_by_role(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    before = tree_snapshot(root)

    with pytest.raises(Forbidden) as excinfo:
        apply_batch(
            repo,
            batch_id=plan.batch_id,
            actor=reviewer(repo, Role.VIEWER),
            root=root,
            now=now_iso(),
        )

    assert excinfo.value.code == Code.ROLE_INSUFFICIENT
    assert tree_snapshot(root) == before
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.APPROVED.value


def test_a_plain_actor_string_is_not_accepted(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    with pytest.raises(InvalidInput) as excinfo:
        apply_batch(
            repo,
            batch_id=plan.batch_id,
            actor="reviewer@example.test",  # type: ignore[arg-type]
            root=root,
            now=now_iso(),
        )

    assert excinfo.value.code == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Revalidation
# ---------------------------------------------------------------------------
def test_a_stale_source_blocks_the_whole_plan_and_moves_nothing(
    repo: Repository, root: Path
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    # The source changes on disk after the plan was built.
    place_file(root, "candidate-001.pdf", b"%PDF-1.4\nDIFFERENT bytes\n")
    before = tree_snapshot(root)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.ok is False
    assert outcome.counts["moved"] == 0
    assert tree_snapshot(root) == before
    assert not (root / REJECTED_DIR / document.id / "candidate-001.pdf").exists()
    assert repo.get_document(document.id).location == Location.ACTIVE


def test_a_decision_revision_change_blocks(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    # A human changes their mind after approval.
    repo.set_decision(document.id, "hold", expected_revision=1, actor="reviewer@example.test")
    before = tree_snapshot(root)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert tree_snapshot(root) == before
    assert repo.get_document(document.id).location == Location.ACTIVE


def test_a_changed_criteria_version_blocks(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    # A criteria version activates after the plan was built. A proposal needs a job
    # requisition first (the frozen repository enforces that ordering).
    repo.create_or_update_job("Operations Manager", "A synthetic job description.")
    repo.create_criteria_proposal("crit_1", "Must have written communication evidence")
    repo.activate_criteria_version(1, actor="reviewer@example.test")
    before = tree_snapshot(root)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert tree_snapshot(root) == before


def test_a_missing_source_at_execution_time_blocks(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    (root / "candidate-001.pdf").unlink()
    before = tree_snapshot(root)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.operations[0].outcome == OpOutcome.BLOCKED
    assert tree_snapshot(root) == before
    # A reconciliation task exists so a human can investigate.
    tasks = repo.list_tasks(document_id=document.id, state="open")
    assert any(t.task_type == "reconciliation" for t in tasks)


# ---------------------------------------------------------------------------
# Replay and crash reconciliation
# ---------------------------------------------------------------------------
def test_replaying_an_apply_repeats_nothing(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    first = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )
    assert first.ok is True
    after_first = tree_snapshot(root)
    revision_after_first = repo.get_batch(plan.batch_id)["execution_revision"]

    second = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert second.ok is True
    assert second.state == ExecutionState.COMPLETED.value
    assert second.counts["moved"] == 0
    assert second.counts["already_completed"] == 1
    assert second.operations[0].outcome == OpOutcome.ALREADY_COMPLETED
    # Nothing on disk changed, and the batch revision did not move again.
    assert tree_snapshot(root) == after_first
    assert repo.get_batch(plan.batch_id)["execution_revision"] == revision_after_first


def test_a_crash_after_the_move_is_reconciled_not_re_moved(
    repo: Repository, root: Path
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    # Simulate: the executor recorded the intent and performed the move, then died
    # before committing. The file is already at the destination and the source is
    # gone.
    #
    # The move must be simulated with a real rename, not by writing a fresh file at
    # the destination. A rename preserves the inode, which is what makes the
    # destination provably the same physical file as the moved source; writing a new
    # file produces a different inode and models the PRD 13.3 case that must NOT be
    # reconciled -- a copy that appeared at the destination while the original
    # vanished. Simulating it the inaccurate way previously passed only because
    # recovery consulted the document's current identity, which is exactly the hole
    # the per-operation source identity closes.
    destination_rel = f"{REJECTED_DIR}/{document.id}/candidate-001.pdf"
    (root / destination_rel).parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / "candidate-001.pdf", root / destination_rel)
    set_identity(repo, document.id, file_identity(root / destination_rel).digest_hint())
    operation_id = repo.list_file_operations(plan.batch_id)[0].id
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash") as conn:
        conn.execute(
            "UPDATE file_operations SET state = 'file_moved' WHERE id = ?", (operation_id,)
        )
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash.batch") as conn:
        conn.execute(
            "UPDATE action_batches SET execution_state = 'applying' WHERE id = ?",
            (plan.batch_id,),
        )

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.ok is True
    assert outcome.state == ExecutionState.COMPLETED.value
    assert outcome.operations[0].outcome == OpOutcome.RECONCILED
    assert outcome.counts["moved"] == 0
    # The file was not moved a second time, and its bytes are unchanged.
    assert (root / destination_rel).read_bytes() == DATA
    assert not (root / "candidate-001.pdf").exists()
    assert repo.get_document(document.id).location == Location.REJECTED
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.COMMITTED


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_dry_run_moves_nothing_and_writes_nothing(repo: Repository, root: Path) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)
    before = tree_snapshot(root)
    batch_before = dict(repo.get_batch(plan.batch_id))
    revision_before = repo.db.state_revision()

    outcome = apply_batch(
        repo,
        batch_id=plan.batch_id,
        actor=reviewer(repo),
        root=root,
        now=now_iso(),
        dry_run=True,
    )

    assert outcome.dry_run is True
    assert outcome.counts["moved"] == 1  # what *would* happen
    assert outcome.state == ExecutionState.COMPLETED.value
    assert tree_snapshot(root) == before
    batch_after = repo.get_batch(plan.batch_id)
    assert batch_after["execution_state"] == batch_before["execution_state"]
    assert batch_after["execution_revision"] == batch_before["execution_revision"]
    assert repo.db.state_revision() == revision_before
    assert repo.get_document(document.id).location == Location.ACTIVE
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.PLANNED
    # The journal is not even created by a dry run.
    assert not (root / ".review" / "journals" / f"{plan.batch_id}.json").exists()


# ---------------------------------------------------------------------------
# No degradation
# ---------------------------------------------------------------------------
def test_a_cross_volume_move_blocks_and_does_not_degrade(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    import resume_review.actions.executor as executor_module

    def fake_move(*args, **kwargs) -> MoveResult:
        return MoveResult(
            outcome=MoveOutcome.CROSS_VOLUME,
            detail="Source and destination are on different volumes.",
            source_identity=FileIdentity(volume=1, inode=2, size=len(DATA), mtime_ns=3),
        )

    monkeypatch.setattr(executor_module, "atomic_no_clobber_move", fake_move)
    before = tree_snapshot(root)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.operations[0].error_code == Code.CROSS_VOLUME_MOVE
    # No copy-and-delete fallback ran: the source is still exactly where it was.
    assert tree_snapshot(root) == before
    assert (root / "candidate-001.pdf").is_file()


def test_an_unsupported_move_blocks(repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    import resume_review.actions.executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "atomic_no_clobber_move",
        lambda *a, **k: MoveResult(
            outcome=MoveOutcome.UNSUPPORTED,
            detail="This host provides no safe no-clobber move primitive.",
        ),
    )

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.operations[0].error_code == Code.NO_CLOBBER_UNSUPPORTED
    assert (root / "candidate-001.pdf").is_file()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_a_concurrent_writer_is_detected_not_overwritten(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, plan = make_reject_plan(repo, root)
    persist_batch(repo, plan)
    approve(repo, plan)

    real_list = repo.list_file_operations

    def racing_list(batch_id: str):
        rows = real_list(batch_id)
        # Another writer moves the batch forward between our read and our write.
        repo.set_batch_state(batch_id, ExecutionState.APPLYING.value, 0)
        return rows

    monkeypatch.setattr(repo, "list_file_operations", racing_list)

    with pytest.raises(RevisionConflict):
        apply_batch(
            repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
        )


# ---------------------------------------------------------------------------
# Structural safety
# ---------------------------------------------------------------------------
def test_the_executor_contains_no_overwriting_or_shell_move() -> None:
    """The module must offer no path that overwrites, copies, deletes, or shells out.

    A static check, because this is the property that must hold even on a branch no
    test exercises.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "resume_review"
        / "actions"
        / "executor.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    forbidden_calls = {
        "os.replace",
        "os.rename",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "shutil.move",
        "shutil.copy",
        "shutil.copy2",
        "shutil.rmtree",
    }
    shell_modules = {"subprocess", "shlex", "pty"}
    imported: set[str] = set()
    bare_calls: set[str] = set()
    attribute_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                bare_calls.add(func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                attribute_calls.add(f"{func.value.id}.{func.attr}")

    assert not (attribute_calls & forbidden_calls), attribute_calls & forbidden_calls
    assert not (imported & shell_modules), imported & shell_modules
    # The one move it performs is the no-clobber primitive, called directly.
    assert "atomic_no_clobber_move" in bare_calls
    assert not (bare_calls & {"move", "replace", "rename", "remove", "unlink", "rmtree"})
