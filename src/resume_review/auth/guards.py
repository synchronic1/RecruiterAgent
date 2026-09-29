"""Request-level defences applied before any business logic.

Authority: PRD section 16.1, acceptance test AT-33.

    "Enforce Host and Origin checks, CSRF protection for session-authenticated
     mutations, strict CORS, conservative content security policy, clickjacking
     protections, request limits, and session expiry. Use instance-specific
     session binding; do not assume cookies are isolated by TCP port."

Every check in this module runs ahead of the route handler and either returns
cleanly or raises a :class:`resume_review.errors.ResumeReviewError` carrying the
specific machine-readable code the client and the audit log expect:

=====================  ==========================
Check                  Failure code
=====================  ==========================
Origin                 ``ORIGIN_REJECTED``
Host                   ``HOST_REJECTED``
CSRF                   ``CSRF_FAILED``
Role                   ``ROLE_INSUFFICIENT``
Rate limit             ``RATE_LIMITED``
=====================  ==========================

Two properties hold for every message produced here: it never contains a
candidate name or a filesystem path, and it is safe to write to a log verbatim.
The offending Origin or Host value is deliberately *not* echoed back, because a
Host header is attacker-controlled text and does not belong in a rendered error.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from ..errors import Code, Forbidden, ResumeReviewError, Unauthenticated
from ..models import Principal, Role
from . import csrf as _csrf

__all__ = [
    "OriginRejected",
    "HostRejected",
    "CsrfFailed",
    "RateLimited",
    "normalize_origin",
    "normalize_host",
    "check_origin",
    "check_host",
    "check_csrf",
    "require_role",
    "check_mutation",
    "RateLimiter",
    "check_rate_limit",
    "check_pairing_rate",
    "check_chat_rate",
    "PAIRING_RATE_LIMITER",
    "CHAT_RATE_LIMITER",
    "DEFAULT_RATE_LIMITER",
]


class OriginRejected(Forbidden):
    """The request Origin is absent when required, opaque, or not allowlisted."""

    code = Code.ORIGIN_REJECTED


class HostRejected(Forbidden):
    """The Host header is missing, malformed, or not allowlisted."""

    code = Code.HOST_REJECTED


class CsrfFailed(Forbidden):
    """The CSRF token is missing or does not match the session's token."""

    code = Code.CSRF_FAILED


class RateLimited(ResumeReviewError):
    """Too many requests from one key inside the window."""

    code = Code.RATE_LIMITED
    http_status = 429


