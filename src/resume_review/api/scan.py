"""``POST /scan`` -- queue the deterministic scan (PRD section 12.1).

Authority: PRD section 12.1 (endpoint surface), section 12.2 ("Long work returns
202 with a durable job ID"; retryable POSTs require ``Idempotency-Key``), and
section 6.3 (the deterministic stage sequence runs without inference).

The endpoint only *queues*. It runs no discovery, extraction, or model call in the
request, so a slow folder never holds an HTTP connection open: the durable job id
is the handle, and a worker drains it later. The scan job key is derived from the
authenticated actor and the idempotency key, so a retry of the same request maps
to the same job while a genuinely new request is a new job.

Allowed principals (PRD 12.1): reviewer or scoped orchestrator. Both are modeled
as a session at REVIEWER rank or above; identity comes from the session, never
from the body.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..auth import require_role
from ..models import Role
from .deps import InstanceContext, get_request_id, require_mutation
from .envelope import accepted_response
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = ["ScanRequest", "register"]

_ROUTE = "scan"


class ScanRequest(BaseModel):
    """An empty, strict body.

    The scan has no parameters: its scope is the instance's configured folder.
    ``extra="forbid"`` rejects a caller that tries to smuggle a path, an endpoint,
    or a model id into the request.
    """

    model_config = ConfigDict(extra="forbid")


def register(router: APIRouter) -> None:
    @router.post("/scan", name="scan", response_class=JSONResponse)
    def queue_scan(
        payload: ScanRequest | None = None,
        guard: IdempotencyGuard = Depends(idempotency_guard(_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        body = payload.model_dump() if payload is not None else {}

        hit = guard.replay(body)
        if hit is not None:
            return accepted_response(
                hit.response,
                job_id=hit.job_id,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        job_key = f"scan:{ctx.principal.actor_ref}:{guard.key}"
        job_id = ctx.repository.enqueue_job(
            job_key,
            "scan",
            input_versions={"requested_by": ctx.principal.actor_ref},
        )
        data = {"kind": "scan", "state": "queued"}
        guard.commit(body, response=data, job_id=job_id)
        return accepted_response(
            data,
            job_id=job_id,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )
