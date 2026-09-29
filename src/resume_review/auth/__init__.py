"""Authentication, session binding, CSRF, and request guards.

Authority: PRD sections 15.1, 15.3 and 16.1; acceptance test AT-33.

This package owns everything that decides *who* a request is and *what it may do*.
It holds no applicant data and touches no managed file; it is deliberately
importable by ``api`` and by ``bootstrap`` without dragging in a layer that imports
them back.

Identity flows in one direction only: an operating-system-owned pairing token or an
account password authenticates an ``actor_ref``; a session binds that actor to one
instance and one role; a ``Principal`` carries the result into a route handler.
There is no code path anywhere in this package that accepts an actor, a role, or a
display name from request text.
"""

from __future__ import annotations

from .csrf import CsrfStore
from .csrf import discard as csrf_discard
from .csrf import issue as csrf_issue
from .csrf import rotate as csrf_rotate
from .csrf import verify as csrf_verify
from .guards import (
    CHAT_RATE_LIMITER,
    DEFAULT_RATE_LIMITER,
    PAIRING_RATE_LIMITER,
    CsrfFailed,
    HostRejected,
    OriginRejected,
    RateLimited,
    RateLimiter,
    check_chat_rate,
    check_csrf,
    check_host,
    check_mutation,
    check_origin,
    check_pairing_rate,
    check_rate_limit,
    normalize_host,
    normalize_origin,
    require_role,
)
from .pairing import (
    DEFAULT_PAIRING_TTL_SECONDS,
    PairingManager,
    PairingTicket,
    PairingTokenExpired,
    PairingTokenInvalid,
    build_pair_url,
    current_operating_user,
)
from .sessions import (
    DEFAULT_ABSOLUTE_TTL_SECONDS,
    DEFAULT_IDLE_TTL_SECONDS,
    InstanceMismatch,
    Session,
    SessionExpired,
    SessionStore,
    session_cookie_name,
)
from .store import (
    MIN_PASSWORD_LENGTH,
    SCRYPT_DKLEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    AccountStore,
    AccountStoreCorrupt,
    User,
    assert_host_local_secret_path,
    hash_password,
    verify_password,
)

__all__ = [
    # Account store
    "AccountStore",
    "AccountStoreCorrupt",
    "User",
    "hash_password",
    "verify_password",
    "assert_host_local_secret_path",
    "MIN_PASSWORD_LENGTH",
    "SCRYPT_N",
    "SCRYPT_R",
    "SCRYPT_P",
    "SCRYPT_DKLEN",
    # Sessions
    "Session",
    "SessionStore",
    "SessionExpired",
    "InstanceMismatch",
    "session_cookie_name",
    "DEFAULT_ABSOLUTE_TTL_SECONDS",
    "DEFAULT_IDLE_TTL_SECONDS",
    # Pairing
    "PairingManager",
    "PairingTicket",
    "PairingTokenInvalid",
    "PairingTokenExpired",
    "current_operating_user",
    "build_pair_url",
    "DEFAULT_PAIRING_TTL_SECONDS",
    # CSRF
    "CsrfStore",
    "csrf_issue",
    "csrf_verify",
    "csrf_rotate",
    "csrf_discard",
    # Guards
    "OriginRejected",
    "HostRejected",
    "CsrfFailed",
    "RateLimited",
    "RateLimiter",
    "normalize_origin",
    "normalize_host",
    "check_origin",
    "check_host",
    "check_csrf",
    "check_mutation",
    "require_role",
    "check_rate_limit",
    "check_pairing_rate",
    "check_chat_rate",
    "DEFAULT_RATE_LIMITER",
    "PAIRING_RATE_LIMITER",
    "CHAT_RATE_LIMITER",
]
