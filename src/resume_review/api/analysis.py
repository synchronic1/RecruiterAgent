"""Analysis endpoints: queue bounded work and accept a leased result.

Authority: PRD section 12.1 (endpoint surface), section 12.2 (202 with a durable
job id; ``Idempotency-Key`` for retryable POSTs), section 7.1 (two-stage analysis),
and section 7.2 (the helper can verify schema, permitted criterion IDs, source
revision, span existence, and quote occurrence -- nothing more).

Three separations are load-bearing here and each has a test:

1. **Queue is not run.** ``POST /analysis/jobs`` enqueues a durable job bound to
   the current revision and the *active approved* criteria version and returns 202.
   It refuses to queue anything when no criteria version is approved
   (``CRITERIA_NOT_APPROVED``), so an unapproved proposal can never drive analysis.
2. **The result route is a worker route.** ``POST /analysis/results`` accepts a
   payload only from the identity that holds the lease: the principal must be a
   bound worker (``worker:``), the lease must be live, the presented lease token
   must match the stored one in constant time, and the lease owner must equal the
   worker's actor reference. A reviewer session, a stale lease, or a job leased to
   another worker is refused.
3. **Model output is data.** A submitted payload is checked against
   ``schemas/analysis_result.schema.json`` and then against the bound inputs
   (permitted criterion IDs, revision, criteria version, span ids, quoted text)
   before anything is written. A payload computed for superseded inputs is refused;
   it never becomes the current assessment.

The route never activates a criteria version, never sets a review decision, and
never takes a storage path from the payload: the document comes from the leased
job, and the profile is written through the repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..analysis.pipeline import materialize_result
from ..analysis.validate import ProblemCode, validate_analysis_result
from ..auth import require_role
from ..db import RevisionConflict
from ..errors import (
    Code,
    Conflict,
    Forbidden,
    NotFound,
    ResumeReviewError,
    ValidationFailed,
)
from ..ingest import extract_docx, extract_pdf, extract_txt
from ..ingest.extract import ExtractionCache
from ..models import MediaType, ModelRoute, ProcessingState, Role, TaskOrigin
from ..openclaw_adapter.prompts import PROMPT_VERSION
from ..util import constant_time_equals
from .deps import InstanceContext, get_request_id, require_mutation
from .envelope import accepted_response, ok_response, sanitize_message
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = [
    "AnalysisJobsRequest",
    "AnalysisResultSubmission",
    "load_analysis_schema",
    "validate_against_schema",
    "register",
]

_JOBS_ROUTE = "analysis.jobs"
_RESULTS_ROUTE = "analysis.results"

#: States in which a job holds a live lease a worker may submit against.
_LEASED_STATES = frozenset({"leased", "running"})

#: Result problem code -> HTTP 403 when the payload is bound to superseded inputs,
#: 422 otherwise. Ordered: the first code present decides the reported reason.
_PROBLEM_RESPONSE: tuple[tuple[str, int], ...] = (
    (ProblemCode.CRITERIA_VERSION_MISMATCH, 403),
    (ProblemCode.SPAN_NOT_FOUND, 422),
    (ProblemCode.QUOTE_NOT_FOUND, 422),
    (ProblemCode.UNKNOWN_CRITERION, 422),
    (ProblemCode.REVISION_MISMATCH, 422),
    (ProblemCode.SCHEMA_VERSION, 422),
)

_SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parents[1] / "schemas" / "analysis_result.schema.json",
    Path(__file__).resolve().parents[3] / "schemas" / "analysis_result.schema.json",
)
_SCHEMA_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class AnalysisJobsRequest(BaseModel):
    """A bounded set of documents to analyse. No endpoint, model, or path field."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0)


class AnalysisRunMeta(BaseModel):
    """Worker-attested run metadata.

    These values describe the run the worker performed. They are recorded as the
    worker's attestation, never as proof: the helper attaches its own prompt
    version and validation state, and normalizes an unrecognized route to
    ``unavailable`` rather than trusting a free string.
    """

    model_config = ConfigDict(extra="forbid")

    model_route: str | None = None
    model_version: str | None = None
    run_request_id: str | None = None
    run_started_at: str | None = None
    run_ended_at: str | None = None
    token_usage: int | None = Field(default=None, ge=0)


