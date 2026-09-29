"""The analysis pipeline: discovery through published snapshot (PRD section 6.3).

This module owns the stage sequence exactly as the PRD names it::

    discover -> stabilize -> register revision -> extract source spans
        -> validate extraction -> enqueue bounded analysis
        -> validate result -> commit profile/evidence/tasks -> publish snapshot

Two cache levels keep a no-op rescan free of model calls. Extraction is cached by
``(content hash, parser name, parser version)`` through the existing
:class:`~resume_review.ingest.extract.ExtractionCache`; an assessment is reusable
when the document revision, the approved criteria version, the prompt version,
the analysis schema version and the model route all still match, which is what
:meth:`~resume_review.db.repository_analysis.AnalysisRepositoryMixin.find_reusable_profile`
checks. A rescan of unchanged bytes therefore extracts nothing and calls no model.

Three guarantees this module is built to keep, each with a test:

* **A superseded result never becomes current.** The input revision and the
  criteria version are re-checked against live state at commit time. A result
  computed against a superseded input may be retained for history, but it is
  written non-current and never displaces the assessment for the live inputs.
* **A parser failure does not hide the row.** A document whose extraction fails
  keeps any previous valid profile (flagged stale) and gets a manual-review task;
  it is never deleted or removed from a listing.
* **Analysis cannot write a human decision or an approval.** This module never
  imports :mod:`resume_review.actions` and never calls ``set_decision``,
  ``approve_batch`` or ``set_intent``; the only tables it writes are documents,
  revisions, the extraction cache, profiles, evidence, review tasks and the job
  queue. ``not_found`` is a first-class accepted result and is stored as-is; it is
  never converted to a negative finding and never sets a review decision.

Model output is data. It is validated against the versioned analysis schema before
it reaches the database, and every storage path is resolved from a document ID
rather than taken from a model response.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..errors import ResumeReviewError
from ..ingest import extract_docx, extract_pdf, extract_txt
from ..ingest.discover import discover
from ..ingest.extract import ExtractionCache, extract_with_cache
from ..ingest.sniff import sniff_path
from ..ingest.stabilize import Stabilizer
from ..models import (
    DEFAULT_LIMITS,
    EvidenceItem,
    MediaType,
    ProcessingState,
    REPORT_FILENAME,
    ResourceLimits,
    TaskOrigin,
)
from ..openclaw_adapter.policy import RoutePolicy
from ..openclaw_adapter.prompts import (
    PROMPT_SCHEMA_VERSION,
    PROMPT_VERSION,
    AnalysisRequest,
    build_analysis_request,
    build_repair_turn,
)
from ..reporting import build_snapshot_payload, write_snapshot
from .validate import ANALYSIS_SCHEMA_VERSION, validate_analysis_result

__all__ = [
    "AnalysisClient",
    "AdapterCompletionClient",
    "Completion",
    "AnalysisOutcome",
    "CommitOutcome",
    "ScanSummary",
    "Pipeline",
    "bounded_backoff",
]

#: Extraction state values that mean the source could not be turned into spans.
_EXTRACTION_FAILURE_STATES = frozenset({"failed", "unsupported"})

#: Extraction states that mean the source is usable, even if truncated.
_EXTRACTION_OK_STATES = frozenset({"ok", "partial"})

_REPAIR_PROBLEM_LIMIT = 400


# ---------------------------------------------------------------------------
# Completion client contract
# ---------------------------------------------------------------------------
@dataclass
class Completion:
    """One model answer plus the trusted run metadata the caller records.

    The pipeline reads only fields the adapter attached itself. ``route`` is a
    string because the pipeline compares it against the reuse key; it is never
    taken from model text.
    """

    text: str
    request_id: str | None = None
    route: str | None = None
    model_version: str | None = None
    provider_label: str | None = None
    run_started_at: str | None = None
    run_ended_at: str | None = None
    token_usage: int | None = None


class AnalysisClient(Protocol):
    """Synchronous one-shot completion client the pipeline depends on.

    Defined as a protocol so a test can pass a stub that records call counts and
    returns scripted text without any live route (PRD section 6.4 forbids a live
    route in the deterministic suite).
    """

    def complete(self, request: AnalysisRequest, *, request_id: str | None = None) -> Completion:
        """Return the model's answer for ``request`` or raise ``ResumeReviewError``."""


