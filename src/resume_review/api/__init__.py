"""The helper HTTP API: application factory, dependencies, and envelope.

Authority: PRD section 12 (endpoint surface and mutation rules), section 5.3 (the
machine-readable response contract), and section 16.1 (network boundaries).

This package is an *entry point*: nothing below it (``db``, ``storage``, ``auth``,
``analysis``, ``actions``) may import it. It owns the response envelope, the
status-code mapping, the request dependencies, idempotency policy, and the
application factory. Business endpoints live in separate modules that register
themselves with the factory's router.

Public surface for endpoint modules:

* :func:`create_app` -- build the application.
* :data:`API_PREFIX`, :func:`build_api_router`, :func:`register_route_module` --
  route registration.
* Envelope builders and response helpers: :func:`ok_response`,
  :func:`error_response`, :func:`accepted_response`, :func:`ok_envelope`,
  :func:`error_envelope`, :func:`envelope_from_exception`,
  :func:`sanitize_message`, :func:`validate_envelope`.
* Dependencies: :func:`get_request_id`, :func:`current_principal`,
  :func:`authorized_instance`, :func:`require_operation`,
  :func:`resolve_document`, :func:`resolve_document_id`,
  :func:`current_state_revision`, :func:`require_mutation`, and the
  :data:`InstanceCtx` / :data:`RequestId` annotated aliases.
* Idempotency: :func:`idempotency_guard`, :class:`IdempotencyGuard`.
"""

from __future__ import annotations

from .app import (
    API_PREFIX,
    DEFAULT_ROUTE_MODULES,
    ChatAdapter,
    build_api_router,
    create_app,
    register_route_module,
)
from .deps import (
    ApiConfig,
    ApiRuntime,
    InstanceContext,
    InstanceCtx,
    RequestId,
    authorized_instance,
    current_principal,
    current_session,
    current_state_revision,
    effective_allowed_hosts,
    get_request_id,
    get_runtime,
    install_runtime,
    require_csrf,
    require_mutation,
    require_operation,
    resolve_document,
    resolve_document_id,
)
from .envelope import (
    ACCEPTED_CODE,
    DEFAULT_OK_CODE,
    MUTATION_HEADER_IDEMPOTENCY,
    REQUEST_ID_HEADER,
    EnvelopeSchemaError,
    accepted_response,
    envelope_from_exception,
    envelope_response,
    error_envelope,
    error_response,
    message_is_safe,
    new_request_id,
    normalise_warnings,
    ok_envelope,
    ok_response,
    sanitize_message,
    validate_envelope,
)
from .errors import (
    HTTP_STATUS_BY_CODE,
    error_code_for,
    exception_response,
    register_exception_handlers,
    retryable_for,
    status_for_error,
)
from .idempotency import (
    IDEMPOTENCY_HEADER,
    IdempotencyGuard,
    IdempotencyResult,
    build_scope,
    idempotency_guard,
    request_hash,
    validate_key,
)

__all__ = [
    # Factory and routing
    "create_app",
    "API_PREFIX",
    "DEFAULT_ROUTE_MODULES",
    "build_api_router",
    "register_route_module",
    "ChatAdapter",
    # Envelope
    "REQUEST_ID_HEADER",
    "MUTATION_HEADER_IDEMPOTENCY",
    "DEFAULT_OK_CODE",
    "ACCEPTED_CODE",
    "EnvelopeSchemaError",
    "sanitize_message",
    "message_is_safe",
    "normalise_warnings",
    "new_request_id",
    "ok_envelope",
    "error_envelope",
    "envelope_from_exception",
    "envelope_response",
    "ok_response",
    "error_response",
    "accepted_response",
    "validate_envelope",
    # Errors
    "HTTP_STATUS_BY_CODE",
    "status_for_error",
    "error_code_for",
    "retryable_for",
    "register_exception_handlers",
    "exception_response",
    # Dependencies
    "ApiConfig",
    "ApiRuntime",
    "InstanceContext",
    "InstanceCtx",
    "RequestId",
    "get_runtime",
    "get_request_id",
    "current_session",
    "current_principal",
    "authorized_instance",
    "require_operation",
    "require_csrf",
    "require_mutation",
    "resolve_document",
    "resolve_document_id",
    "current_state_revision",
    "effective_allowed_hosts",
    "install_runtime",
    # Idempotency
    "IDEMPOTENCY_HEADER",
    "IdempotencyGuard",
    "IdempotencyResult",
    "idempotency_guard",
    "request_hash",
    "build_scope",
    "validate_key",
]
