"""Local launch and pairing for the folder-local helper.

Authority: PRD section 15.1, acceptance test AT-33.

    "Bind to loopback by default. Use an authenticated launch/pairing mechanism
     tied to the operating user, then issue an application session. No
     administrative privilege should be needed after initial approved
     installation."

The mechanism is deliberately narrow:

* The operator launches the helper from their own operating-system account. The
  helper prints a loopback URL carrying a single-use, short-lived pairing token.
* Opening that URL exchanges the token for a session exactly once. The token dies
  at the moment of exchange, so the URL is not a bearer credential that can be
  reused from a browser history, a chat message, or a shoulder-surfed tab.
* The token is additionally bound to the operating user it was minted for. A
  different OS account on the same machine presenting the token is refused, so a
  token leaked to another local user does not grant access.
* After the initial installation there is no elevation anywhere in this path: the
  helper runs as the operator, not as an administrator.

There is no long-lived secret in a URL. If the URL is lost before it is opened the
operator simply launches again, which mints a fresh token.
"""

from __future__ import annotations

import getpass
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from urllib.parse import urlencode

from ..errors import Code, InvalidInput, Unauthenticated
from ..models import Role
from ..util import new_token, now_iso, parse_iso
from .sessions import Session, SessionStore

__all__ = [
    "PairingTicket",
    "PairingManager",
    "PairingTokenInvalid",
    "PairingTokenExpired",
    "current_operating_user",
    "build_pair_url",
    "DEFAULT_PAIRING_TTL_SECONDS",
]

#: Two minutes is long enough for a human to click a link and short enough that a
#: leaked URL is usually already dead.
DEFAULT_PAIRING_TTL_SECONDS = 120


class PairingTokenInvalid(Unauthenticated):
    """The token is unknown, already used, or was presented by another OS user."""

    code = Code.PAIRING_TOKEN_INVALID


class PairingTokenExpired(Unauthenticated):
    """The token was valid but has passed its short lifetime."""

    code = Code.PAIRING_TOKEN_EXPIRED


def current_operating_user() -> str:
    """The operating-system account that launched the helper.

    ``getpass.getuser()`` reads the environment and falls back to the login name;
    it is the same identity the OS uses for ownership, which is what ties pairing
    to "the operating user" without requiring a password the operator would have to
    type.
    """
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - only on an exotic stripped environment
        import os

        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown-user"


def build_pair_url(
    host: str,
    port: int,
    token: str,
    *,
    scheme: str = "http",
    path: str = "/pair",
) -> str:
    """Compose the loopback launch URL for a token.

    The token is the only variable in the query string; nothing else about the
    workspace is placed in a URL that might be pasted or logged.
    """
    query = urlencode({"token": token})
    return f"{scheme}://{host}:{int(port)}{path}?{query}"


@dataclass(frozen=True)
class PairingTicket:
    """A minted, not-yet-exchanged launch token."""

    token: str
    instance_id: str
    operating_user: str
    actor_ref: str
    role: Role
    display_name: str | None
    created_at: str
    expires_at: str
    url: str

    def is_expired(self, now: str) -> bool:
        try:
            return parse_iso(now) >= parse_iso(self.expires_at)
        except (ValueError, TypeError):
            return True