def _default_asyncio_runner(coro: Any) -> Any:
    """Run one coroutine on a fresh loop. The worker thread owns its loop."""
    return asyncio.run(coro)


class AdapterCompletionClient:
    """Bridge the async :class:`OpenClawAdapter` to the sync pipeline contract.

    The adapter's ``analyze`` is a coroutine; the durable worker is synchronous.
    ``runner`` is injectable so a test can drive the bridge without a real event
    loop, and defaults to ``asyncio.run`` so production code needs no setup.
    """

    def __init__(self, adapter: Any, *, runner: Callable[[Any], Any] | None = None) -> None:
        self._adapter = adapter
        self._runner = runner or _default_asyncio_runner

    def complete(self, request: AnalysisRequest, *, request_id: str | None = None) -> Completion:
        result = self._runner(
            self._adapter.analyze(request, conversation_user=None, request_id=request_id)
        )
        route = getattr(result, "route", None)
        return Completion(
            text=getattr(result, "text", "") or "",
            request_id=getattr(result, "request_id", None),
            route=str(getattr(route, "value", route)) if route is not None else None,
            provider_label=getattr(result, "provider_label", None),
            run_started_at=getattr(result, "started_at", None),
            run_ended_at=getattr(result, "ended_at", None),
            token_usage=getattr(result, "token_usage", None),
        )


@dataclass
class AnalysisOutcome:
    """What one ``analyze_job`` call did, in terms the queue can act on."""

    status: str
    model_calls: int = 0
    profile_id: str | None = None
    detail: str | None = None
    error_code: str | None = None
    retryable: bool = False


@dataclass
class CommitOutcome:
    """The result of writing a validated analysis result to the database."""

    status: str
    profile_id: str | None = None
    is_current: bool = False
    source_revision: int = 0


@dataclass
class ScanSummary:
    """Counts from one pipeline pass over the job folder."""

    discovered: int = 0
    created: int = 0
    revisions_added: int = 0
    extractions: int = 0
    cache_hits: int = 0
    pending: int = 0
    parser_failures: int = 0
    analyses_enqueued: int = 0
    reused_assessments: int = 0
    awaiting_criteria: int = 0
    route_unavailable: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def bounded_backoff(attempts: int, *, base: float = 1.0, factor: float = 2.0, cap: float = 30.0) -> float:
    """Bounded exponential backoff for attempt number ``attempts`` (1-based).

    ``attempts`` is the value recorded at claim time, so the first failed attempt
    is attempt 1. The delay grows monotonically and is capped, never unbounded.
    """
    count = int(attempts)
    if count < 1:
        return 0.0
    return min(float(cap), float(base) * (float(factor) ** (count - 1)))


def _parser_identity(media_type: MediaType) -> tuple[str, str] | None:
    """The parser name and version for a media type, mirroring the extractor."""
    if media_type is MediaType.PDF:
        return extract_pdf.PARSER_NAME, extract_pdf.parser_version()
    if media_type is MediaType.DOCX:
        return extract_docx.PARSER_NAME, extract_docx.parser_version()
    if media_type is MediaType.TXT:
        return extract_txt.PARSER_NAME, extract_txt.parser_version()
    return None


def _parse_versions(raw: Any) -> dict[str, Any]:
    """Decode a job's ``input_versions`` column, tolerating a corrupt value."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _parse_json_object(text: str) -> tuple[dict[str, Any], str | None]:
    """Parse one model answer into a JSON object, or return a short problem."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {"_unparsed": True}, "response was not valid JSON"
    if not isinstance(value, dict):
        return {"_unparsed": True}, "response JSON was not an object"
    return value, None


def _problem_summary(outcome: Any) -> str:
    """A short machine description of what the validator rejected."""
    codes = [str(getattr(problem, "code", "?")) for problem in outcome.problems]
    if not codes:
        return "unspecified"
    unique: list[str] = []
    for code in codes:
        if code not in unique:
            unique.append(code)
    return ", ".join(unique)[:_REPAIR_PROBLEM_LIMIT]


