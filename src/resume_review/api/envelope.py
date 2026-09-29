"""The standard response envelope and its sanitisation rules.

Authority: PRD section 5.3 ("Machine-readable output uses ``ok``, ``code``,
``instance_id``, ``data``, ``warnings``, and ``request_id``") and section 12.2
("A standard response includes request ID, instance ID, committed state revision,
data, and warnings."). The normative shape is
``schemas/api_envelope.schema.json``; :func:`validate_envelope` checks any payload
this module builds against that file.

Two rules are enforced here rather than left to callers:

1. **Every payload is a JSON object** with ``ok`` plus the optional keys the schema
   declares, and nothing else (``additionalProperties`` is ``false``). Builders
   never invent keys.
2. **An error message is safe to display and safe to log.** :func:`sanitize_message`
   strips absolute paths, credential-looking strings, e-mail addresses, and file
   names before a message reaches an envelope. ``detail`` is structured identifier
   data and is deliberately *not* free text.

This module has no FastAPI import at module scope beyond :class:`JSONResponse`, so
the pure builders can be unit-tested without an application.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from fastapi.responses import JSONResponse

from ..models import jsonable
from ..util import new_id

__all__ = [
    "REQUEST_ID_HEADER",
    "MUTATION_HEADER_IDEMPOTENCY",
    "DEFAULT_OK_CODE",
    "ACCEPTED_CODE",
    "EnvelopeSchemaError",
    "new_request_id",
    "valid_request_id",
    "sanitize_message",
    "message_is_safe",
    "normalise_warnings",
    "ok_envelope",
    "error_envelope",
    "envelope_from_exception",
    "envelope_response",
    "ok_response",
    "error_response",
    "accepted_response",
    "validate_envelope",
    "load_envelope_schema",
]

#: Header used to propagate a caller-supplied request id and to echo the effective
#: one back on every response.
REQUEST_ID_HEADER = "X-Request-ID"

#: The retryable-request header (PRD 12.2). Re-exported here so endpoint modules
#: have a single import site for the envelope-adjacent constants.
MUTATION_HEADER_IDEMPOTENCY = "Idempotency-Key"

DEFAULT_OK_CODE = "SUCCESS"
ACCEPTED_CODE = "ACCEPTED"

#: ``code`` and the top-level ``code`` must match this pattern (schema). Note the
#: schema requires at least three characters, so ``resume_review.errors.Code.OK``
#: (the string ``"OK"``) is deliberately *not* used as an envelope code: it would
#: fail the normative pattern. ``error`` codes are all long enough already.
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

#: A request id supplied by a client must be short, opaque, and boring. Anything
#: else is replaced with a freshly generated id rather than echoed.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_MAX_MESSAGE_LENGTH = 500


class EnvelopeSchemaError(ValueError):
    """A built envelope does not satisfy ``api_envelope.schema.json``.

    This is a programming error in the helper, not a client error, so it is never
    mapped onto an HTTP status; tests raise it and fail loudly.
    """


# ---------------------------------------------------------------------------
# Message sanitisation
# ---------------------------------------------------------------------------
#: Credential-shaped strings. Ordered so a scheme prefix is redacted before the
#: assignment rule can split it.
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}"), "<redacted>"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
            r"secret|authorization|credential)\b\s*[:=]\s*\S+"
        ),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)[?&](?:api[_-]?key|access[_-]?token|token|password|secret)=[^&\s\"']+"), "=<redacted>"),
)

#: E-mail addresses. A candidate name commonly travels in one.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Absolute filesystem paths, in the three forms this application can produce.
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+")
_UNC_PATH_RE = re.compile(r"\\\\[^\s\"'<>|]+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:./])/(?:[^\s/:\\\"'<>|]+/)+[^\s/\\\"'<>|]*")

#: Document file names. Replaced wholesale: a real file name often *is* a
#: candidate name, which is exactly what must never appear in an error message.
_FILENAME_RE = re.compile(r"(?i)\b[\w][\w ().,'\-]{0,80}\.(?:pdf|docx|doc|txt|rtf|odt|pages)\b")

_REDACTION_ORDER: tuple[tuple[re.Pattern[str], str], ...] = (
    *_CREDENTIAL_PATTERNS,
    (_UNC_PATH_RE, "<path>"),
    (_WINDOWS_PATH_RE, "<path>"),
    (_POSIX_PATH_RE, "<path>"),
    (_EMAIL_RE, "<email>"),
    (_FILENAME_RE, "<filename>"),
)


def sanitize_message(
    message: Any,
    *,
    names: Iterable[str] = (),
    max_length: int = _MAX_MESSAGE_LENGTH,
) -> str:
    """Return a display-safe form of ``message``.

    ``names`` lets a caller who *does* know a sensitive literal (for example a
    document display name it just handled) redact every occurrence of it. The
    regex sweep covers the three categories the PRD names unconditionally: an
    absolute path, a credential, and a file name that may carry a candidate name.
    """
    if message is None:
        return ""
    text = message if isinstance(message, str) else str(message)
    for literal in names:
        if literal:
            text = text.replace(str(literal), "<redacted>")
    for pattern, replacement in _REDACTION_ORDER:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def message_is_safe(message: str) -> bool:
    """True when ``message`` is already free of paths, credentials, and file names.

    Used by tests and by :func:`envelope_from_exception` as a self-check. A message
    that changes under :func:`sanitize_message` is not safe.
    """
    return sanitize_message(message) == " ".join(str(message).split())


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
def normalise_warnings(warnings: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Coerce warning inputs into the schema's warning objects.

    Accepts :class:`resume_review.models.Warning`, a mapping, a plain string (its
    ``code``), or any object exposing ``code``/``message``/``detail`` attributes.
    Only ``code`` is required by the schema.
    """
    out: list[dict[str, Any]] = []
    for item in warnings or ():
        if item is None:
            continue
        if isinstance(item, str):
            entry: dict[str, Any] = {"code": item}
        elif isinstance(item, Mapping):
            entry = {
                "code": str(item.get("code", "WARNING")),
                "message": str(item.get("message", "")),
                "detail": dict(item.get("detail", {}) or {}),
            }
        else:
            entry = {
                "code": str(getattr(item, "code", "WARNING")),
                "message": str(getattr(item, "message", "") or ""),
                "detail": dict(getattr(item, "detail", {}) or {}),
            }
        if not entry["message"]:
            entry.pop("message", None)
        if not entry["detail"]:
            entry.pop("detail", None)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def new_request_id() -> str:
    """A fresh opaque request id (``req_<uuid>``)."""
    return new_id("request")


