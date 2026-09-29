"""Stable error codes and process exit codes.

Authority: PRD section 5.3 ("Machine-readable output uses ok, code, instance_id, data,
warnings, and request_id. Define stable exit codes for success, invalid input,
permission failure, conflict, dependency failure, and unsupported storage.").

Two rules govern everything in this module:

1. Error codes are API. Once published they are never repurposed. Adding is fine;
   changing the meaning of an existing code is a breaking change.
2. Error telemetry never encodes candidate names, root paths, or credentials.
   ``message`` is safe for display and for logs; ``detail`` carries identifiers only.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Process exit codes (PRD 5.3)
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INVALID_INPUT = 2
EXIT_PERMISSION = 3
EXIT_CONFLICT = 4
EXIT_DEPENDENCY = 5
EXIT_UNSUPPORTED_STORAGE = 6


class ExitCode:
    """Named exit codes, grouped by the failure class the PRD requires."""

    OK = EXIT_OK
    UNEXPECTED = EXIT_UNEXPECTED
    INVALID_INPUT = EXIT_INVALID_INPUT
    PERMISSION = EXIT_PERMISSION
    CONFLICT = EXIT_CONFLICT
    DEPENDENCY = EXIT_DEPENDENCY
    UNSUPPORTED_STORAGE = EXIT_UNSUPPORTED_STORAGE


# ---------------------------------------------------------------------------
# Machine-readable error codes
# ---------------------------------------------------------------------------
class Code:
    # Generic
    OK = "OK"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    # Instance / lifecycle
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"
    INSTANCE_MISMATCH = "INSTANCE_MISMATCH"
    INSTANCE_ALREADY_EXISTS = "INSTANCE_ALREADY_EXISTS"
    INSTANCE_LOCKED_BY_OTHER_OWNER = "INSTANCE_LOCKED_BY_OTHER_OWNER"
    INSTANCE_NOT_OWNED = "INSTANCE_NOT_OWNED"
    SETUP_COLLISION = "SETUP_COLLISION"
    WORKSPACE_UNINITIALISED = "WORKSPACE_UNINITIALISED"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    DOWNGRADE_REFUSED = "DOWNGRADE_REFUSED"

    # Integrity
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_UNTRUSTED = "MANIFEST_UNTRUSTED"

    # Auth / authorization
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    ROLE_INSUFFICIENT = "ROLE_INSUFFICIENT"
    CSRF_FAILED = "CSRF_FAILED"
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    HOST_REJECTED = "HOST_REJECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PAIRING_TOKEN_INVALID = "PAIRING_TOKEN_INVALID"
    PAIRING_TOKEN_EXPIRED = "PAIRING_TOKEN_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"

    # Concurrency
    REVISION_CONFLICT = "REVISION_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    LEASE_HELD = "LEASE_HELD"

    # Ingest / extraction
    FILE_NOT_STABLE = "FILE_NOT_STABLE"
    FILE_MISSING = "FILE_MISSING"
    FILE_UNREADABLE = "FILE_UNREADABLE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    ENCRYPTED_DOCUMENT = "ENCRYPTED_DOCUMENT"
    SCAN_ONLY_DOCUMENT = "SCAN_ONLY_DOCUMENT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    CHARACTER_LIMIT_EXCEEDED = "CHARACTER_LIMIT_EXCEEDED"
    PATH_ESCAPE = "PATH_ESCAPE"
    SYMLINK_REJECTED = "SYMLINK_REJECTED"

    # Analysis
    CRITERIA_NOT_APPROVED = "CRITERIA_NOT_APPROVED"
    ANALYSIS_SCHEMA_INVALID = "ANALYSIS_SCHEMA_INVALID"
    ANALYSIS_UNKNOWN_CRITERION = "ANALYSIS_UNKNOWN_CRITERION"
    ANALYSIS_REVISION_MISMATCH = "ANALYSIS_REVISION_MISMATCH"
    EVIDENCE_QUOTE_NOT_FOUND = "EVIDENCE_QUOTE_NOT_FOUND"
    EVIDENCE_SPAN_NOT_FOUND = "EVIDENCE_SPAN_NOT_FOUND"
    ANALYSIS_STALE_RESULT = "ANALYSIS_STALE_RESULT"

    # Model route
    ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
    ROUTE_NOT_RESTRICTED = "ROUTE_NOT_RESTRICTED"
    ROUTE_POLICY_VIOLATION = "ROUTE_POLICY_VIOLATION"
    LOCAL_ONLY_FALLBACK_BLOCKED = "LOCAL_ONLY_FALLBACK_BLOCKED"
    ADAPTER_TIMEOUT = "ADAPTER_TIMEOUT"
    ADAPTER_BAD_RESPONSE = "ADAPTER_BAD_RESPONSE"

    # Filters / chat
    FILTER_TOO_DEEP = "FILTER_TOO_DEEP"
    FILTER_TOO_WIDE = "FILTER_TOO_WIDE"
    FILTER_FIELD_NOT_ALLOWED = "FILTER_FIELD_NOT_ALLOWED"
    FILTER_OPERATOR_NOT_ALLOWED = "FILTER_OPERATOR_NOT_ALLOWED"
    FILTER_UNKNOWN_VALUE = "FILTER_UNKNOWN_VALUE"
    FILTER_INVALID_FOR_CRITERIA = "FILTER_INVALID_FOR_CRITERIA"
    CHAT_SCOPE_NOT_BOUND = "CHAT_SCOPE_NOT_BOUND"

    # Actions
    PLAN_EMPTY = "PLAN_EMPTY"
    PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_MUST_BE_HUMAN = "APPROVAL_MUST_BE_HUMAN"
    PLAN_STALE = "PLAN_STALE"
    DECISION_NEEDS_RECHECK = "DECISION_NEEDS_RECHECK"
    DESTINATION_COLLISION = "DESTINATION_COLLISION"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    SOURCE_MISSING = "SOURCE_MISSING"
    BATCH_ALREADY_STARTED = "BATCH_ALREADY_STARTED"
    BATCH_ALREADY_COMPLETED = "BATCH_ALREADY_COMPLETED"
    BATCH_PARTIAL = "BATCH_PARTIAL"
    CROSS_VOLUME_MOVE = "CROSS_VOLUME_MOVE"
    NO_CLOBBER_UNSUPPORTED = "NO_CLOBBER_UNSUPPORTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    INTENT_FROZEN = "INTENT_FROZEN"

    # Storage / topology
    UNSUPPORTED_STORAGE_TOPOLOGY = "UNSUPPORTED_STORAGE_TOPOLOGY"
    NETWORK_FILESYSTEM_DATABASE = "NETWORK_FILESYSTEM_DATABASE"
    INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
    READ_ONLY_LOCATION = "READ_ONLY_LOCATION"

    # Reporting
    SNAPSHOT_PUBLISH_FAILED = "SNAPSHOT_PUBLISH_FAILED"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"


#: Error code -> process exit code. Anything unlisted maps to EXIT_UNEXPECTED.
_EXIT_MAP: dict[str, int] = {
    Code.OK: EXIT_OK,
    Code.INVALID_INPUT: EXIT_INVALID_INPUT,
    Code.VALIDATION_FAILED: EXIT_INVALID_INPUT,
    Code.NOT_FOUND: EXIT_INVALID_INPUT,
    Code.INSTANCE_NOT_FOUND: EXIT_INVALID_INPUT,
    Code.SCHEMA_VERSION_UNSUPPORTED: EXIT_INVALID_INPUT,
    Code.DOWNGRADE_REFUSED: EXIT_INVALID_INPUT,
    Code.UNAUTHENTICATED: EXIT_PERMISSION,
    Code.FORBIDDEN: EXIT_PERMISSION,
    Code.ROLE_INSUFFICIENT: EXIT_PERMISSION,
    Code.CSRF_FAILED: EXIT_PERMISSION,
    Code.ORIGIN_REJECTED: EXIT_PERMISSION,
    Code.HOST_REJECTED: EXIT_PERMISSION,
    Code.PAIRING_TOKEN_INVALID: EXIT_PERMISSION,
    Code.PAIRING_TOKEN_EXPIRED: EXIT_PERMISSION,
    Code.INSTANCE_LOCKED_BY_OTHER_OWNER: EXIT_PERMISSION,
    Code.INSTANCE_NOT_OWNED: EXIT_PERMISSION,
    Code.MANIFEST_MISMATCH: EXIT_PERMISSION,
    Code.MANIFEST_UNTRUSTED: EXIT_PERMISSION,
    Code.REVISION_CONFLICT: EXIT_CONFLICT,
    Code.IDEMPOTENCY_KEY_REUSED: EXIT_CONFLICT,
    Code.PLAN_HASH_MISMATCH: EXIT_CONFLICT,
    Code.APPROVAL_REQUIRED: EXIT_CONFLICT,
    Code.APPROVAL_EXPIRED: EXIT_CONFLICT,
    Code.PLAN_STALE: EXIT_CONFLICT,
    Code.DESTINATION_COLLISION: EXIT_CONFLICT,
    Code.SOURCE_CHANGED: EXIT_CONFLICT,
    Code.SOURCE_MISSING: EXIT_CONFLICT,
    Code.BATCH_ALREADY_STARTED: EXIT_CONFLICT,
    Code.SETUP_COLLISION: EXIT_CONFLICT,
    Code.NEEDS_RECONCILIATION: EXIT_CONFLICT,
    Code.ROUTE_UNAVAILABLE: EXIT_DEPENDENCY,
    Code.ROUTE_NOT_RESTRICTED: EXIT_DEPENDENCY,
    Code.ROUTE_POLICY_VIOLATION: EXIT_DEPENDENCY,
    Code.ADAPTER_TIMEOUT: EXIT_DEPENDENCY,
    Code.ADAPTER_BAD_RESPONSE: EXIT_DEPENDENCY,
    Code.EXTRACTION_FAILED: EXIT_DEPENDENCY,
    Code.MANIFEST_MISSING: EXIT_DEPENDENCY,
    Code.UNSUPPORTED_STORAGE_TOPOLOGY: EXIT_UNSUPPORTED_STORAGE,
    Code.NETWORK_FILESYSTEM_DATABASE: EXIT_UNSUPPORTED_STORAGE,
    Code.CROSS_VOLUME_MOVE: EXIT_UNSUPPORTED_STORAGE,
    Code.NO_CLOBBER_UNSUPPORTED: EXIT_UNSUPPORTED_STORAGE,
}


def exit_code_for(code: str) -> int:
    """Map a machine-readable error code onto a stable process exit code."""
    return _EXIT_MAP.get(code, EXIT_UNEXPECTED)


class ResumeReviewError(Exception):
    """Base class for every error the application raises deliberately.

    ``message`` must be safe to show a reviewer and safe to write to a log: no
    candidate names, no absolute paths, no credentials. Put identifiers in
    ``detail`` instead.
    """

    code: str = Code.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail: dict[str, Any] = dict(detail or {})
        if http_status is not None:
            self.http_status = http_status
        self.retryable = retryable

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }
        if self.detail:
            payload["error"]["detail"] = self.detail
        if request_id:
            payload["request_id"] = request_id
        return payload


class InvalidInput(ResumeReviewError):
    code = Code.INVALID_INPUT
    http_status = 422


class ValidationFailed(ResumeReviewError):
    code = Code.VALIDATION_FAILED
    http_status = 422


class NotFound(ResumeReviewError):
    code = Code.NOT_FOUND
    http_status = 404


class Unauthenticated(ResumeReviewError):
    code = Code.UNAUTHENTICATED
    http_status = 401


class Forbidden(ResumeReviewError):
    code = Code.FORBIDDEN
    http_status = 403


class Conflict(ResumeReviewError):
    code = Code.REVISION_CONFLICT
    http_status = 409


class DependencyUnavailable(ResumeReviewError):
    code = Code.ROUTE_UNAVAILABLE
    http_status = 503


class UnsupportedStorage(ResumeReviewError):
    code = Code.UNSUPPORTED_STORAGE_TOPOLOGY
    http_status = 409


class IntegrityError(ResumeReviewError):
    code = Code.MANIFEST_MISMATCH
    http_status = 409
