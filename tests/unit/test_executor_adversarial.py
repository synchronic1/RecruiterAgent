"""Adversarial tests for the apply executor (PRD 13.1, 13.2, 13.3).

These are deliberately hostile. Where ``test_executor.py`` exercises the executor's
happy path and its own failure modes, this file tries to *break* the safety
properties by arranging the world so that a lazy implementation would lose or
corrupt a managed file:

1.  A pre-existing destination with different bytes must never be overwritten.
2.  A failing kernel move primitive must never degrade into copy-and-delete.
3.  State mutated between plan and apply must block, and never move a file that no
    longer conforms to the plan (including a source whose bytes changed).
4.  Approval cannot be manufactured by a missing record, a hash mismatch, a lapse,
    or a principal without the role.
5.  A collision on the second of three operations stops the batch after the first,
    leaves the third untouched, and never rolls the first back.
6.  A replay changes nothing; a crash after the move reconciles to "commit it".
7.  A crafted destination, a ``..`` component, and a link planted between planning
    and execution are all refused.
8.  The durable intent is on disk *before* the kernel move is even attempted.

Every test uses synthetic data, a real temporary workspace, and a real database.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.actions import plan_actions
from resume_review.actions.journal import Journal, journal_path, load_journal
from resume_review.actions.executor import (
    OpOutcome,
    _plan_from_batch,
    apply_batch,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, Conflict, Forbidden
from resume_review.models import (
    REJECTED_DIR,
    ExecutionState,
    Location,
    MediaType,
    OperationState,
    Principal,
    Role,
    jsonable,
)
from resume_review.storage import no_clobber
from resume_review.storage.no_clobber import MoveOutcome, file_identity
from resume_review.util import now_iso, seconds_from_now_iso, sha256_file

ACTOR = "reviewer@example.test"
DATA = b"%PDF-1.4\nadversarial synthetic resume\n"
ALT = b"%PDF-1.4\nDIFFERENT bytes written after planning\n"
FOREIGN = b"%PDF-1.4\nforeign file placed by another actor\n"


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
    repository.create_instance("inst_adv", __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "Job - Operations Manager"
    workspace.mkdir(parents=True)
    return workspace


def reviewer(repo: Repository, role: Role = Role.REVIEWER) -> Principal:
    return Principal(
        actor_ref=ACTOR,
        role=role,
        session_id="sess_adv",
        instance_id=repo.instance_id,
    )


def place_file(root: Path, rel_path: str, data: bytes = DATA) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Every managed file under the root; the ``.review`` journal is excluded.

    "The tree is unchanged" is a claim about managed documents, so the journal the
    executor is expected to write does not count. Directory layout is not included.
    """
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".review":
            continue
        if path.is_file() and not path.is_symlink():
            out[rel.as_posix()] = path.read_bytes()
    return out


def register_rejected(repo: Repository, root: Path, name: str, data: bytes = DATA):
    """Place bytes, register the document, and mark it Reject (active -> rejected)."""
    place_file(root, name, data)
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=len(data),
        content_sha256=sha256_file(root / name),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor=ACTOR)
    return document


def build_batch(repo: Repository, root: Path, names: list[str]):
    documents = [register_rejected(repo, root, name) for name in names]
    plan = plan_actions(
        repo,
        document_ids=[d.id for d in documents],
        requested_by=ACTOR,
        criteria_version=0,
        root=root,
    )
    assert len(plan.operations) == len(documents), "every document must yield an operation"
    repo.create_batch(plan, created_by=ACTOR)
    repo.create_file_operations(plan.batch_id, plan.operations)
    return documents, plan


def build_single(repo: Repository, root: Path, name: str = "candidate.pdf"):
    documents, plan = build_batch(repo, root, [name])
    return documents[0], plan


def approve(repo: Repository, plan, *, expires_at: str | None = None) -> None:
    repo.approve_batch(
        plan.batch_id,
        actor=ACTOR,
        plan_hash=plan.plan_hash,
        expires_at=expires_at or seconds_from_now_iso(900),
    )