def valid_request_id(value: Any) -> bool:
    """Whether a caller-supplied request id is acceptable to echo back."""
    return isinstance(value, str) and bool(_REQUEST_ID_RE.match(value))


def _base(
    *,
    ok: bool,
    code: str,
    request_id: str,
    instance_id: str | None,
    state_revision: int | None,
    warnings: Iterable[Any] | None,
    job_id: str | None,
) -> dict[str, Any]:
    if not _CODE_RE.match(str(code)):
        raise EnvelopeSchemaError(f"code {code!r} does not match ^[A-Z][A-Z0-9_]{{2,63}}$")
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "code": str(code),
        "instance_id": instance_id,
        "request_id": str(request_id),
        "state_revision": state_revision,
        "data": None,
        "warnings": normalise_warnings(warnings),
    }
    if job_id is not None:
        payload["job_id"] = str(job_id)
    return payload


def ok_envelope(
    data: Any = None,
    *,
    request_id: str,
    code: str = DEFAULT_OK_CODE,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
    job_id: str | None = None,
) -> dict[str, Any]:
    """Build a success envelope. ``data`` is JSON-coerced via ``models.jsonable``."""
    payload = _base(
        ok=True,
        code=code,
        request_id=request_id,
        instance_id=instance_id,
        state_revision=state_revision,
        warnings=warnings,
        job_id=job_id,
    )
    payload["data"] = jsonable(data)
    return payload


def error_envelope(
    code: str,
    message: str,
    *,
    request_id: str,
    detail: Mapping[str, Any] | None = None,
    retryable: bool = False,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
    job_id: str | None = None,
) -> dict[str, Any]:
    """Build a failure envelope. ``message`` is always passed through sanitisation."""
    error: dict[str, Any] = {
        "code": str(code),
        "message": sanitize_message(message),
        "retryable": bool(retryable),
    }
    if detail:
        error["detail"] = jsonable(dict(detail))
    payload = _base(
        ok=False,
        code=code,
        request_id=request_id,
        instance_id=instance_id,
        state_revision=state_revision,
        warnings=warnings,
        job_id=job_id,
    )
    payload["error"] = error
    return payload


