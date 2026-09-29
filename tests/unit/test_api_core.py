"""Core HTTP API tests: envelope, status mapping, identity, idempotency.

Authority: PRD section 5.3 (response contract), section 12 (endpoint scope and
mutation rules), section 16.1 (Host/Origin/CSRF), and acceptance test AT-33.

These tests exercise the core this package owns -- the envelope, the error
mapping, the dependency chain, and idempotency -- through ``TestClient`` against a
real migrated database and real session/CSRF stores. Synthetic data only. The
endpoint surface itself is owned by other modules and is not asserted here beyond
the test-only probe routes registered below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import (
    API_PREFIX,
    ApiConfig,
    IdempotencyGuard,
    InstanceContext,
    InstanceCtx,
    RequestId,
    authorized_instance,
    create_app,
    current_state_revision,
    get_request_id,
    idempotency_guard,
    message_is_safe,
    ok_response,
    require_operation,
    sanitize_message,
    validate_envelope,
)
from resume_review.api.envelope import (
    EnvelopeSchemaError,
    accepted_response,
    error_envelope,
    ok_envelope,
)
from resume_review.api.errors import status_for_error
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository, RevisionConflict
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import (
    Code,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    ResumeReviewError,
    Unauthenticated,
)
from resume_review.models import Role

INSTANCE_ID = "inst_test"
ORIGIN = "http://testserver"

#: Number of times the idempotent probe route actually executed. A replay must not
#: move it.
_EXECUTIONS = {"n": 0}


# ---------------------------------------------------------------------------
# Test-only route module (registered through create_app's register_routes hook)
# ---------------------------------------------------------------------------
def register_probe_routes(router) -> None:
    @router.get("/probe")
    def probe(
        ctx: InstanceContext = Depends(authorized_instance),
        request_id: str = Depends(get_request_id),
    ):
        return ok_response(
            {"instance": ctx.instance_id, "role": ctx.principal.role.value},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.get("/admin-only")
    def admin_only(
        ctx: InstanceContext = Depends(require_operation(Role.ADMINISTRATOR)),
        request_id: str = Depends(get_request_id),
    ):
        return ok_response({"admin": True}, request_id=request_id, instance_id=ctx.instance_id)

    @router.get("/revision")
    def revision(
        value: int = Depends(current_state_revision),
        request_id: str = Depends(get_request_id),
    ):
        return ok_response({"state_revision": value}, request_id=request_id)

    @router.get("/aliased")
    def aliased(ctx: InstanceCtx, request_id: RequestId):
        # Exercises the Annotated dependency aliases phase-B endpoint modules use.
        return ok_response(
            {"aliased": True},
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )

    @router.get("/boom/{kind}")
    def boom(kind: str, request_id: str = Depends(get_request_id)):
        if kind == "unauth":
            raise Unauthenticated("Sign in.", detail={"reason": "test"})
        if kind == "forbidden":
            raise Forbidden("Nope.")
        if kind == "conflict":
            raise Conflict("State moved on.")
        if kind == "revision":
            raise RevisionConflict("State moved on.", current_revision=9)
        if kind == "invalid":
            raise InvalidInput("Bad input.", detail={"field": "x"})
        if kind == "dependency":
            raise DependencyUnavailable("Route down.", retryable=True)
        if kind == "notfound":
            raise NotFound("Gone.")
        if kind == "base":
            raise ResumeReviewError("Base failure.")
        if kind == "leaky":
            raise InvalidInput(
                "Could not read C:\\Users\\bob\\Smith, John resume.pdf with token=SECRETVALUE99"
            )
        if kind == "unexpected":
            raise ValueError("internal detail that must not escape")
        raise NotFound("No such probe.")

    @router.post("/count")
    def count(
        payload: dict,
        guard: IdempotencyGuard = Depends(idempotency_guard("test.count")),
        ctx: InstanceContext = Depends(authorized_instance),
        request_id: str = Depends(get_request_id),
    ):
        hit = guard.replay(payload)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
                job_id=hit.job_id,
            )
        _EXECUTIONS["n"] += 1
        result = {"count": _EXECUTIONS["n"], "echo": payload}
        guard.commit(payload, response=result)
        return ok_response(
            result,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path):
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


@pytest.fixture(autouse=True)
def _reset_executions():
    _EXECUTIONS["n"] = 0


@pytest.fixture
def make_app(repo, sessions, csrf, config):
    def factory(**kwargs):
        kwargs.setdefault("register_routes", [register_probe_routes])
        return create_app(repo, sessions=sessions, csrf_store=csrf, config=config, **kwargs)

    return factory


def auth(sessions: SessionStore, csrf: CsrfStore, role: Role = Role.REVIEWER, instance_id: str = INSTANCE_ID):
    """Issue a session and its CSRF token; return (session, headers, cookies)."""
    session = sessions.issue(instance_id, "reviewer_1", role)
    headers = {
        "X-CSRF-Token": csrf.issue(session.session_id),
        "Origin": ORIGIN,
    }
    cookies = {session_cookie_name(instance_id): session.session_id}
    return session, headers, cookies


def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


# ---------------------------------------------------------------------------
# Envelope conformance
# ---------------------------------------------------------------------------
def test_ok_envelope_conforms_to_normative_schema(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/probe"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)  # raises EnvelopeSchemaError if it does not conform
    assert body["ok"] is True
    assert body["code"] == "SUCCESS"
    assert body["instance_id"] == INSTANCE_ID
    assert body["request_id"]
    assert isinstance(body["state_revision"], int)
    assert body["warnings"] == []


def test_accepted_envelope_carries_job_id_and_conforms():
    response = accepted_response(
        {"queued": 3}, job_id="job_abc", request_id="req_1", instance_id=INSTANCE_ID
    )
    assert response.status_code == 202
    import json

    body = json.loads(response.body)
    validate_envelope(body)
    assert body["job_id"] == "job_abc"
    assert body["ok"] is True


def test_error_envelope_conforms_and_omits_job_id():
    body = error_envelope(
        Code.REVISION_CONFLICT,
        "The record changed.",
        request_id="req_9",
        instance_id=INSTANCE_ID,
        state_revision=4,
        detail={"current_revision": 9},
        retryable=False,
    )
    validate_envelope(body)
    assert body["ok"] is False
    assert body["error"]["code"] == Code.REVISION_CONFLICT
    assert "job_id" not in body


def test_validate_envelope_rejects_extra_and_bad_types():
    with pytest.raises(EnvelopeSchemaError):
        validate_envelope({"ok": True, "surprise": 1})
    with pytest.raises(EnvelopeSchemaError):
        validate_envelope({"ok": "yes"})
    with pytest.raises(EnvelopeSchemaError):
        validate_envelope({"ok": False, "code": "ok"})  # lowercase fails the pattern


def test_annotated_dependency_aliases_resolve(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/aliased"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)
    assert body["data"] == {"aliased": True}
    assert body["instance_id"] == INSTANCE_ID


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------
def test_sanitize_message_strips_paths_credentials_and_names():
    dirty = (
        r"failed to open C:\Users\bob\Smith, John resume.pdf while calling "
        r"http://gw.example/v1?token=abcd1234 with Authorization: Bearer zzz999aaa"
    )
    clean = sanitize_message(dirty)
    assert "C:\\Users" not in clean
    assert "Smith, John resume.pdf" not in clean
    assert "abcd1234" not in clean
    assert "zzz999aaa" not in clean
    assert "<path>" in clean or "<filename>" in clean
    assert message_is_safe(clean)


def test_sanitize_message_redacts_caller_supplied_names():
    clean = sanitize_message("Could not read Jane Q. Applicant's file.", names=["Jane Q. Applicant"])
    assert "Jane Q. Applicant" not in clean
    assert "<redacted>" in clean


def test_sanitize_message_redacts_email_and_is_idempotent():
    clean = sanitize_message("Contact jane.applicant@example.com failed.")
    assert "jane.applicant@example.com" not in clean
    assert "<email>" in clean
    assert sanitize_message(clean) == clean


# ---------------------------------------------------------------------------
# Error -> status mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_code"),
    [
        ("unauth", 401, Code.UNAUTHENTICATED),
        ("forbidden", 403, Code.FORBIDDEN),
        ("conflict", 409, Code.REVISION_CONFLICT),
        ("revision", 409, Code.REVISION_CONFLICT),
        ("invalid", 422, Code.INVALID_INPUT),
        ("dependency", 503, Code.ROUTE_UNAVAILABLE),
        ("notfound", 404, Code.NOT_FOUND),
        ("base", 500, Code.INTERNAL_ERROR),
        ("leaky", 422, Code.INVALID_INPUT),
        ("unexpected", 500, Code.INTERNAL_ERROR),
    ],
)
def test_error_to_status_mapping(make_app, kind, expected_status, expected_code):
    client = TestClient(make_app(), raise_server_exceptions=False)
    response = client.get(url(f"/boom/{kind}"))
    assert response.status_code == expected_status, response.text
    body = response.json()
    validate_envelope(body)
    assert body["ok"] is False
    assert body["error"]["code"] == expected_code
    assert body["request_id"]


def test_error_message_is_sanitised_end_to_end(make_app):
    client = TestClient(make_app())
    body = client.get(url("/boom/leaky")).json()
    message = body["error"]["message"]
    assert "C:\\Users" not in message
    assert "SECRETVALUE99" not in message
    assert "Smith" not in message
    assert message_is_safe(message)


def test_unexpected_error_does_not_leak_internals(make_app):
    client = TestClient(make_app(), raise_server_exceptions=False)
    body = client.get(url("/boom/unexpected")).json()
    assert body["error"]["code"] == Code.INTERNAL_ERROR
    assert "internal detail" not in body["error"]["message"]


def test_unknown_route_returns_envelope_404(make_app):
    client = TestClient(make_app())
    response = client.get(url("/does-not-exist"))
    assert response.status_code == 404
    body = response.json()
    validate_envelope(body)
    assert body["error"]["code"] == Code.NOT_FOUND


def test_status_for_error_uses_code_table_over_class_default():
    # RevisionConflict inherits Conflict (409) but the code is authoritative anyway.
    assert status_for_error(RevisionConflict("x")) == 409
    assert status_for_error(Forbidden("x")) == 403
    assert status_for_error(Unauthenticated("x")) == 401


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_unauthenticated_request_is_rejected(make_app):
    client = TestClient(make_app())
    response = client.get(url("/probe"))
    assert response.status_code == 401
    body = response.json()
    validate_envelope(body)
    assert body["error"]["code"] == Code.UNAUTHENTICATED


def test_wrong_instance_session_is_rejected(make_app, sessions, csrf):
    client = TestClient(make_app())
    session, _, _ = auth(sessions, csrf, instance_id="inst_other")
    # The session is genuine but bound to another instance; present it for this one.
    response = client.get(
        url("/probe"), headers={"Authorization": f"Bearer {session.session_id}"}
    )
    assert response.status_code == 403
    body = response.json()
    validate_envelope(body)
    assert body["error"]["code"] == Code.INSTANCE_MISMATCH


def test_missing_csrf_on_mutation_is_rejected(make_app, sessions, csrf):
    client = TestClient(make_app())
    session, _, cookies = auth(sessions, csrf)
    response = client.post(
        url("/count"),
        json={"a": 1},
        headers={"Idempotency-Key": "k-csrf", "Origin": ORIGIN},
        cookies=cookies,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.CSRF_FAILED


def test_forged_origin_is_rejected(make_app, sessions, csrf):
    client = TestClient(make_app())
    session, headers, cookies = auth(sessions, csrf)
    headers["Origin"] = "http://evil.example"
    response = client.post(
        url("/count"),
        json={"a": 1},
        headers={**headers, "Idempotency-Key": "k-origin"},
        cookies=cookies,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.ORIGIN_REJECTED


def test_default_config_accepts_same_origin_mutation(repo, sessions, csrf):
    """A browser's own Origin matches the Host, so the default config allows it."""
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(),
        register_routes=[register_probe_routes],
    )
    client = TestClient(app)
    _, headers, cookies = auth(sessions, csrf)  # Origin is http://testserver; Host is testserver
    response = client.post(
        url("/count"),
        json={"a": 1},
        headers={**headers, "Idempotency-Key": "k-same-origin"},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_default_config_rejects_cross_origin_mutation(repo, sessions, csrf):
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(),
        register_routes=[register_probe_routes],
    )
    client = TestClient(app)
    _, headers, cookies = auth(sessions, csrf)
    headers["Origin"] = "http://evil.example"
    response = client.post(
        url("/count"),
        json={"a": 1},
        headers={**headers, "Idempotency-Key": "k-cross-origin"},
        cookies=cookies,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.ORIGIN_REJECTED


def test_forged_host_is_rejected(make_app):
    client = TestClient(make_app())
    response = client.get(url("/probe"), headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.HOST_REJECTED


def test_wrong_role_is_rejected(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.get(url("/admin-only"), headers=headers, cookies=cookies)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_correct_role_passess_and_state_revision_is_exposed(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf, role=Role.ADMINISTRATOR)
    assert client.get(url("/admin-only"), headers=headers, cookies=cookies).status_code == 200
    body = client.get(url("/revision"), headers=headers, cookies=cookies).json()
    assert isinstance(body["data"]["state_revision"], int)


# ---------------------------------------------------------------------------
# Request id
# ---------------------------------------------------------------------------
def test_request_id_is_propagated_from_header(make_app):
    client = TestClient(make_app())
    response = client.get(url("/probe"), headers={"X-Request-ID": "req_caller-supplied_01"})
    body = response.json()
    assert body["request_id"] == "req_caller-supplied_01"
    assert response.headers["X-Request-ID"] == "req_caller-supplied_01"


def test_invalid_request_id_is_replaced_and_echoed(make_app):
    client = TestClient(make_app())
    response = client.get(url("/probe"), headers={"X-Request-ID": "has a space and !!"})
    body = response.json()
    assert body["request_id"].startswith("req_")
    assert response.headers["X-Request-ID"] == body["request_id"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_idempotent_replay_returns_original_without_repeating_side_effect(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    payload = {"decision": "reject", "document_id": "doc_1"}
    request_headers = {**headers, "Idempotency-Key": "key-abc"}

    first = client.post(url("/count"), json=payload, headers=request_headers, cookies=cookies)
    assert first.status_code == 200, first.text
    first_body = first.json()
    validate_envelope(first_body)
    assert first_body["data"]["count"] == 1

    second = client.post(url("/count"), json=payload, headers=request_headers, cookies=cookies)
    assert second.status_code == 200
    second_body = second.json()
    validate_envelope(second_body)
    assert second_body["data"] == first_body["data"]
    assert _EXECUTIONS["n"] == 1  # the side effect ran exactly once


def test_idempotency_key_reuse_with_different_payload_conflicts(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    request_headers = {**headers, "Idempotency-Key": "key-conflict"}

    assert (
        client.post(url("/count"), json={"a": 1}, headers=request_headers, cookies=cookies).status_code
        == 200
    )
    conflict = client.post(
        url("/count"), json={"a": 2}, headers=request_headers, cookies=cookies
    )
    assert conflict.status_code == 409
    body = conflict.json()
    validate_envelope(body)
    assert body["error"]["code"] == Code.IDEMPOTENCY_KEY_REUSED
    assert _EXECUTIONS["n"] == 1


def test_missing_idempotency_key_is_invalid_input(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(url("/count"), json={"a": 1}, headers=headers, cookies=cookies)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.INVALID_INPUT


def test_malformed_idempotency_key_is_invalid_input(make_app, sessions, csrf):
    client = TestClient(make_app())
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/count"),
        json={"a": 1},
        headers={**headers, "Idempotency-Key": "bad key with spaces"},
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# The chat adapter route (the factory's fallback bridge)
# ---------------------------------------------------------------------------
# ``resume_review.api.chat`` is in ``DEFAULT_ROUTE_MODULES`` and owns ``/chat`` in a
# normally-composed application; the factory's narrower bridge only registers when
# nothing else claimed the path. These tests cover that bridge, so they pin
# ``route_modules`` to nothing rather than depending on which handler happened to be
# registered first. The composed application's ``/chat`` -- scope resolution, queue
# mode, route policy -- is covered by tests/unit/test_api_chat.py.
_BRIDGE_ONLY = {"route_modules": ()}


def test_chat_route_absent_without_adapter(make_app):
    client = TestClient(make_app(**_BRIDGE_ONLY))
    assert client.post(url("/chat"), json={"message": "hi"}).status_code == 404


def test_chat_route_forwards_a_bounded_turn(make_app, sessions, csrf):
    seen: dict = {}

    def adapter(payload, *, principal, instance_id):
        seen["payload"] = payload
        seen["actor"] = principal.actor_ref
        return {"reply": "hello"}

    client = TestClient(make_app(chat_adapter=adapter, **_BRIDGE_ONLY))
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/chat"),
        json={"message": "summarize", "document_ids": ["doc_1"]},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)
    assert body["data"]["reply"] == "hello"
    assert seen["actor"] == "reviewer_1"
    assert seen["payload"] == {"message": "summarize", "conversation_id": None, "document_ids": ["doc_1"]}


def test_chat_route_rejects_unknown_fields(make_app, sessions, csrf):
    client = TestClient(make_app(chat_adapter=lambda payload, **kw: {}, **_BRIDGE_ONLY))
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/chat"),
        json={"message": "hi", "endpoint": "http://evil"},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.VALIDATION_FAILED


def test_chat_route_requires_reviewer_role(make_app, sessions, csrf):
    client = TestClient(make_app(chat_adapter=lambda payload, **kw: {}, **_BRIDGE_ONLY))
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.post(url("/chat"), json={"message": "hi"}, headers=headers, cookies=cookies)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


# ---------------------------------------------------------------------------
# Surface hygiene
# ---------------------------------------------------------------------------
def test_app_constructs_without_endpoint_modules(repo, sessions, csrf, config):
    app = create_app(repo, sessions=sessions, csrf_store=csrf, config=config)
    client = TestClient(app)
    # No probe route was registered; the instance prefix still resolves to a 404 envelope.
    response = client.get(url("/probe"))
    assert response.status_code == 404
    validate_envelope(response.json())


def test_no_generic_execution_or_proxy_routes_exist(make_app):
    app = make_app()
    paths = [getattr(route, "path", "") for route in app.routes]
    forbidden = ("sql", "shell", "exec", "proxy", "gateway", "static", "upload", "read-file")
    for path in paths:
        lowered = path.lower()
        for needle in forbidden:
            assert needle not in lowered, f"route {path!r} looks like a forbidden surface"
    # Docs are disabled by default, so the schema is not served either.
    assert "/openapi.json" not in paths


def test_api_prefix_matches_prd_scope():
    assert API_PREFIX == "/api/v1/instances/{instance_id}"
