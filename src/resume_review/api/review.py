"""Reviewer-owned mutation endpoints: disposition, bulk decisions, and notes.

Authority: PRD section 8.3 (decisions and the atomic bulk set), section 10 (the
five state dimensions), section 11.2 (separate decision and note revisions),
section 12.1 (endpoint table), section 12.2 (mutation rules), and acceptance tests
AT-17 and AT-18.

Three invariants this module is built around:

* **A decision saves only the review record.** It never moves a file, even for
  ``reject``. Moving is the actions surface's job, behind an approved plan.
* **Bulk applies exactly the supplied, immutable id set.** The request carries a
  list of ``(document_id, disposition, expected_revision)`` triples; the route
  validates that every id exists in this instance *before* writing, and the
  repository refuses the whole set if any revision is stale. A later discovery
  cannot enlarge the set, because the set is the request and nothing else.
* **Identity comes from the session.** ``actor`` is ``ctx.principal.actor_ref``;
  there is no actor field in any request body.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from ..auth import require_role
from ..errors import Code, NotFound, ValidationFailed
from ..models import Role
from .deps import InstanceContext, get_request_id, require_mutation, resolve_document
from .documents import serialize_document, serialize_note
from .envelope import ok_response
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = ["register"]

_MAX_BULK_ITEMS = 1000
_MAX_NOTE_LENGTH = 20_000
_DISPOSITIONS = {"unreviewed", "keep", "reject", "hold"}


class DecisionUpdate(BaseModel):
    """PATCH body for one document's disposition (PRD 8.3)."""

    model_config = ConfigDict(extra="forbid")

    disposition: str | None = None
    review_state: str | None = None
    expected_revision: int = Field(ge=0)


class BulkDecisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    disposition: str
    expected_revision: int = Field(ge=0)


class BulkDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BulkDecisionItem] = Field(min_length=1, max_length=_MAX_BULK_ITEMS)


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=_MAX_NOTE_LENGTH)


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=_MAX_NOTE_LENGTH)
    expected_revision: int = Field(ge=0)
    #: Required on the document-scoped form (the note is not in the path there).
    note_id: str | None = None
    #: Required on the ``PATCH /notes/{note_id}`` form, to scope the lookup.
    document_id: str | None = None


def _reviewer_mutation(
    ctx: InstanceContext = Depends(require_mutation),
) -> InstanceContext:
    """Authorize a reviewer-role mutation and return the context."""
    require_role(ctx.principal, Role.REVIEWER)
    return ctx


def _require_disposition(value: str | None) -> str:
    if value is None:
        raise ValidationFailed(
            "A disposition is required.",
            code=Code.VALIDATION_FAILED,
            detail={"field": "disposition"},
        )
    text = str(value)
    if text not in _DISPOSITIONS:
        raise ValidationFailed(
            "Unsupported disposition.",
            code=Code.VALIDATION_FAILED,
            detail={"field": "disposition"},
        )
    return text


def _note_in_document(ctx: InstanceContext, document_id: str, note_id: str) -> Any:
    """Return the note, proving it belongs to ``document_id`` in this instance."""
    for note in ctx.repository.list_notes(document_id):
        if note.id == note_id:
            return note
    raise NotFound("That note does not exist.", detail={"entity": "note"})


def _update_note(
    ctx: InstanceContext,
    document_id: str,
    note_id: str,
    payload: NoteUpdate,
    request_id: str,
):
    _note_in_document(ctx, document_id, note_id)
    note = ctx.repository.update_note(
        note_id, payload.body, int(payload.expected_revision), ctx.principal.actor_ref
    )
    data = {"note": serialize_note(note), "document_id": document_id}
    return ok_response(
        data,
        request_id=request_id,
        instance_id=ctx.instance_id,
        state_revision=ctx.state_revision,
    )


