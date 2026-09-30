"""Criteria endpoints, and the route-registration hub for this endpoint group.

Authority: PRD section 12.1 (endpoint surface), section 12.2 (retryable POSTs
require ``Idempotency-Key``; versioned changes require ``expected_revision``), and
section 7.4 ("The operator reviews proposed criteria before activation").

The central separation this module enforces and tests:

* ``POST /criteria/proposals`` records definitions **only**. It is open to a
  reviewer or a scoped agent (the model may suggest job-related criteria), and it
  never activates anything: the new rows are unapproved, and the active criteria
  version is unchanged by the call.
* ``POST /criteria/{version}/activate`` is the human decision. It refuses an
  agent- or worker-identity session with ``APPROVAL_MUST_BE_HUMAN``, checks the
  caller's ``expected_revision`` against committed state, and only then approves
  the version.

Identity (creator, origin, approver) is taken from the authenticated session,
never from the body: the request schemas forbid unknown fields, so a caller cannot
name its own actor.

Registration
------------

``api.app.DEFAULT_ROUTE_MODULES`` lists this module (and only this one of the
group), so :func:`register` also wires the sibling endpoint modules -- ``scan``,
``analysis``, ``jobs``, and ``backup`` -- onto the same prefixed router. Each
sibling exposes its own ``register(router)``; importing this module imports them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import require_role
from ..db import RevisionConflict
from ..errors import Code, Forbidden
from ..models import Role
from .deps import InstanceContext, get_request_id, require_mutation, require_operation
from .envelope import ok_response
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = [
    "ActivationRequest",
    "CriterionProposal",
    "ProposalRequest",
    "register",
]

_PROPOSE_ROUTE = "criteria.proposals"
_ACTIVATE_ROUTE = "criteria.activate"

#: Origins a proposal may carry. Derived from the session, never the body.
_ORIGIN_AGENT = "agent_proposal"
_ORIGIN_HUMAN = "human"


class CriterionProposal(BaseModel):
    """One proposed criterion definition. No ``origin``/``created_by`` field exists."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1, max_length=128)
    definition: str = Field(min_length=1, max_length=8000)
    rationale: str = Field(default="", max_length=8000)
    evidence_rule: str = Field(default="", max_length=8000)
    label: str | None = Field(default=None, max_length=32)


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[CriterionProposal] = Field(min_length=1, max_length=100)


class ActivationRequest(BaseModel):
    """The human approval. ``expected_revision`` guards against a stale page."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


def _proposal_view(criterion: Any) -> dict[str, Any]:
    return {
        "criterion_id": criterion.criterion_id,
        "version": int(criterion.version),
        "definition": criterion.definition,
        "label": criterion.label,
        "origin": criterion.origin,
        "approved": bool(criterion.approved),
    }


def register(router: APIRouter) -> None:
    @router.get("/criteria", name="get_criteria", response_class=JSONResponse)
    def get_criteria(
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        request_id: str = Depends(get_request_id),
    ):
        pending = ctx.db.scalar(
            "SELECT MAX(version) FROM criteria WHERE instance_id = ? AND approved_at IS NULL",
            (ctx.instance_id,),
        )
        proposals = ctx.repository.list_criteria(int(pending), approved_only=False) if pending else []
        return ok_response(
            {
                "active_version": ctx.repository.active_criteria_version(),
                "active": [_proposal_view(item) for item in ctx.repository.list_criteria()],
                "pending_version": int(pending) if pending else None,
                "proposals": [_proposal_view(item) for item in proposals],
            },
            request_id=request_id, instance_id=ctx.instance_id, state_revision=ctx.state_revision,
        )

    @router.post("/criteria/proposals", name="criteria_proposals", response_class=JSONResponse)
    def propose_criteria(
        payload: ProposalRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard(_PROPOSE_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        body = payload.model_dump()

        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        origin = _ORIGIN_AGENT if ctx.principal.is_agent else _ORIGIN_HUMAN
        created_by = ctx.principal.actor_ref
        created = [
            ctx.repository.create_criteria_proposal(
                criterion_id=item.criterion_id,
                definition=item.definition,
                rationale=item.rationale,
                evidence_rule=item.evidence_rule,
                label=item.label,
                created_by=created_by,
                origin=origin,
            )
            for item in payload.proposals
        ]

        result = {
            "version": int(created[-1].version),
            "proposals": [_proposal_view(criterion) for criterion in created],
            "active_version": ctx.repository.active_criteria_version(),
            "activated": False,
        }
        guard.commit(body, response=result)
        return ok_response(
            result,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=[
                {
                    "code": "PROPOSAL_NOT_ACTIVE",
                    "message": "Proposed criteria are not active until a human approves the version.",
                }
            ],
        )

    @router.post(
        "/criteria/{version}/activate",
        name="criteria_activate",
        response_class=JSONResponse,
    )
    def activate_criteria(
        version: int,
        payload: ActivationRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard(_ACTIVATE_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.REVIEWER)
        if ctx.principal.is_agent or ctx.principal.is_worker:
            raise Forbidden(
                "Only a human reviewer can approve a criteria version.",
                code=Code.APPROVAL_MUST_BE_HUMAN,
                detail={"reason": "human_approval_required"},
            )

        body = {"version": int(version), **payload.model_dump()}
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        if int(payload.expected_revision) != ctx.state_revision:
            raise RevisionConflict(
                "The criteria changed since this approval was prepared.",
                current_revision=ctx.state_revision,
            )

        activated = ctx.repository.activate_criteria_version(int(version), actor=ctx.principal.actor_ref)
        result = {
            "version": int(version),
            "active_version": ctx.repository.active_criteria_version(),
            "criteria": [criterion.criterion_id for criterion in activated],
        }
        guard.commit(body, response=result)
        return ok_response(
            result,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    # Sibling endpoint modules for this group. ``api.app`` discovers only this
    # module by default, so registration is delegated here.
    from . import analysis as analysis_module
    from . import backup as backup_module
    from . import jobs as jobs_module
    from . import scan as scan_module

    scan_module.register(router)
    analysis_module.register(router)
    jobs_module.register(router)
    backup_module.register(router)
