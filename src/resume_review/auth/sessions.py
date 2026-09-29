"""Session issuance, instance binding, and expiry.

Authority: PRD sections 15.1, 12 and 16.1, acceptance test AT-33.

    "Use an authenticated launch/pairing mechanism tied to the operating user,
     then issue an application session."

    "Enforce ... session expiry. Use instance-specific session binding; do not
     assume cookies are isolated by TCP port."

A session is an opaque, unguessable id held **in the helper process only**. It is
never a JWT, never signed, and never persisted to the portable database: the
moment the helper stops, every session is gone, which is the correct default for a
folder-local tool whose authority comes from being able to launch it.

Every session is bound to exactly one ``instance_id``. A UUID is not
authorization (PRD section 12): presenting a valid session for instance A against
instance B is refused with ``INSTANCE_MISMATCH`` even though the session itself is
well formed. The session cookie name is derived from the instance id so that two
instances served from the same host cannot collide through a shared cookie jar,
regardless of which TCP port each helper happens to bind.

There are two independent expiries. The absolute expiry bounds the total lifetime;
the idle expiry is refreshed on use and bounds a forgotten open tab. Both are
enforced on every resolve.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from ..errors import Code, Forbidden, ResumeReviewError, Unauthenticated
from ..models import Principal, Role, sha256_hex
from ..util import new_token, now_iso, parse_iso

__all__ = [
    "Session",
    "SessionStore",
    "InstanceMismatch",
    "SessionExpired",
    "session_cookie_name",
    "DEFAULT_ABSOLUTE_TTL_SECONDS",
    "DEFAULT_IDLE_TTL_SECONDS",
]

#: A full working day before a reviewer must re-pair. Short enough that a lost
#: laptop does not hold a live session for long, long enough not to interrupt work.
DEFAULT_ABSOLUTE_TTL_SECONDS = 12 * 3600

#: Thirty minutes of inactivity ends a session. Generous for reading a resume,
#: short for an unattended browser left on a shared machine.
DEFAULT_IDLE_TTL_SECONDS = 30 * 60


class InstanceMismatch(Forbidden):
    """A valid session presented for the wrong instance."""

    code = Code.INSTANCE_MISMATCH


class SessionExpired(Unauthenticated):
    """A session past its absolute or idle expiry."""

    code = Code.SESSION_EXPIRED


def session_cookie_name(instance_id: str) -> str:
    """A cookie name scoped to one instance id.

    Cookie scoping by port does not exist, so a plain ``session`` cookie would be
    shared by every helper on the host. Hashing the instance id into the name keeps
    the namespaces separate even inside one browser profile.
    """
    return f"rr_sess_{sha256_hex(instance_id)[:16]}"


@dataclass
class Session:
    """One authenticated browser session for one instance."""

    session_id: str
    instance_id: str
    actor_ref: str
    role: Role
    display_name: str | None = None
    is_local_owner: bool = False
    created_at: str = ""
    expires_at: str = ""
    idle_expires_at: str = ""
    last_seen_at: str = ""

    def is_expired(self, now: str) -> bool:
        return _at_or_after(now, self.expires_at)

    def is_idle_expired(self, now: str) -> bool:
        return _at_or_after(now, self.idle_expires_at)

    def to_public_dict(self) -> dict[str, object]:
        """Serialisable view. The session id itself is included so the client can
        echo it; nothing else here is secret."""
        return {
            "session_id": self.session_id,
            "instance_id": self.instance_id,
            "actor_ref": self.actor_ref,
            "role": str(self.role.value),
            "display_name": self.display_name,
            "is_local_owner": self.is_local_owner,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "idle_expires_at": self.idle_expires_at,
        }


class SessionStore:
    """In-memory session registry for one helper process.

    Serialised with a lock because FastAPI may resolve a session from any worker
    thread. There is no persistence by design (see the module docstring).
    """

    def __init__(
        self,
        *,
        absolute_ttl_seconds: int = DEFAULT_ABSOLUTE_TTL_SECONDS,
        idle_ttl_seconds: int = DEFAULT_IDLE_TTL_SECONDS,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._absolute_ttl = int(absolute_ttl_seconds)
        self._idle_ttl = int(idle_ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    # -- issuance ----------------------------------------------------------
    def issue(
        self,
        instance_id: str,
        actor_ref: str,
        role: Role,
        *,
        display_name: str | None = None,
        is_local_owner: bool = False,
        absolute_ttl_seconds: int | None = None,
        idle_ttl_seconds: int | None = None,
    ) -> Session:
        """Mint a new session bound to ``instance_id``.

        The caller must already have authenticated ``actor_ref`` (for example via
        :class:`resume_review.auth.store.AccountStore` or a pairing exchange).
        """
        if not instance_id or not actor_ref:
            raise ResumeReviewError(
                "A session needs both an instance and an authenticated actor.",
                code=Code.INVALID_INPUT,
                http_status=422,
            )
        role = role if isinstance(role, Role) else Role(str(role))
        absolute = int(absolute_ttl_seconds if absolute_ttl_seconds is not None else self._absolute_ttl)
        idle = int(idle_ttl_seconds if idle_ttl_seconds is not None else self._idle_ttl)
        now = self._clock()
        now_dt = parse_iso(now)
        session = Session(
            session_id=new_token(32),
            instance_id=instance_id,
            actor_ref=actor_ref,
            role=role,
            display_name=display_name,
            is_local_owner=is_local_owner,
            created_at=now,
            expires_at=(now_dt + timedelta(seconds=absolute)).isoformat(),
            idle_expires_at=(now_dt + timedelta(seconds=idle)).isoformat(),
            last_seen_at=now,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    # -- resolution --------------------------------------------------------
    def resolve(
        self,
        session_id: str | None,
        *,
        instance_id: str | None = None,
        touch: bool = True,
    ) -> Session:
        """Look up a session and enforce expiry, then instance binding.

        Raises ``Unauthenticated`` for an unknown id, ``SessionExpired`` for either
        expiry, and ``InstanceMismatch`` when a live session is used against a
        different instance. Expired sessions are dropped as a side effect.
        """
        if not session_id:
            raise Unauthenticated("Sign in to continue.", detail={"reason": "no_session"})
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise Unauthenticated("Sign in to continue.", detail={"reason": "unknown_session"})

            now = self._clock()
            if session.is_expired(now):
                self._sessions.pop(session_id, None)
                raise SessionExpired(
                    "Your session has expired. Sign in again.",
                    detail={"reason": "absolute"},
                )
            if session.is_idle_expired(now):
                self._sessions.pop(session_id, None)
                raise SessionExpired(
                    "Your session timed out from inactivity. Sign in again.",
                    detail={"reason": "idle"},
                )
            if instance_id is not None and instance_id != session.instance_id:
                # The session is genuine; the request is simply aimed at the wrong
                # instance. Do not reveal the session's own instance id.
                raise InstanceMismatch(
                    "This session is not valid for the requested instance.",
                    detail={"reason": "instance_mismatch"},
                )

            if touch:
                now_dt = parse_iso(now)
                refreshed = now_dt + timedelta(seconds=self._idle_ttl)
                absolute = parse_iso(session.expires_at)
                if refreshed > absolute:
                    refreshed = absolute
                session = replace(
                    session,
                    last_seen_at=now,
                    idle_expires_at=refreshed.isoformat(),
                )
                self._sessions[session_id] = session
            return session

    def principal(self, session: Session) -> Principal:
        """Build the immutable principal a request handler authorizes against."""
        return Principal(
            actor_ref=session.actor_ref,
            role=session.role,
            session_id=session.session_id,
            instance_id=session.instance_id,
            is_local_owner=session.is_local_owner,
        )

    # -- revocation and maintenance ---------------------------------------
    def revoke(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def revoke_actor(self, actor_ref: str) -> int:
        """Drop every session for an actor. Used when a role changes or an account
        is disabled, so a stale principal cannot keep acting with an old role."""
        with self._lock:
            doomed = [sid for sid, s in self._sessions.items() if s.actor_ref == actor_ref]
            for sid in doomed:
                self._sessions.pop(sid, None)
            return len(doomed)

    def sweep_expired(self, *, now: str | None = None) -> int:
        """Drop every expired session and return how many were removed."""
        moment = now or self._clock()
        with self._lock:
            doomed = [
                sid
                for sid, s in self._sessions.items()
                if s.is_expired(moment) or s.is_idle_expired(moment)
            ]
            for sid in doomed:
                self._sessions.pop(sid, None)
            return len(doomed)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


def _at_or_after(now: str, deadline: str) -> bool:
    """True when ``now`` has reached ``deadline``.

    Compared as parsed datetimes rather than strings so a caller-injected clock
    with a different precision or offset still behaves.
    """
    try:
        return _parse(now) >= _parse(deadline)
    except (ValueError, TypeError):
        # An unparseable deadline must fail closed: treat the session as expired.
        return True


def _parse(value: str) -> datetime:
    return parse_iso(value)
