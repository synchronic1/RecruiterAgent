"""The FastAPI application factory, middleware, and route registration.

Authority: PRD section 12 (helper API and permission contracts), section 12.1
("The application must not expose generic SQL, shell, upload-and-execute,
arbitrary path read, or proxy-any-Gateway-request endpoints.") and section 16.1
(Host/Origin checks, request limits, instance-specific session binding).

What this module deliberately does **not** do:

* It exposes no generic SQL, shell, upload, file-read, or Gateway-proxy route. The
  only OpenClaw-facing surface is one narrow ``POST /chat`` route, and it exists
  only when a :data:`ChatAdapter` is supplied.
* It never mounts ``.review`` as a static directory.
* It owns no business endpoint. Endpoint modules register themselves (see
  :func:`register_route_module`) and answer through the envelope helpers.

Registration contract for endpoint modules (phase-B)

Each endpoint module is an importable module that exposes **one** of:

* ``register(router: APIRouter) -> None`` -- preferred. The ``router`` passed in
  already carries the ``/api/v1/instances/{instance_id}`` prefix, so the module
  adds plain paths such as ``router.get("/documents")(handler)``.
* a module-level ``router: APIRouter`` attribute, which the factory includes.

The default discovery list is :data:`DEFAULT_ROUTE_MODULES`; a module that does not
exist is skipped, so the application constructs before any endpoint module lands.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
from typing import Any, Callable, Sequence

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import (
    CsrfStore,
    SessionStore,
    check_csrf,
    check_host,
    check_origin,
    require_role,
)
from ..db import Repository
from ..errors import ResumeReviewError
from ..models import Role
from . import envelope as env
from . import errors as api_errors
from .deps import (
    MUTATING_METHODS,
    ApiConfig,
    ApiRuntime,
    InstanceContext,
    effective_allowed_hosts,
    effective_allowed_origins,
    get_request_id,
    install_runtime,
    require_mutation,
    session_token_from_request,
)

__all__ = [
    "API_PREFIX",
    "DEFAULT_ROUTE_MODULES",
    "ChatAdapter",
    "build_api_router",
    "register_route_module",
    "create_app",
]

#: Every business endpoint lives under this prefix (PRD 12).
API_PREFIX = "/api/v1/instances/{instance_id}"

#: Modules tried, in order, when ``route_modules`` is not given. Absent modules are
#: skipped; present modules must follow the registration contract above.
DEFAULT_ROUTE_MODULES: tuple[str, ...] = (
    "resume_review.api.documents",
    "resume_review.api.requisition",
    "resume_review.api.criteria",
    "resume_review.api.actions",
    "resume_review.api.chat",
)

#: A page-bound chat adapter: ``adapter(payload, *, principal, instance_id)`` where
#: ``payload`` is a bounded, typed turn (see :class:`ChatTurn`). It is the sole
#: OpenClaw-facing entry point; the application forwards nothing the caller did not
#: name in that type.
ChatAdapter = Callable[..., Any]

_INSTANCE_PATH_RE = re.compile(r"^/api/v1/instances/([^/]+)(?:/|$)")


class ChatTurn(BaseModel):
    """A bounded chat turn. No caller-supplied endpoint, model, or tool field."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = None
    document_ids: list[str] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------------------
# Router construction and module registration
# ---------------------------------------------------------------------------
def build_api_router() -> APIRouter:
    """A fresh router carrying the instance-scoped prefix.

    The factory uses this; endpoint modules normally do not need it, because the
    router they receive in :func:`register_route_module` is this object.
    """
    return APIRouter(prefix=API_PREFIX, tags=["instance"])


def register_route_module(router: APIRouter, module: Any) -> bool:
    """Register one endpoint module onto ``router``. Returns whether it did.

    Prefers ``register(router)``; falls back to including a module-level ``router``.
    A module that offers neither returns ``False`` rather than raising, so a stray
    module on the discovery path cannot stop the application from constructing.
    """
    register = getattr(module, "register", None)
    if callable(register):
        register(router)
        return True
    child = getattr(module, "router", None)
    if child is not None and hasattr(child, "routes"):
        router.include_router(child)
        return True
    return False


def _import_route_modules(names: Sequence[str]) -> list[Any]:
    found: list[Any] = []
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        found.append(importlib.import_module(name))
    return found


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
def _resolve_request_id(request: Request, config: ApiConfig) -> str:
    raw = request.headers.get(config.request_id_header)
    if env.valid_request_id(raw):
        return str(raw)
    return env.new_request_id()


def _instance_id_from_path(path: str) -> str | None:
    match = _INSTANCE_PATH_RE.match(path)
    return match.group(1) if match else None


def _enforce_csrf(request: Request, runtime: ApiRuntime) -> None:
    """Verify the CSRF token when a live session is present; otherwise stand aside.

    A request with no resolvable session is left for the identity dependency to
    answer with 401, so a missing session is never reported as a CSRF failure. A
    request that *does* carry a live session must present the token: this is the
    central half of the PRD 16.1 requirement, and it also covers an endpoint that
    forgot to declare ``require_mutation``.
    """
    instance_id = _instance_id_from_path(request.url.path)
    if not instance_id:
        return
    token = session_token_from_request(request, instance_id)
    try:
        session = runtime.sessions.resolve(token, instance_id=instance_id, touch=False)
    except ResumeReviewError:
        return
    check_csrf(
        session.session_id,
        request.headers.get(runtime.csrf_store.header_name),
        store=runtime.csrf_store,
    )


