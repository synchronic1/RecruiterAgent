"""Per-session CSRF tokens for session-authenticated mutations.

Authority: PRD section 16.1, acceptance test AT-33.

    "Enforce Host and Origin checks, CSRF protection for session-authenticated
     mutations, strict CORS ..."

A CSRF token is only meaningful when it is bound to a session and compared in
constant time. This module keeps one token per session id, in the helper process,
next to the session store. A mutating route must present the token on every
request; a safe (read-only) route need not, because a cross-site read is not the
threat CSRF describes.

The token is delivered to the client by the page the helper itself served. It is
never placed in a URL and never persisted to disk, so a copied token is useless
once the session it belongs to is gone. A token presented for a session that does
not exist fails, and :func:`CsrfStore.verify` returns a boolean rather than raising
so the caller (``auth.guards``) owns the response code.
"""

from __future__ import annotations

import threading

from ..util import constant_time_equals, new_token

__all__ = ["CsrfStore", "issue", "verify", "rotate", "discard", "DEFAULT_CSRF_STORE"]

_HEADER_NAME = "X-CSRF-Token"


class CsrfStore:
    """In-memory map of session id to CSRF token.

    Tokens are stable for the life of a session so that a page refresh does not
    strand an in-flight form. ``rotate`` exists for a deliberate re-issue, for
    example after a privilege change.
    """

    def __init__(self, *, token_bytes: int = 32) -> None:
        self._token_bytes = int(token_bytes)
        self._lock = threading.RLock()
        self._tokens: dict[str, str] = {}

    @property
    def header_name(self) -> str:
        """The request header a client sends the token in."""
        return _HEADER_NAME

    def issue(self, session_id: str) -> str:
        """Return the session's token, minting one on first request."""
        if not session_id:
            raise ValueError("A CSRF token requires a session id.")
        with self._lock:
            existing = self._tokens.get(session_id)
            if existing is not None:
                return existing
            token = new_token(self._token_bytes)
            self._tokens[session_id] = token
            return token

    def rotate(self, session_id: str) -> str:
        """Replace the session's token with a fresh one and return it."""
        if not session_id:
            raise ValueError("A CSRF token requires a session id.")
        with self._lock:
            token = new_token(self._token_bytes)
            self._tokens[session_id] = token
            return token

    def verify(self, session_id: str | None, presented: str | None) -> bool:
        """Constant-time check of a presented token against the session's token.

        An unknown session, a missing value, or a mismatch all return ``False``.
        The comparison is constant time so the response does not leak how many
        leading characters were correct.
        """
        if not session_id or not presented:
            return False
        with self._lock:
            expected = self._tokens.get(session_id)
        if expected is None:
            return False
        return constant_time_equals(expected, presented)

    def discard(self, session_id: str) -> None:
        """Forget a session's token; call this when the session is revoked."""
        with self._lock:
            self._tokens.pop(session_id, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._tokens)


#: A process-wide store for the single helper. Route handlers that do not carry an
#: explicit store use this one, mirroring the process-wide session registry.
DEFAULT_CSRF_STORE = CsrfStore()


def issue(session_id: str) -> str:
    return DEFAULT_CSRF_STORE.issue(session_id)


def rotate(session_id: str) -> str:
    return DEFAULT_CSRF_STORE.rotate(session_id)


def verify(session_id: str | None, presented: str | None) -> bool:
    return DEFAULT_CSRF_STORE.verify(session_id, presented)


def discard(session_id: str) -> None:
    DEFAULT_CSRF_STORE.discard(session_id)
