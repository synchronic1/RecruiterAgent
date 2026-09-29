"""Review task endpoints: a human creates a task and a human closes it.

Authority: PRD section 8.4 (the review drawer's task list), section 12.1 (endpoint
table), and section 12.2 (mutation rules). A task records a human's own follow-up;
it does not move a file, and closing one never changes a disposition.

Both creation and closure attribute the actor to the authenticated session, never
to a request field. Creation is a retryable ``POST`` and therefore requires an
``Idempotency-Key``; the repository deduplicates on a task key, so a repeated
create returns the existing task with ``created=false``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from ..errors import Code, NotFound, ValidationFailed
from .deps import InstanceContext, get_request_id
from .documents import serialize_task
from .envelope import ok_response
from .idempotency import IdempotencyGuard, idempotency_guard
from .review import _reviewer_mutation

__all__ = ["register"]

_MAX_TITLE_LENGTH = 500
_MAX_DETAIL_LENGTH = 20_000
_MAX_RESOLUTION_LENGTH = 200
_SEVERITIES = {"info", "normal", "attention"}


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str = Field(min_length=1, max_length=_MAX_TITLE_LENGTH)
    task_type: str = Field(default="general", max_length=100)
    criterion_id: str | None = None
    source_revision: int | None = None
    detail: str = Field(default="", max_length=_MAX_DETAIL_LENGTH)
    severity: str = "normal"


class TaskClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: str = Field(min_length=1, max_length=_MAX_RESOLUTION_LENGTH)
    note: str | None = Field(default=None, max_length=_MAX_DETAIL_LENGTH)
    #: Required on the unscoped ``PATCH /tasks`` form.
    task_id: str | None = None


def _find_task(ctx: InstanceContext, task_id: str) -> Any:
    """Resolve a task id within this instance, or 404.

    The repository's listing is already instance-scoped, so a foreign id is
    indistinguishable from an absent one.
    """
    for task in ctx.repository.list_tasks():
        if task.id == task_id:
            return task
    raise NotFound("That task does not exist.", detail={"entity": "task"})


def register(router: Any) -> None:
    """Attach the task endpoints onto the instance-scoped router."""

    @router.post("/tasks", name="create_task")
    def create_task(
        payload: TaskCreate,
        guard: IdempotencyGuard = Depends(idempotency_guard("tasks.create")),
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Create a human review task (or return the existing one)."""
        document = ctx.repository.get_document(payload.document_id)
        if document is None or document.instance_id != ctx.instance_id:
            raise NotFound("That document does not exist.", detail={"entity": "document"})
        if payload.severity not in _SEVERITIES:
            raise ValidationFailed(
                "Unsupported task severity.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "severity"},
            )

        request_payload = payload.model_dump(mode="json")
        hit = guard.replay(request_payload)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
                job_id=hit.job_id,
                status_code=200,
            )

        task, created = ctx.repository.upsert_task(
            document.id,
            payload.task_type,
            payload.title,
            criterion_id=payload.criterion_id,
            source_revision=payload.source_revision,
            origin="human",
            detail=payload.detail,
            severity=payload.severity,
        )
        data = {"task": serialize_task(task), "created": bool(created)}
        guard.commit(request_payload, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            status_code=201,
        )

    @router.patch("/tasks/{task_id}", name="close_task")
    def close_task(
        task_id: str,
        payload: TaskClose,
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Close a task, recording who closed it and why (path-id form)."""
        _find_task(ctx, task_id)
        task = ctx.repository.close_task(
            task_id, payload.resolution, ctx.principal.actor_ref, payload.note
        )
        return ok_response(
            {"task": serialize_task(task)},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.patch("/tasks", name="close_task_in_body")
    def close_task_in_body(
        payload: TaskClose,
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Close a task named in the body (the PRD table's ``PATCH /tasks`` form)."""
        if not payload.task_id:
            raise ValidationFailed(
                "A task id is required.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "task_id"},
            )
        _find_task(ctx, payload.task_id)
        task = ctx.repository.close_task(
            payload.task_id, payload.resolution, ctx.principal.actor_ref, payload.note
        )
        return ok_response(
            {"task": serialize_task(task)},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )
