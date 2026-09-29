"""Retryable-POST protection keyed by ``Idempotency-Key`` (PRD section 12.2).

    "For retryable POST operations require ``Idempotency-Key``, scoped to
    principal, instance, and route. Reusing a key with a different payload returns
    a conflict; repeating the same request returns the original result or job ID
    without repeating side effects."

There is exactly one durable store for this: the ``idempotency_records`` table
reached through :meth:`resume_review.db.repository_analysis.AnalysisRepositoryMixin\
.idempotency_lookup` and ``.idempotency_store``. This module adds only the
request-scoped policy: how the header is validated, how the scope string is built
from the principal and route, and how a handler replays a stored outcome.

Scope layout: ``api:<route>:<actor_ref>:<role>``. The instance id is already part
of the table's key, so it is not repeated here; the role is included so a role
change cannot replay another role's stored result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, Request

from ..db import Repository
from ..errors import InvalidInput
from ..models import Principal, canonical_hash
from .deps import InstanceContext, authorized_instance
from .envelope import MUTATION_HEADER_IDEMPOTENCY

__all__ = [
    "IDEMPOTENCY_HEADER",
    "MAX_KEY_LENGTH",
    "IdempotencyResult",
    "IdempotencyGuard",
    "request_hash",
    "build_scope",
    "validate_key",
    "idempotency_guard",
]

IDEMPOTENCY_HEADER = MUTATION_HEADER_IDEMPOTENCY
MAX_KEY_LENGTH = 128

#: Keys are opaque; a conservative charset keeps them safe for a database key and
#: a log line. An invalid key is a client bug and is refused, never truncated.
_KEY_RE = re.compile(r"^[A-Za-z0-9._~:@+\-]{1," + str(MAX_KEY_LENGTH) + r"}$")


def request_hash(payload: Any) -> str:
    """The canonical hash of a request payload.

    Uses the same canonical JSON as plan hashes, so two semantically equal JSON
    bodies hash the same regardless of key order or whitespace.
    """
    return canonical_hash(payload)


def build_scope(route: str, principal: Principal) -> str:
    """The principal-and-route scope for one retryable operation."""
    return f"api:{route}:{principal.actor_ref}:{principal.role.value}"


def validate_key(raw: str | None) -> str:
    """Validate an ``Idempotency-Key`` header value, or raise 422."""
    if not raw:
        raise InvalidInput(
            "This request requires an Idempotency-Key header.",
            detail={"header": IDEMPOTENCY_HEADER, "reason": "missing"},
        )
    key = raw.strip()
    if not _KEY_RE.match(key):
        raise InvalidInput(
            "The Idempotency-Key header is not a valid key.",
            detail={"header": IDEMPOTENCY_HEADER, "reason": "malformed"},
        )
    return key


@dataclass(frozen=True)
class IdempotencyResult:
    """A stored outcome that satisfies a repeat of the same request."""

    scope: str
    key: str
    request_hash: str
    response: Any = None
    job_id: str | None = None
    state_revision: int | None = None


class IdempotencyGuard:
    """Per-request helper that replays or records one operation's outcome.

    A handler calls :meth:`replay` with the parsed request payload *before*
    performing any side effect. When it returns non-``None`` the handler returns the
    stored result and does nothing else. After a successful first execution the
    handler calls :meth:`commit` with the same payload and the result to persist.
    A repeated key with a different payload raises ``409`` from the lookup.
    """

    def __init__(self, repository: Repository, *, scope: str, key: str) -> None:
        self._repository = repository
        self.scope = scope
        self.key = key
        self._hash: str | None = None

    def replay(self, payload: Any) -> IdempotencyResult | None:
        """The stored outcome for ``payload``, or ``None`` on a first execution."""
        digest = request_hash(payload)
        stored = self._repository.idempotency_lookup(self.scope, self.key, digest)
        if stored is None:
            return None
        self._hash = digest
        return IdempotencyResult(
            scope=str(stored.get("scope", self.scope)),
            key=str(stored.get("key", self.key)),
            request_hash=str(stored.get("request_hash", digest)),
            response=stored.get("response"),
            job_id=stored.get("job_id"),
            state_revision=stored.get("state_revision"),
        )

    def commit(self, payload: Any, *, response: Any = None, job_id: str | None = None) -> None:
        """Persist the outcome of a first execution under this key."""
        digest = self._hash or request_hash(payload)
        self._repository.idempotency_store(
            self.scope, self.key, digest, response=response, job_id=job_id
        )


def idempotency_guard(route: str) -> Callable[..., Any]:
    """Build the FastAPI dependency for one retryable route.

    Usage::

        @router.post("/decisions/bulk")
        def bulk(
            payload: BulkRequest,
            guard: IdempotencyGuard = Depends(idempotency_guard("decisions.bulk")),
            ...
        ):
            hit = guard.replay(payload.model_dump())
            if hit is not None:
                return ok_response(hit.response, ...)
            ...
            guard.commit(payload.model_dump(), response=result)
    """

    async def dependency(
        request: Request,
        ctx: InstanceContext = Depends(authorized_instance),
    ) -> IdempotencyGuard:
        key = validate_key(request.headers.get(IDEMPOTENCY_HEADER))
        return IdempotencyGuard(
            ctx.repository,
            scope=build_scope(route, ctx.principal),
            key=key,
        )

    dependency.__name__ = f"idempotency_guard_{route.replace('.', '_')}"
    return dependency