def envelope_from_exception(
    exc: BaseException,
    *,
    request_id: str,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
) -> dict[str, Any]:
    """Build a failure envelope from a :class:`~resume_review.errors.ResumeReviewError`.

    Falls back to a generic internal error for any other exception, so a raw
    traceback can never be reflected into the response.
    """
    from ..errors import Code, ResumeReviewError

    if isinstance(exc, ResumeReviewError):
        return error_envelope(
            getattr(exc, "code", Code.INTERNAL_ERROR),
            getattr(exc, "message", str(exc)),
            request_id=request_id,
            detail=getattr(exc, "detail", None),
            retryable=bool(getattr(exc, "retryable", False)),
            instance_id=instance_id,
            state_revision=state_revision,
            warnings=warnings,
        )
    return error_envelope(
        Code.INTERNAL_ERROR,
        "An unexpected error occurred.",
        request_id=request_id,
        instance_id=instance_id,
        state_revision=state_revision,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Response wrappers
# ---------------------------------------------------------------------------
def envelope_response(
    payload: Mapping[str, Any],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Wrap a built envelope in a JSON response, asserting schema conformance."""
    validate_envelope(payload)
    return JSONResponse(status_code=status_code, content=dict(payload), headers=dict(headers or {}))


def ok_response(
    data: Any = None,
    *,
    request_id: str,
    code: str = DEFAULT_OK_CODE,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
    status_code: int = 200,
    job_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A 200 (or caller-chosen) success response."""
    return envelope_response(
        ok_envelope(
            data,
            request_id=request_id,
            code=code,
            instance_id=instance_id,
            state_revision=state_revision,
            warnings=warnings,
            job_id=job_id,
        ),
        status_code=status_code,
        headers=headers,
    )


def error_response(
    code: str,
    message: str,
    *,
    request_id: str,
    status_code: int,
    detail: Mapping[str, Any] | None = None,
    retryable: bool = False,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A failure response carrying the mapped status code."""
    return envelope_response(
        error_envelope(
            code,
            message,
            request_id=request_id,
            detail=detail,
            retryable=retryable,
            instance_id=instance_id,
            state_revision=state_revision,
            warnings=warnings,
        ),
        status_code=status_code,
        headers=headers,
    )


def accepted_response(
    data: Any = None,
    *,
    job_id: str,
    request_id: str,
    code: str = ACCEPTED_CODE,
    instance_id: str | None = None,
    state_revision: int | None = None,
    warnings: Iterable[Any] | None = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A 202 response carrying a durable job id (PRD 12.2: "Long work returns 202")."""
    return envelope_response(
        ok_envelope(
            data,
            request_id=request_id,
            code=code,
            instance_id=instance_id,
            state_revision=state_revision,
            warnings=warnings,
            job_id=job_id,
        ),
        status_code=202,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "api_envelope.schema.json"
_SCHEMA_CACHE: dict[str, Any] = {}


def load_envelope_schema() -> dict[str, Any]:
    """Load and cache the normative envelope schema from the repository."""
    if "schema" not in _SCHEMA_CACHE:
        _SCHEMA_CACHE["schema"] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE["schema"]


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES.get(expected, ()))


def _validate_node(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if "type" in schema:
        expected = schema["type"]
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, cand) for cand in candidates):
            errors.append(f"{path}: expected {expected!r}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} is not one of {schema['enum']!r}")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            errors.append(f"{path}: {value!r} is below minimum {schema['minimum']!r}")
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
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_node(item, schema["items"], f"{path}[{index}]", errors)


def validate_envelope(payload: Any, schema: Mapping[str, Any] | None = None) -> None:
    """Raise :class:`EnvelopeSchemaError` unless ``payload`` satisfies the schema.

    A compact validator for exactly the JSON-Schema keywords the envelope schema
    uses. It is intentionally not a general-purpose implementation; it exists so the
    builders can be checked against the normative file without a new dependency.
    """
    active = schema if schema is not None else load_envelope_schema()
    errors: list[str] = []
    _validate_node(payload, active, "$", errors)
    if errors:
        raise EnvelopeSchemaError("; ".join(errors))
