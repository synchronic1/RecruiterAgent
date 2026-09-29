"""Bounded requisition API tests using only synthetic data."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, create_app
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.models import MediaType, Role, sha256_hex


INSTANCE_ID = "inst_requisition"
ORIGIN = "http://testserver"
URL = f"/api/v1/instances/{INSTANCE_ID}/requisition"


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
def client(repo: Repository, sessions: SessionStore, csrf: CsrfStore) -> TestClient:
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
    )
    return TestClient(app)


def login(
    sessions: SessionStore,
    csrf: CsrfStore,
    *,
    actor: str = "reviewer_1",
    role: Role = Role.REVIEWER,
    instance_id: str = INSTANCE_ID,
):
    session = sessions.issue(instance_id, actor, role)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf.issue(session.session_id)}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return session, headers, cookies


def payload(repo: Repository, **changes):
    value = {
        "title": "Backend Engineer",
        "description_text": "Build and operate a synthetic review service.",
        "source_reference": "https://jobs.example.test/roles/backend?id=42",
        "expected_revision": repo.db.state_revision(),
    }
    value.update(changes)
    return value


def test_get_is_authenticated_read_only_and_returns_null(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    _, headers, cookies = login(sessions, csrf, role=Role.VIEWER)
    revision = repo.db.state_revision()

    response = client.get(URL, headers={"Origin": ORIGIN}, cookies=cookies)

    assert response.status_code == 200
    assert response.json()["data"] == {"requisition": None}
    assert repo.db.state_revision() == revision
    assert client.get(URL).status_code == 401


def test_put_persists_bounded_view_and_session_audit(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    _, headers, cookies = login(sessions, csrf, actor="human_reviewer")
    response = client.put(
        URL,
        json=payload(repo),
        headers={**headers, "X-Request-ID": "req-requisition-1"},
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    saved = body["data"]["requisition"]
    assert saved["title"] == "Backend Engineer"
    assert saved["description_sha256"] == sha256_hex(payload(repo)["description_text"])
    assert saved["source_reference"].startswith("https://")
    assert saved["criteria_version"] == 0
    assert body["warnings"][0]["code"] == "APPROVED_CRITERIA_UNCHANGED"

    durable = repo.get_requisition()
    assert durable is not None
    assert durable["description_text"] == saved["description_text"]
    event = repo.list_audit(limit=1)[0]
    assert event["event"] == "job.upsert"
    assert event["actor"] == "human_reviewer"
    assert event["actor_kind"] == "human"
    assert event["request_id"] == "req-requisition-1"


def test_put_requires_human_reviewer_or_admin(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    _, viewer_headers, viewer_cookies = login(sessions, csrf, actor="viewer", role=Role.VIEWER)
    assert client.put(URL, json=payload(repo), headers=viewer_headers, cookies=viewer_cookies).status_code == 403

    for actor in ("agent:model", "worker:queue-1"):
        _, headers, cookies = login(sessions, csrf, actor=actor)
        response = client.put(URL, json=payload(repo), headers=headers, cookies=cookies)
        assert response.status_code == 403

    _, admin_headers, admin_cookies = login(
        sessions, csrf, actor="admin_1", role=Role.ADMINISTRATOR
    )
    assert client.put(
        URL, json=payload(repo), headers=admin_headers, cookies=admin_cookies
    ).status_code == 200


def test_put_enforces_instance_csrf_and_origin(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    _, headers, cookies = login(sessions, csrf)
    no_csrf = dict(headers)
    del no_csrf["X-CSRF-Token"]
    assert client.put(URL, json=payload(repo), headers=no_csrf, cookies=cookies).status_code == 403

    bad_origin = {**headers, "Origin": "https://attacker.example"}
    assert client.put(URL, json=payload(repo), headers=bad_origin, cookies=cookies).status_code == 403

    _, foreign_headers, foreign_cookies = login(
        sessions, csrf, actor="foreign", instance_id="inst_other"
    )
    response = client.put(
        URL, json=payload(repo), headers=foreign_headers, cookies=foreign_cookies
    )
    assert response.status_code in {401, 403}
    assert repo.get_requisition() is None


@pytest.mark.parametrize(
    "change",
    [
        {"description_text": "   "},
        {"description_text": "x" * 20_001},
        {"title": "x" * 201},
        {"source_reference": "ftp://jobs.example.test/role"},
        {"source_reference": "https://user:secret@jobs.example.test/role"},
        {"source_reference": "https://jobs.example.test/role\nnext"},
        {"source_reference": "https://jobs.example.test/" + "x" * 2050},
        {"unexpected": "field"},
    ],
)
def test_put_rejects_invalid_inputs_without_writing(
    change: dict[str, object],
    client: TestClient,
    sessions: SessionStore,
    csrf: CsrfStore,
    repo: Repository,
) -> None:
    _, headers, cookies = login(sessions, csrf)
    response = client.put(URL, json=payload(repo, **change), headers=headers, cookies=cookies)
    assert response.status_code == 422
    assert repo.get_requisition() is None


def test_stale_revision_is_atomic_and_does_not_overwrite(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    _, headers, cookies = login(sessions, csrf)
    first = client.put(URL, json=payload(repo), headers=headers, cookies=cookies)
    assert first.status_code == 200
    stale_revision = first.json()["state_revision"] - 1

    response = client.put(
        URL,
        json=payload(repo, title="Stale overwrite", expected_revision=stale_revision),
        headers=headers,
        cookies=cookies,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "REVISION_CONFLICT"
    assert repo.get_requisition()["title"] == "Backend Engineer"


def test_save_preserves_approved_criteria_and_human_decision(
    client: TestClient, sessions: SessionStore, csrf: CsrfStore, repo: Repository
) -> None:
    repo.create_or_update_job("Original", "Original synthetic description")
    proposal = repo.create_criteria_proposal(
        "c_python", "Python experience", created_by="reviewer_1"
    )
    repo.activate_criteria_version(proposal.version, actor="reviewer_1")
    document = repo.create_document(
        original_filename="synthetic.pdf",
        rel_path="synthetic.pdf",
        media_type=MediaType.PDF,
        size_bytes=10,
        content_sha256="synthetic-hash",
        fs_identity="synthetic-identity",
    )
    decision = repo.set_decision(document.id, "keep", expected_revision=0, actor="reviewer_1")
    active_before = repo.active_criteria_version()
    criteria_before = repo.list_criteria(approved_only=True)

    _, headers, cookies = login(sessions, csrf)
    response = client.put(URL, json=payload(repo), headers=headers, cookies=cookies)

    assert response.status_code == 200, response.text
    assert repo.active_criteria_version() == active_before
    assert repo.list_criteria(approved_only=True) == criteria_before
    current_decision = repo.get_decision(document.id)
    assert current_decision.disposition == decision.disposition
    assert current_decision.decision_revision == decision.decision_revision
    assert repo.get_requisition()["criteria_version"] == active_before