def materialize_result(result: Any, spans: Sequence[Any]) -> Any:
    """Enrich an :class:`AnalysisResult` so every assessment is persisted.

    The frozen repository stores criterion assessments as ``evidence`` rows with
    ``claim_kind='criterion'`` (there is no assessments table). This function
    attaches each assessment's ``criterion_id`` and ``result`` to the evidence rows
    it cites, and synthesizes one row for an assessment that cites nothing -- so a
    ``not_found`` / ``unclear`` / ``needs_manual_review`` result is stored as the
    neutral value the model returned and never dropped.
    """
    criteria = list(getattr(result, "criteria", []) or [])
    evidence = list(getattr(result, "evidence", []) or [])
    summary_ids = set(getattr(result, "summary_evidence_ids", []) or [])

    owner_of: dict[str, Any] = {}
    for assessment in criteria:
        for evidence_id in assessment.evidence_ids:
            owner_of.setdefault(evidence_id, assessment)

    rewritten: list[EvidenceItem] = []
    cited_by: set[str] = set()
    for item in evidence:
        owner = owner_of.get(item.id)
        claim_kind = item.claim_kind
        criterion_id = item.criterion_id
        item_result = item.result
        if owner is not None:
            cited_by.add(owner.criterion_id)
            claim_kind = "criterion"
            criterion_id = owner.criterion_id
            item_result = owner.result
        elif item.id in summary_ids:
            claim_kind = "summary"
        validated = "verified" if (item.span_id and item.quote) else "unchecked"
        rewritten.append(
            EvidenceItem(
                id=item.id,
                span_id=item.span_id,
                quote=item.quote,
                locator=dict(item.locator or {}),
                criterion_id=criterion_id,
                result=item_result,
                claim_kind=claim_kind,
                validation=validated,
                validation_detail=item.validation_detail,
            )
        )

    for assessment in criteria:
        if assessment.criterion_id in cited_by:
            continue
        rewritten.append(
            EvidenceItem(
                id=f"criterion:{assessment.criterion_id}",
                span_id="",
                quote="",
                locator={},
                criterion_id=assessment.criterion_id,
                result=assessment.result,
                claim_kind="criterion",
                validation="unchecked",
            )
        )

    result.evidence = rewritten
    return result