def register(router: Any) -> None:
    """Attach the review-state endpoints onto the instance-scoped router."""

    @router.patch("/documents/{document_id}/decision", name="set_decision")
    def set_decision(
        payload: DecisionUpdate,
        record=Depends(resolve_document),
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Set a human disposition with an optimistic revision check (AT-18).

        A stale ``expected_revision`` raises ``REVISION_CONFLICT`` (409) carrying
        the current revision and value; nothing is overwritten.
        """
        disposition = _require_disposition(payload.disposition or payload.review_state)
        ctx.repository.set_decision(
            record.id,
            disposition,
            int(payload.expected_revision),
            ctx.principal.actor_ref,
            request_id=request_id,
        )
        refreshed = ctx.repository.get_document(record.id) or record
        decision = ctx.repository.get_decision(record.id)
        intent = ctx.repository.get_intent(record.id)
        tasks = ctx.repository.list_tasks(document_id=record.id)
        profile = ctx.repository.current_profile(record.id)
        document = serialize_document(
            refreshed, decision=decision, intent=intent, tasks=tasks, profile=profile
        )
        data = {
            "document_id": record.id,
            "review_state": decision.disposition.value,
            "decision_revision": decision.decision_revision,
            "decision_needs_recheck": bool(decision.needs_recheck),
            "disposition_frozen": bool(decision.disposition_frozen),
            "document": document,
        }
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.post("/decisions/bulk", name="bulk_decisions")
    def bulk_decisions(
        payload: BulkDecisionRequest,
        guard: IdempotencyGuard = Depends(idempotency_guard("decisions.bulk")),
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Apply dispositions to an explicit, immutable set of documents (AT-17).

        Every id is resolved against this instance first; a foreign or absent id is
        a 404 and nothing is written. The repository then validates every revision
        before any row is written, so the set is applied whole or not at all.
        """
        normalized: list[dict[str, Any]] = []
        for item in payload.items:
            disposition = _require_disposition(item.disposition)
            document = ctx.repository.get_document(item.document_id)
            if document is None or document.instance_id != ctx.instance_id:
                raise NotFound(
                    "That document does not exist.", detail={"entity": "document"}
                )
            normalized.append(
                {
                    "document_id": document.id,
                    "disposition": disposition,
                    "expected_revision": int(item.expected_revision),
                }
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
            )

        result = ctx.repository.bulk_set_decisions(
            normalized, ctx.principal.actor_ref, request_id=request_id
        )
        data = {
            "updated": int(result.get("updated", len(normalized))),
            "document_ids": list(result.get("document_ids", [])),
            "conflicts": [],
        }
        guard.commit(request_payload, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.post("/documents/{document_id}/notes", name="add_note")
    def add_note(
        payload: NoteCreate,
        record=Depends(resolve_document),
        guard: IdempotencyGuard = Depends(idempotency_guard("notes.create")),
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Add a human note to a document (PRD 8.4)."""
        request_payload = {"document_id": record.id, **payload.model_dump(mode="json")}
        hit = guard.replay(request_payload)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
                job_id=hit.job_id,
            )
        note = ctx.repository.add_note(record.id, payload.body, ctx.principal.actor_ref)
        data = {"note": serialize_note(note), "document_id": record.id}
        guard.commit(request_payload, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
            status_code=201,
        )

    @router.patch("/documents/{document_id}/notes", name="update_note_in_document")
    def update_note_in_document(
        payload: NoteUpdate,
        record=Depends(resolve_document),
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Edit a note named in the body, within the document path scope."""
        if not payload.note_id:
            raise ValidationFailed(
                "A note id is required.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "note_id"},
            )
        return _update_note(ctx, record.id, payload.note_id, payload, request_id)

    @router.patch("/notes/{note_id}", name="update_note")
    def update_note(
        note_id: str,
        payload: NoteUpdate,
        ctx: InstanceContext = Depends(_reviewer_mutation),
        request_id: str = Depends(get_request_id),
    ):
        """Edit a note by path id; ``document_id`` in the body scopes the lookup."""
        if not payload.document_id:
            raise ValidationFailed(
                "A document id is required to scope this note.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "document_id"},
            )
        document = ctx.repository.get_document(payload.document_id)
        if document is None or document.instance_id != ctx.instance_id:
            raise NotFound("That document does not exist.", detail={"entity": "document"})
        return _update_note(ctx, document.id, note_id, payload, request_id)