class AnalysisResultSubmission(BaseModel):
    """One leased result, bound to the job and lease it was produced under."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=1, max_length=200)
    result: dict[str, Any]
    run: AnalysisRunMeta | None = None


# ---------------------------------------------------------------------------
# Schema validation (compact, dependency-free)
# ---------------------------------------------------------------------------
def load_analysis_schema() -> dict[str, Any]:
    """Load and cache the normative analysis-result schema from the repository."""
    if "schema" not in _SCHEMA_CACHE:
        for candidate in _SCHEMA_CANDIDATES:
            if candidate.is_file():
                _SCHEMA_CACHE["schema"] = json.loads(candidate.read_text(encoding="utf-8"))
                break
        else:  # pragma: no cover - the schema is part of the package
            raise ResumeReviewError(
                "The analysis result schema is missing from the installation.",
                code=Code.INTERNAL_ERROR,
            )
    return _SCHEMA_CACHE["schema"]


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    mapping: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "null": type(None),
    }
    python_type = mapping.get(expected)
    return python_type is not None and isinstance(value, python_type)


def _validate_node(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    """Check ``value`` against one schema node.

    A compact validator for exactly the JSON-Schema keywords the analysis-result
    schema uses (``type``, ``const``, ``enum``, ``minLength``, ``maxLength``,
    ``minimum``, ``minItems``, ``maxItems``, ``required``, ``properties``,
    ``additionalProperties``, ``items``). It is intentionally not general-purpose;
    it exists so the endpoint can enforce the normative file without a new runtime
    dependency.
    """
    if "type" in schema:
        expected = schema["type"]
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, candidate) for candidate in candidates):
            errors.append(f"{path}: expected {expected!r}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not one of the permitted values")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']!r}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required_key in schema.get("required", []):
            if required_key not in value:
                errors.append(f"{path}: missing required key {required_key!r}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in properties:
                _validate_node(item, properties[key], child, errors)
            elif additional is False:
                errors.append(f"{child}: additional property is not allowed")
            elif isinstance(additional, dict):
                _validate_node(item, additional, child, errors)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_node(item, items, f"{path}[{index}]", errors)


def validate_against_schema(payload: Any, schema: Mapping[str, Any]) -> list[str]:
    """Return the schema violations of ``payload``, or an empty list.

    Messages name a JSON path and a structural reason only; they never echo the
    offending text, so a violation is safe to return and to log.
    """
    errors: list[str] = []
    _validate_node(payload, schema, "$", errors)
    return errors


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_versions(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _parser_identity(media_type: MediaType) -> tuple[str, str] | None:
    """The parser name and version for a media type, mirroring the extractor."""
    if media_type is MediaType.PDF:
        return extract_pdf.PARSER_NAME, extract_pdf.parser_version()
    if media_type is MediaType.DOCX:
        return extract_docx.PARSER_NAME, extract_docx.parser_version()
    if media_type is MediaType.TXT:
        return extract_txt.PARSER_NAME, extract_txt.parser_version()
    return None


def _load_spans(repository: Any, document: Any, revision: int) -> list[Any]:
    """Load the spans recorded for a revision from the extraction cache."""
    row = repository.get_revision(document.id, revision)
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
    cache = ExtractionCache(repository.db.connect(), repository.instance_id)
    cached = cache.get(str(sha), str(parser_name), str(parser_version))
    return list(cached.spans) if cached is not None else []


def _problem_detail(outcome: Any) -> list[dict[str, Any]]:
    """The safe view of a validation outcome: code, path, and a sanitized reason."""
    return [
        {
            "code": str(problem.code),
            "path": sanitize_message(problem.path),
            "detail": sanitize_message(problem.detail),
            "repairable": bool(problem.repairable),
        }
        for problem in list(outcome.problems)[:20]
    ]


def _analysis_error(outcome: Any) -> ResumeReviewError:
    """Map a failed validation outcome onto one error, preserving the reason."""
    codes = {str(problem.code) for problem in outcome.problems}
    detail = {"problems": _problem_detail(outcome)}
    for code, status in _PROBLEM_RESPONSE:
        if code not in codes:
            continue
        if status == 403:
            return Forbidden(
                "The result is bound to inputs that have been superseded.",
                code=code,
                detail=detail,
            )
        return ValidationFailed(
            "The submitted result did not pass evidence validation.",
            code=code,
            detail=detail,
        )
    return ValidationFailed(
        "The submitted result did not pass validation.",
        code=Code.ANALYSIS_SCHEMA_INVALID,
        detail=detail,
    )


def _run_meta(run: AnalysisRunMeta | None, actor: str) -> dict[str, Any]:
    route: str | None = None
    if run is not None and run.model_route:
        try:
            route = ModelRoute(str(run.model_route)).value
        except ValueError:
            route = None
    return {
        "prompt_version": PROMPT_VERSION,
        "model_route": route or ModelRoute.UNAVAILABLE.value,
        "model_version": run.model_version if run else None,
        "validation_state": "valid",
        "validation_detail": None,
        "run_request_id": run.run_request_id if run else None,
        "run_started_at": run.run_started_at if run else None,
        "run_ended_at": run.run_ended_at if run else None,
        "token_usage": run.token_usage if run else None,
        "actor": actor,
    }


def _job_not_found() -> NotFound:
    return NotFound("That job does not exist.", detail={"entity": "job"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def register(router: APIRouter) -> None:
    @router.post("/analysis/jobs", name="analysis_jobs", response_class=JSONResponse)
    def queue_analysis(
        payload: AnalysisJobsRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard(_JOBS_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        body = payload.model_dump()

        hit = guard.replay(body)
        if hit is not None:
            return accepted_response(
                hit.response,
                job_id=hit.job_id,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        if payload.expected_revision is not None and int(payload.expected_revision) != ctx.state_revision:
            raise RevisionConflict(
                "The instance changed since this request was prepared.",
                current_revision=ctx.state_revision,
            )

        criteria_version = ctx.repository.active_criteria_version()
        if criteria_version < 1:
            raise ValidationFailed(
                "No approved criteria version exists, so analysis cannot be queued.",
                code=Code.CRITERIA_NOT_APPROVED,
                detail={"reason": "criteria_not_approved"},
            )

        planned: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for document_id in payload.document_ids:
            document = ctx.repository.get_document(document_id)
            if document is None or document.instance_id != ctx.instance_id:
                raise NotFound("That document does not exist.", detail={"entity": "document"})
            revision = int(document.current_revision or 0)
            if revision < 1:
                warnings.append(
                    {
                        "code": "DOCUMENT_HAS_NO_REVISION",
                        "message": "The document has no registered revision to analyse.",
                        "detail": {"document_id": document.id},
                    }
                )
                continue
            job_key = f"analysis:{document.id}:r{revision}:c{criteria_version}"
            job_id = ctx.repository.enqueue_job(
                job_key,
                "analysis",
                document_id=document.id,
                input_versions={
                    "source_revision": revision,
                    "criteria_version": criteria_version,
                    "requested_by": ctx.principal.actor_ref,
                },
                priority=200,
            )
            ctx.repository.set_document_processing(document.id, ProcessingState.ANALYZING.value, None)
            planned.append(
                {
                    "document_id": document.id,
                    "job_id": job_id,
                    "source_revision": revision,
                    "criteria_version": criteria_version,
                }
            )

        if not planned:
            result = {"queued": 0, "jobs": [], "criteria_version": criteria_version}
            guard.commit(body, response=result)
            return ok_response(
                result,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=ctx.state_revision,
                warnings=warnings,
            )

        result = {"queued": len(planned), "jobs": planned, "criteria_version": criteria_version}
        guard.commit(body, response=result, job_id=planned[0]["job_id"])
        return accepted_response(
            result,
            job_id=planned[0]["job_id"],
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=warnings,
        )

    @router.post("/analysis/results", name="analysis_results", response_class=JSONResponse)
    def submit_analysis_result(
        payload: AnalysisResultSubmission,
        guard: IdempotencyGuard = Depends(idempotency_guard(_RESULTS_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        principal = ctx.principal
        if not principal.is_worker:
            raise Forbidden(
                "Only the bound worker identity may submit a leased analysis result.",
                code=Code.FORBIDDEN,
                detail={"reason": "worker_identity_required"},
            )

        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
                job_id=hit.job_id,
            )

        job = ctx.repository.get_job(payload.job_id)
        if (
            job is None
            or str(job.get("instance_id") or "") != ctx.instance_id
            or str(job.get("kind") or "") != "analysis"
        ):
            raise _job_not_found()

        if str(job.get("state") or "") not in _LEASED_STATES:
            raise Conflict(
                "This job does not hold a live lease.",
                code=Code.LEASE_HELD,
                detail={"reason": "job_not_leased"},
            )

        stored_token = job.get("lease_token")
        if not isinstance(stored_token, str) or not constant_time_equals(stored_token, payload.lease_token):
            raise Forbidden(
                "The lease token does not match the job's active lease.",
                code=Code.FORBIDDEN,
                detail={"reason": "lease_token_mismatch"},
            )

        if str(job.get("lease_owner") or "") != principal.actor_ref:
            raise Forbidden(
                "This job is leased to a different worker.",
                code=Code.FORBIDDEN,
                detail={"reason": "lease_owner_mismatch"},
            )

        document_id = str(job.get("document_id") or "")
        versions = _parse_versions(job.get("input_versions"))
        source_revision = int(versions.get("source_revision") or 0)
        criteria_version = int(versions.get("criteria_version") or 0)
        if not document_id or source_revision < 1 or criteria_version < 1:
            raise Conflict(
                "This job is not bound to analysable inputs.",
                code=Code.LEASE_HELD,
                detail={"reason": "job_inputs_missing"},
            )

        document = ctx.repository.get_document(document_id)
        if document is None:
            raise NotFound("That document does not exist.", detail={"entity": "document"})

        active_criteria = ctx.repository.active_criteria_version()
        if int(document.current_revision) != source_revision or active_criteria != criteria_version:
            raise Forbidden(
                "The inputs this job was computed against have been superseded.",
                code=Code.ANALYSIS_STALE_RESULT,
                detail={
                    "reason": "inputs_superseded",
                    "active_criteria_version": active_criteria,
                    "current_revision": int(document.current_revision),
                },
            )

        schema_problems = validate_against_schema(payload.result, load_analysis_schema())
        if schema_problems:
            raise ValidationFailed(
                "The submitted result does not match the analysis result schema.",
                code=Code.ANALYSIS_SCHEMA_INVALID,
                detail={"problems": [sanitize_message(item) for item in schema_problems[:20]]},
            )

        criteria = ctx.repository.list_criteria(criteria_version, approved_only=True)
        spans = _load_spans(ctx.repository, document, source_revision)
        outcome = validate_analysis_result(
            payload=payload.result,
            document_id=document_id,
            source_revision=source_revision,
            criteria_version=criteria_version,
            criteria=criteria,
            spans=spans,
        )
        if not outcome.ok:
            raise _analysis_error(outcome)

        materialized = materialize_result(outcome.result, spans)
        profile = ctx.repository.insert_profile(
            materialized,
            _run_meta(payload.run, principal.actor_ref),
            is_current=True,
        )
        for task in materialized.suggested_tasks or []:
            ctx.repository.upsert_task(
                document.id,
                task_type=str(getattr(task, "type", "") or "agent_suggestion"),
                title=str(getattr(task, "title", "") or "Suggested follow-up"),
                criterion_id=getattr(task, "criterion_id", None),
                source_revision=source_revision,
                origin=TaskOrigin.AGENT.value,
                detail=str(getattr(task, "detail", "") or ""),
            )
        ctx.repository.set_document_processing(document.id, ProcessingState.READY.value, None)
        ctx.repository.complete_job(payload.job_id, result_ref=profile.id)

        data = {
            "status": "committed",
            "job_id": payload.job_id,
            "document_id": document.id,
            "profile_id": profile.id,
            "source_revision": source_revision,
            "criteria_version": criteria_version,
        }
        guard.commit(body, response=data, job_id=payload.job_id)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            job_id=payload.job_id,
        )
