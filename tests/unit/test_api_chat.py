"""HTTP-level tests for the folder-scoped chat endpoint (PRD 9.1-9.4, 12.1).

Authority: PRD section 12.1 (``POST /chat`` -- "Queue or stream folder-scoped
conversation", allowed principal Reviewer), section 12.2 (mutation rules and the
response envelope), section 9.4 (opaque conversation id, bounded retrieval,
incomplete-coverage disclosure), and section 14.2 (fail closed when the route is
not adequately restricted).

These tests drive the real endpoint over ``TestClient`` against a real migrated
database, real session/CSRF stores, and a stub model client injected into
:class:`~resume_review.api.chat.FolderChatService`. No live OpenClaw route is
required and only synthetic data is used.

They assert what the endpoint itself must hold:

* it answers ``404`` exactly as an absent path when no chat service is configured;
* a capped retrieval discloses partial coverage and never reads as though every
  submission was inspected;
* the value sent to the model as the conversation identifier is opaque and
  server-bound, never a candidate name, file name or folder path;
* an unavailable or unrestricted route fails closed before any write, in both the
  direct and the queued path;
* an action request proposes and moves nothing -- no approval, no batch, no saved
  intent, no filesystem change;
* a criteria proposal is recorded unapproved and never activates a version; a
  repeat on a later turn warns instead of crashing;
* the queued path returns ``202`` with a durable job id without calling the model
  in the request, and replays the job idempotently under an ``Idempotency-Key``;
* the replayed history window is the newest turns, not the oldest.

The endpoint is isolated from sibling route modules (owned by other work) by
pinning ``route_modules`` to this module alone, so these tests exercise exactly
one surface.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.chat import ChatRetrievalBudget
from resume_review.api import ApiConfig, create_app
from resume_review.api.chat import CHAT_JOB_KIND, FolderChatService
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, ResumeReviewError
from resume_review.models import (
    AnalysisResult,
    CriterionResult,
    EvidenceItem,
    MediaType,
    ModelRoute,
    PendingIntent,
    ReviewState,
    Role,
)
from resume_review.openclaw_adapter.client import AdapterResult
from resume_review.openclaw_adapter.policy import RouteAttestation, RoutePolicy
from resume_review.openclaw_adapter.prompts import PROMPT_VERSION

INSTANCE_ID = "inst_test"
ORIGIN = "http://testserver"
CHAT_MODULE = ("resume_review.api.chat",)

#: Only this module is discovered, so a sibling route module that is mid-flight in
#: another workstream cannot affect these tests.
ROUTE_MODULES = CHAT_MODULE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def sessions() -> SessionStore:
    return SessionStore()


@pytest.fixture
def csrf() -> CsrfStore:
    return CsrfStore()


@pytest.fixture
def config() -> ApiConfig:
    return ApiConfig(allowed_origins=(ORIGIN,))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_client(repo, sessions, csrf, config, *, service=None) -> TestClient:
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=config,
        route_modules=ROUTE_MODULES,
    )
    if service is not None:
        # The runtime is the documented collaborator holder; the endpoint reads the
        # configured chat service from it. Setting it here (rather than passing the
        # core's ``chat_adapter`` bridge) exercises this module's own route.
        app.state.runtime.chat_adapter = service
    return TestClient(app)


def auth_headers(sessions: SessionStore, csrf: CsrfStore, role: Role = Role.REVIEWER):
    session = sessions.issue(INSTANCE_ID, "reviewer_1", role)
    headers = {
        "X-CSRF-Token": csrf.issue(session.session_id),
        "Origin": ORIGIN,
    }
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return headers, cookies


def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def post_chat(client: TestClient, headers, cookies, **payload):
    return client.post(url("/chat"), json=payload, headers=headers, cookies=cookies)


def json_answer(
    answer: str,
    citations: list[dict] | None = None,
    criteria: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "answer": answer,
            "citations": citations or [],
            "criteria_proposals": criteria or [],
        }
    )


class StubModelClient:
    """Deterministic stand-in for the OpenClaw adapter. Records every call."""

    def __init__(self, *, text: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._text = text
        self._error = error

    async def analyze(self, request, *, conversation_user=None, request_id=None):
        self.calls.append(
            {"request": request, "conversation_user": conversation_user, "request_id": request_id}
        )
        if self._error is not None:
            raise self._error
        return AdapterResult(
            text=self._text or "",
            request_id="req_test",
            document_id=request.document_id,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            route=ModelRoute.LOCAL_ONLY,
            provider_label="test-local",
            agent_target="openclaw/analysis",
            endpoint_label="http://127.0.0.1:8080",
            started_at="2026-09-29T00:00:00+00:00",
            ended_at="2026-09-29T00:00:01+00:00",
            duration_ms=1000,
            http_status=200,
        )


def attested_policy(**overrides) -> RoutePolicy:
    params = {
        "route": ModelRoute.LOCAL_ONLY,
        "restricted": True,
        "attestation": RouteAttestation(
            attested_by="operator",
            attested_at="2026-09-29T00:00:00+00:00",
            no_shell=True,
            no_write_or_edit=True,
            no_browser_control=True,
            no_messaging=True,
            no_credential_read=True,
            no_unrestricted_file_read=True,
            no_cross_session=True,
            no_agent_spawning=True,
            trusted_instruction_workspace=True,
        ),
    }
    params.update(overrides)
    return RoutePolicy(**params)


def service_for(
    repo: Repository,
    client: StubModelClient,
    *,
    policy: RoutePolicy | None = None,
    budget: ChatRetrievalBudget | None = None,
) -> FolderChatService:
    return FolderChatService(
        repo,
        client,
        route_policy=policy if policy is not None else attested_policy(),
        budget=budget or ChatRetrievalBudget(),
    )


def add_document(
    repo: Repository,
    name: str,
    *,
    summary: str = "",
    quotes: tuple[str, ...] = (),
    result: CriterionResult = CriterionResult.SUPPORTED,
    decision: ReviewState | None = None,
):
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=1024,
        content_sha256=None,
        fs_identity=None,
    )
    repo.set_document_processing(document.id, "ready")
    evidence = [
        EvidenceItem(
            id=f"ev_{document.id}_{index}",
            span_id=f"span_{index}",
            quote=quote,
            criterion_id="cr_01",
            result=result,
            claim_kind="criterion",
            validation="verified",
        )
        for index, quote in enumerate(quotes)
    ]
    repo.insert_profile(
        AnalysisResult(
            schema_version="1.0",
            document_id=document.id,
            source_revision=1,
            criteria_version=1,
            summary_text=summary,
            evidence=evidence,
        ),
        {
            "prompt_version": PROMPT_VERSION,
            "model_route": ModelRoute.FIXTURE.value,
            "validation_state": "valid",
            "actor": "helper",
        },
    )
    if decision is not None:
        repo.set_decision(document.id, decision.value, 0, actor="reviewer_1")
    return document


def table_count(repo: Repository, table: str) -> int:
    return int(repo.db.scalar(f"SELECT COUNT(*) FROM {table}", (), default=0) or 0)


# ---------------------------------------------------------------------------
# Configuration and authorization
# ---------------------------------------------------------------------------
def test_chat_route_is_absent_when_no_service_is_configured(repo, sessions, csrf, config) -> None:
    client = make_client(repo, sessions, csrf, config, service=None)
    # Even unauthenticated, an unconfigured helper exposes no chat surface: 404, not
    # a 401 that would reveal a configured route.
    response = client.post(url("/chat"), json={"message": "hi"})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == Code.NOT_FOUND
    assert response.json()["error"]["detail"]["reason"] == "chat_not_configured"


def test_requires_reviewer_role(repo, sessions, csrf, config) -> None:
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient())
    )
    headers, cookies = auth_headers(sessions, csrf, role=Role.VIEWER)
    response = post_chat(client, headers, cookies, message="hi")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_unknown_field_is_rejected(repo, sessions, csrf, config) -> None:
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient())
    )
    headers, cookies = auth_headers(sessions, csrf)
    response = post_chat(client, headers, cookies, message="hi", actor="someone_else")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.VALIDATION_FAILED


def test_csrf_is_required_for_the_mutation(repo, sessions, csrf, config) -> None:
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient())
    )
    session = sessions.issue(INSTANCE_ID, "reviewer_1", Role.REVIEWER)
    response = client.post(
        url("/chat"),
        json={"message": "hi"},
        headers={"Origin": ORIGIN},
        cookies={session_cookie_name(INSTANCE_ID): session.session_id},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.CSRF_FAILED


# ---------------------------------------------------------------------------
# Coverage disclosure
# ---------------------------------------------------------------------------
def test_partial_coverage_is_disclosed_over_http(repo, sessions, csrf, config) -> None:
    target = add_document(
        repo,
        "a.pdf",
        summary="Reports commercial construction coordination.",
        quotes=("Led commercial construction coordination for three sites.",),
    )
    add_document(repo, "b.pdf", summary="Plumbing.", quotes=("Repaired boilers.",))
    add_document(repo, "c.pdf", summary="Roofing.", quotes=("Installed roof tiles.",))

    stub = StubModelClient(text=json_answer("One submission reports the experience."))
    service = service_for(repo, stub, budget=ChatRetrievalBudget(max_documents=1))
    client = make_client(repo, sessions, csrf, config, service=service)
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(client, headers, cookies, message="commercial construction experience")
    assert response.status_code == 200, response.text
    coverage = response.json()["data"]["coverage"]
    assert coverage["documents_in_scope"] == 3
    assert coverage["documents_inspected"] == 1
    assert coverage["documents_not_inspected"] == 2
    assert coverage["partial"] is True
    assert coverage["retrieved_document_ids"] == [target.id]
    assert "1 of 3" in coverage["message"]
    # The other submissions' evidence never reached the prompt.
    prompt = stub.calls[0]["request"].messages[-1].content
    assert "Repaired boilers" not in prompt
    assert "Installed roof tiles" not in prompt


def test_full_coverage_is_not_reported_as_partial(repo, sessions, csrf, config) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction coordination.",))
    add_document(repo, "b.pdf", quotes=("Commercial construction supervision.",))
    stub = StubModelClient(text=json_answer("Both were inspected."))
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    body = post_chat(client, headers, cookies, message="commercial construction").json()
    assert body["data"]["coverage"]["partial"] is False
    assert body["data"]["coverage"]["documents_inspected"] == 2


# ---------------------------------------------------------------------------
# Opaque, server-bound conversation id
# ---------------------------------------------------------------------------
def test_provider_visible_conversation_id_is_opaque_and_server_bound(
    repo, sessions, csrf, config
) -> None:
    add_document(repo, "Jane Doe resume.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("Grounded."))
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    body = post_chat(client, headers, cookies, message="commercial construction").json()
    conversation_id = body["data"]["conversation_id"]

    sent = stub.calls[0]["conversation_user"]
    # The value actually sent to the model is the helper's opaque id: no name, no
    # file name, no folder path, no email.
    assert sent == conversation_id
    assert sent == stub.calls[0]["request"].document_id
    assert sent.startswith("conv_")
    assert "Jane" not in sent and "resume" not in sent and ".pdf" not in sent
    assert "\\" not in sent and "/" not in sent


def test_non_opaque_conversation_id_is_refused_before_any_write(
    repo, sessions, csrf, config
) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient(text=json_answer("x")))
    )
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(
        client, headers, cookies, message="hi", conversation_id="Jane Doe resume.pdf"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.CHAT_SCOPE_NOT_BOUND
    assert table_count(repo, "conversations") == 0
    assert table_count(repo, "messages") == 0


def test_conversation_id_bound_to_another_thread_is_refused(
    repo, sessions, csrf, config
) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("Grounded."))
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    post_chat(client, headers, cookies, message="first question")
    # A well-formed but unbound opaque id must not select a thread.
    response = post_chat(
        client, headers, cookies, message="second question", conversation_id="conv_0123456789abcdef"
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["reason"] == "conversation_mismatch"


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------
def test_unknown_scope_document_is_refused(repo, sessions, csrf, config) -> None:
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient(text=json_answer("x")))
    )
    headers, cookies = auth_headers(sessions, csrf)
    response = post_chat(client, headers, cookies, message="hi", document_ids=["doc_absent"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == Code.NOT_FOUND


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("policy", "expected_code"),
    [
        (attested_policy(route=ModelRoute.UNAVAILABLE, restricted=True), Code.ROUTE_UNAVAILABLE),
        (attested_policy(restricted=False), Code.ROUTE_NOT_RESTRICTED),
    ],
)
def test_direct_turn_fails_closed_before_any_write(
    repo, sessions, csrf, config, policy, expected_code
) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("must never run"))
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, stub, policy=policy)
    )
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(client, headers, cookies, message="commercial construction")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == expected_code
    assert stub.calls == []
    assert table_count(repo, "conversations") == 0
    assert table_count(repo, "messages") == 0


def test_queued_turn_fails_closed_before_any_write(repo, sessions, csrf, config) -> None:
    policy = attested_policy(route=ModelRoute.UNAVAILABLE, restricted=True)
    client = make_client(
        repo,
        sessions,
        csrf,
        config,
        service=service_for(repo, StubModelClient(text=json_answer("x")), policy=policy),
    )
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(client, headers, cookies, message="hi", mode="queue")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == Code.ROUTE_UNAVAILABLE
    assert table_count(repo, "processing_jobs") == 0
    assert table_count(repo, "conversations") == 0


def test_adapter_failure_fails_the_question_and_persists_no_exchange(
    repo, sessions, csrf, config
) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    policy = attested_policy()
    failure = policy.route_failure_error(
        Code.ROUTE_UNAVAILABLE, message="The analysis route could not be reached."
    )
    stub = StubModelClient(error=failure)
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, stub, policy=policy)
    )
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(client, headers, cookies, message="commercial construction")
    # LOCAL_ONLY reports a lost route as the moment a fallback must be refused.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == Code.LOCAL_ONLY_FALLBACK_BLOCKED
    assert len(stub.calls) == 1
    assert table_count(repo, "messages") == 0


# ---------------------------------------------------------------------------
# Chat proposes; it never moves, approves or records intent
# ---------------------------------------------------------------------------
def test_action_request_proposes_and_moves_nothing(repo, sessions, csrf, config) -> None:
    rejected = add_document(
        repo, "reject.pdf", quotes=("Commercial construction.",), decision=ReviewState.REJECT
    )
    add_document(repo, "keep.pdf", quotes=("Commercial construction.",), decision=ReviewState.KEEP)
    stub = StubModelClient(text=json_answer("Moving files requires an approved plan."))
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    body = post_chat(client, headers, cookies, message="move the rejects to trash").json()
    action = body["data"]["proposed_action"]
    assert action is not None
    assert action["kind"] == PendingIntent.MOVE_TRASH.value
    assert action["requires_human_approval"] is True
    assert action["approval_created"] is False
    assert action["plan_persisted"] is False
    assert action["filesystem_changed"] is False

    # Nothing was approved, batched, journalled or intended.
    assert repo.list_batches() == []
    assert repo.find_operations_in_state(
        ["planned", "intent_recorded", "file_moved", "committed", "needs_reconciliation"]
    ) == []
    assert table_count(repo, "action_intents") == 0
    assert repo.get_intent(rejected.id).intent is PendingIntent.NONE
    # The human decision is untouched.
    decision = repo.get_decision(rejected.id)
    assert decision.disposition is ReviewState.REJECT
    assert decision.decision_revision == 1


# ---------------------------------------------------------------------------
# Criteria proposals never activate; repeats warn instead of crashing
# ---------------------------------------------------------------------------
def test_criteria_proposal_is_recorded_but_never_activated(repo, sessions, csrf, config) -> None:
    repo.create_or_update_job("Operations Manager", "Synthetic job description.")
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(
        text=json_answer(
            "Consider a new criterion.",
            criteria=[
                {
                    "criterion_id": "cr_new",
                    "definition": "Document reports coordinating subcontractors.",
                    "rationale": "Recurring in scope.",
                    "label": None,
                }
            ],
        )
    )
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    body = post_chat(client, headers, cookies, message="Should we track this?").json()
    assert body["data"]["criteria_proposed"] == ["cr_new"]
    assert repo.active_criteria_version() == 0
    row = repo.db.query_one(
        "SELECT version, approved_at, approved_by, origin FROM criteria WHERE criterion_id = ?",
        ("cr_new",),
    )
    assert row["approved_at"] is None
    assert row["approved_by"] is None
    assert str(row["origin"]) == "agent_proposal"


def test_repeated_criteria_proposal_warns_and_does_not_crash(repo, sessions, csrf, config) -> None:
    repo.create_or_update_job("Operations Manager", "Synthetic job description.")
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(
        text=json_answer(
            "Consider a new criterion.",
            criteria=[
                {
                    "criterion_id": "cr_new",
                    "definition": "Document reports coordinating subcontractors.",
                    "rationale": "Recurring in scope.",
                    "label": None,
                }
            ],
        )
    )
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    first = post_chat(client, headers, cookies, message="first")
    assert first.status_code == 200
    second = post_chat(client, headers, cookies, message="second")
    assert second.status_code == 200, second.text
    warnings = second.json()["data"]["warnings"]
    assert any(item["code"] == "criteria_proposal_rejected" for item in warnings)
    # A repeated proposal changed nothing about the active criteria set.
    assert repo.active_criteria_version() == 0


# ---------------------------------------------------------------------------
# Queue mode: 202 without blocking, and idempotent replay
# ---------------------------------------------------------------------------
def test_queue_mode_returns_202_without_calling_the_model(repo, sessions, csrf, config) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("should not run in the request"))
    client = make_client(repo, sessions, csrf, config, service=service_for(repo, stub))
    headers, cookies = auth_headers(sessions, csrf)

    response = post_chat(client, headers, cookies, message="commercial construction", mode="queue")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["code"] == "ACCEPTED"
    assert body["job_id"].startswith("job_")
    assert body["data"]["mode"] == "queue"
    # The model was not called to answer the request: the work is queued.
    assert stub.calls == []
    job = repo.get_job(body["job_id"])
    assert job is not None
    assert str(job["kind"]) == CHAT_JOB_KIND
    assert str(job["state"]) == "queued"
    # The exchange has not been persisted yet.
    assert table_count(repo, "messages") == 0


def test_queued_turn_executes_from_the_stored_job(repo, sessions, csrf, config) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("Answered from the queue."))
    service = service_for(repo, stub)
    client = make_client(repo, sessions, csrf, config, service=service)
    headers, cookies = auth_headers(sessions, csrf)

    job_id = post_chat(client, headers, cookies, message="commercial construction", mode="queue").json()[
        "job_id"
    ]
    assert stub.calls == []

    answer = asyncio.run(service.run_job(repo.get_job(job_id)))
    assert answer["answer"] == "Answered from the queue."
    assert len(stub.calls) == 1
    # The exchange is now persisted locally.
    assert table_count(repo, "messages") == 2


def test_queue_mode_replays_the_job_for_a_repeated_idempotency_key(
    repo, sessions, csrf, config
) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient(text=json_answer("x")))
    )
    headers, cookies = auth_headers(sessions, csrf)
    request_headers = {**headers, "Idempotency-Key": "chat-key-1"}

    first = client.post(
        url("/chat"),
        json={"message": "q", "mode": "queue"},
        headers=request_headers,
        cookies=cookies,
    )
    second = client.post(
        url("/chat"),
        json={"message": "q", "mode": "queue"},
        headers=request_headers,
        cookies=cookies,
    )
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert table_count(repo, "processing_jobs") == 1


def test_queue_mode_rejects_a_malformed_idempotency_key(repo, sessions, csrf, config) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    client = make_client(
        repo, sessions, csrf, config, service=service_for(repo, StubModelClient(text=json_answer("x")))
    )
    headers, cookies = auth_headers(sessions, csrf)
    response = client.post(
        url("/chat"),
        json={"message": "q", "mode": "queue"},
        headers={**headers, "Idempotency-Key": "bad key with spaces"},
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.INVALID_INPUT
    assert table_count(repo, "processing_jobs") == 0


# ---------------------------------------------------------------------------
# History window
# ---------------------------------------------------------------------------
def test_history_window_is_the_newest_turns_not_the_oldest(repo, sessions, csrf, config) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    stub = StubModelClient(text=json_answer("Acknowledged."))
    service = service_for(repo, stub, budget=ChatRetrievalBudget(max_history_messages=3))
    client = make_client(repo, sessions, csrf, config, service=service)
    headers, cookies = auth_headers(sessions, csrf)

    for index in range(6):
        response = post_chat(client, headers, cookies, message=f"question number {index}")
        assert response.status_code == 200, response.text

    replayed = "".join(message.content for message in stub.calls[-1]["request"].messages)
    # The most recent prior turn is replayed; the oldest question is not.
    assert "question number 5" in replayed
    assert "question number 0" not in replayed
    assert "question number 1" not in replayed
