"""File-action endpoints: intent, plan, approve, apply, cancel, restore-plan.

Authority: PRD section 12.1 (endpoint surface), 12.2 (mutation rules), 12.3 (the
action-plan payload), and section 13 (safe file actions, approvals, recovery).
AGENTS.md constraints 3, 4, and 5 are the ones this module exists to enforce.

This module is the *only* HTTP surface for the safety-critical file path. It owns
no move logic: every concrete plan, every approval gate, every revalidation, and
every filesystem operation is delegated to the real
:mod:`resume_review.actions` modules. A handler does four things and nothing else:

1. resolve the authenticated reviewer identity from the session (never a body
   field) and authorize the operation;
2. read an explicit, validated request body;
3. call the planner, the executor, or a repository method; and
4. answer through the standard envelope.

The rules this module encodes:

* **A decision never moves a file.** Saving an intent changes only the pending
  intent record (PRD 10.1); it is a request awaiting a plan, not a move.
* **Approval is bound to the plan hash and is human-only.** ``POST
  /actions/{batch}/approve`` refuses an ``agent:``/``worker:`` principal with
  ``APPROVAL_MUST_BE_HUMAN``, and a plan whose stored hash differs from the one the
  human saw is a conflict. Model text and worker identity can never manufacture an
  approval (PRD 13.1).
* **Apply requires a still-valid approval.** The executor revalidates the whole
  plan and each remaining operation against committed state; this module never
  repairs a stale plan (PRD 13.1).
* **Restore is a new plan, not a move.** ``restore-plan`` mints a fresh inverse
  plan and travels the same plan -> approve -> apply path.
* **No endpoint accepts a destination path.** Destinations are derived from
  document ids by the planner. Every request model forbids unknown fields, so a
  caller-supplied ``destination``, ``actor``, ``requested_by``, or ``approval`` is
  a 422 rather than a silently honored field.

Workspace-root resolution
-------------------------
The frozen API core's :class:`~resume_review.api.deps.ApiRuntime` carries no
workspace root, yet the planner and executor require one. This module resolves it
without trusting the caller: the protected host registry is the authoritative
``instance_id -> canonical root`` mapping (the same source the CLI uses), with a
fallback to the documented folder layout (``<root>/.review/review.db``) for an
embedded instance that has no registry entry. Neither path accepts a
caller-supplied value. This gap in the frozen core is reported separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..actions.executor import APPROVAL_LIFETIME_SECONDS, apply_batch
from ..actions.planner import plan_actions
from ..actions.restore import plan_restore_from_batch
from ..auth import require_role
from ..db import RevisionConflict
from ..errors import (
    Code,
    Conflict,
    Forbidden,
    NotFound,
    ResumeReviewError,
)
from ..models import (
    REVIEW_DIR,
    ExecutionState,
    Principal,
    Role,
    jsonable,
)
from ..util import now_iso, seconds_from_now_iso
from .deps import (
    InstanceContext,
    get_request_id,
    require_mutation,
    resolve_document_id,
)
from .envelope import ok_response
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = [
    "register",
    "IntentRequest",
    "PlanRequest",
    "ApproveRequest",
    "ApplyRequest",
    "CancelRequest",
    "RestorePlanRequest",
]

#: Batch states from which cancel is allowed: work has not started.
_CANCELABLE_STATES = frozenset(
    {ExecutionState.PLANNED.value, ExecutionState.APPROVED.value}
)


# ---------------------------------------------------------------------------
# Request models (all forbid unknown fields, so no destination can be smuggled in)
# ---------------------------------------------------------------------------
class IntentRequest(BaseModel):
    """Save or cancel one document's pending intent (PRD 10)."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=32)
    expected_revision: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=2000)


class PlanRequest(BaseModel):
    """Resolve an explicit set of document ids into a concrete plan (PRD 12.3).

    ``intents`` is ``{document_id: intent_value}``. ``overrides`` is
    ``{document_id: {...}}``; the planner honors only ``decision_reconfirmed`` and
    ``override_reason`` there. A destination is never accepted from the caller.
    """

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=500)
    intents: dict[str, str] | None = None
    overrides: dict[str, dict[str, Any]] | None = None