class PairingManager:
    """Issues single-use pairing tokens and exchanges them for sessions."""

    def __init__(
        self,
        instance_id: str,
        sessions: SessionStore,
        *,
        host: str = "127.0.0.1",
        default_port: int | None = None,
        clock: Callable[[], str] = now_iso,
        ttl_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
        operating_user: str | None = None,
    ) -> None:
        if not instance_id:
            raise InvalidInput("Pairing requires an instance id.")
        self._instance_id = instance_id
        self._sessions = sessions
        self._host = host
        self._default_port = default_port
        self._clock = clock
        self._ttl = int(ttl_seconds)
        self._operating_user = operating_user
        self._lock = threading.RLock()
        self._tickets: dict[str, PairingTicket] = {}

    # -- issuance ----------------------------------------------------------
    def create(
        self,
        *,
        actor_ref: str,
        role: Role = Role.REVIEWER,
        display_name: str | None = None,
        host: str | None = None,
        port: int | None = None,
        ttl_seconds: int | None = None,
        operating_user: str | None = None,
    ) -> PairingTicket:
        """Mint a single-use token and return the ticket describing its URL."""
        if not actor_ref:
            raise InvalidInput("Pairing requires an authenticated actor reference.")
        chosen_port = port if port is not None else self._default_port
        if chosen_port is None:
            raise InvalidInput(
                "A pairing URL needs the helper's loopback port.",
                detail={"reason": "port_missing"},
            )
        role = role if isinstance(role, Role) else Role(str(role))
        now = self._clock()
        ttl = int(ttl_seconds if ttl_seconds is not None else self._ttl)
        token = new_token(32)
        ticket = PairingTicket(
            token=token,
            instance_id=self._instance_id,
            operating_user=operating_user or self._resolve_operating_user(),
            actor_ref=actor_ref,
            role=role,
            display_name=display_name,
            created_at=now,
            expires_at=(parse_iso(now) + timedelta(seconds=ttl)).isoformat(),
            url=build_pair_url(host or self._host, chosen_port, token),
        )
        with self._lock:
            self._tickets[token] = ticket
        return ticket

    # -- exchange ----------------------------------------------------------
    def exchange(
        self,
        token: str | None,
        *,
        operating_user: str | None = None,
    ) -> Session:
        """Consume a token and issue the session it authorises.

        The token is removed before any validation, so a rejected attempt cannot be
        retried and a successful one cannot be replayed. Failures use the specific
        code the caller must report: ``PAIRING_TOKEN_INVALID`` for unknown, already
        used, or wrong-os-user tokens, and ``PAIRING_TOKEN_EXPIRED`` for a token
        that aged out.
        """
        if not token:
            raise PairingTokenInvalid(
                "That pairing link is not valid. Launch the helper again.",
                detail={"reason": "missing"},
            )
        with self._lock:
            ticket = self._tickets.pop(token, None)
        if ticket is None:
            raise PairingTokenInvalid(
                "That pairing link has already been used or is not valid. "
                "Launch the helper again.",
                detail={"reason": "unknown_or_used"},
            )

        if ticket.is_expired(self._clock()):
            raise PairingTokenExpired(
                "That pairing link has expired. Launch the helper again.",
                detail={"reason": "expired"},
            )

        presented_user = operating_user if operating_user is not None else self._resolve_operating_user()
        if presented_user != ticket.operating_user:
            raise PairingTokenInvalid(
                "That pairing link belongs to a different operating-system account.",
                detail={"reason": "wrong_operating_user"},
            )

        # A pairing exchange is a local-owner launch, so the resulting session is
        # marked as such; the role still comes from the account, never the URL.
        return self._sessions.issue(
            ticket.instance_id,
            ticket.actor_ref,
            ticket.role,
            display_name=ticket.display_name,
            is_local_owner=True,
        )

    # -- maintenance -------------------------------------------------------
    def sweep_expired(self, *, now: str | None = None) -> int:
        moment = now or self._clock()
        with self._lock:
            doomed = [tok for tok, t in self._tickets.items() if t.is_expired(moment)]
            for tok in doomed:
                self._tickets.pop(tok, None)
            return len(doomed)

    def active_count(self) -> int:
        with self._lock:
            return len(self._tickets)

    def describe(self, ticket: PairingTicket) -> dict[str, object]:
        """A loggable summary that deliberately omits the token itself."""
        return {
            "instance_id": ticket.instance_id,
            "operating_user": ticket.operating_user,
            "actor_ref": ticket.actor_ref,
            "role": str(ticket.role.value),
            "created_at": ticket.created_at,
            "expires_at": ticket.expires_at,
        }

    def _resolve_operating_user(self) -> str:
        return self._operating_user or current_operating_user()