def _enforce_transport(request: Request, runtime: ApiRuntime) -> None:
    """Host for every request; Origin and CSRF for an instance-scoped mutation."""
    config = runtime.config
    check_host(
        request.headers,
        effective_allowed_hosts(request.headers, config),
        allow_any=config.allow_any_host,
    )
    if request.method.upper() not in MUTATING_METHODS:
        return
    if _instance_id_from_path(request.url.path) is None:
        return
    if config.enforce_origin:
        check_origin(
            request.headers,
            effective_allowed_origins(request.headers, config),
            require=config.require_origin_for_mutations,
        )
    if config.enforce_csrf:
        _enforce_csrf(request, runtime)


def _apply_response_headers(response: JSONResponse, request_id: str) -> None:
    response.headers[env.REQUEST_ID_HEADER] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )


# ---------------------------------------------------------------------------
# The chat adapter route
# ---------------------------------------------------------------------------
def _has_post_route(router: APIRouter, path: str) -> bool:
    """Whether ``router`` already carries a POST handler for ``path``.

    ``path`` is the suffix after the instance prefix, because an ``APIRouter``
    stores the prefix in each route's own ``path``.
    """
    wanted = f"{API_PREFIX}{path}"
    for route in getattr(router, "routes", ()) or ():
        methods = getattr(route, "methods", None) or set()
        if getattr(route, "path", None) == wanted and "POST" in methods:
            return True
    return False


def _add_chat_route(router: APIRouter, adapter: ChatAdapter) -> None:
    """The fallback ``/chat`` route, for a deployment that does not load ``api.chat``.

    It accepts a :class:`ChatTurn` and nothing else: no endpoint, model id, tool
    definition, or path is forwarded from the caller. Authorization is
    ``REVIEWER`` and the transport guards run first.

    This is deliberately a *fallback*, not a second implementation of the chat
    surface. The full endpoint lives in :mod:`resume_review.api.chat` (route-policy
    fail-closed, caller-supplied document ids resolved against this instance, queue
    mode, its own request budget). :func:`create_app` registers this one only when
    nothing else claimed ``/chat``, because two handlers on one path would mean
    FastAPI dispatching whichever was registered first and silently disabling the
    other.

    The difference is not cosmetic: this route forwards the caller's
    ``document_ids`` to the adapter as given, so it does not itself check that they
    name documents in *this* instance. A deployment that chooses the fallback by
    narrowing ``route_modules`` accepts that the adapter does its own scoping.
    """

    @router.post("/chat", name="chat", response_class=JSONResponse)
    async def chat(
        turn: ChatTurn,
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        result = adapter(
            turn.model_dump(),
            principal=ctx.principal,
            instance_id=ctx.instance_id,
        )
        if inspect.isawaitable(result):
            result = await result
        return env.ok_response(
            result,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_app(
    repository: Repository,
    *,
    sessions: SessionStore | None = None,
    csrf_store: CsrfStore | None = None,
    config: ApiConfig | None = None,
    chat_adapter: ChatAdapter | None = None,
    route_modules: Sequence[str] | None = None,
    register_routes: Sequence[Callable[[APIRouter], None]] | None = None,
) -> FastAPI:
    """Build the helper application for one instance.

    ``repository`` supplies the authoritative instance id; the path
    ``{instance_id}`` must equal it. ``sessions`` and ``csrf_store`` default to
    fresh process-local stores, which the caller reaches through
    ``app.state.runtime`` when it needs to issue or inspect them. ``chat_adapter``
    is stored on the runtime as the configured chat service, and additionally
    contributes a fallback ``/chat`` route when no loaded route module registered
    one (with the default module list, :mod:`resume_review.api.chat` does).
    ``route_modules`` overrides the default discovery list; ``register_routes`` are
    callables invoked with the prefixed router after discovery (used by the
    application itself and by tests).
    """
    if not isinstance(repository, Repository):
        raise TypeError("create_app requires a Repository instance.")

    active_config = config or ApiConfig()
    active_sessions = sessions if sessions is not None else SessionStore()
    active_csrf = csrf_store if csrf_store is not None else CsrfStore()

    runtime = ApiRuntime(
        repository=repository,
        db=repository.db,
        sessions=active_sessions,
        csrf_store=active_csrf,
        config=active_config,
        instance_id=str(repository.instance_id),
        chat_adapter=chat_adapter,
    )

    if active_config.enable_docs:
        app = FastAPI(title="resume-review helper", version="1.0")
    else:
        app = FastAPI(
            title="resume-review helper",
            version="1.0",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

    install_runtime(app, runtime)
    api_errors.register_exception_handlers(app)

    @app.middleware("http")
    async def _transport_guards(request: Request, call_next):
        request_id = _resolve_request_id(request, runtime.config)
        request.state.request_id = request_id
        try:
            _enforce_transport(request, runtime)
        except ResumeReviewError as exc:
            response = api_errors.exception_response(request, exc)
            _apply_response_headers(response, request_id)
            return response
        response = await call_next(request)
        _apply_response_headers(response, request_id)
        return response

    router = build_api_router()

    if chat_adapter is not None and not callable(chat_adapter):
        raise TypeError("chat_adapter must be callable.")

    modules = tuple(route_modules) if route_modules is not None else DEFAULT_ROUTE_MODULES
    for module in _import_route_modules(modules):
        register_route_module(router, module)

    for register in register_routes or ():
        register(router)

    # Only now, once every real route module has had its turn, does the fallback
    # bridge register -- and only if ``/chat`` is still unclaimed. With the default
    # module list ``api.chat`` owns the path, so the bridge stays out of the way and
    # a caller who narrows ``route_modules`` (but still passes an adapter) keeps the
    # simple route it has always had.
    if chat_adapter is not None and not _has_post_route(router, "/chat"):
        _add_chat_route(router, chat_adapter)

    app.include_router(router)
    return app