class ApproveRequest(BaseModel):
    """Record plan-bound human authorization. ``plan_hash`` is the plan the human saw."""

    model_config = ConfigDict(extra="forbid")

    plan_hash: str = Field(min_length=8, max_length=128)
    expected_revision: int = Field(ge=0)


class ApplyRequest(BaseModel):
    """Start an already approved batch. ``expected_revision`` is the batch revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    dry_run: bool = False


class CancelRequest(BaseModel):
    """Cancel a batch whose work has not started."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class RestorePlanRequest(BaseModel):
    """Create a new inverse plan for an applied batch. Carries no destination."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _warnings(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Shape free-text action warnings into schema-valid warning objects."""
    out: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, Mapping):
            out.append(dict(item))
        else:
            out.append({"code": "ACTION_WARNING", "message": str(item)})
    return out


def _require_reviewer(ctx: InstanceContext) -> None:
    require_role(ctx.principal, Role.REVIEWER)


def _reject_non_human(principal: Principal) -> None:
    """Refuse an approval attempted by a model or worker identity (PRD 13.1)."""
    if principal.is_agent or principal.is_worker:
        raise Forbidden(
            "Only an authenticated human reviewer can approve a plan.",
            code=Code.APPROVAL_MUST_BE_HUMAN,
            detail={"reason": "non_human_approver"},
        )


def _resolve_workspace_root(ctx: InstanceContext) -> Path:
    """The registered workspace root for this instance, or raise.

    The host registry is authoritative (PRD section 4: the registry holds the
    canonical root for an instance id). A database under the documented ``.review``
    folder is the fallback for an embedded instance with no registry entry. A
    caller-supplied root is never accepted.
    """
    root = _registered_root(ctx.repository.instance_id)
    if root is None:
        root = _derived_root(ctx.repository.db.path)
    if root is None:
        raise ResumeReviewError(
            "The workspace root for this instance could not be resolved, so no file "
            "action may be planned or applied.",
            code=Code.WORKSPACE_UNINITIALISED,
            detail={"reason": "root_unresolved"},
        )
    return root


def _registered_root(instance_id: str) -> Path | None:
    """The canonical root recorded for ``instance_id``, or ``None``.

    A registered root that is no longer a directory is ignored so the fallback can
    answer, rather than planning against a vanished path. A corrupt registry is not
    swallowed: it propagates as the integrity error it is.
    """
    from ..bootstrap import HostRegistry

    try:
        entry = HostRegistry().get(str(instance_id))
    except OSError:
        return None
    if entry is None or not entry.canonical_root:
        return None
    candidate = Path(entry.canonical_root)
    return candidate if candidate.is_dir() else None


def _derived_root(db_path: Path) -> Path | None:
    """Derive the root from the database location using the documented layout."""
    parent = db_path.parent
    candidate = parent.parent if parent.name == REVIEW_DIR else parent
    return candidate if candidate.is_dir() else None


def _get_batch(ctx: InstanceContext, batch_id: str) -> Mapping[str, Any]:
    batch = ctx.repository.get_batch(str(batch_id))
    if batch is None:
        raise NotFound(
            "That action batch does not exist.",
            code=Code.NOT_FOUND,
            detail={"entity": "batch"},
        )
    return batch


def _batch_revision(batch: Mapping[str, Any]) -> int:
    return int(batch.get("execution_revision") or 0)


def _validate_batch_revision(batch: Mapping[str, Any], expected_revision: int) -> None:
    """Refuse a batch write whose caller saw a different execution revision."""
    current = _batch_revision(batch)
    if current != int(expected_revision):
        raise RevisionConflict(
            "The batch execution state changed since it was read.",
            current_value=str(batch.get("execution_state")),
            current_revision=current,
        )


def _plan_data(
    plan: Any,
    *,
    batch_id: str,
    execution_state: str,
    execution_revision: int,
    next_action: str,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "execution_state": execution_state,
        "execution_revision": execution_revision,
        "plan": jsonable(plan),
        "next_action": next_action,
    }


