"""The folder-scoped chat endpoint: ``POST /chat`` (PRD sections 9.1-9.4, 12.1).

Authority: PRD section 12.1 ("POST ``/chat`` -- Queue or stream folder-scoped
conversation", allowed principal Reviewer), section 9.4 (context isolation: one
opaque conversation id per instance and reviewer, bounded retrieval, an
all-folder answer that never reads as though every submission was inspected), and
section 14.2 (fail closed when the route is not adequately restricted).

This module is the HTTP edge of the chat pipeline in
:mod:`resume_review.analysis.chat`. It performs no retrieval or model call of its
own; it authenticates, authorizes, resolves the scope to real document ids in
this instance, and then hands the turn to a :class:`FolderChatService` built
around the frozen :class:`~resume_review.analysis.chat.FolderChat`. Everything the
pipeline guarantees -- bounded retrieval, partial-coverage disclosure, an opaque
provider-visible conversation id, proposals that move nothing, a route that fails
closed -- therefore holds at the endpoint by construction, not by a second
implementation that could drift.

Two request modes are supported, which is the PRD's "queue or stream" choice:

* ``mode="direct"`` (the default) runs one bounded turn and answers ``200`` with
  the answer, its citations, its coverage object, and any proposed action. The
  call is bounded by :class:`~resume_review.analysis.chat.ChatRetrievalBudget` and
  the adapter's own timeout.
* ``mode="queue"`` records a durable ``chat`` job through the frozen job store and
  answers ``202`` with a job id **without calling the model in the request**. This
  is the non-blocking path: a long model turn does not hold the HTTP request. The
  queued turn is executed by :meth:`FolderChatService.run_job`, which a worker (the
  analysis queue's ``chat`` lane) drives.

The endpoint never trusts a caller-supplied actor: the reviewer identity is the
authenticated principal. A caller may supply a ``conversation_id``, but it is only
ever accepted as an assertion that must equal the id the helper already binds to
that reviewer; it can never select another reviewer's thread (PRD 9.4).

The route is registered through ``register(router)`` and adds nothing but that one
path. When the helper has no chat service configured the route answers ``404``, the
same as an unregistered path, so an unconfigured deployment exposes no chat
surface at all.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Literal, Mapping, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..analysis.chat import (
    DEFAULT_CHAT_BUDGET,
    ChatModelClient,
    ChatRetrievalBudget,
    FolderChat,
)
from ..auth import require_role
from ..db import Repository
from ..db.connection import json_column
from ..errors import Code, InvalidInput, NotFound, ResumeReviewError
from ..models import ChatKind, Principal, Role, canonical_hash
from ..openclaw_adapter.policy import RoutePolicy
from ..util import new_id
from . import envelope as env
from .deps import (
    ApiRuntime,
    InstanceContext,
    RequestId,
    get_runtime,
    require_mutation,
)
from .idempotency import IDEMPOTENCY_HEADER, validate_key

__all__ = [
    "CHAT_JOB_KIND",
    "MAX_SCOPE_DOCUMENTS",
    "ChatRequest",
    "FolderChatService",
    "register",
]

#: Job kind for a queued chat turn. ``chat`` is a reserved interactive kind in the
#: job store and in :class:`~resume_review.analysis.queue.HostCapacity`.
CHAT_JOB_KIND = "chat"

#: Hard ceiling on the explicit scope of one turn. Mirrors the core bridge's bound.
MAX_SCOPE_DOCUMENTS = 50

#: The opaque conversation-id shape the adapter and the pipeline both enforce. A
#: candidate name, a file name, an email or a folder path fails this pattern, so a
#: caller can never smuggle one in as a session reference (PRD 9.4).
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")


class ChatRequest(BaseModel):
    """One folder-scoped chat turn. No endpoint, model, tool, or actor field.

    ``extra="forbid"`` means an attempt to add ``actor``, ``endpoint``, ``model`` or
    any other field is a validation error rather than a silently ignored key.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    #: Optional assertion of the conversation this reviewer is already bound to. It
    #: is never used to select a thread; a mismatch is refused (PRD 9.4).
    conversation_id: str | None = Field(default=None, max_length=128)
    document_ids: list[str] = Field(default_factory=list, max_length=MAX_SCOPE_DOCUMENTS)
    #: ``direct`` answers one bounded turn (200); ``queue`` records durable work and
    #: returns a job id (202) without waiting on the model.
    mode: Literal["direct", "queue"] = "direct"