class Pipeline:
    """Deterministic stages plus the bounded analysis step over one instance."""

    def __init__(
        self,
        *,
        repository: Any,
        root: str | Path,
        adapter: AnalysisClient,
        route_policy: RoutePolicy,
        stabilizer: Stabilizer | None = None,
        limits: ResourceLimits | None = None,
    ) -> None:
        self.repository = repository
        self.root = Path(root)
        self.adapter = adapter
        self.route_policy = route_policy
        self.limits = limits or DEFAULT_LIMITS
        self.stabilizer = stabilizer or Stabilizer(limits=self.limits)

    # -- small accessors ---------------------------------------------------
    @property
    def db(self) -> Any:
        return self.repository.db

    @property
    def instance_id(self) -> str:
        return self.repository.instance_id

    @property
    def route_value(self) -> str:
        return str(self.route_policy.route.value)

    def _extraction_cache(self) -> ExtractionCache:
        return ExtractionCache(self.db.connect(), self.instance_id)

    # ==================================================================
    # Deterministic stages: discover -> stabilize -> revision -> extract
    # ==================================================================
    def scan(self) -> ScanSummary:
        """Run the deterministic stages for every discoverable file."""
        summary = ScanSummary()
        census = discover(self.root, limits=self.limits)
        summary.discovered = len(census.files)

        for entry in census.files:
            if entry.exceeds_max_bytes:
                # Too large to process; keep it visible as a manual-review row.
                self._record_unreadable(entry, "exceeds_max_bytes")
                summary.parser_failures += 1
                continue

            sniff = sniff_path(entry.absolute_path, filename=entry.rel_path)
            document = self.repository.get_document_by_path(entry.rel_path)
            if document is None:
                document = self.repository.create_document(
                    entry.original_filename,
                    entry.rel_path,
                    sniff.media_type,
                    entry.size_bytes,
                    None,
                    entry.fs_identity,
                    entry.submitted_at,
                )
                summary.created += 1

            snapshot = self.stabilizer.stabilize(entry.absolute_path)
            if not snapshot.stable:
                # Still being written: leave the row discovered, do not extract.
                summary.pending += 1
                continue

            try:
                revision, added = self._register_revision(document, entry, snapshot, sniff)
                if added:
                    summary.revisions_added += 1
                extracted, cache_hit = self._extract(snapshot, sniff)
            finally:
                snapshot.cleanup()

            if cache_hit:
                summary.cache_hits += 1
            else:
                summary.extractions += 1

            self.repository.set_revision_extraction(
                document.id,
                revision,
                extracted.state,
                extracted.detail or None,
                None,
                len(extracted.spans),
                extracted.char_count,
                extracted.page_count,
            )

            if self._handle_extraction_outcome(document, revision, extracted, summary):
                continue

            self._schedule_analysis(self.repository.get_document(document.id), revision, summary)

        return summary

    def _register_revision(
        self, document: Any, entry: Any, snapshot: Any, sniff: Any
    ) -> tuple[int, bool]:
        """Append a revision when the bytes changed; return ``(revision, added)``."""
        revision = int(document.current_revision or 0)
        prior_hash = document.content_sha256
        if prior_hash and prior_hash == snapshot.sha256 and revision >= 1:
            return revision, False

        # Derive the parser from the *current* bytes, not the type recorded when the
        # document was first seen: a file whose content changed must be re-sniffed.
        identity = _parser_identity(MediaType(sniff.media_type))
        parser_name, parser_version = identity if identity else (None, None)
        revision = self.repository.add_revision(
            document.id,
            snapshot.sha256,
            snapshot.size_bytes,
            entry.rel_path,
            parser_name=parser_name,
            parser_version=parser_version,
        )
        if prior_hash and prior_hash != snapshot.sha256:
            # A material change asks a human to look again; it never overwrites a
            # decision (PRD 6.3).
            self.repository.flag_decision_needs_recheck(
                document.id, reason="document_changed"
            )
        return revision, True

    def _extract(self, snapshot: Any, sniff: Any) -> tuple[Any, bool]:
        return extract_with_cache(
            self._extraction_cache(),
            snapshot.snapshot_path,
            sniff.media_type,
            snapshot.sha256,
            sniff=sniff,
            limits=self.limits,
        )

    def _handle_extraction_outcome(
        self, document: Any, revision: int, extracted: Any, summary: ScanSummary
    ) -> bool:
        """Handle a failed/unsupported extraction. Returns True when analysis stops."""
        if str(extracted.state) not in _EXTRACTION_FAILURE_STATES:
            return False
        had_profile = self.repository.current_profile(document.id) is not None
        self.repository.mark_profiles_stale(
            document.id, reason=f"extraction_{extracted.state}"
        )
        state = ProcessingState.STALE if had_profile else ProcessingState.MANUAL_REVIEW
        detail = f"extraction_{extracted.state}:{extracted.detail or extracted.state}"
        self.repository.set_document_processing(document.id, state.value, detail)
        self.repository.upsert_task(
            document.id,
            task_type="manual_review",
            title="Manual review: document could not be read",
            source_revision=revision,
            origin=TaskOrigin.SYSTEM.value,
            detail=(
                "Automatic extraction did not produce usable text. The submission stays "
                "visible and needs a human to review it or supply a readable copy."
            ),
            severity="attention",
        )
        summary.parser_failures += 1
        return True

    def _record_unreadable(self, entry: Any, reason: str) -> None:
        """Register or update a file too large to process, kept visible."""
        document = self.repository.get_document_by_path(entry.rel_path)
        if document is None:
            document = self.repository.create_document(
                entry.original_filename,
                entry.rel_path,
                MediaType.UNSUPPORTED,
                entry.size_bytes,
                None,
                entry.fs_identity,
                entry.submitted_at,
            )
        self.repository.set_document_processing(document.id, ProcessingState.MANUAL_REVIEW.value, reason)
        self.repository.upsert_task(
            document.id,
            task_type="manual_review",
            title="Manual review: document exceeds the size limit",
            origin=TaskOrigin.SYSTEM.value,
            detail=(
                "The submission is larger than the configured processing limit. It stays "
                "visible and needs a human decision on how to proceed."
            ),
            severity="attention",
        )

    # ==================================================================
    # Bounded analysis scheduling
    # ==================================================================
    def _analysis_job_key(self, document_id: str, revision: int, criteria_version: int) -> str:
        return f"analysis:{document_id}:r{revision}:c{criteria_version}"

    def _enqueue_analysis(self, document: Any, revision: int, criteria_version: int) -> str | None:
        """Enqueue one analysis job for the given inputs, if the route allows it.

        The route gate runs before the job exists so an unavailable or unattested
        route produces a manual-review state, never a job that will only fail.
        """
        try:
            self.route_policy.assert_inference_allowed()
        except ResumeReviewError as exc:
            self.repository.set_document_processing(
                document.id, ProcessingState.MANUAL_REVIEW.value, exc.code
            )
            return None
        job_key = self._analysis_job_key(document.id, revision, criteria_version)
        return self.repository.enqueue_job(
            job_key,
            "analysis",
            document_id=document.id,
            input_versions={
                "source_revision": int(revision),
                "criteria_version": int(criteria_version),
            },
            priority=200,
        )

    def _schedule_analysis(self, document: Any, revision: int, summary: ScanSummary) -> None:
        """Decide whether this revision needs a model run, then enqueue it."""
        criteria_version = self.repository.active_criteria_version()
        if criteria_version < 1:
            self.repository.set_document_processing(
                document.id, ProcessingState.MANUAL_REVIEW.value, "criteria_not_approved"
            )
            self.repository.upsert_task(
                document.id,
                task_type="manual_review",
                title="Manual review: no approved criteria",
                source_revision=revision,
                origin=TaskOrigin.SYSTEM.value,
                detail=(
                    "No approved criteria version exists, so a job-match assessment cannot "
                    "be requested. The document is extracted and visible."
                ),
                severity="attention",
            )
            summary.awaiting_criteria += 1
            return

        reusable = self.repository.find_reusable_profile(
            document.id,
            int(revision),
            int(criteria_version),
            PROMPT_VERSION,
            ANALYSIS_SCHEMA_VERSION,
            self.route_value,
        )
        if reusable is not None:
            self.repository.set_document_processing(
                document.id, ProcessingState.READY.value, "cached_assessment"
            )
            summary.reused_assessments += 1
            return

        job_id = self._enqueue_analysis(document, int(revision), int(criteria_version))
        if job_id is None:
            summary.route_unavailable += 1
            return
        self.repository.set_document_processing(document.id, ProcessingState.ANALYZING.value, None)
        summary.analyses_enqueued += 1

    # ==================================================================
    # Analysis worker
    # ==================================================================
    def analyze_job(self, job: Mapping[str, Any]) -> AnalysisOutcome:
        """Run one analysis job: reuse, one bounded model call, validate, commit.

        Returns an outcome the queue maps onto the durable job lifecycle. This
        method never touches a decision or approval table; it commits only a
        profile, its evidence and suggested tasks.
        """
        job_id = str(job.get("id") or "")
        document_id = job.get("document_id")
        versions = _parse_versions(job.get("input_versions"))
        revision = int(versions.get("source_revision") or 0)
        criteria_version = int(versions.get("criteria_version") or 0)

        document = self.repository.get_document(document_id) if document_id else None
        if document is None or revision < 1:
            return AnalysisOutcome(status="skipped", detail="document_or_revision_missing")

        if (
            int(document.current_revision) != revision
            or self.repository.active_criteria_version() != criteria_version
        ):
            # The job is bound to inputs the instance has moved past. Do not run a
            # model, and do not commit against the wrong revision.
            return AnalysisOutcome(status="superseded", detail="inputs_superseded")

        reusable = self.repository.find_reusable_profile(
            document.id,
            revision,
            criteria_version,
            PROMPT_VERSION,
            ANALYSIS_SCHEMA_VERSION,
            self.route_value,
        )
        if reusable is not None:
            return AnalysisOutcome(status="reused", profile_id=reusable.id)

        try:
            self.route_policy.assert_inference_allowed()
        except ResumeReviewError as exc:
            return self._manual_review(document, revision, exc.code, status="manual_review")

        criteria = self.repository.list_criteria(criteria_version, approved_only=True)
        if not criteria:
            return self._manual_review(document, revision, "criteria_not_approved")

        spans = self._spans_for(document, revision)
        if not spans:
            return self._manual_review(document, revision, "no_spans")

        try:
            request = build_analysis_request(
                document_id=document.id,
                source_revision=revision,
                criteria_version=criteria_version,
                criteria=criteria,
                spans=spans,
            )
        except ResumeReviewError as exc:
            return self._manual_review(document, revision, exc.code)

        model_calls = 0
        request_id = f"{job_id}-1"
        try:
            completion = self.adapter.complete(request, request_id=request_id)
            model_calls += 1
        except ResumeReviewError as exc:
            return AnalysisOutcome(
                status="retry" if exc.retryable else "manual_review",
                model_calls=model_calls,
                error_code=exc.code,
                retryable=bool(exc.retryable),
                detail=exc.message,
            )

        payload, _ = _parse_json_object(completion.text)
        outcome = validate_analysis_result(
            payload=payload,
            document_id=document.id,
            source_revision=revision,
            criteria_version=criteria_version,
            criteria=criteria,
            spans=spans,
        )

        if not outcome.ok and outcome.repairable:
            # Structural problems get exactly one repair turn inside the attempt.
            repair_request = build_repair_turn(
                request,
                prior_text=completion.text,
                problem=_problem_summary(outcome),
            )
            try:
                repair_completion = self.adapter.complete(
                    repair_request, request_id=f"{job_id}-1-repair"
                )
                model_calls += 1
            except ResumeReviewError as exc:
                return AnalysisOutcome(
                    status="retry" if exc.retryable else "manual_review",
                    model_calls=model_calls,
                    error_code=exc.code,
                    retryable=bool(exc.retryable),
                    detail=exc.message,
                )
            payload, _ = _parse_json_object(repair_completion.text)
            completion = repair_completion
            outcome = validate_analysis_result(
                payload=payload,
                document_id=document.id,
                source_revision=revision,
                criteria_version=criteria_version,
                criteria=criteria,
                spans=spans,
            )

        if not outcome.ok:
            # Non-repairable, or a repair that still failed: a human, not a loop.
            code = _problem_summary(outcome)
            return self._manual_review(
                document, revision, "invalid_model_output", detail=code, model_calls=model_calls
            )

        commit = self.commit_analysis(
            outcome.result,
            run_meta=self._run_meta(completion),
            job_id=job_id,
        )
        if commit.status == "committed":
            return AnalysisOutcome(
                status="committed", model_calls=model_calls, profile_id=commit.profile_id
            )
        if commit.status == "superseded":
            return AnalysisOutcome(
                status="superseded",
                model_calls=model_calls,
                profile_id=commit.profile_id,
                detail="superseded_at_commit",
            )
        return AnalysisOutcome(status="manual_review", model_calls=model_calls, detail=commit.status)

    def _manual_review(
        self,
        document: Any,
        revision: int,
        code: str | None,
        *,
        status: str = "manual_review",
        detail: str | None = None,
        model_calls: int = 0,
    ) -> AnalysisOutcome:
        """Route a document to a human without spinning a retry loop."""
        self.repository.set_document_processing(
            document.id, ProcessingState.MANUAL_REVIEW.value, code
        )
        self.repository.upsert_task(
            document.id,
            task_type="manual_review",
            title="Manual review: assessment not produced",
            source_revision=revision,
            origin=TaskOrigin.SYSTEM.value,
            detail=(
                "The automatic assessment could not be completed and needs a human. "
                f"Reason code: {code or 'unspecified'}"
            ),
            severity="attention",
        )
        return AnalysisOutcome(
            status=status,
            model_calls=model_calls,
            error_code=code,
            detail=detail or code,
        )

    def _spans_for(self, document: Any, revision: int) -> list[Any]:
        """Load the spans recorded for a revision from the extraction cache."""
        row = self.repository.get_revision(document.id, revision)
        if row is None:
            return []
        sha = row.get("content_sha256")
        parser_name = row.get("parser_name")
        parser_version = row.get("parser_version")
        if not (sha and parser_name and parser_version):
            identity = _parser_identity(MediaType(document.media_type))
            if identity is None:
                return []
            parser_name, parser_version = identity
        cached = self._extraction_cache().get(str(sha), str(parser_name), str(parser_version))
        return list(cached.spans) if cached is not None else []

    def _run_meta(self, completion: Completion) -> dict[str, Any]:
        return {
            "prompt_version": PROMPT_VERSION,
            "model_route": completion.route or self.route_value,
            "model_version": completion.model_version,
            "validation_state": "valid",
            "validation_detail": None,
            "run_request_id": completion.request_id,
            "run_started_at": completion.run_started_at,
            "run_ended_at": completion.run_ended_at,
            "token_usage": completion.token_usage,
            "actor": "helper",
        }

    # ==================================================================
    # Commit
    # ==================================================================
    def commit_analysis(
        self, result: Any, *, run_meta: Mapping[str, Any], job_id: str | None = None
    ) -> CommitOutcome:
        """Commit a validated result, refusing to make a superseded one current.

        The live revision and the live criteria version are re-read here; if either
        has moved past the result's inputs, the result is written non-current for
        history and a fresh analysis is scheduled; it never becomes the current
        assessment (PRD 6.3).
        """
        document = self.repository.get_document(result.document_id)
        if document is None:
            return CommitOutcome(status="missing")
        active_criteria = self.repository.active_criteria_version()

        materialized = materialize_result(result, [])
        if (
            int(document.current_revision) != int(result.source_revision)
            or active_criteria != int(result.criteria_version)
        ):
            profile = self.repository.insert_profile(
                materialized, dict(run_meta), is_current=False
            )
            self._reschedule_after_supersede(document, active_criteria)
            return CommitOutcome(
                status="superseded",
                profile_id=profile.id,
                is_current=False,
                source_revision=int(result.source_revision),
            )

        profile = self.repository.insert_profile(materialized, dict(run_meta), is_current=True)

        for task in getattr(result, "suggested_tasks", []) or []:
            self.repository.upsert_task(
                document.id,
                task_type=str(getattr(task, "type", "") or "agent_suggestion"),
                title=str(getattr(task, "title", "") or "Suggested follow-up"),
                criterion_id=getattr(task, "criterion_id", None),
                source_revision=int(result.source_revision),
                origin=TaskOrigin.AGENT.value,
                detail=str(getattr(task, "detail", "") or ""),
            )

        self.repository.set_document_processing(document.id, ProcessingState.READY.value, None)
        return CommitOutcome(
            status="committed",
            profile_id=profile.id,
            is_current=True,
            source_revision=int(result.source_revision),
        )

    def _reschedule_after_supersede(self, document: Any, active_criteria: int) -> None:
        """Queue a fresh analysis for the inputs that are now live."""
        revision = int(document.current_revision or 0)
        if revision < 1 or active_criteria < 1:
            return
        try:
            job_id = self._enqueue_analysis(document, revision, active_criteria)
        except ResumeReviewError:
            return
        if job_id is not None:
            self.repository.set_document_processing(document.id, ProcessingState.ANALYZING.value, None)

    # ==================================================================
    # Publish
    # ==================================================================
    def publish_snapshot(self, *, document_ids: Sequence[str] | None = None, report_path: str | Path | None = None) -> Any:
        """Render and write the report snapshot for this instance (PRD 6.3 final)."""
        payload = build_snapshot_payload(
            self.db, mode="snapshot", document_ids=list(document_ids) if document_ids else None
        )
        target = Path(report_path) if report_path else self.root / REPORT_FILENAME
        return write_snapshot(target, payload)