def set_db_identity(repo: Repository, document_id: str, identity: str) -> None:
    with repo.db.write(actor="helper", actor_kind="helper", event="test.identity") as conn:
        conn.execute("UPDATE documents SET fs_identity = ? WHERE id = ?", (identity, document_id))


def make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink/junction. Raises if the host refuses."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise OSError((proc.stdout or "") + (proc.stderr or ""))
        else:
            os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create a directory link; reparse traversal unproven here")


def dest_of(document, name: str) -> str:
    return f"{REJECTED_DIR}/{document.id}/{name}"


# ===========================================================================
# 1. No overwrite, ever
# ===========================================================================
def test_preregistered_destination_is_never_overwritten(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    # Another actor already placed a file where the plan wants to move this one.
    destination = place_file(root, dest_of(document, "candidate.pdf"), FOREIGN)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    # The pre-existing file is byte-identical; the source survives.
    assert destination.read_bytes() == FOREIGN
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert repo.get_document(document.id).location == Location.ACTIVE
    assert [r.state for r in repo.list_file_operations(plan.batch_id)] == [
        OperationState.NEEDS_RECONCILIATION
    ]


def test_a_directory_at_the_destination_is_not_replaced(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    (root / REJECTED_DIR / document.id / "candidate.pdf").mkdir(parents=True)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert (root / REJECTED_DIR / document.id / "candidate.pdf").is_dir()
    assert (root / "candidate.pdf").read_bytes() == DATA


# ===========================================================================
# 2. No copy-and-delete fallback
# ===========================================================================
def test_a_raising_kernel_primitive_does_not_degrade_to_a_copy(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    before = tree_snapshot(root)

    real_primitive = no_clobber._platform_move

    def exploding_primitive(src: str, dst: str):
        raise OSError("simulated kernel move failure")

    monkeypatch.setattr(no_clobber, "_platform_move", exploding_primitive)

    with pytest.raises(OSError):
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    # Nothing was copied or deleted: the managed tree is exactly as it was.
    assert tree_snapshot(root) == before
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert not (root / dest_of(document, "candidate.pdf")).exists()
    # Intent was recorded durably before the failing attempt, so a resume is safe.
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.INTENT_RECORDED

    # With the primitive restored, the same batch resumes and completes (no copy path).
    monkeypatch.setattr(no_clobber, "_platform_move", real_primitive)
    resumed = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())
    assert resumed.ok is True
    assert resumed.counts["moved"] == 1
    assert (root / dest_of(document, "candidate.pdf")).read_bytes() == DATA
    assert not (root / "candidate.pdf").exists()


def test_a_failed_move_result_blocks_without_a_copy(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    before = tree_snapshot(root)

    monkeypatch.setattr(
        no_clobber, "_platform_move", lambda src, dst: (MoveOutcome.FAILED, 5)
    )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.operations[0].error_code == Code.INTERNAL_ERROR
    assert tree_snapshot(root) == before
    assert (root / "candidate.pdf").read_bytes() == DATA


def test_the_move_primitive_has_no_copy_or_read_write_move() -> None:
    """Static check: the no-clobber primitive and the executor never copy a file.

    An AST check rather than a substring check, so a docstring that merely mentions
    ``shutil.move`` (this module's own docs do) is not mistaken for a call.
    """
    import ast

    src_root = Path(__file__).resolve().parents[2] / "src" / "resume_review"
    sources = [
        src_root / "actions" / "executor.py",
        src_root / "storage" / "no_clobber.py",
    ]
    forbidden_attr_calls = {
        "shutil.move",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copyfileobj",
        "shutil.rmtree",
        "os.replace",
    }
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    calls.add(f"{node.func.value.id}.{node.func.attr}")
        assert "shutil" not in imported, f"{source.name} imports shutil"
        assert not (calls & forbidden_attr_calls), f"{source.name}: {calls & forbidden_attr_calls}"


# ===========================================================================
# 3. Revalidation is real
# ===========================================================================
def _blocked_without_moving(repo: Repository, root: Path, plan, document, outcome) -> None:
    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert not (root / dest_of(document, "candidate.pdf")).exists()
    assert repo.get_document(document.id).location == Location.ACTIVE


def _warnings_mention(outcome, needle: str) -> bool:
    return any(needle in w for w in outcome.warnings)


def test_flipped_decision_between_plan_and_apply_blocks(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    repo.set_decision(document.id, "hold", expected_revision=1, actor=ACTOR)
    before = tree_snapshot(root)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    _blocked_without_moving(repo, root, plan, document, outcome)
    # Non-vacuous: the block is attributable to the flipped decision.
    assert _warnings_mention(outcome, "decision changed"), outcome.warnings
    assert tree_snapshot(root) == before


def test_bumped_decision_revision_blocks(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    repo.set_decision(document.id, "reject", expected_revision=1, actor=ACTOR)  # rev 1 -> 2

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    _blocked_without_moving(repo, root, plan, document, outcome)
    assert _warnings_mention(outcome, "decision changed"), outcome.warnings


def test_bumped_location_version_blocks(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    repo.set_document_location(
        document.id, "candidate.pdf", Location.ACTIVE.value, expected_location_version=0
    )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    _blocked_without_moving(repo, root, plan, document, outcome)
    assert _warnings_mention(outcome, "location version"), outcome.warnings


def test_changed_criteria_version_blocks(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    repo.create_or_update_job("Operations Manager", "A synthetic job description.")
    repo.create_criteria_proposal("crit_adv", "Must have written communication evidence")
    repo.activate_criteria_version(1, actor=ACTOR)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    _blocked_without_moving(repo, root, plan, document, outcome)
    assert _warnings_mention(outcome, "criteria version"), outcome.warnings


def test_deleted_source_blocks(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    (root / "candidate.pdf").unlink()

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert not (root / dest_of(document, "candidate.pdf")).exists()
    assert _warnings_mention(outcome, "Neither the source nor the destination exists"), outcome.warnings
    tasks = repo.list_tasks(document_id=document.id, state="open")
    assert any(t.task_type == "reconciliation" for t in tasks)


def test_a_source_whose_bytes_changed_is_not_moved(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    # Same path, same DB revision, DIFFERENT content. Only a real content check
    # can catch this; a revision comparison cannot.
    place_file(root, "candidate.pdf", ALT)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert not (root / dest_of(document, "candidate.pdf")).exists()
    # The changed file stays exactly where it was; it is not moved to the planned path.
    assert (root / "candidate.pdf").read_bytes() == ALT
    assert repo.get_document(document.id).location == Location.ACTIVE
    # Non-vacuous: the block is attributable to the content mismatch, not a DB revision.
    assert _warnings_mention(outcome, "content no longer matches"), outcome.warnings


# ===========================================================================
# 4. Approval cannot be manufactured
# ===========================================================================
def test_no_approval_record_refuses_and_moves_nothing(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert excinfo.value.code == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == before
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.PLANNED.value
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.PLANNED


def test_plan_hash_mismatch_refuses(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    # The stored plan no longer matches the approved hash.
    with repo.db.write(actor="attacker", event="test.tamper") as conn:
        conn.execute(
            "UPDATE action_batches SET plan_json = replace(plan_json, ?, ?) WHERE id = ?",
            ("candidate.pdf", "candidate-999.pdf", plan.batch_id),
        )
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert excinfo.value.code == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == before


def test_expired_approval_refuses(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan, expires_at=seconds_from_now_iso(-1))
    before = tree_snapshot(root)

    with pytest.raises(Conflict) as excinfo:
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert excinfo.value.code == Code.APPROVAL_EXPIRED
    assert tree_snapshot(root) == before
    assert repo.get_document(document.id).location == Location.ACTIVE


def test_a_principal_without_the_role_cannot_execute(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
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
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.PLANNED


# ===========================================================================
# 5. Mid-batch conflict
# ===========================================================================
def test_collision_on_the_second_operation_stops_after_the_first(repo: Repository, root: Path) -> None:
    documents, plan = build_batch(repo, root, ["alpha.pdf", "bravo.pdf", "charlie.pdf"])
    approve(repo, plan)
    alpha, bravo, charlie = documents

    # Operation 2's destination is occupied before execution reaches it.
    place_file(root, dest_of(bravo, "bravo.pdf"), FOREIGN)
    before = tree_snapshot(root)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.PARTIAL.value
    assert outcome.code == Code.BATCH_PARTIAL
    assert outcome.remaining == 2
    assert outcome.counts["moved"] == 1
    assert outcome.counts["completed"] == 1

    # Operation 1 completed on disk and was NOT rolled back.
    assert not (root / "alpha.pdf").exists()
    assert (root / dest_of(alpha, "alpha.pdf")).read_bytes() == DATA
    assert repo.get_document(alpha.id).location == Location.REJECTED
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.COMMITTED

    # Operation 2 blocked; neither its source nor the foreign destination moved.
    assert (root / "bravo.pdf").read_bytes() == DATA
    assert (root / dest_of(bravo, "bravo.pdf")).read_bytes() == FOREIGN

    # Operation 3 was never reached; its source is untouched and no destination appeared.
    assert (root / "charlie.pdf").read_bytes() == DATA
    assert not (root / dest_of(charlie, "charlie.pdf")).exists()

    rows = {r.document_id: r.state for r in repo.list_file_operations(plan.batch_id)}
    assert rows[alpha.id] == OperationState.COMMITTED
    assert rows[bravo.id] == OperationState.NEEDS_RECONCILIATION
    assert rows[charlie.id] == OperationState.PLANNED, "an unstarted operation must not be rewritten"

    # The only net change is operation 1's completed move; nothing else was touched.
    after = tree_snapshot(root)
    assert set(after) == (set(before) - {"alpha.pdf"}) | {dest_of(alpha, "alpha.pdf")}
    for rel, payload in before.items():
        if rel == "alpha.pdf":
            continue
        assert after[rel] == payload


# ===========================================================================
# 6. Idempotent replay and crash reconciliation
# ===========================================================================
def test_apply_twice_is_a_no_op_the_second_time(repo: Repository, root: Path) -> None:
    documents, plan = build_batch(repo, root, ["one.pdf", "two.pdf"])
    approve(repo, plan)

    first = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())
    assert first.ok is True
    after_first = tree_snapshot(root)
    revision = repo.get_batch(plan.batch_id)["execution_revision"]

    second = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert second.ok is True
    assert second.counts["moved"] == 0
    assert second.counts["already_completed"] == 2
    assert tree_snapshot(root) == after_first
    assert repo.get_batch(plan.batch_id)["execution_revision"] == revision


def test_crash_after_move_before_commit_is_reconciled_not_repeated(
    repo: Repository, root: Path
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)

    # Discovery recorded the file identity at the active path. A rename preserves
    # volume and inode, so the destination carries the same identity.
    set_db_identity(repo, document.id, file_identity(root / "candidate.pdf").digest_hint())

    # Simulate the crash: the move happened, but only "intent_recorded" is durable.
    destination_rel = dest_of(document, "candidate.pdf")
    destination_path = root / destination_rel
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / "candidate.pdf", destination_path)
    operation_id = repo.list_file_operations(plan.batch_id)[0].id
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash") as conn:
        conn.execute("UPDATE file_operations SET state = 'intent_recorded' WHERE id = ?", (operation_id,))
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash.batch") as conn:
        conn.execute(
            "UPDATE action_batches SET execution_state = 'applying' WHERE id = ?", (plan.batch_id,)
        )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.ok is True
    assert outcome.operations[0].outcome == OpOutcome.RECONCILED
    assert outcome.counts["moved"] == 0, "a crash-recovered move must not be repeated"
    assert (root / destination_rel).read_bytes() == DATA
    assert not (root / "candidate.pdf").exists()
    assert repo.get_document(document.id).location == Location.REJECTED
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.COMMITTED


def test_crash_reconciliation_without_identity_evidence_blocks_rather_than_guesses(
    repo: Repository, root: Path
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    destination_rel = dest_of(document, "candidate.pdf")
    destination_path = root / destination_rel
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / "candidate.pdf", destination_path)
    operation_id = repo.list_file_operations(plan.batch_id)[0].id
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash") as conn:
        # Clear the operation-bound source identity as well, to model a journal row
        # written BEFORE migration 0002. Planning now records
        # file_operations.source_identity, so a real rename would otherwise be
        # corroborated and legitimately commit. The scenario under test is the one
        # where no per-operation identity was ever captured, and recovery must then
        # refuse to treat a size-and-hash match as proof of the move.
        conn.execute(
            "UPDATE file_operations SET state = 'file_moved', source_identity = NULL WHERE id = ?",
            (operation_id,),
        )
    with repo.db.write(actor="helper", actor_kind="helper", event="test.crash.batch") as conn:
        conn.execute(
            "UPDATE action_batches SET execution_state = 'applying' WHERE id = ?", (plan.batch_id,)
        )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    # No recorded identity: refusing to claim the destination is proof of this
    # move is correct, and it must not move the file again or delete anything.
    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert (root / destination_rel).read_bytes() == DATA


# ===========================================================================
# 7. Path escape
# ===========================================================================
def _tamper_destination(repo: Repository, plan, new_destination: str) -> str:
    """Rewrite the stored plan's destination to an attacker-chosen relative path.

    The stored plan_hash is recomputed so the approval binding is internally
    consistent: this models a destination that reached the executor by any route.
    The executor's containment revalidation, not the hash, must refuse it.
    """
    batch = repo.get_batch(plan.batch_id)
    raw = json.loads(json.dumps(batch["plan"]))
    raw["operations"][0]["destination"] = new_destination
    rebuilt = _plan_from_batch({"plan": raw, "instance_id": repo.instance_id, "id": plan.batch_id})
    new_hash = rebuilt.compute_hash()
    rebuilt.plan_hash = new_hash
    with repo.db.write(actor="attacker", event="test.tamper.plan") as conn:
        conn.execute(
            "UPDATE action_batches SET plan_json = ?, plan_hash = ? WHERE id = ?",
            (json.dumps(jsonable(rebuilt)), new_hash, plan.batch_id),
        )
    return new_hash


def test_a_destination_with_a_parent_traversal_is_refused(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    new_hash = _tamper_destination(repo, plan, "../escape-target.pdf")
    # Approve the tampered, self-consistent plan so only containment can stop it.
    repo.approve_batch(
        plan.batch_id, actor=ACTOR, plan_hash=new_hash, expires_at=seconds_from_now_iso(900)
    )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert not (root.parent / "escape-target.pdf").exists()
    assert not (root / "escape-target.pdf").exists()
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert _warnings_mention(outcome, "containment validation"), outcome.warnings


def test_an_absolute_destination_is_refused(repo: Repository, root: Path) -> None:
    document, plan = build_single(repo, root)
    outside = root.parent / "absolute-escape.pdf"
    new_hash = _tamper_destination(repo, plan, str(outside))
    repo.approve_batch(
        plan.batch_id, actor=ACTOR, plan_hash=new_hash, expires_at=seconds_from_now_iso(900)
    )

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert not outside.exists()
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert _warnings_mention(outcome, "containment validation"), outcome.warnings


def test_a_link_planted_between_planning_and_execution_is_refused(
    repo: Repository, root: Path
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    outside = root.parent / "outside-target"
    outside.mkdir()
    # The destination's first component becomes a link that escapes the root.
    make_dir_link(root / REJECTED_DIR, outside)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert not (outside / document.id / "candidate.pdf").exists()
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert _warnings_mention(outcome, "containment validation"), outcome.warnings


def test_a_link_at_the_final_destination_component_is_refused(
    repo: Repository, root: Path
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    outside = root.parent / "outside-file-target"
    outside.mkdir()
    (root / REJECTED_DIR / document.id).mkdir(parents=True)
    make_dir_link(root / REJECTED_DIR / document.id / "candidate.pdf", outside)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert not any(outside.iterdir())
    assert _warnings_mention(outcome, "containment validation"), outcome.warnings


# ===========================================================================
# 8. Intent is durable before the file is touched
# ===========================================================================
class _KillSwitch(Exception):
    """Stands in for the process dying the instant the move is attempted."""


def test_intent_is_durable_on_disk_before_the_move_is_attempted(
    repo: Repository, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, plan = build_single(repo, root)
    approve(repo, plan)
    operation_id = repo.list_file_operations(plan.batch_id)[0].id

    import resume_review.actions.executor as executor_module

    observed: dict[str, object] = {}

    def inspecting_move(src, dst, **kwargs):
        # This runs at the operation boundary, immediately before the kernel move.
        snapshot = load_journal(journal_path(root, plan.batch_id))
        observed["journal_state"] = snapshot.states().get(operation_id)
        observed["journal_exists"] = snapshot.exists
        observed["db_state"] = repo.list_file_operations(plan.batch_id)[0].state.value
        observed["source_present"] = Path(src).is_file()
        observed["destination_present"] = os.path.lexists(dst)
        raise _KillSwitch("simulated crash between durable intent and the move")

    real_move = executor_module.atomic_no_clobber_move
    monkeypatch.setattr(executor_module, "atomic_no_clobber_move", inspecting_move)

    with pytest.raises(_KillSwitch):
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    # The durable records were already advanced before the move was attempted.
    assert observed["journal_exists"] is True
    assert observed["journal_state"] == OperationState.INTENT_RECORDED.value
    assert observed["db_state"] == OperationState.INTENT_RECORDED.value
    assert observed["source_present"] is True
    assert observed["destination_present"] is False
    # The source is untouched by the interrupted attempt.
    assert (root / "candidate.pdf").read_bytes() == DATA

    # The journal plus the surviving source is sufficient to resume safely.
    monkeypatch.setattr(executor_module, "atomic_no_clobber_move", real_move)
    resumed = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())
    assert resumed.ok is True
    assert resumed.counts["moved"] == 1
    assert not (root / "candidate.pdf").exists()
    assert (root / dest_of(document, "candidate.pdf")).read_bytes() == DATA


# ===========================================================================
# 9. Further hostile arrangements
# ===========================================================================
def test_a_foreign_file_at_the_destination_with_the_source_gone_blocks(
    repo: Repository, root: Path
) -> None:
    """Source vanished, but something else now sits at the planned destination.

    A careless recovery would "finish the move" by treating whatever is at the
    destination as the document and committing it. The executor must refuse and
    leave the foreign bytes exactly where they are.
    """
    document, plan = build_single(repo, root)
    approve(repo, plan)
    destination = place_file(root, dest_of(document, "candidate.pdf"), FOREIGN)
    (root / "candidate.pdf").unlink()

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    # The foreign file is preserved byte-for-byte and is not adopted as the document.
    assert destination.read_bytes() == FOREIGN
    assert repo.get_document(document.id).location == Location.ACTIVE
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.NEEDS_RECONCILIATION


def test_a_principal_from_another_instance_is_refused(repo: Repository, root: Path) -> None:
    """An authenticated reviewer for a different instance must not execute this one."""
    document, plan = build_single(repo, root)
    approve(repo, plan)
    before = tree_snapshot(root)

    intruder = Principal(
        actor_ref=ACTOR,
        role=Role.REVIEWER,
        session_id="sess_other",
        instance_id="inst_somewhere_else",
    )
    with pytest.raises(Forbidden) as excinfo:
        apply_batch(repo, batch_id=plan.batch_id, actor=intruder, root=root, now=now_iso())

    assert excinfo.value.code == Code.FORBIDDEN
    assert excinfo.value.detail.get("reason") == "instance_mismatch"
    assert tree_snapshot(root) == before
    assert repo.list_file_operations(plan.batch_id)[0].state == OperationState.PLANNED


def test_a_plan_operation_without_a_durable_row_blocks(repo: Repository, root: Path) -> None:
    """A plan operation the database has no record of is a journal/DB disagreement.

    Trusting the plan alone would move a file whose commit could never be recorded.
    """
    document, plan = build_single(repo, root)
    approve(repo, plan)
    with repo.db.write(actor="attacker", event="test.tamper.rows") as conn:
        conn.execute("DELETE FROM file_operations WHERE batch_id = ?", (plan.batch_id,))
    assert repo.list_file_operations(plan.batch_id) == []

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.code == Code.PLAN_STALE
    assert outcome.counts["moved"] == 0
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert not (root / dest_of(document, "candidate.pdf")).exists()
    assert _warnings_mention(outcome, "no durable record"), outcome.warnings


def test_a_later_operations_stale_decision_blocks_the_whole_batch(repo: Repository, root: Path) -> None:
    """Whole-plan revalidation: a stale *second* operation must stop the first move.

    This is the test that separates "revalidate the whole plan up front" from
    "revalidate each operation just as it runs". Only the former leaves the first
    operation's file untouched when the second is stale.
    """
    documents, plan = build_batch(repo, root, ["alpha.pdf", "bravo.pdf"])
    approve(repo, plan)
    alpha, bravo = documents
    # The second operation's decision changes after approval.
    repo.set_decision(bravo.id, "hold", expected_revision=1, actor=ACTOR)
    before = tree_snapshot(root)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.code == Code.PLAN_STALE
    assert outcome.counts["moved"] == 0, "no operation may move when the plan is stale"
    # Not even the first, otherwise-conforming operation was moved.
    assert (root / "alpha.pdf").read_bytes() == DATA
    assert (root / "bravo.pdf").read_bytes() == DATA
    assert not (root / dest_of(alpha, "alpha.pdf")).exists()
    assert not (root / dest_of(bravo, "bravo.pdf")).exists()
    assert tree_snapshot(root) == before
    assert _warnings_mention(outcome, "decision changed"), outcome.warnings


def test_a_later_operations_missing_source_stops_there_leaving_earlier_work(
    repo: Repository, root: Path,
) -> None:
    """A source that disappears *during* execution stops the batch at that point.

    Unlike a stale database row, a vanished file cannot be seen during the up-front
    revalidation (the plan records the path, not the bytes on disk). The PRD's rule
    is then partial completion: the already-moved file stays put, the missing one is
    reported, and nothing is rolled back or deleted.
    """
    documents, plan = build_batch(repo, root, ["alpha.pdf", "bravo.pdf"])
    approve(repo, plan)
    alpha, bravo = documents
    (root / "bravo.pdf").unlink()

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.PARTIAL.value
    assert outcome.code == Code.BATCH_PARTIAL
    assert outcome.counts["moved"] == 1
    assert outcome.remaining == 1

    # The first operation completed and was not rolled back.
    assert not (root / "alpha.pdf").exists()
    assert (root / dest_of(alpha, "alpha.pdf")).read_bytes() == DATA

    # The missing source produced no destination and no deletion.
    assert not (root / "bravo.pdf").exists()
    assert not (root / dest_of(bravo, "bravo.pdf")).exists()
    row = next(r for r in repo.list_file_operations(plan.batch_id) if r.document_id == bravo.id)
    assert row.state == OperationState.NEEDS_RECONCILIATION
    tasks = repo.list_tasks(document_id=bravo.id, state="open")
    assert any(t.task_type == "reconciliation" for t in tasks)


def test_a_stale_journal_from_a_different_plan_blocks_before_any_move(
    repo: Repository, root: Path,
) -> None:
    """A journal bound to another plan must not be reused to drive this batch's move.

    The journal is the crash-recovery record; if it can be inherited across plans a
    file could be reconciled against the wrong intent.
    """
    document, plan = build_single(repo, root)
    approve(repo, plan)

    # Plant a journal for this batch id that is bound to a different plan hash.
    journal = Journal.load(root, plan.batch_id)
    journal.initialize(plan.operations, instance_id=repo.instance_id, plan_hash="a-different-plan-hash")
    before = tree_snapshot(root)

    outcome = apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())

    assert outcome.state == ExecutionState.BLOCKED.value
    assert outcome.counts["moved"] == 0
    assert tree_snapshot(root) == before
    assert (root / "candidate.pdf").read_bytes() == DATA
    assert not (root / dest_of(document, "candidate.pdf")).exists()
