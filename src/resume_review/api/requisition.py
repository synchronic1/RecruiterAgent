"""Instance-scoped job requisition reference API.

The source reference is provenance supplied by a human. It is stored and returned
as text only; this module never opens, downloads, previews, or otherwise fetches it.
Saving a requisition does not change approved criteria or reviewer decisions.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth import require_role
from ..errors import Code, Forbidden
from ..models import Role
from .deps import InstanceContext, authorized_instance, get_request_id, require_mutation
from .envelope import ok_response

__all__ = ["RequisitionUpdate", "register"]


class RequisitionUpdate(BaseModel):
    """A bounded human-authored requisition update."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    description_text: str = Field(min_length=1, max_length=20_000)
    source_reference: str | None = Field(default=None, max_length=2048)
    expected_revision: int = Field(ge=0)

    @field_validator("description_text")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description_text must not be blank")
        return value

    @field_validator("source_reference")
    @classmethod
    def source_reference_is_safe_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source_reference must not contain control characters")
        if any(char.isspace() for char in value):
            raise ValueError("source_reference must not contain whitespace")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("source_reference must be a valid HTTP(S) URL") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
            raise ValueError("source_reference must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source_reference must not contain credentials")
        # Accessing ``port`` above validates malformed and out-of-range ports.
        del port
        return value


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "description_text": row["description_text"],
        "source_reference": row["source_reference"],
        "description_sha256": row["description_sha256"],
        "updated_at": row["updated_at"],
        "criteria_version": int(row["criteria_version"]),
    }


def register(router: APIRouter) -> None:
    @router.get("/requisition", name="get_requisition", response_class=JSONResponse)
    def get_requisition(
        ctx: InstanceContext = Depends(authorized_instance),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        return ok_response(
            {"requisition": _view(ctx.repository.get_requisition())},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.put("/requisition", name="put_requisition", response_class=JSONResponse)
    def put_requisition(
        payload: RequisitionUpdate,
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        if ctx.principal.is_agent or ctx.principal.is_worker:
            raise Forbidden(
                "Only a human reviewer or administrator can save a requisition.",
                code=Code.FORBIDDEN,
                detail={"reason": "human_reviewer_required"},
            )

        row = ctx.repository.create_or_update_job(
            payload.title,
            payload.description_text,
            payload.source_reference,
            actor=ctx.principal.actor_ref,
            actor_kind="human",
            request_id=request_id,
            expected_revision=payload.expected_revision,
        )
        return ok_response(
            {"requisition": _view(row)},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=[
                {
                    "code": "APPROVED_CRITERIA_UNCHANGED",
                    "message": "Approved criteria remain unchanged until separately reviewed and approved.",
                }
            ],
        )
