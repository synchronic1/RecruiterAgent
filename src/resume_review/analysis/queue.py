"""Durable analysis job queue with bounded concurrency and retry (PRD section 6.4).

The queue is a thin, synchronous driver over the frozen job store in
:mod:`resume_review.db.repository`. It does not keep jobs of its own: every claim,
completion, failure and cancellation is a row in ``processing_jobs``, so a crash
or restart loses nothing. Its responsibilities are:

* **One model request in flight per instance.** A bulk analysis lease is held for
  the whole time one job is processed, and :class:`HostCapacity` refuses a second
  bulk lease for the same instance even when the host has room.
* **A reserved interactive lane.** Interactive chat keeps capacity that bulk
  analysis cannot consume, so a large folder cannot starve the chat surface.
* **Bounded retry.** A transient failure is retried at most three total attempts
  with bounded backoff; one structured-output repair turn is allowed *inside* an
  attempt (the pipeline owns that budget, see :mod:`resume_review.analysis.pipeline`).
  A permanent parser failure or repeatedly invalid model output becomes a
  manual-review task, never an infinite retry.
* **Cancellation that stops unstarted work without discarding committed results.**
  ``cancel`` marks queued work canceled; a job that already succeeded keeps its
  committed profile and only records the cancel request.

Every method is deterministic and calls no live route; the pipeline it drives is
given a stubbed or fixture adapter in tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .pipeline import AnalysisOutcome, Pipeline, bounded_backoff

__all__ = ["HostCapacity", "AnalysisQueue", "QueueOutcome", "DrainSummary"]

#: Job kinds that run bulk inference and must not saturate the host.
BULK_KINDS = frozenset({"analysis", "extraction", "scan", "snapshot"})

#: Job kinds that serve a person waiting on a reply.
INTERACTIVE_KINDS = frozenset({"chat"})


@dataclass
class QueueOutcome:
    """What one :meth:`AnalysisQueue.process_next` call did."""

    status: str
    job_id: str | None = None
    model_calls: int = 0
    profile_id: str | None = None
    error_code: str | None = None
    detail: str | None = None
    retry_after_seconds: float | None = None


@dataclass
class DrainSummary:
    """Counts from one drain pass."""

    processed: int = 0
    committed: int = 0
    reused: int = 0
    superseded: int = 0
    manual_review: int = 0
    retried: int = 0
    failed: int = 0
    model_calls: int = 0
    stopped_reason: str = "exhausted"
    delays: list[float] = field(default_factory=list)


class HostCapacity:
    """Host-wide and per-instance limits on in-flight model requests.

    State is guarded by a lock so two worker threads on the same host cannot both
    take the last bulk slot. ``reserved_for_chat`` is a floor of capacity that only
    an interactive lease may consume, which is what keeps chat responsive while
    bulk analysis runs.
    """

    def __init__(self, *, max_in_flight: int = 4, reserved_for_chat: int = 1) -> None:
        if int(max_in_flight) < 1:
            raise ValueError("max_in_flight must be at least 1")
        if int(reserved_for_chat) < 0:
            raise ValueError("reserved_for_chat must not be negative")
        if int(reserved_for_chat) >= int(max_in_flight):
            raise ValueError("reserved_for_chat must leave at least one bulk slot")
        self.max_in_flight = int(max_in_flight)
        self.reserved_for_chat = int(reserved_for_chat)
        self._lock = threading.Lock()
        self._total = 0
        self._instance_bulk: dict[str, int] = {}

    @property
    def bulk_limit(self) -> int:
        """The host-wide number of concurrent bulk leases allowed."""
        return max(0, self.max_in_flight - self.reserved_for_chat)

    def try_acquire(self, instance_id: str, kind: str = "analysis") -> bool:
        """Reserve a slot for one in-flight request, or return False."""
        interactive = str(kind) in INTERACTIVE_KINDS
        with self._lock:
            if interactive:
                if self._total >= self.max_in_flight:
                    return False
                self._total += 1
                return True
            if self._total >= self.bulk_limit:
                return False
            if self._instance_bulk.get(instance_id, 0) >= 1:
                # One bulk request per instance: a single document is never
                # assessed twice concurrently.
                return False
            self._instance_bulk[instance_id] = self._instance_bulk.get(instance_id, 0) + 1
            self._total += 1
            return True

    def release(self, instance_id: str, kind: str = "analysis") -> None:
        with self._lock:
            if self._total > 0:
                self._total -= 1
            if str(kind) in INTERACTIVE_KINDS:
                return
            current = self._instance_bulk.get(instance_id, 0)
            if current <= 1:
                self._instance_bulk.pop(instance_id, None)
            else:
                self._instance_bulk[instance_id] = current - 1

    def in_flight(self, instance_id: str | None = None, kind: str | None = None) -> int:
        with self._lock:
            if instance_id is None and kind is None:
                return self._total
            if instance_id is not None and (kind is None or str(kind) in BULK_KINDS):
                return self._instance_bulk.get(instance_id, 0)
            return 0

    def try_acquire_interactive(self, instance_id: str) -> bool:
        return self.try_acquire(instance_id, kind="chat")

    def release_interactive(self, instance_id: str) -> None:
        self.release(instance_id, kind="chat")


class AnalysisQueue:
    """Drive the durable job store with bounded concurrency and bounded retry."""

    def __init__(
        self,
        *,
        repository: Any,
        pipeline: Pipeline,
        capacity: HostCapacity | None = None,
        lease_owner: str = "analysis-queue",
        lease_seconds: float = 300.0,
        sleep: Callable[[float], None] | None = None,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], AnalysisOutcome]] | None = None,
        retry_base: float = 1.0,
        retry_cap: float = 30.0,
    ) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self.capacity = capacity or HostCapacity()
        self.lease_owner = lease_owner
        self.lease_seconds = float(lease_seconds)
        self._sleep = sleep
        self.handlers: dict[str, Callable[[Mapping[str, Any]], AnalysisOutcome]] = dict(
            handlers or {}
        )
        self.handlers.setdefault("analysis", pipeline.analyze_job)
        self.retry_base = float(retry_base)
        self.retry_cap = float(retry_cap)

    @property
    def instance_id(self) -> str:
        return self.repository.instance_id

    # -- single step -------------------------------------------------------
    def process_next(self, *, kind: str = "analysis") -> QueueOutcome:
        """Lease and process one job of ``kind``, or report why none ran.

        Capacity is reserved *before* the claim because ``claim_job`` increments a
        job's attempt counter; refusing to claim when the host is full is what
        stops a saturated queue from burning a job's retry budget.
        """
        handler = self.handlers.get(kind)
        if handler is None:
            return QueueOutcome(status="unsupported_kind", detail=kind)

        if not self.capacity.try_acquire(self.instance_id, kind):
            return QueueOutcome(status="no_capacity")

        try:
            job = self.repository.claim_job(self.lease_owner, self.lease_seconds, kinds=[kind])
            if job is None:
                return QueueOutcome(status="idle")
            return self._run(job, handler)
        finally:
            self.capacity.release(self.instance_id, kind)

    def _run(
        self, job: Mapping[str, Any], handler: Callable[[Mapping[str, Any]], AnalysisOutcome]
    ) -> QueueOutcome:
        job_id = str(job.get("id") or "")
        try:
            outcome = handler(job)
        except Exception as exc:  # noqa: BLE001 - a handler crash must not kill the worker
            error_code = getattr(exc, "code", "UNEXPECTED_ERROR")
            will_retry = self.repository.fail_job(job_id, str(error_code), str(exc)[:500])
            return QueueOutcome(
                status="retry" if will_retry else "failed",
                job_id=job_id,
                error_code=str(error_code),
                detail=str(exc)[:500],
                retry_after_seconds=self._delay(job, will_retry),
            )

        if outcome.status == "retry":
            will_retry = self.repository.fail_job(
                job_id, outcome.error_code or "ANALYSIS_RETRY", outcome.detail or ""
            )
            return QueueOutcome(
                status="retry" if will_retry else "failed",
                job_id=job_id,
                model_calls=outcome.model_calls,
                error_code=outcome.error_code,
                detail=outcome.detail,
                retry_after_seconds=self._delay(job, will_retry),
            )

        # Every other terminal status records the job as done. Manual review is a
        # completed decision to involve a human, not a failure to retry.
        self.repository.complete_job(job_id, result_ref=outcome.profile_id)
        return QueueOutcome(
            status=outcome.status,
            job_id=job_id,
            model_calls=outcome.model_calls,
            profile_id=outcome.profile_id,
            error_code=outcome.error_code,
            detail=outcome.detail,
        )

    def _delay(self, job: Mapping[str, Any], will_retry: bool) -> float | None:
        if not will_retry:
            return None
        attempts = int(job.get("attempts") or 1)
        return bounded_backoff(attempts, base=self.retry_base, cap=self.retry_cap)

    # -- drain -------------------------------------------------------------
    def drain(self, *, max_jobs: int | None = None, kind: str = "analysis") -> DrainSummary:
        """Process runnable jobs until the queue empties or capacity is exhausted.

        ``sleep`` is only invoked between retries, and only when one was injected,
        so tests record the delays without ever waiting on the clock.
        """
        summary = DrainSummary()
        while max_jobs is None or summary.processed < max_jobs:
            outcome = self.process_next(kind=kind)
            if outcome.status in ("idle", "no_capacity", "unsupported_kind"):
                summary.stopped_reason = outcome.status
                break
            summary.processed += 1
            summary.model_calls += outcome.model_calls
            if outcome.status == "committed":
                summary.committed += 1
            elif outcome.status == "reused":
                summary.reused += 1
            elif outcome.status == "superseded":
                summary.superseded += 1
            elif outcome.status == "manual_review":
                summary.manual_review += 1
            elif outcome.status == "retry":
                summary.retried += 1
            elif outcome.status == "failed":
                summary.failed += 1

            if outcome.retry_after_seconds is not None:
                summary.delays.append(outcome.retry_after_seconds)
                if self._sleep is not None:
                    self._sleep(outcome.retry_after_seconds)
        else:
            summary.stopped_reason = "max_jobs"
        return summary

    # -- lifecycle helpers -------------------------------------------------
    def cancel(self, job_id: str) -> None:
        """Cancel work not yet started; a committed result is never discarded."""
        self.repository.cancel_job(job_id)

    def reap_expired_leases(self) -> int:
        """Return leases abandoned by a crashed worker to the queue."""
        return self.repository.reap_expired_leases()
