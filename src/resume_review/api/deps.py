"""FastAPI dependencies: identity, instance authorization, and CSRF.

Authority: PRD section 12 ("All business endpoints are scoped under
``/api/v1/instances/{instance_id}``. Authenticate first, authorize the instance and
operation, then resolve document IDs.") and section 16.1 (Host/Origin/CSRF).

The resolution order is fixed and every dependency below depends only on the ones
before it:

    runtime -> authenticated session -> Principal -> authorized instance
            -> authorized operation -> resolved document id

Two rules are non-negotiable here:

* **Identity never comes from the request body.** The only inputs are the session
  cookie (or a bearer session token for non-browser clients) established by
  authentication, and the ``instance_id`` path segment. There is no ``actor``
  parameter anywhere in this package.
* **Instance binding is checked twice.** The session store refuses a session used
  against another instance (``INSTANCE_MISMATCH``), and :func:`authorized_instance`
  refuses an instance this helper does not serve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Mapping
from urllib.parse import urlsplit

from fastapi import Depends, Request

from ..auth import (
    CHAT_RATE_LIMITER,
    CsrfStore,
    DEFAULT_RATE_LIMITER,
    RateLimiter,
    Session,
    SessionStore,
    check_csrf,
    check_mutation,
    normalize_host,
    normalize_origin,
    require_role,
    session_cookie_name,
)
from ..db import Database, Repository
from ..errors import Code, Forbidden, NotFound, Unauthenticated
from ..models import DocumentRecord, Principal, Role
from .envelope import new_request_id

__all__ = [
    "MUTATING_METHODS",
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
    "resolve_document",
    "resolve_document_id",
    "current_state_revision",
    "require_csrf",
    "require_mutation",
    "effective_allowed_hosts",
    "effective_allowed_origins",
    "install_runtime",
    "csrf_token_from_request",
    "session_token_from_request",
]

#: Methods that change state and therefore need Origin and CSRF checks.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class ApiConfig:
    """Static policy for one application instance.

    The defaults fail closed for a browser-facing helper and keep a test client
    working without ceremony. ``allowed_origins`` is empty by default. A request
    whose ``Origin`` authority matches the request ``Host`` is treated as
    same-origin and accepted (see :func:`effective_allowed_origins`); any other
    ``Origin`` is refused unless the operator lists it, because it cannot match the
    empty allowlist. A request with no ``Origin`` is accepted, because
    ``require_origin_for_mutations`` is false. A deployment behind a distinct public
    origin must add that origin to ``allowed_origins``.
    """

    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "::1", "localhost", "testserver")
    allow_any_host: bool = False
    enforce_origin: bool = True
    require_origin_for_mutations: bool = False
    enforce_csrf: bool = True
    request_id_header: str = "X-Request-ID"
    enable_docs: bool = False
    default_page_size: int = 50
    max_page_size: int = 200


@dataclass
class ApiRuntime:
    """The collaborators one application instance resolves against.

    Installed on ``app.state.runtime`` and read back by :func:`get_runtime`, which
    is what keeps endpoint modules free of import-time coupling to the app factory.
    """

    repository: Repository
    db: Database
    sessions: SessionStore
    csrf_store: CsrfStore
    config: ApiConfig
    instance_id: str
    chat_adapter: Any = None
    #: Request limits (PRD 16.1). Overridable so a test can drive a small window,
    #: and so an operator-facing deployment can widen it without touching code.
    rate_limiter: RateLimiter = DEFAULT_RATE_LIMITER
    chat_rate_limiter: RateLimiter = CHAT_RATE_LIMITER


@dataclass(frozen=True)
class InstanceContext:
    """An authenticated principal that has been authorized for one instance."""

    principal: Principal
    instance_id: str
    repository: Repository
    db: Database

    @property
    def state_revision(self) -> int:
        return int(self.db.state_revision())


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------
def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return None if value is None else str(value)
    return None


def _hostname(value: str) -> str:
    """The host part of a ``host[:port]`` value, lowercased, brackets stripped."""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else text
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


def _authority(value: str) -> str:
    """``host[:port]`` for a ``host`` or ``scheme://host[:port]`` value, or ``""``.

    A default port for the scheme is dropped, so ``localhost:80`` and
    ``http://localhost`` share an authority. Used only to decide whether an
    ``Origin`` and the request ``Host`` name the same server.
    """
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    scheme = (parsed.scheme or "http").lower()
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


def effective_allowed_hosts(headers: Mapping[str, str], config: ApiConfig) -> list[str]:
    """The allowlist for :func:`resume_review.auth.check_host`.

    A configured host entry without a port also accepts the request's own
    ``host:port`` form, so an operator can allowlist ``127.0.0.1`` once and still
    be reached on whatever ephemeral port the helper bound. The hostname still has
    to match; only the port is forgiven.
    """
    allowed = [entry for entry in config.allowed_hosts if entry]
    if config.allow_any_host:
        return allowed
    raw = _header(headers, "host")
    if not raw:
        return allowed
    normalized = normalize_host(raw)
    if not normalized:
        return allowed
    known = {_hostname(entry) for entry in config.allowed_hosts}
    if _hostname(normalized) in known and normalized not in allowed:
        allowed.append(normalized)
    return allowed


def effective_allowed_origins(headers: Mapping[str, str], config: ApiConfig) -> list[str]:
    """The allowlist for :func:`resume_review.auth.check_origin`.

    A browser sends an ``Origin`` for every mutation, including a same-origin one
    that an operator never needs to enumerate. The request's own
    ``Origin``/``Host`` pair *is* the same-origin case: an origin whose authority
    (host and, when present, port) equals the request's Host is added to the
    configured allowlist. A forged cross-origin value never matches the Host, so
    it is still rejected unless the operator configured it explicitly.
    """
    allowed = [entry for entry in config.allowed_origins if entry]
    if config.allow_any_host:
        return allowed
    origin = _header(headers, "origin")
    host = _header(headers, "host")
    if not origin or not host:
        return allowed
    normalized_origin = normalize_origin(origin)
    normalized_host = normalize_host(host)
    if not normalized_origin or not normalized_host:
        return allowed
    if normalized_origin not in allowed and _authority(normalized_origin) == normalized_host:
        allowed.append(normalized_origin)
    return allowed


def csrf_token_from_request(request: Request, runtime: ApiRuntime) -> str | None:
    """The presented CSRF token, read from the header the auth package defines."""
    return request.headers.get(runtime.csrf_store.header_name)


def session_token_from_request(request: Request, instance_id: str) -> str | None:
    """The session id from the instance-scoped cookie, else a bearer token.

    A non-browser client (the CLI, a test) has no cookie jar; a bearer session
    token is the same process-held opaque id, so it carries identical semantics.
    The cookie wins when both are present.
    """
    cookie = request.cookies.get(session_cookie_name(instance_id))
    if cookie:
        return cookie
    authorization = _header(request.headers, "authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
def install_runtime(app: Any, runtime: ApiRuntime) -> None:
    """Attach ``runtime`` to an application's state."""
    app.state.runtime = runtime