# ---------------------------------------------------------------------------
# The chat service
# ---------------------------------------------------------------------------
class FolderChatService:
    """A folder-scoped chat turn, wired to one instance repository.

    It is a ``ChatAdapter`` in the core sense -- ``service(payload, *,
    principal, instance_id)`` returns a JSON-ready mapping -- and it additionally
    exposes the two richer entry points the endpoint uses: :meth:`answer_turn` for a
    direct turn and :meth:`enqueue` for a queued one.

    ``route_policy`` is checked by :meth:`assert_route_usable` at the very top of
    both entry points, before any database write, and again inside the pipeline. A
    policy that reports the route unavailable, unrestricted, or unattested refuses
    the turn; there is no second route and nothing to fall back to (PRD 14.2).
    """

    def __init__(
        self,
        repository: Repository,
        model_client: ChatModelClient,
        *,
        route_policy: RoutePolicy | None = None,
        budget: ChatRetrievalBudget = DEFAULT_CHAT_BUDGET,
    ) -> None:
        self._repo = repository
        self._model_client = model_client
        self._route_policy = route_policy
        self._budget = budget

    @property
    def repository(self) -> Repository:
        return self._repo

    @property
    def route_policy(self) -> RoutePolicy | None:
        return self._route_policy

    def assert_route_usable(self) -> None:
        """Fail closed before any write when the configured route is unusable."""
        if self._route_policy is not None:
            self._route_policy.assert_inference_allowed()

    def _pipeline(self) -> FolderChat:
        return FolderChat(
            self._repo,
            self._model_client,
            route_policy=self._route_policy,
            budget=self._budget,
        )

    def _bind_conversation(self, reviewer: str, conversation_id: str | None) -> str | None:
        """Validate a caller-supplied conversation id against the bound one.

        Run only *after* :meth:`assert_route_usable`, so a refused route writes
        nothing. An id that is not opaque, or that is opaque but belongs to a
        different thread than the one bound to this reviewer, is refused with
        ``CHAT_SCOPE_NOT_BOUND`` (PRD 9.4).
        """
        if conversation_id is None:
            return None
        if not _CONVERSATION_ID_RE.match(str(conversation_id)):
            raise ResumeReviewError(
                "The conversation id must be an opaque application value, not a name, "
                "path or address.",
                code=Code.CHAT_SCOPE_NOT_BOUND,
                http_status=422,
                detail={"reason": "conversation_id_not_opaque"},
            )
        bound = str(self._repo.get_or_create_conversation(reviewer, ChatKind.CHAT.value)["id"])
        if str(conversation_id) != bound:
            raise ResumeReviewError(
                "The conversation id does not match the thread bound to this reviewer.",
                code=Code.CHAT_SCOPE_NOT_BOUND,
                http_status=422,
                detail={"reason": "conversation_mismatch"},
            )
        return bound

    async def answer_turn(
        self,
        *,
        reviewer: str,
        question: str,
        scope_document_ids: Sequence[str] | None = None,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one bounded direct turn and return the JSON-ready answer."""
        self.assert_route_usable()
        self._bind_conversation(reviewer, conversation_id)
        answer = await self._pipeline().ask(
            reviewer=reviewer,
            question=question,
            scope_document_ids=scope_document_ids,
            request_id=request_id,
        )
        return answer.to_dict()

    def enqueue(
        self,
        *,
        reviewer: str,
        question: str,
        scope_document_ids: Sequence[str] | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, str]:
        """Record a durable chat job and return ``(job_id, conversation_id)``.

        No model call happens here. The route is checked first, so an unusable route
        queues nothing. The job key is derived from the caller's ``Idempotency-Key``
        when present, so a retried request returns the original job; without one,
        each request is a distinct turn (see the endpoint's docstring on
        idempotency).
        """
        self.assert_route_usable()
        conversation = str(
            self._repo.get_or_create_conversation(reviewer, ChatKind.CHAT.value)["id"]
        )
        if conversation_id is not None:
            if not _CONVERSATION_ID_RE.match(str(conversation_id)):
                raise ResumeReviewError(
                    "The conversation id must be an opaque application value, not a name, "
                    "path or address.",
                    code=Code.CHAT_SCOPE_NOT_BOUND,
                    http_status=422,
                    detail={"reason": "conversation_id_not_opaque"},
                )
            if str(conversation_id) != conversation:
                raise ResumeReviewError(
                    "The conversation id does not match the thread bound to this reviewer.",
                    code=Code.CHAT_SCOPE_NOT_BOUND,
                    http_status=422,
                    detail={"reason": "conversation_mismatch"},
                )
        versions = {
            "reviewer": reviewer,
            "message": question,
            "scope_document_ids": list(scope_document_ids or ()),
        }
        if idempotency_key is not None:
            job_key = f"chat:{reviewer}:{canonical_hash({'key': validate_key(idempotency_key)})}"
        else:
            job_key = f"chat:{reviewer}:{new_id('turn')}"
        job_id = self._repo.enqueue_job(job_key, CHAT_JOB_KIND, input_versions=versions)
        return job_id, conversation

    async def run_job(self, job: Mapping[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        """Execute a queued chat turn from the job's stored payload.

        This is the work the ``202`` promised. It performs the same bounded turn as
        :meth:`answer_turn`; the pipeline persists the exchange locally and nothing
        else is changed.
        """
        # ``input_versions`` is stored as JSON text and read back undecoded, so
        # accept either the raw column or an already-decoded mapping.
        versions = json_column(job.get("input_versions"), {})
        if not isinstance(versions, Mapping):
            versions = {}
        reviewer = str(versions.get("reviewer") or "")
        message = str(versions.get("message") or "")
        scope = versions.get("scope_document_ids") or []
        if not reviewer or not message:
            raise InvalidInput(
                "The queued chat job has no usable turn payload.",
                detail={"reason": "chat_job_payload_invalid"},
            )
        return await self.answer_turn(
            reviewer=reviewer,
            question=message,
            scope_document_ids=scope,
            request_id=request_id,
        )

    async def __call__(
        self,
        payload: Mapping[str, Any],
        *,
        principal: Principal,
        instance_id: str,
    ) -> dict[str, Any]:
        """Core ``ChatAdapter`` surface: one direct turn for the authenticated actor.

        ``principal`` is supplied by the caller's authorization layer; it is never a
        field of ``payload``.
        """
        del instance_id  # the repository is already bound to one instance
        return await self.answer_turn(
            reviewer=principal.actor_ref,
            question=str(payload.get("message") or ""),
            scope_document_ids=payload.get("document_ids") or None,
            conversation_id=payload.get("conversation_id"),
        )


# ---------------------------------------------------------------------------
# Endpoint registration
# ---------------------------------------------------------------------------
def _configured_chat(runtime: ApiRuntime = Depends(get_runtime)) -> ApiRuntime:
    """Answer ``404`` when no chat service is configured.

    Declared first on the route so an unconfigured helper answers exactly as if the
    path did not exist -- no authentication prompt for a surface the deployment does
    not expose.
    """
    adapter = getattr(runtime, "chat_adapter", None)
    if adapter is None or not callable(adapter):
        raise NotFound(
            "The folder chat endpoint is not configured on this helper.",
            detail={"reason": "chat_not_configured"},
        )
    return runtime


def _resolve_scope(ctx: InstanceContext, document_ids: Sequence[str]) -> list[str]:
    """Resolve caller-supplied ids to documents in *this* instance, or 404.

    A foreign or unknown id is indistinguishable from an absent one, and a caller
    can never widen the scope to another instance (PRD 12). The resolved ids are
    what the retrieval step is allowed to consider.
    """
    resolved: list[str] = []
    for document_id in dict.fromkeys(str(value) for value in document_ids):
        record = ctx.repository.get_document(document_id)
        if record is None or record.instance_id != ctx.instance_id:
            raise NotFound("That document does not exist.", detail={"entity": "document"})
        resolved.append(document_id)
    return resolved


def register(router: APIRouter) -> None:
    """Register ``POST /chat`` on the instance-scoped router.

    The router already carries ``/api/v1/instances/{instance_id}``, so the path
    added here is exactly ``/chat``.
    """

    @router.post("/chat", name="post_chat", response_class=JSONResponse)
    async def post_chat(
        turn: ChatRequest,
        request: Request,
        request_id: RequestId,
        runtime: ApiRuntime = Depends(_configured_chat),
        ctx: InstanceContext = Depends(require_mutation),
    ) -> JSONResponse:
        # Authorization: authenticate -> authorize instance -> authorize operation
        # (reviewer) -> resolve document ids. Identity is the session's principal.
        require_role(ctx.principal, Role.REVIEWER)
        # Chat is interactive, so it carries its own budget as well as the general
        # mutation limit (PRD 16.1). The chat window is the tighter of the two, so
        # it binds first: a reviewer clicking through turns is unimpeded, while a
        # runaway loop is stopped.
        runtime.chat_rate_limiter.check(f"chat:{ctx.principal.actor_ref}")
        scope = _resolve_scope(ctx, turn.document_ids)
        adapter = runtime.chat_adapter

        if turn.mode == "queue":
            enqueue = getattr(adapter, "enqueue", None)
            if not callable(enqueue):
                raise InvalidInput(
                    "This helper's chat service does not support queued turns.",
                    detail={"reason": "queue_unsupported"},
                )
            job_id, conversation_id = enqueue(
                reviewer=ctx.principal.actor_ref,
                question=turn.message,
                scope_document_ids=scope or None,
                conversation_id=turn.conversation_id,
                idempotency_key=request.headers.get(IDEMPOTENCY_HEADER),
            )
            return env.accepted_response(
                {"mode": "queue", "conversation_id": conversation_id},
                job_id=str(job_id),
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=ctx.state_revision,
            )

        if isinstance(adapter, FolderChatService):
            data = await adapter.answer_turn(
                reviewer=ctx.principal.actor_ref,
                question=turn.message,
                scope_document_ids=scope or None,
                conversation_id=turn.conversation_id,
                request_id=request_id,
            )
        else:
            # A generic adapter (the core bridge's shape): it is given only the
            # bounded, typed payload and the authorized principal.
            result = adapter(
                turn.model_dump(),
                principal=ctx.principal,
                instance_id=ctx.instance_id,
            )
            data = await result if inspect.isawaitable(result) else result

        return env.ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )
