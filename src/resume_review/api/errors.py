"""Exception-to-status mapping and the application's error handlers.

Authority: PRD section 12.2 ("Use 401/403 for authentication/authorization
failure, 409 for stale state or conflicting idempotency reuse, 422 for invalid
application data, and 503 for unavailable dependencies. Long work returns 202 with
a durable job ID.") and section 5.3.

The mapping is deliberately *code first*: the machine-readable
:class:`resume_review.errors.Code` decides the status, and the exception class's
own ``http_status`` is only a fallback. That means a genuine
``REVISION_CONFLICT`` raised through the generic base class still answers 409, and
adding a new error code without touching this module degrades to the class default
rather than to a guess.

Nothing here reflects a traceback or a raw message. Every handler builds its body
through :mod:`resume_review.api.envelope`, which sanitises the message.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import Code, ResumeReviewError
from . import envelope as env

__all__ = [
    "HTTP_STATUS_BY_CODE",
    "status_for_error",
    "error_code_for",
    "retryable_for",
    "register_exception_handlers",
    "exception_response",
]

_log = logging.getLogger("resume_review.api")


#: Machine-readable code -> HTTP status, for the groups PRD 12.2 names explicitly.
#: Codes absent from this table fall back to the exception's own ``http_status``.
HTTP_STATUS_BY_CODE: dict[str, int] = {
    # 401 - not authenticated
    Code.UNAUTHENTICATED: 401,
    Code.SESSION_EXPIRED: 401,
    Code.PAIRING_TOKEN_INVALID: 401,
    Code.PAIRING_TOKEN_EXPIRED: 401,
    # 403 - authenticated but not allowed (wrong role or wrong instance)
    Code.FORBIDDEN: 403,
    Code.ROLE_INSUFFICIENT: 403,
    Code.CSRF_FAILED: 403,
    Code.ORIGIN_REJECTED: 403,
    Code.HOST_REJECTED: 403,
    Code.INSTANCE_MISMATCH: 403,
    Code.INSTANCE_NOT_OWNED: 403,
    Code.INSTANCE_LOCKED_BY_OTHER_OWNER: 403,
    Code.MANIFEST_UNTRUSTED: 403,
    Code.ANALYSIS_STALE_RESULT: 403,
    # 404 - absent
    Code.NOT_FOUND: 404,
    Code.INSTANCE_NOT_FOUND: 404,
    Code.FILE_MISSING: 404,
    # 405 - wrong method
    Code.METHOD_NOT_ALLOWED: 405,
    # 409 - stale state or a conflicting reuse
    Code.REVISION_CONFLICT: 409,
    Code.IDEMPOTENCY_KEY_REUSED: 409,
    Code.PLAN_HASH_MISMATCH: 409,
    Code.APPROVAL_REQUIRED: 409,
    Code.APPROVAL_EXPIRED: 409,
    Code.PLAN_STALE: 409,
    Code.DESTINATION_COLLISION: 409,
    Code.SOURCE_CHANGED: 409,
    Code.SOURCE_MISSING: 409,
    Code.BATCH_ALREADY_STARTED: 409,
    Code.BATCH_ALREADY_COMPLETED: 409,
    Code.BATCH_PARTIAL: 409,
    Code.SETUP_COLLISION: 409,
    Code.NEEDS_RECONCILIATION: 409,
    Code.INTENT_FROZEN: 409,
    Code.DECISION_NEEDS_RECHECK: 409,
    Code.LEASE_HELD: 409,
    Code.INSTANCE_ALREADY_EXISTS: 409,
    Code.WORKSPACE_UNINITIALISED: 409,
    Code.MANIFEST_MISMATCH: 409,
    Code.UNSUPPORTED_STORAGE_TOPOLOGY: 409,
    Code.NETWORK_FILESYSTEM_DATABASE: 409,
    Code.CROSS_VOLUME_MOVE: 409,
    Code.NO_CLOBBER_UNSUPPORTED: 409,
    Code.DOWNGRADE_REFUSED: 409,
    Code.SCHEMA_VERSION_UNSUPPORTED: 409,
    # 422 - invalid application data
    Code.INVALID_INPUT: 422,
    Code.VALIDATION_FAILED: 422,
    Code.CRITERIA_NOT_APPROVED: 422,
    Code.ANALYSIS_SCHEMA_INVALID: 422,
    Code.ANALYSIS_UNKNOWN_CRITERION: 422,
    Code.ANALYSIS_REVISION_MISMATCH: 422,
    Code.EVIDENCE_QUOTE_NOT_FOUND: 422,
    Code.EVIDENCE_SPAN_NOT_FOUND: 422,
    Code.APPROVAL_MUST_BE_HUMAN: 422,
    Code.FILTER_TOO_DEEP: 422,
    Code.FILTER_TOO_WIDE: 422,
    Code.FILTER_FIELD_NOT_ALLOWED: 422,
    Code.FILTER_OPERATOR_NOT_ALLOWED: 422,
    Code.FILTER_UNKNOWN_VALUE: 422,
    Code.FILTER_INVALID_FOR_CRITERIA: 422,
    Code.CHAT_SCOPE_NOT_BOUND: 422,
    Code.UNSUPPORTED_FORMAT: 422,
    Code.FILE_TOO_LARGE: 422,
    Code.CHARACTER_LIMIT_EXCEEDED: 422,
    Code.TOO_MANY_PAGES: 422,
    Code.ENCRYPTED_DOCUMENT: 422,
    Code.SCAN_ONLY_DOCUMENT: 422,
    Code.PATH_ESCAPE: 422,
    Code.SYMLINK_REJECTED: 422,
    Code.PLAN_EMPTY: 422,
    # 429 - throttled
    Code.RATE_LIMITED: 429,
    # 503 - unavailable dependency
    Code.ROUTE_UNAVAILABLE: 503,
    Code.ROUTE_NOT_RESTRICTED: 503,
    Code.ROUTE_POLICY_VIOLATION: 503,
    Code.LOCAL_ONLY_FALLBACK_BLOCKED: 503,
    Code.ADAPTER_TIMEOUT: 503,
    Code.ADAPTER_BAD_RESPONSE: 503,
    Code.EXTRACTION_FAILED: 503,
    Code.MANIFEST_MISSING: 503,
    Code.INSUFFICIENT_SPACE: 503,
    Code.READ_ONLY_LOCATION: 503,
}

#: Status -> code, for a framework-raised ``HTTPException`` that carries no
#: application code.
_STATUS_TO_CODE: dict[int, str] = {
    400: Code.INVALID_INPUT,
    401: Code.UNAUTHENTICATED,
    403: Code.FORBIDDEN,
    404: Code.NOT_FOUND,
    405: Code.METHOD_NOT_ALLOWED,
    409: Code.REVISION_CONFLICT,
    413: Code.FILE_TOO_LARGE,
    415: Code.UNSUPPORTED_FORMAT,
    422: Code.VALIDATION_FAILED,
    429: Code.RATE_LIMITED,
    503: Code.ROUTE_UNAVAILABLE,
}


def error_code_for(exc: BaseException) -> str:
    """The machine-readable code for ``exc`` (``INTERNAL_ERROR`` when absent)."""
    code = getattr(exc, "code", None)
    return str(code) if code else Code.INTERNAL_ERROR


def status_for_error(exc: BaseException) -> int:
    """The HTTP status for ``exc``.

    The code table wins over the class attribute so a specific code is never
    mislabelled by a generic base class. An out-of-range ``http_status`` degrades
    to 500 rather than producing an invalid response.
    """
    code = getattr(exc, "code", None)
    if code is not None and str(code) in HTTP_STATUS_BY_CODE:
        return HTTP_STATUS_BY_CODE[str(code)]
    if isinstance(exc, StarletteHTTPException) and isinstance(exc.status_code, int):
        return int(exc.status_code)
    status = getattr(exc, "http_status", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    return 500


def retryable_for(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False))


# ---------------------------------------------------------------------------
# Request context for a handler
# ---------------------------------------------------------------------------
def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else env.new_request_id()


def _instance_id(request: Request) -> str | None:
    value = getattr(request.state, "instance_id", None)
    if value:
        return str(value)
    path_params = getattr(request, "path_params", None) or {}
    candidate = path_params.get("instance_id")
    return str(candidate) if candidate else None


def _state_revision(request: Request) -> int | None:
    value = getattr(request.state, "state_revision", None)
    if isinstance(value, int):
        return value
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        try:
            return int(runtime.db.state_revision())
        except Exception:  # pragma: no cover - the database is expected to be up
            return None
    return None


def _envelope_for(request: Request, exc: BaseException) -> dict[str, Any]:
    return env.envelope_from_exception(
        exc,
        request_id=_request_id(request),
        instance_id=_instance_id(request),
        state_revision=_state_revision(request),
    )


def exception_response(request: Request, exc: BaseException) -> JSONResponse:
    """Build the JSON response for ``exc``. Used by handlers and by middleware.

    Middleware runs outside Starlette's ``ExceptionMiddleware``, so a transport
    guard that raises there must be turned into a response here rather than
    re-raised.
    """
    status = status_for_error(exc)
    payload = _envelope_for(request, exc)
    headers = {env.REQUEST_ID_HEADER: payload["request_id"]}
    return JSONResponse(status_code=status, content=payload, headers=headers)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _validation_detail(exc: RequestValidationError) -> list[dict[str, Any]]:
    """A safe view of a pydantic error: location, type, and message only.

    The raw ``input`` and ``ctx`` are dropped on purpose; a validation error over a
    request body can otherwise echo applicant-supplied text straight back.
    """
    detail: list[dict[str, Any]] = []
    for item in exc.errors()[:20]:
        if not isinstance(item, Mapping):
            continue
        loc = item.get("loc", ())
        detail.append(
            {
                "loc": [str(part) for part in loc] if isinstance(loc, (list, tuple)) else [str(loc)],
                "type": str(item.get("type", "invalid")),
                "msg": env.sanitize_message(item.get("msg", "Invalid value.")),
            }
        )
    return detail


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the envelope-producing handlers to ``app``."""

    @app.exception_handler(ResumeReviewError)
    async def _handle_resume_review(request: Request, exc: ResumeReviewError) -> JSONResponse:
        return exception_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = _request_id(request)
        payload = env.error_envelope(
            Code.VALIDATION_FAILED,
            "The request did not match the expected shape.",
            request_id=request_id,
            detail={"errors": _validation_detail(exc)},
            instance_id=_instance_id(request),
            state_revision=_state_revision(request),
        )
        return JSONResponse(
            status_code=422,
            content=payload,
            headers={env.REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = int(exc.status_code)
        code = _STATUS_TO_CODE.get(status, Code.INVALID_INPUT if 400 <= status < 500 else Code.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        request_id = _request_id(request)
        payload = env.error_envelope(
            code,
            message,
            request_id=request_id,
            instance_id=_instance_id(request),
            state_revision=_state_revision(request),
        )
        return JSONResponse(
            status_code=status,
            content=payload,
            headers={env.REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        # Log identifiers only: never a traceback body and never request text.
        _log.error(
            "unhandled error request_id=%s type=%s",
            request_id,
            type(exc).__name__,
        )
        payload = env.error_envelope(
            Code.INTERNAL_ERROR,
            "An unexpected error occurred.",
            request_id=request_id,
            instance_id=_instance_id(request),
            state_revision=_state_revision(request),
        )
        return JSONResponse(
            status_code=500,
            content=payload,
            headers={env.REQUEST_ID_HEADER: request_id},
        )