def _raise_apply_failure(
    batch_id: str, code: str, message: str, report: Mapping[str, Any]
) -> None:
    """Raise the mapped error for a batch that did not complete.

    The full per-operation report travels in ``detail`` so a caller can show which
    operation blocked. Nothing here treats a blocked or partial batch as success.
    """
    detail = {"batch_id": batch_id, "report": dict(report)}
    resolved = str(code or Code.INTERNAL_ERROR)
    if resolved == Code.INTERNAL_ERROR:
        raise ResumeReviewError(
            message or "The batch did not complete.",
            code=resolved,
            detail=detail,
            http_status=500,
        )
    raise Conflict(message or "The batch did not complete.", code=resolved, detail=detail)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def register(router: APIRouter) -> None:
    """Register the file-action routes on the instance-prefixed router."""

    # -- pending intent --------------------------------------------------
    @router.put(
        "/documents/{document_id}/action-intent",
        name="action-intent-put",
        response_class=JSONResponse,
    )
    def put_action_intent(
        payload: IntentRequest,
        document_id: str = Depends(resolve_document_id),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Save or cancel one document's pending intent. Moves nothing (PRD 10.1)."""
        _require_reviewer(ctx)
        record = ctx.repository.set_intent(
            document_id,
            intent=str(payload.intent),
            requester=ctx.principal.actor_ref,
            expected_revision=int(payload.expected_revision),
            note=payload.note,
        )
        data = {
            "document_id": document_id,
            "intent": str(record.intent.value),
            "intent_revision": int(record.intent_revision),
            "state": str(record.state),
        }
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    # -- plan ------------------------------------------------------------
    @router.post("/actions/plan", name="actions-plan", response_class=JSONResponse)
    def plan_actions_endpoint(
        payload: PlanRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("actions.plan")),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Build and persist one concrete, immutable plan. Planning moves nothing."""
        _require_reviewer(ctx)
        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        root = _resolve_workspace_root(ctx)
        plan = plan_actions(
            ctx.repository,
            document_ids=list(payload.document_ids),
            intent_by_document=payload.intents,
            requested_by=ctx.principal.actor_ref,
            criteria_version=ctx.repository.active_criteria_version(),
            root=root,
            overrides=payload.overrides,
        )
        # Persist the plan and its durable per-operation rows before any file is
        # touched; the executor revalidates against these rows.
        batch_id = ctx.repository.create_batch(plan, created_by=ctx.principal.actor_ref)
        ctx.repository.create_file_operations(batch_id, plan.operations)
        data = _plan_data(
            plan,
            batch_id=batch_id,
            execution_state=ExecutionState.PLANNED.value,
            execution_revision=0,
            next_action=(
                "Review this exact plan and record a plan-bound approval, then apply the "
                "batch. Planning is not approval."
            ),
        )
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=_warnings(plan.warnings),
        )

    # -- approve ---------------------------------------------------------
    @router.post(
        "/actions/{batch_id}/approve", name="actions-approve", response_class=JSONResponse
    )
    def approve_endpoint(
        batch_id: str,
        payload: ApproveRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("actions.approve")),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Record plan-bound human authorization. Model and worker identities are refused."""
        _require_reviewer(ctx)
        _reject_non_human(ctx.principal)
        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        batch = _get_batch(ctx, batch_id)
        _validate_batch_revision(batch, payload.expected_revision)
        updated = ctx.repository.approve_batch(
            batch_id,
            actor=ctx.principal.actor_ref,
            plan_hash=payload.plan_hash,
            expires_at=seconds_from_now_iso(APPROVAL_LIFETIME_SECONDS),
            request_id=request_id,
        )
        data = {
            "batch_id": batch_id,
            "plan_hash": str(updated.get("plan_hash") or ""),
            "execution_state": str(updated.get("execution_state") or ""),
            "execution_revision": _batch_revision(updated),
            "approval_actor": updated.get("approval_actor"),
            "approval_time": updated.get("approval_time"),
            "approval_expires_at": updated.get("approval_expires_at"),
        }
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    # -- apply -----------------------------------------------------------
    @router.post(
        "/actions/{batch_id}/apply", name="actions-apply", response_class=JSONResponse
    )
    def apply_endpoint(
        batch_id: str,
        payload: ApplyRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("actions.apply")),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Start an already approved batch. An approval is revalidated, never assumed."""
        _require_reviewer(ctx)
        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        batch = _get_batch(ctx, batch_id)
        _validate_batch_revision(batch, payload.expected_revision)
        root = _resolve_workspace_root(ctx)
        outcome = apply_batch(
            ctx.repository,
            batch_id=batch_id,
            actor=ctx.principal,
            root=root,
            now=now_iso(),
            dry_run=bool(payload.dry_run),
        )
        report = outcome.to_dict()
        # The absolute root is an internal detail; it never travels to a caller.
        report.pop("root", None)

        if not outcome.ok:
            # A blocked or partial batch is a genuine outcome, not a success. The full
            # per-operation report rides along in the error detail. Nothing is
            # idempotency-committed, so a replay re-runs the executor, which reports
            # the same terminal state without repeating a move.
            _raise_apply_failure(batch_id, str(outcome.code), outcome.message, report)

        data = {
            "batch_id": batch_id,
            "state": outcome.state,
            "dry_run": bool(outcome.dry_run),
            "remaining": int(outcome.remaining),
            "report": report,
        }
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=_warnings(outcome.warnings),
        )

    # -- cancel ----------------------------------------------------------
    @router.post(
        "/actions/{batch_id}/cancel", name="actions-cancel", response_class=JSONResponse
    )
    def cancel_endpoint(
        batch_id: str,
        payload: CancelRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("actions.cancel")),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Cancel a batch that has not started. A completed move is never undone."""
        _require_reviewer(ctx)
        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        batch = _get_batch(ctx, batch_id)
        _validate_batch_revision(batch, payload.expected_revision)
        state = str(batch.get("execution_state") or ExecutionState.PLANNED.value)

        if state == ExecutionState.CANCELED.value:
            data = {
                "batch_id": batch_id,
                "execution_state": ExecutionState.CANCELED.value,
                "execution_revision": _batch_revision(batch),
            }
            guard.commit(body, response=data)
            return ok_response(
                data,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=ctx.state_revision,
            )

        if state not in _CANCELABLE_STATES:
            code = (
                Code.BATCH_ALREADY_STARTED
                if state == ExecutionState.APPLYING.value
                else Code.BATCH_ALREADY_COMPLETED
            )
            raise Conflict(
                "Only a batch that has not started can be canceled; cancel cannot undo a "
                "completed move and does not delete a planned plan.",
                code=code,
                detail={"batch_id": batch_id, "state": state},
            )

        revision = ctx.repository.set_batch_state(
            batch_id,
            ExecutionState.CANCELED.value,
            expected_execution_revision=_batch_revision(batch),
        )
        data = {
            "batch_id": batch_id,
            "execution_state": ExecutionState.CANCELED.value,
            "execution_revision": int(revision),
        }
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    # -- restore plan ----------------------------------------------------
    @router.post(
        "/actions/{batch_id}/restore-plan",
        name="actions-restore-plan",
        response_class=JSONResponse,
    )
    def restore_plan_endpoint(
        batch_id: str,
        payload: RestorePlanRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("actions.restore-plan")),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        """Mint a new inverse plan for an applied batch. It moves nothing.

        Restore travels the same plan -> approve -> apply path as any other move; it
        is never a direct move and it never reuses or mutates the source batch.
        """
        _require_reviewer(ctx)
        body = payload.model_dump()
        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        _get_batch(ctx, batch_id)  # 404 when the source batch is unknown
        root = _resolve_workspace_root(ctx)
        plan = plan_restore_from_batch(
            ctx.repository,
            batch_id=batch_id,
            requested_by=ctx.principal.actor_ref,
            root=root,
        )
        new_batch_id = ctx.repository.create_batch(plan, created_by=ctx.principal.actor_ref)
        ctx.repository.create_file_operations(new_batch_id, plan.operations)
        data = _plan_data(
            plan,
            batch_id=new_batch_id,
            execution_state=ExecutionState.PLANNED.value,
            execution_revision=0,
            next_action=(
                "This is a new inverse plan. Review and approve it, then apply it; the "
                "original batch is unchanged and no file was moved by this request."
            ),
        )
        data["source_batch_id"] = batch_id
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            warnings=_warnings(plan.warnings),
        )
