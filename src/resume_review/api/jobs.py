"""``GET /jobs/{id}`` -- poll one durable job (PRD section 12.1).

Authority: PRD section 12.1 ("Poll progress/result", allowed principal
"Authorized requester") and section 12.2 (a standard response carries the
request ID, instance ID, committed state revision, data, and warnings).

Scoping, not obscurity
----------------------

Two independent checks decide whether a caller may read a job, and both are
required:

* the job row must belong to the instance this helper serves, and
* the caller must be *the* requester -- either the actor recorded when the job was
  enqueued, the worker currently holding the lease, or an administrator, who owns
  the workspace and needs to see a job that has stalled or failed (a failed job has
  already had its lease cleared, so no worker can read it).

Anything else -- a different reviewer, a viewer, a job id from another instance --
is answered with the same ``404 NOT_FOUND`` a missing job produces. The response
therefore never reveals whether a job the caller may not see exists.

The lease token is a secret the worker uses to submit a result; it is deliberately
never included in this view.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..errors import NotFound
from ..models import Role
from .deps import InstanceCtx, RequestId
from .envelope import ok_response

__all__ = ["register"]


def _parse_versions(raw: Any) -> dict[str, Any]:
    """Decode a job's ``input_versions`` column, tolerating a corrupt value."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _job_not_found() -> NotFound:
    """One indistinguishable error for absent and unauthorized alike."""
    return NotFound("That job does not exist.", detail={"entity": "job"})


def _job_view(job: Mapping[str, Any]) -> dict[str, Any]:
    """A safe projection of a job row: no lease token, no request text."""
    versions = _parse_versions(job.get("input_versions"))
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 0)
    view: dict[str, Any] = {
        "job_id": str(job.get("id") or ""),
        "kind": str(job.get("kind") or ""),
        "state": str(job.get("state") or ""),
        "document_id": job.get("document_id"),
        "attempts": attempts,
        "max_attempts": max_attempts,
        "progress": {"attempts": attempts, "max_attempts": max_attempts},
        "cancel_requested": bool(job.get("cancel_requested")),
        "result_ref": job.get("result_ref"),
        "error_code": job.get("error_code"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "lease_expires_at": job.get("lease_expires_at"),
    }
    if "source_revision" in versions:
        view["source_revision"] = int(versions["source_revision"])
    if "criteria_version" in versions:
        view["criteria_version"] = int(versions["criteria_version"])
    return view


def register(router: APIRouter) -> None:
    @router.get("/jobs/{job_id}", name="get_job", response_class=JSONResponse)
    def get_job(job_id: str, ctx: InstanceCtx, request_id: RequestId) -> JSONResponse:
        job = ctx.repository.get_job(job_id)
        if job is None or str(job.get("instance_id") or "") != ctx.instance_id:
            raise _job_not_found()

        principal = ctx.principal
        versions = _parse_versions(job.get("input_versions"))
        requester = str(versions.get("requested_by") or "")
        lease_owner = str(job.get("lease_owner") or "")

        authorized = (
            (requester and requester == principal.actor_ref)
            or (lease_owner and lease_owner == principal.actor_ref)
            or principal.role == Role.ADMINISTRATOR
        )
        if not authorized:
            raise _job_not_found()

        return ok_response(
            _job_view(job),
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            job_id=str(job.get("id") or "") or None,
        )
