"""Tests for the durable analysis queue (:mod:`resume_review.analysis.queue`).

The job store is the frozen ``processing_jobs`` table, so these tests exercise
real claims, completions, failures, cancellations and lease reaping against a
real migrated database. The pipeline handler is a small stub, which keeps each
test focused on queue mechanics. No live route is ever used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.pipeline import AnalysisOutcome
from resume_review.analysis.queue import AnalysisQueue, HostCapacity
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations


@pytest.fixture
def db(tmp_path: Path):
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


class _FakePipeline:
    """Minimal pipeline whose only job is the analysis handler."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def analyze_job(self, job):
        return self._handler(job)


def commit_handler(_job) -> AnalysisOutcome:
    return AnalysisOutcome(status="committed", profile_id="profile_x", model_calls=1)


def make_queue(repo: Repository, handler, *, capacity=None, sleep=None, **kwargs) -> AnalysisQueue:
    return AnalysisQueue(
        repository=repo,
        pipeline=_FakePipeline(handler),
        capacity=capacity or HostCapacity(max_in_flight=2, reserved_for_chat=1),
        sleep=sleep,
        **kwargs,
    )


def enqueue(repo: Repository, key: str) -> str:
    return repo.enqueue_job(
        key,
        "analysis",
        input_versions={"source_revision": 1, "criteria_version": 1},
        priority=200,
    )


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------
def test_capacity_reserves_a_lane_for_chat():
    capacity = HostCapacity(max_in_flight=2, reserved_for_chat=1)
    assert capacity.bulk_limit == 1

    assert capacity.try_acquire("inst_a", "analysis") is True
    # One bulk request per instance, even when the host has room.
    assert capacity.try_acquire("inst_a", "analysis") is False
    # The host-wide bulk limit is reached.
    assert capacity.try_acquire("inst_b", "analysis") is False
    # The reserved lane still admits interactive work.
    assert capacity.try_acquire_interactive("inst_a") is True

    capacity.release_interactive("inst_a")
    capacity.release("inst_a", "analysis")
    assert capacity.in_flight() == 0
    assert capacity.try_acquire("inst_b", "analysis") is True


def test_host_wide_cap_blocks_a_second_instance():
    capacity = HostCapacity(max_in_flight=1, reserved_for_chat=0)
    assert capacity.try_acquire("inst_a", "analysis") is True
    assert capacity.try_acquire("inst_b", "analysis") is False
    capacity.release("inst_a", "analysis")
    assert capacity.try_acquire("inst_b", "analysis") is True


def test_full_capacity_does_not_consume_an_attempt(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")
    capacity = HostCapacity(max_in_flight=2, reserved_for_chat=1)
    assert capacity.try_acquire(repo.instance_id, "analysis") is True

    queue = make_queue(repo, commit_handler, capacity=capacity)
    outcome = queue.process_next()
    assert outcome.status == "no_capacity"

    job = repo.get_job(job_id)
    assert job["state"] == "queued"
    assert int(job["attempts"]) == 0, "a refused claim must not burn the retry budget"

    capacity.release(repo.instance_id, "analysis")
    assert queue.process_next().status == "committed"
    assert int(repo.get_job(job_id)["attempts"]) == 1


# ---------------------------------------------------------------------------
# Retry, terminal failure, cancellation
# ---------------------------------------------------------------------------
def test_transient_failure_retries_at_most_three_times_with_backoff(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")
    delays: list[float] = []

    def always_retry(_job) -> AnalysisOutcome:
        return AnalysisOutcome(status="retry", error_code="ADAPTER_TIMEOUT", retryable=True)

    queue = make_queue(repo, always_retry, sleep=delays.append, retry_base=1.0, retry_cap=30.0)
    summary = queue.drain()

    assert summary.processed == 3
    assert summary.retried == 2
    assert summary.failed == 1
    assert delays == [1.0, 2.0], "bounded, monotonic backoff between attempts"

    job = repo.get_job(job_id)
    assert job["state"] == "failed"
    assert int(job["attempts"]) == 3
    assert job["error_code"] == "ADAPTER_TIMEOUT"


def test_manual_review_is_terminal_not_retried(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")

    def manual(_job) -> AnalysisOutcome:
        return AnalysisOutcome(status="manual_review", error_code="invalid_model_output")

    queue = make_queue(repo, manual)
    summary = queue.drain()
    assert summary.processed == 1
    assert summary.manual_review == 1

    job = repo.get_job(job_id)
    assert job["state"] == "succeeded"
    assert int(job["attempts"]) == 1
    # Nothing left to claim.
    assert queue.drain().processed == 0


def test_a_handler_crash_is_retried_then_fails(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")

    def boom(_job):
        raise RuntimeError("handler exploded")

    queue = make_queue(repo, boom)
    summary = queue.drain()
    assert summary.processed == 3
    assert summary.failed == 1
    assert repo.get_job(job_id)["state"] == "failed"


def test_cancel_stops_unstarted_work_and_keeps_committed_results(repo: Repository):
    job_a = enqueue(repo, "analysis:doc:r1:c1")
    job_b = enqueue(repo, "analysis:doc:r1:c2")
    queue = make_queue(repo, commit_handler)

    assert queue.process_next().status == "committed"
    assert repo.get_job(job_a)["state"] == "succeeded"
    assert repo.get_job(job_a)["result_ref"] == "profile_x"

    # Cancel work that has not started; it never runs.
    queue.cancel(job_b)
    assert repo.get_job(job_b)["state"] == "canceled"
    assert queue.drain().processed == 0

    # Cancelling an already-committed job records the request but discards nothing.
    queue.cancel(job_a)
    job = repo.get_job(job_a)
    assert job["state"] == "succeeded"
    assert int(job["cancel_requested"]) == 1
    assert job["result_ref"] == "profile_x"


# ---------------------------------------------------------------------------
# Lease durability
# ---------------------------------------------------------------------------
def test_lease_survives_a_crash_and_is_reclaimable(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")

    # A worker claims the job with an already-expired lease and then "crashes"
    # (never completes or fails it).
    claimed = repo.claim_job("worker-a", 0.0, kinds=["analysis"])
    assert claimed is not None and claimed["id"] == job_id
    assert int(claimed["attempts"]) == 1

    queue = make_queue(repo, commit_handler)
    assert queue.reap_expired_leases() == 1

    recovered = repo.get_job(job_id)
    assert recovered["state"] == "queued"
    assert int(recovered["attempts"]) == 1, "the crashed attempt is counted, not lost"

    # A restarted worker can claim it again.
    again = repo.claim_job("worker-b", 300.0, kinds=["analysis"])
    assert again is not None and again["id"] == job_id
    assert int(again["attempts"]) == 2


def test_reexhausted_lease_becomes_terminal_not_stuck(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")
    # Burn the whole budget with crashed workers.
    for _ in range(3):
        repo.claim_job("worker-a", 0.0, kinds=["analysis"])

    queue = make_queue(repo, commit_handler)
    assert queue.reap_expired_leases() == 1

    job = repo.get_job(job_id)
    assert job["state"] == "failed"
    assert job["error_code"] == "LEASE_HELD"


def test_unknown_kind_is_refused_without_claiming(repo: Repository):
    job_id = enqueue(repo, "analysis:doc:r1:c1")
    queue = make_queue(repo, commit_handler)
    outcome = queue.process_next(kind="chat")
    assert outcome.status == "unsupported_kind"
    assert repo.get_job(job_id)["state"] == "queued"