# ---------------------------------------------------------------------------
# Header access
# ---------------------------------------------------------------------------
def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that tolerates an arbitrary mapping."""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            if value is None:
                return None
            return str(value)
    return None


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------
def normalize_origin(origin: str | None) -> str:
    """Canonicalise an Origin value for allowlist comparison.

    Returns an empty string for an absent, empty, or opaque (``null``) origin. The
    ``null`` origin is what a ``file:`` document, a sandboxed iframe, and some
    redirect chains send; it is never treated as same-origin, so it can never match
    an allowlist entry.
    """
    if origin is None:
        return ""
    value = origin.strip().rstrip("/")
    if not value or value.lower() == "null":
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.lower()
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        # Not a scheme://host[:port] origin; return it lowercased so it can only
        # match an equally unusual allowlist entry, never a real one.
        return value.lower()
    try:
        port = parsed.port
    except ValueError:
        return value.lower()
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def check_origin(
    headers: Mapping[str, str],
    allowed_origins: Iterable[str],
    *,
    require: bool = False,
) -> None:
    """Reject a forged Origin with ``ORIGIN_REJECTED``.

    With ``require=False`` an absent Origin is allowed, which is correct for
    same-origin navigations and non-browser clients such as the CLI. Mutating
    routes should pass ``require=True`` so a request that arrives without an Origin
    is refused rather than assumed same-origin.
    """
    raw = _header(headers, "origin")
    if raw is None:
        if require:
            raise OriginRejected(
                "This request is missing the Origin header required for a change.",
                detail={"reason": "origin_missing"},
            )
        return
    normalized = normalize_origin(raw)
    if not normalized:
        raise OriginRejected(
            "The request Origin is not allowed.",
            detail={"reason": "origin_opaque"},
        )
    allowed = {normalize_origin(item) for item in allowed_origins if item}
    if normalized not in allowed:
        raise OriginRejected(
            "The request Origin is not allowed.",
            detail={"reason": "origin_not_allowed"},
        )


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------
def normalize_host(host: str | None) -> str:
    """Canonicalise a Host header value, or return ``""`` when it is malformed.

    A Host containing whitespace, a slash, or userinfo can be used to confuse
    virtual-host routing and password-reset style links. Anything that unusual is
    reported as malformed and can never match an allowlist entry.
    """
    if host is None:
        return ""
    value = host.strip().lower()
    if not value:
        return ""
    if any(ch in value for ch in (" ", "\t", "/", "\\", "@", "\r", "\n")):
        return ""
    if value.endswith("."):
        value = value[:-1]
    return value


def check_host(
    headers: Mapping[str, str],
    allowed_hosts: Iterable[str],
    *,
    allow_any: bool = False,
) -> None:
    """Reject an unexpected Host with ``HOST_REJECTED``.

    ``allow_any`` exists only for a deliberately bound reverse proxy that has
    already validated the host upstream; the helper itself never passes it.
    """
    raw = _header(headers, "host")
    if raw is None:
        raise HostRejected(
            "This request did not include a Host header.",
            detail={"reason": "host_missing"},
        )
    normalized = normalize_host(raw)
    if not normalized:
        raise HostRejected(
            "The request Host is not valid.",
            detail={"reason": "host_malformed"},
        )
    if allow_any:
        return
    allowed = {item for item in (normalize_host(h) for h in allowed_hosts) if item}
    if normalized not in allowed:
        raise HostRejected(
            "The request Host is not allowed.",
            detail={"reason": "host_not_allowed"},
        )


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def check_csrf(
    session_id: str | None,
    presented: str | None,
    *,
    store: _csrf.CsrfStore | None = None,
) -> None:
    """Reject a missing or wrong CSRF token with ``CSRF_FAILED``."""
    active = store or _csrf.DEFAULT_CSRF_STORE
    if not active.verify(session_id, presented):
        raise CsrfFailed(
            "This change could not be verified. Reload the page and try again.",
            detail={"reason": "csrf_mismatch"},
        )


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------
def require_role(principal: Principal | None, minimum: Role | str) -> Principal:
    """Return the principal when it meets ``minimum``, else raise.

    Delegates the rank comparison to :meth:`resume_review.models.Principal.require`
    so there is exactly one definition of the role ordering. An unauthenticated
    caller raises ``UNAUTHENTICATED``; an authenticated caller with too low a role
    raises ``ROLE_INSUFFICIENT`` (via the principal), which is what the UI uses to
    disable an action rather than hide the record.
    """
    if principal is None:
        raise Unauthenticated("Sign in to continue.", detail={"reason": "no_principal"})
    minimum_role = minimum if isinstance(minimum, Role) else Role(str(minimum))
    principal.require(minimum_role)
    return principal


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    """A small fixed-window limiter kept in process memory.

    Appropriate for a single-helper deployment where the protected routes are the
    pairing exchange and the chat endpoint. It is a speed bump against a runaway
    browser tab or a scripted guess, not a distributed quota; a process restart
    clearing the counters is acceptable for that purpose.
    """

    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("A rate limit needs a positive limit.")
        if window_seconds <= 0:
            raise ValueError("A rate limit needs a positive window.")
        self._limit = int(limit)
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._events: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> float:
        return self._window

    def allow(self, key: str, *, cost: int = 1) -> bool:
        """Record ``cost`` request(s) for ``key`` when under the limit."""
        if cost < 1:
            cost = 1
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) + cost > self._limit:
                return False
            for _ in range(cost):
                events.append(now)
            return True

    def check(self, key: str, *, cost: int = 1) -> None:
        """Raise ``RATE_LIMITED`` when ``key`` is over the limit."""
        if not self.allow(key, cost=cost):
            raise RateLimited(
                "Too many requests. Wait a moment and try again.",
                detail={"reason": "rate_limited", "window_seconds": self._window},
                retryable=True,
            )

    def remaining(self, key: str) -> int:
        """How many requests ``key`` may still make in the current window."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            events = self._events.get(key)
            if events is None:
                return self._limit
            while events and events[0] <= cutoff:
                events.popleft()
            return max(0, self._limit - len(events))

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)

    def sweep(self) -> int:
        """Drop empty windows so a long-lived process does not accumulate keys."""
        now = self._clock()
        cutoff = now - self._window
        removed = 0
        with self._lock:
            for key in list(self._events):
                events = self._events[key]
                while events and events[0] <= cutoff:
                    events.popleft()
                if not events:
                    self._events.pop(key, None)
                    removed += 1
        return removed


#: Pairing exchanges are rare: ten attempts per minute per key is generous for a
#: human and tight for a script.
PAIRING_RATE_LIMITER = RateLimiter(limit=10, window_seconds=60.0)

#: Chat turns are interactive; this bounds a runaway loop without blocking editing.
CHAT_RATE_LIMITER = RateLimiter(limit=30, window_seconds=60.0)

#: Default limiter for any other route that wants one.
DEFAULT_RATE_LIMITER = RateLimiter(limit=60, window_seconds=60.0)


def check_rate_limit(key: str, *, limiter: RateLimiter | None = None, cost: int = 1) -> None:
    """Apply the given limiter (default :data:`DEFAULT_RATE_LIMITER`) to ``key``."""
    (limiter or DEFAULT_RATE_LIMITER).check(key, cost=cost)


def check_pairing_rate(key: str) -> None:
    PAIRING_RATE_LIMITER.check(key)


def check_chat_rate(key: str) -> None:
    CHAT_RATE_LIMITER.check(key)


# ---------------------------------------------------------------------------
# Composite guard for mutating requests
# ---------------------------------------------------------------------------
def check_mutation(
    *,
    headers: Mapping[str, str],
    allowed_origins: Iterable[str],
    allowed_hosts: Iterable[str],
    session_id: str | None,
    csrf_token: str | None,
    csrf_store: _csrf.CsrfStore | None = None,
    require_origin: bool = True,
) -> None:
    """Run the Origin, Host, and CSRF checks for a state-changing request.

    The order is fixed: transport-level checks first, then the token. A route
    handler must call this before reading the body or touching any state, so that a
    forged request can never reach business logic.
    """
    check_host(headers, allowed_hosts)
    check_origin(headers, allowed_origins, require=require_origin)
    check_csrf(session_id, csrf_token, store=csrf_store)
