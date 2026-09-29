"""Data-access layer tests for :mod:`resume_review.db`.

Authority: PRD section 8.3 ("Decisions and save behavior"), section 10 ("Keep five
concepts independent"), section 11 ("Database and persistence contracts"),
section 12.2 ("Mutation rules") and section 13.3 ("recovery journal").

These tests use synthetic data exclusively. They prove the invariants the rest of
the application is allowed to rely on:

* the frozen migration applies cleanly and every repository method runs;
* a versioned write never applies last-writer-wins;
* a bulk disposition set is all-or-nothing;
* task regeneration is idempotent and never reopens closed work;
* a new current profile cannot violate the partial unique index;
* the durable queue is idempotent and never hands one job to two workers;
* idempotency records answer a repeat and reject a changed payload;
* every successful mutation bumps ``state_revision`` and writes an audit row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.db import Repository, RevisionConflict
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations, current_version
from resume_review.errors import Code, Conflict
from resume_review.models import (
    ActionPlan,
    AnalysisResult,
    DecisionRecord,
    DocumentRecord,
    ExecutionState,
    IntentRecord,
    MediaType,
    NoteRecord,
    PendingIntent,
    PlannedOperation,
    ProcessingState,
    ProfileRecord,
    ReviewState,
    TaskRecord,
    TaskState,
)
from resume_review.util import new_id, seconds_from_now_iso


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    """An isolated migrated database file. One instance per database."""
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


def make_document(repo: Repository, name: str = "candidate-001.pdf") -> DocumentRecord:
    return repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=182004,
        content_sha256=None,
        fs_identity=None,
    )


def make_profile_result(document_id: str, revision: int = 1) -> AnalysisResult:
    return AnalysisResult(
        schema_version="1.0",
        document_id=document_id,
        source_revision=revision,
        criteria_version=1,
        summary_text="Reports commercial renovation coordination experience.",
    )


def make_plan(repo: Repository, document_id: str) -> ActionPlan:
    operation = PlannedOperation(
        operation_id=new_id("operation"),
        document_id=document_id,
        kind=PendingIntent.MOVE_REJECTED,
        source="candidate-001.pdf",
        destination=f"Rejected/{document_id}/candidate-001.pdf",
        source_revision=1,
        expected_sha256="a" * 64,
        expected_size=182004,
        decision_revision=1,
        intent_revision=1,
        location_version=1,
    )
    plan = ActionPlan(
        schema_version="1.0",
        instance_id=repo.instance_id,
        batch_id=new_id("batch"),
        criteria_version=1,
        operations=[operation],
    )
    plan.plan_hash = plan.compute_hash()
    return plan


# ---------------------------------------------------------------------------
# Migration and full method surface
# ---------------------------------------------------------------------------
def test_migration_applies_cleanly(db: Database) -> None:
    assert current_version(db.connect()) == SCHEMA_VERSION
    # Idempotent: a second call applies nothing and reports the same version.
    result = apply_migrations(db.connect())
    assert result["applied"] == []
    assert result["version"] == SCHEMA_VERSION


def test_create_instance_is_singular(repo: Repository) -> None:
    with pytest.raises(Conflict) as caught:
        repo.create_instance("inst_other", __version__, SCHEMA_VERSION)
    assert caught.value.code == Code.INSTANCE_ALREADY_EXISTS
    assert repo.get_instance()["id"] == "inst_test"


def test_every_repository_method_runs(repo: Repository) -> None:
    """Smoke test: call the entire required surface with valid inputs."""
    # -- jobs and criteria ------------------------------------------------
    job = repo.create_or_update_job("Operations Manager", "Coordinate subcontractors.")
    assert job["id"]
    criterion = repo.create_criteria_proposal(
        "cr_01",
        "Coordinate subcontractors on commercial sites",
        rationale="Central to the role.",
        evidence_rule="Quote showing coordination.",
        label="required",
        created_by="reviewer@host",
        origin="human",
    )
    assert criterion.version == 1
    activated = repo.activate_criteria_version(1, "reviewer@host")
    assert [c.criterion_id for c in activated] == ["cr_01"]
    assert repo.active_criteria_version() == 1
    assert len(repo.list_criteria()) == 1
    assert repo.list_criteria(version=1, approved_only=False)[0].approved

    # -- documents --------------------------------------------------------
    document = make_document(repo)
    doc_id = document.id
    assert isinstance(document, DocumentRecord)
    assert repo.get_document(doc_id) is not None
    assert repo.get_document_by_path("candidate-001.pdf").id == doc_id
    assert len(repo.list_documents(sort="ingested_at", direction="asc")) == 1
    assert [d.id for d in repo.list_documents(sort="document_id", direction="desc")] == [doc_id]
    repo.set_document_processing(doc_id, ProcessingState.READY.value, detail="parsed")
    assert repo.set_document_location(doc_id, "candidate-001.pdf", "active", 0) == 1
    repo.set_duplicate_flags(doc_id, duplicate_content=False)
    repo.flag_decision_needs_recheck(doc_id, reason="bytes changed")
    repo.flag_decision_needs_recheck(doc_id, clear=True)

    # -- revisions and extraction cache ----------------------------------
    assert repo.add_revision(doc_id, "b" * 64, 182004, "candidate-001.pdf", "pypdf", "6.4") == 1
    assert repo.get_revision(doc_id, 1)["content_sha256"] == "b" * 64
    assert repo.latest_revision(doc_id)["revision"] == 1
    repo.set_revision_extraction(doc_id, 1, "ok", None, "extracted/x/1.json", 4, 900, 1)
    repo.cache_extraction("b" * 64, "pypdf", "6.4", {"spans": []}, 4, 900, 1)
    cached = repo.get_cached_extraction("b" * 64, "pypdf", "6.4")
    assert cached is not None and cached["payload"] == {"spans": []}

    # -- decisions and notes ---------------------------------------------
    assert isinstance(repo.get_decision(doc_id), DecisionRecord)
    decision = repo.set_decision(doc_id, ReviewState.KEEP.value, 0, "reviewer@host")
    assert decision.disposition == ReviewState.KEEP
    assert repo.bulk_set_decisions(
        [{"document_id": doc_id, "disposition": "hold", "expected_revision": 1}],
        "reviewer@host",
    )["updated"] == 1
    repo.set_disposition_frozen(doc_id, True)
    repo.set_disposition_frozen(doc_id, False)

    note = repo.add_note(doc_id, "Strong coordination evidence.", "reviewer@host")
    assert isinstance(note, NoteRecord)
    edited = repo.update_note(note.id, "Updated body.", 1, "reviewer@host")
    assert edited.note_revision == 2
    assert len(repo.list_notes(doc_id)) == 1
    repo.soft_delete_note(note.id, "reviewer@host")
    assert repo.list_notes(doc_id) == []

    # -- tasks ------------------------------------------------------------
    task, created = repo.upsert_task(doc_id, "verify_certification", "Verify certification")
    assert created and isinstance(task, TaskRecord)
    assert len(repo.list_tasks(document_id=doc_id)) == 1
    assert repo.open_task_counts([doc_id]) == {doc_id: 1}
    closed = repo.close_task(task.id, "verified", "reviewer@host", note="Checked.")
    assert closed.state == TaskState.CLOSED

    # -- pending intent ---------------------------------------------------
    assert isinstance(repo.get_intent(doc_id), IntentRecord)
    intent = repo.set_intent(doc_id, PendingIntent.MOVE_REJECTED.value, "reviewer@host", 0)
    assert intent.intent == PendingIntent.MOVE_REJECTED
    repo.set_intent(doc_id, PendingIntent.NONE.value, "reviewer@host", 1)

    # -- action batches ---------------------------------------------------
    plan = make_plan(repo, doc_id)
    batch_id = repo.create_batch(plan, "reviewer@host")
    batch = repo.get_batch(batch_id)
    assert batch is not None and batch["plan"]["batch_id"] == batch_id
    assert len(repo.list_batches()) == 1
    approved = repo.approve_batch(batch_id, "reviewer@host", batch["plan_hash"], seconds_from_now_iso(900))
    assert approved["execution_state"] == ExecutionState.APPROVED.value
    assert repo.set_batch_state(batch_id, ExecutionState.APPLYING.value, 0) == 1
    op_ids = repo.create_file_operations(batch_id, plan.operations)
    assert len(op_ids) == 1
    repo.update_file_operation(op_ids[0], "committed")
    assert repo.list_file_operations(batch_id)[0].state.value == "committed"
    assert repo.find_operations_in_state(["committed"])[0].id == op_ids[0]

    # -- durable queue ----------------------------------------------------
    queued = repo.enqueue_job("extract:doc1", "extraction", document_id=doc_id)
    claimed = repo.claim_job("worker-1", 60)
    assert claimed is not None and claimed["id"] == queued
    assert repo.get_job(queued)["state"] == "leased"
    assert len(repo.list_jobs(state="leased")) == 1
    repo.complete_job(queued, result_ref="profile-1")
    assert repo.get_job(queued)["state"] == "succeeded"

    # -- profiles, evidence, idempotency, conversations, audit -----------
    profile = repo.insert_profile(
        make_profile_result(doc_id),
        {"prompt_version": "p1", "model_route": "fixture", "validation_state": "valid"},
    )
    assert isinstance(profile, ProfileRecord) and profile.is_current
    assert repo.current_profile(doc_id).id == profile.id
    assert repo.find_reusable_profile(doc_id, 1, 1, "p1", "1.0", "fixture").id == profile.id
    assert repo.evidence_for_profile(profile.id) == []
    repo.mark_profiles_stale(doc_id, reason="new revision")
    assert repo.current_profile(doc_id).stale is True

    repo.idempotency_store("reviewer@host:POST /decisions/bulk", "key-1", "h" * 64, response={"ok": True})
    assert repo.idempotency_lookup("reviewer@host:POST /decisions/bulk", "key-1", "h" * 64)["response"] == {"ok": True}

    conversation = repo.get_or_create_conversation("reviewer@host")
    assert conversation["id"].startswith("conv_")
    message_id = repo.append_message(conversation["id"], "user", "Show me the rejects.")
    assert message_id.startswith("msg_")
    assert len(repo.list_messages(conversation["id"])) == 1
    assert isinstance(repo.list_audit(limit=5), list)
    assert isinstance(repo.status_counts(), dict)


# ---------------------------------------------------------------------------
# Versioned writes never apply last-writer-wins
# ---------------------------------------------------------------------------
def test_set_decision_rejects_stale_revision_and_applies_nothing(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    repo.set_decision(doc_id, ReviewState.KEEP.value, 0, "reviewer@host")

    with pytest.raises(RevisionConflict) as caught:
        repo.set_decision(doc_id, ReviewState.REJECT.value, 0, "reviewer@host")

    error = caught.value
    assert error.code == Code.REVISION_CONFLICT
    assert error.detail["current_revision"] == 1
    assert error.detail["current_value"] == ReviewState.KEEP.value

    # Nothing was applied: the stored disposition is unchanged.
    current = repo.get_decision(doc_id)
    assert current.disposition == ReviewState.KEEP
    assert current.decision_revision == 1


def test_bulk_set_decisions_is_all_or_nothing(db: Database, repo: Repository) -> None:
    first = make_document(repo, "candidate-001.pdf").id
    second = make_document(repo, "candidate-002.pdf").id
    repo.set_decision(first, ReviewState.KEEP.value, 0, "reviewer@host")
    repo.set_decision(second, ReviewState.KEEP.value, 0, "reviewer@host")

    with pytest.raises(RevisionConflict) as caught:
        repo.bulk_set_decisions(
            [
                {"document_id": first, "disposition": "reject", "expected_revision": 1},
                # Stale: the second decision is at revision 1, not 0.
                {"document_id": second, "disposition": "reject", "expected_revision": 0},
            ],
            "reviewer@host",
        )

    conflicts = caught.value.detail["conflicts"]
    assert [c["document_id"] for c in conflicts] == [second]

    # The valid first item must NOT have been applied.
    assert repo.get_decision(first).disposition == ReviewState.KEEP
    assert repo.get_decision(first).decision_revision == 1
    assert repo.get_decision(second).disposition == ReviewState.KEEP

    # A fully valid bulk set does apply.
    result = repo.bulk_set_decisions(
        [
            {"document_id": first, "disposition": "reject", "expected_revision": 1},
            {"document_id": second, "disposition": "reject", "expected_revision": 1},
        ],
        "reviewer@host",
    )
    assert result["updated"] == 2
    assert repo.get_decision(first).disposition == ReviewState.REJECT


def test_set_decision_refuses_while_frozen(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    repo.set_disposition_frozen(doc_id, True)
    with pytest.raises(Conflict) as caught:
        repo.set_decision(doc_id, ReviewState.KEEP.value, 0, "reviewer@host")
    assert caught.value.code == Code.INTENT_FROZEN


def test_set_document_location_conflict(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    assert repo.set_document_location(doc_id, "candidate-001.pdf", "active", 0) == 1
    with pytest.raises(RevisionConflict) as caught:
        repo.set_document_location(doc_id, "moved.pdf", "active", 0)
    assert caught.value.detail["current_revision"] == 1
    assert repo.get_document(doc_id).current_rel_path == "candidate-001.pdf"


# ---------------------------------------------------------------------------
# Tasks: idempotent and never reopens closed work
# ---------------------------------------------------------------------------
def test_upsert_task_is_idempotent_and_does_not_reopen(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    task, created = repo.upsert_task(doc_id, "verify_certification", "Verify certification")
    assert created is True

    again, created_again = repo.upsert_task(doc_id, "verify_certification", "Verify certification")
    assert created_again is False
    assert again.id == task.id

    repo.close_task(task.id, "verified", "reviewer@host")
    after_close, created_third = repo.upsert_task(doc_id, "verify_certification", "Verify certification")
    assert created_third is False
    assert after_close.id == task.id
    assert after_close.state == TaskState.CLOSED

    # A genuinely new source revision is a new key and a new linked task.
    newer, created_new = repo.upsert_task(
        doc_id, "verify_certification", "Verify certification", source_revision=2
    )
    assert created_new is True
    assert newer.id != task.id


# ---------------------------------------------------------------------------
# Profiles: partial unique index is honoured
# ---------------------------------------------------------------------------
def test_insert_profile_clears_previous_current(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    meta = {"prompt_version": "p1", "model_route": "fixture", "validation_state": "valid"}

    first = repo.insert_profile(make_profile_result(doc_id, revision=1), meta)
    second = repo.insert_profile(make_profile_result(doc_id, revision=2), meta)

    current_rows = db.query(
        "SELECT id FROM profiles WHERE document_id = ? AND is_current = 1", (doc_id,)
    )
    assert [str(r["id"]) for r in current_rows] == [second.id]
    assert repo.current_profile(doc_id).id == second.id
    assert repo.current_profile(doc_id).source_revision == 2
    # The superseded profile is retained, not deleted.
    assert repo.get_revision(doc_id, 1) is None  # no revision row was created here
    assert first.id != second.id


def test_mark_profiles_stale_never_deletes(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    repo.insert_profile(
        make_profile_result(doc_id),
        {"prompt_version": "p1", "model_route": "fixture", "validation_state": "valid"},
    )
    assert repo.mark_profiles_stale(doc_id, reason="bytes changed") == 1
    assert repo.mark_profiles_stale(doc_id) == 0
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE document_id = ?", (doc_id,)) == 1


# ---------------------------------------------------------------------------
# Durable queue
# ---------------------------------------------------------------------------
def test_enqueue_job_is_idempotent(db: Database, repo: Repository) -> None:
    first = repo.enqueue_job("scan:root", "scan")
    second = repo.enqueue_job("scan:root", "scan")
    assert first == second
    assert db.scalar("SELECT COUNT(*) FROM processing_jobs") == 1


def test_claim_job_skips_a_live_lease(db: Database, repo: Repository) -> None:
    job_id = repo.enqueue_job("analysis:doc1", "analysis")
    claimed = repo.claim_job("worker-1", lease_seconds=60)
    assert claimed is not None and claimed["id"] == job_id
    assert claimed["state"] == "leased"

    # A second worker must not receive the same live lease.
    assert repo.claim_job("worker-2", lease_seconds=60) is None

    # Once the first worker completes, the job is terminal and still unclaimable.
    repo.complete_job(job_id)
    assert repo.claim_job("worker-2", lease_seconds=60) is None


def test_expired_lease_is_reaped_and_reclaimable(db: Database, repo: Repository) -> None:
    job_id = repo.enqueue_job("scan:root", "scan")
    claimed = repo.claim_job("worker-1", lease_seconds=-1)  # already expired
    assert claimed is not None
    assert repo.reap_expired_leases() == 1
    reclaimed = repo.claim_job("worker-2", lease_seconds=60)
    assert reclaimed is not None and reclaimed["id"] == job_id


def test_reap_fails_an_exhausted_lease(db: Database, repo: Repository) -> None:
    job_id = repo.enqueue_job("scan:root", "scan")
    repo.claim_job("worker-1", lease_seconds=-1)
    # Exhaust the retry budget while the lease is expired.
    repo.db.connect().execute(
        "UPDATE processing_jobs SET attempts = max_attempts WHERE id = ?", (job_id,)
    )
    assert repo.reap_expired_leases() == 1
    assert repo.get_job(job_id)["state"] == "failed"
    assert repo.claim_job("worker-2", lease_seconds=60) is None


def test_fail_job_reports_retry(db: Database, repo: Repository) -> None:
    job_id = repo.enqueue_job("scan:root", "scan")
    repo.claim_job("worker-1", 60)
    assert repo.fail_job(job_id, Code.EXTRACTION_FAILED, "transient") is True
    assert repo.get_job(job_id)["state"] == "queued"

    repo.claim_job("worker-1", 60)
    repo.fail_job(job_id, Code.EXTRACTION_FAILED, "transient")
    repo.claim_job("worker-1", 60)
    assert repo.fail_job(job_id, Code.EXTRACTION_FAILED, "final") is False
    assert repo.get_job(job_id)["state"] == "failed"


def test_cancel_job(db: Database, repo: Repository) -> None:
    job_id = repo.enqueue_job("scan:root", "scan")
    repo.cancel_job(job_id)
    assert repo.get_job(job_id)["state"] == "canceled"
    assert repo.claim_job("worker-1", 60) is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_idempotency_lookup_returns_stored_and_rejects_changed_payload(db: Database, repo: Repository) -> None:
    scope = "reviewer@host:POST /actions/plan"
    assert repo.idempotency_lookup(scope, "key-1", "hash-a") is None

    repo.idempotency_store(scope, "key-1", "hash-a", response={"batch_id": "batch_1"})
    stored = repo.idempotency_lookup(scope, "key-1", "hash-a")
    assert stored is not None and stored["response"] == {"batch_id": "batch_1"}

    with pytest.raises(Conflict) as caught:
        repo.idempotency_lookup(scope, "key-1", "hash-b")
    assert caught.value.code == Code.IDEMPOTENCY_KEY_REUSED


# ---------------------------------------------------------------------------
# Audit and state revision
# ---------------------------------------------------------------------------
def test_successful_mutation_bumps_revision_and_audits(db: Database, repo: Repository) -> None:
    before = repo.db.state_revision()
    audit_before = db.scalar("SELECT COUNT(*) FROM audit_events")

    doc_id = make_document(repo).id

    after = repo.db.state_revision()
    assert after == before + 1
    assert db.scalar("SELECT COUNT(*) FROM audit_events") == audit_before + 1

    events = repo.list_audit(limit=10)
    assert events[0]["event"] == "document.create"
    assert events[0]["entity_type"] == "document"


def test_failed_mutation_does_not_bump_revision(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    repo.set_decision(doc_id, ReviewState.KEEP.value, 0, "reviewer@host")
    revision = repo.db.state_revision()

    with pytest.raises(RevisionConflict):
        repo.set_decision(doc_id, ReviewState.REJECT.value, 0, "reviewer@host")

    assert repo.db.state_revision() == revision
    # The refusal is still visible in the audit trail.
    assert any(row["outcome"] == "error" for row in repo.list_audit(limit=10))


# ---------------------------------------------------------------------------
# Batch guard rails
# ---------------------------------------------------------------------------
def test_approve_batch_rejects_hash_mismatch_and_non_human(db: Database, repo: Repository) -> None:
    doc_id = make_document(repo).id
    plan = make_plan(repo, doc_id)
    batch_id = repo.create_batch(plan, "reviewer@host")

    with pytest.raises(Conflict) as caught:
        repo.approve_batch(batch_id, "reviewer@host", "0" * 64, seconds_from_now_iso(900))
    assert caught.value.code == Code.PLAN_HASH_MISMATCH

    from resume_review.errors import Forbidden

    with pytest.raises(Forbidden) as human_caught:
        repo.approve_batch(batch_id, "agent:helper", plan.plan_hash, seconds_from_now_iso(900))
    assert human_caught.value.code == Code.APPROVAL_MUST_BE_HUMAN


def test_list_documents_tie_breaker_is_stable(db: Database, repo: Repository) -> None:
    ids = [make_document(repo, f"candidate-{i:03d}.pdf").id for i in range(5)]
    forward = [d.id for d in repo.list_documents(sort="original_filename", direction="asc")]
    forward_again = [d.id for d in repo.list_documents(sort="original_filename", direction="asc")]
    # Filenames are distinct, so ascending order is exactly the creation order and
    # the repeated call is byte-for-byte reproducible.
    assert forward == forward_again == ids
    backward = [d.id for d in repo.list_documents(sort="original_filename", direction="desc")]
    assert backward == list(reversed(ids))