def get_runtime(request: Request) -> ApiRuntime:
    """The runtime installed by :func:`resume_review.api.app.create_app`."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError("The API runtime has not been installed on this application.")
    return runtime


def get_request_id(request: Request) -> str:
    """The effective request id, set by the request-id middleware."""
    value = getattr(request.state, "request_id", None)
    if value:
        return str(value)
    return new_request_id()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def current_session(
    request: Request,
    runtime: ApiRuntime = Depends(get_runtime),
) -> Session:
    """Resolve and validate the authenticated session for the path instance."""
    path_params = getattr(request, "path_params", None) or {}
    instance_id = path_params.get("instance_id")
    if not instance_id:
        raise Unauthenticated("Sign in to continue.", detail={"reason": "no_instance_scope"})
    token = session_token_from_request(request, str(instance_id))
    return runtime.sessions.resolve(token, instance_id=str(instance_id))


def current_principal(
    session: Session = Depends(current_session),
    runtime: ApiRuntime = Depends(get_runtime),
) -> Principal:
    """The immutable principal for the authenticated session."""
    return runtime.sessions.principal(session)


def authorized_instance(
    instance_id: str,
    principal: Principal = Depends(current_principal),
    runtime: ApiRuntime = Depends(get_runtime),
) -> InstanceContext:
    """Authorize the resolved principal for the path instance.

    Reached only after the session was resolved and bound to the same instance id,
    so a mismatch here means the helper does not serve that instance at all. It is
    reported as ``403 INSTANCE_MISMATCH`` rather than 404 so the caller learns
    nothing about which instances exist on the host.
    """
    if str(instance_id) != runtime.instance_id:
        raise Forbidden(
            "This helper does not serve the requested instance.",
            code=Code.INSTANCE_MISMATCH,
            detail={"reason": "instance_not_served"},
        )
    if principal.instance_id != runtime.instance_id:
        raise Forbidden(
            "This session is not valid for the requested instance.",
            code=Code.INSTANCE_MISMATCH,
            detail={"reason": "instance_mismatch"},
        )
    return InstanceContext(
        principal=principal,
        instance_id=runtime.instance_id,
        repository=runtime.repository,
        db=runtime.db,
    )


def require_operation(minimum: Role | str):
    """Build a dependency that authorizes ``minimum`` and returns the context.

    Usage::

        @router.post("/decisions/bulk")
        def bulk(ctx: InstanceContext = Depends(require_operation(Role.REVIEWER))):
            ...
    """

    def dependency(ctx: InstanceContext = Depends(authorized_instance)) -> InstanceContext:
        require_role(ctx.principal, minimum)
        return ctx

    return dependency


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def resolve_document(
    document_id: str,
    ctx: InstanceContext = Depends(authorized_instance),
) -> DocumentRecord:
    """Resolve a caller-supplied id to a record in this instance, or 404.

    The id is never trusted as authorization: the lookup is scoped to the
    authorized instance and a foreign id is indistinguishable from an absent one.
    """
    record = ctx.repository.get_document(document_id)
    if record is None or record.instance_id != ctx.instance_id:
        raise NotFound("That document does not exist.", detail={"entity": "document"})
    return record


def resolve_document_id(
    record: DocumentRecord = Depends(resolve_document),
) -> str:
    """The validated document id, for handlers that only need the identifier."""
    return record.id


def current_state_revision(
    ctx: InstanceContext = Depends(authorized_instance),
) -> int:
    """The committed ``instances.state_revision`` for this instance."""
    return ctx.state_revision


# ---------------------------------------------------------------------------
# Mutation guards
# ---------------------------------------------------------------------------
def require_csrf(
    request: Request,
    ctx: InstanceContext = Depends(authorized_instance),
    runtime: ApiRuntime = Depends(get_runtime),
) -> InstanceContext:
    """Verify the CSRF token for a session-authenticated mutation."""
    check_csrf(
        ctx.principal.session_id,
        csrf_token_from_request(request, runtime),
        store=runtime.csrf_store,
    )
    return ctx


def require_mutation(
    request: Request,
    ctx: InstanceContext = Depends(authorized_instance),
    runtime: ApiRuntime = Depends(get_runtime),
) -> InstanceContext:
    """Host + Origin + CSRF, then the authorized context (PRD 16.1).

    A mutating endpoint declares this dependency. The request-id middleware runs the
    same transport checks centrally, so this is belt-and-suspenders for a route that
    is mounted outside the standard prefix.
    """
    config = runtime.config
    check_mutation(
        headers=request.headers,
        allowed_origins=effective_allowed_origins(request.headers, config),
        allowed_hosts=effective_allowed_hosts(request.headers, config),
        session_id=ctx.principal.session_id,
        csrf_token=csrf_token_from_request(request, runtime),
        csrf_store=runtime.csrf_store,
        require_origin=config.require_origin_for_mutations,
    )
    # Request limits (PRD 16.1). Keyed by the authenticated actor and the route so
    # one reviewer's runaway tab cannot exhaust another reviewer's budget, and a
    # burst against one endpoint does not silence the rest. This is the speed bump
    # the limiter documents itself as -- a bound on a looping page or a scripted
    # guess, not a distributed quota -- so a process restart clearing the counters
    # is acceptable. It is applied here rather than in middleware because this
    # dependency is what every mutating route already declares; a test asserts no
    # mutating route omits it.
    runtime.rate_limiter.check(f"mutation:{ctx.principal.actor_ref}:{request.url.path}")
    return ctx


# ---------------------------------------------------------------------------
# Annotated aliases for concise handler signatures
# ---------------------------------------------------------------------------
RequestId = Annotated[str, Depends(get_request_id)]
InstanceCtx = Annotated[InstanceContext, Depends(authorized_instance)]
