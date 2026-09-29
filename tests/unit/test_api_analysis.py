"""Endpoint tests for criteria, scan, analysis, jobs, and backup (PRD section 12.1).

Authority: PRD section 12.1 (the endpoint surface and its allowed principals) and
section 12.2 (202 plus a durable job id for long work; ``Idempotency-Key`` for
retryable POSTs; the response envelope). Synthetic data only.

The tests below assert the four separations this work exists to enforce:

* proposing criteria never activates them, and only a human reviewer may activate;
* ``POST /analysis/results`` is a bound-worker route -- a reviewer session, a wrong
  lease token, a job leased to another worker, and a superseded revision are all
  refused before anything is written;
* a submitted result is checked against ``schemas/analysis_result.schema.json``
  before it can reach the database;
* ``GET /jobs/{id}`` is scoped to the requester, and ``/scan`` and
  ``/analysis/jobs`` answer 202 with a durable job id rather than blocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, create_app, validate_envelope
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code
from resume_review.models import MediaType, Role

INSTANCE_ID = "inst_test"
ORIGIN = "http://testserver"


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


@pytest.fixture
def app(repo, sessions, csrf, config):
    # No route_modules override: the real discovery path must register the group.
    return create_app(repo, sessions=sessions, csrf_store=csrf, config=config)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def login(sessions: SessionStore, csrf: CsrfStore, actor: str, role: Role = Role.REVIEWER):
    """Issue a session and its CSRF token; return (session, headers, cookies)."""
    session = sessions.issue(INSTANCE_ID, actor, role)
    headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return session, headers, cookies


def posting(headers: dict[str, str], key: str) -> dict[str, str]:
    """A mutating request's headers plus a unique idempotency key."""
    return {**headers, "Idempotency-Key": key}


def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def seed_job(repo: Repository) -> None:
    repo.create_or_update_job("Backend Engineer", "Build and operate the review helper.")


def seed_document(repo: Repository, rel_path: str = "inbox/resume.pdf"):
    document = repo.create_document("resume.pdf", rel_path, MediaType.PDF, 120, f"sha:{rel_path}", None)
    repo.add_revision(document.id, f"sha:{rel_path}", 120, rel_path, parser_name="pypdf", parser_version="1")
    return repo.get_document(document.id)


def activate_criteria(client: TestClient, repo, headers, cookies, criterion_id: str = "c_python") -> int:
    """Seed a job, propose one criterion, and approve it. Returns the version."""
    seed_job(repo)
    proposal = client.post(
        url("/criteria/proposals"),
        json={
            "proposals": [
                {
                    "criterion_id": criterion_id,
                    "definition": "Demonstrated professional Python experience.",
                    "label": "required",
                }
            ]
        },
        headers=posting(headers, f"prop-{criterion_id}"),
        cookies=cookies,
    )
    assert proposal.status_code == 200, proposal.text
    version = int(proposal.json()["data"]["version"])
    activation = client.post(
        url(f"/criteria/{version}/activate"),
        json={"expected_revision": repo.db.state_revision()},
        headers=posting(headers, f"act-{criterion_id}"),
        cookies=cookies,
    )
    assert activation.status_code == 200, activation.text
    return version


def queue_analysis(client: TestClient, headers, cookies, document_id: str) -> str:
    response = client.post(
        url("/analysis/jobs"),
        json={"document_ids": [document_id]},
        headers=posting(headers, f"jobs-{document_id}"),
        cookies=cookies,
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def minimal_result(document_id: str, revision: int, criteria_version: int, criterion_id: str | None = None):
    criteria = []
    if criterion_id:
        criteria.append(
            {"criterion_id": criterion_id, "result": "not_found", "explanation": "", "evidence_ids": []}
        )
    return {
        "schema_version": "1.0",
        "document_id": document_id,
        "source_revision": revision,
        "criteria_version": criteria_version,
        "summary": {"text": "No criterion was established.", "evidence_ids": []},
        "criteria": criteria,
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Propose vs. activate (PRD 12.1, 7.4)
# ---------------------------------------------------------------------------
def test_propose_records_definitions_and_does_not_activate(client, repo, sessions, csrf):
    seed_job(repo)
    _, headers, cookies = login(sessions, csrf, "reviewer_1")

    response = client.post(
        url("/criteria/proposals"),
        json={"proposals": [{"criterion_id": "c_python", "definition": "Python experience."}]},
        headers=posting(headers, "propose-1"),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)
    assert body["data"]["activated"] is False
    assert body["data"]["active_version"] == 0
    assert body["data"]["proposals"][0]["approved"] is False
    # The proposal is stored, but nothing is approved and analysis cannot run yet.
    assert repo.active_criteria_version() == 0
    assert [c.criterion_id for c in repo.list_criteria(None, approved_only=False)] == ["c_python"]


def test_worker_and_agent_cannot_activate_criteria(client, repo, sessions, csrf):
    seed_job(repo)
    _, reviewer_headers, reviewer_cookies = login(sessions, csrf, "reviewer_1")
    proposed = client.post(
        url("/criteria/proposals"),
        json={"proposals": [{"criterion_id": "c_python", "definition": "Python experience."}]},
        headers=posting(reviewer_headers, "propose-1"),
        cookies=reviewer_cookies,
    )
    version = int(proposed.json()["data"]["version"])

    for actor in ("worker:w1", "agent:a1"):
        _, headers, cookies = login(sessions, csrf, actor)
        response = client.post(
            url(f"/criteria/{version}/activate"),
            json={"expected_revision": repo.db.state_revision()},
            headers=posting(headers, f"activate-{actor}"),
            cookies=cookies,
        )
        # ``APPROVAL_MUST_BE_HUMAN`` maps to 422 in the frozen error table; the
        # code, not the status, names the refusal (same convention as actions.py).
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == Code.APPROVAL_MUST_BE_HUMAN

    # The refusal is real: the version is still unapproved and inactive.
    assert repo.active_criteria_version() == 0


def test_agent_may_propose_with_agent_origin(client, repo, sessions, csrf):
    seed_job(repo)
    _, headers, cookies = login(sessions, csrf, "agent:a1")

    response = client.post(
        url("/criteria/proposals"),
        json={"proposals": [{"criterion_id": "c_go", "definition": "Go experience."}]},
        headers=posting(headers, "propose-agent"),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    assert repo.active_criteria_version() == 0
    proposals = repo.list_criteria(None, approved_only=False)
    assert proposals[0].origin == "agent_proposal"
    assert proposals[0].created_by == "agent:a1"


def test_activation_requires_the_current_expected_revision(client, repo, sessions, csrf):
    seed_job(repo)
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    proposed = client.post(
        url("/criteria/proposals"),
        json={"proposals": [{"criterion_id": "c_python", "definition": "Python experience."}]},
        headers=posting(headers, "propose-1"),
        cookies=cookies,
    )
    version = int(proposed.json()["data"]["version"])

    stale = client.post(
        url(f"/criteria/{version}/activate"),
        json={"expected_revision": repo.db.state_revision() + 100},
        headers=posting(headers, "activate-stale"),
        cookies=cookies,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == Code.REVISION_CONFLICT
    assert repo.active_criteria_version() == 0

    fresh = client.post(
        url(f"/criteria/{version}/activate"),
        json={"expected_revision": repo.db.state_revision()},
        headers=posting(headers, "activate-fresh"),
        cookies=cookies,
    )
    assert fresh.status_code == 200, fresh.text
    assert repo.active_criteria_version() == version


# ---------------------------------------------------------------------------
# Queued work returns 202 with a durable job id (PRD 12.2)
# ---------------------------------------------------------------------------
def test_scan_returns_202_with_a_durable_job_id(client, repo, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    response = client.post(
        url("/scan"), json={}, headers=posting(headers, "scan-1"), cookies=cookies
    )
    assert response.status_code == 202, response.text
    body = response.json()
    validate_envelope(body)
    assert body["code"] == "ACCEPTED"
    assert body["job_id"]
    job = repo.get_job(body["job_id"])
    assert job is not None and job["kind"] == "scan" and job["state"] == "queued"


def test_scan_replay_returns_the_same_job_id(client, repo, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    request_headers = posting(headers, "scan-same")
    first = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies)
    second = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len([j for j in repo.list_jobs() if j["kind"] == "scan"]) == 1


def test_scan_requires_an_idempotency_key(client, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    response = client.post(url("/scan"), json={}, headers=headers, cookies=cookies)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == Code.INVALID_INPUT


def test_analysis_jobs_returns_202_with_a_job_id(client, repo, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    version = activate_criteria(client, repo, headers, cookies)
    document = seed_document(repo)

    response = client.post(
        url("/analysis/jobs"),
        json={"document_ids": [document.id]},
        headers=posting(headers, "jobs-1"),
        cookies=cookies,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    validate_envelope(body)
    assert body["job_id"]
    job = repo.get_job(body["job_id"])
    assert job is not None and job["kind"] == "analysis" and job["document_id"] == document.id
    assert body["data"]["criteria_version"] == version
    assert body["data"]["jobs"][0]["source_revision"] == document.current_revision


def test_analysis_jobs_refused_without_approved_criteria(client, repo, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    seed_job(repo)  # a requisition exists, but no criteria are approved
    document = seed_document(repo)

    response = client.post(
        url("/analysis/jobs"),
        json={"document_ids": [document.id]},
        headers=posting(headers, "jobs-nocriteria"),
        cookies=cookies,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == Code.CRITERIA_NOT_APPROVED
    assert repo.list_jobs() == []


# ---------------------------------------------------------------------------
# POST /analysis/results is a bound-worker route
# ---------------------------------------------------------------------------
def _leased_job(client, repo, sessions, csrf, actor="worker:w1"):
    """Seed a document and criteria, queue one analysis job, and lease it."""
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    version = activate_criteria(client, repo, headers, cookies)
    document = seed_document(repo)
    job_id = queue_analysis(client, headers, cookies, document.id)
    claimed = repo.claim_job(actor, 300.0, kinds=["analysis"])
    assert claimed is not None and str(claimed["id"]) == job_id
    token = str(repo.get_job(job_id)["lease_token"])
    return document, version, job_id, token


def test_reviewer_session_cannot_submit_a_result(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    _, headers, cookies = login(sessions, csrf, "reviewer_1")

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": token,
            "result": minimal_result(document.id, document.current_revision, version),
        },
        headers=posting(headers, "result-reviewer"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.FORBIDDEN
    assert repo.current_profile(document.id) is None
    assert repo.get_job(job_id)["state"] == "leased"


def test_wrong_lease_token_is_refused(client, repo, sessions, csrf):
    document, version, job_id, _token = _leased_job(client, repo, sessions, csrf)
    _, headers, cookies = login(sessions, csrf, "worker:w1")

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": "not-the-real-token",
            "result": minimal_result(document.id, document.current_revision, version),
        },
        headers=posting(headers, "result-badtoken"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["detail"]["reason"] == "lease_token_mismatch"
    assert repo.current_profile(document.id) is None


def test_result_for_a_job_leased_to_another_worker_is_refused(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf, actor="worker:w1")
    _, headers, cookies = login(sessions, csrf, "worker:w2")

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": token,
            "result": minimal_result(document.id, document.current_revision, version),
        },
        headers=posting(headers, "result-wrongworker"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["detail"]["reason"] == "lease_owner_mismatch"
    assert repo.current_profile(document.id) is None


def test_result_for_a_job_without_a_live_lease_is_refused(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    repo.complete_job(job_id, result_ref="prof_manual")
    _, headers, cookies = login(sessions, csrf, "worker:w1")

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": token,
            "result": minimal_result(document.id, document.current_revision, version),
        },
        headers=posting(headers, "result-completed"),
        cookies=cookies,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == Code.LEASE_HELD


# ---------------------------------------------------------------------------
# Schema and supersession checks
# ---------------------------------------------------------------------------
def test_result_is_checked_against_the_versioned_schema(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    _, headers, cookies = login(sessions, csrf, "worker:w1")

    invalid = minimal_result(document.id, document.current_revision, version)
    invalid["unexpected_field"] = True  # additionalProperties is false in the schema

    response = client.post(
        url("/analysis/results"),
        json={"job_id": job_id, "lease_token": token, "result": invalid},
        headers=posting(headers, "result-schema"),
        cookies=cookies,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == Code.ANALYSIS_SCHEMA_INVALID
    assert repo.current_profile(document.id) is None
    assert repo.get_job(job_id)["state"] == "leased"


def test_result_with_an_invalid_criterion_id_is_refused(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    _, headers, cookies = login(sessions, csrf, "worker:w1")

    payload = minimal_result(document.id, document.current_revision, version, criterion_id="c_invented")
    response = client.post(
        url("/analysis/results"),
        json={"job_id": job_id, "lease_token": token, "result": payload},
        headers=posting(headers, "result-unknown-criterion"),
        cookies=cookies,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == Code.ANALYSIS_UNKNOWN_CRITERION


def test_result_for_a_superseded_revision_is_refused(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    # The document advances while the job still holds its original revision.
    repo.add_revision(document.id, "sha:new", 140, "inbox/resume.pdf", parser_name="pypdf", parser_version="1")
    _, headers, cookies = login(sessions, csrf, "worker:w1")

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": token,
            "result": minimal_result(document.id, document.current_revision, version),
        },
        headers=posting(headers, "result-stale"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.ANALYSIS_STALE_RESULT


def test_valid_result_is_committed_as_the_current_profile(client, repo, sessions, csrf):
    document, version, job_id, token = _leased_job(client, repo, sessions, csrf)
    _, headers, cookies = login(sessions, csrf, "worker:w1")
    criteria = repo.list_criteria(version, approved_only=True)

    response = client.post(
        url("/analysis/results"),
        json={
            "job_id": job_id,
            "lease_token": token,
            "result": minimal_result(
                document.id, document.current_revision, version, criterion_id=criteria[0].criterion_id
            ),
            "run": {"model_route": "fixture", "model_version": "stub-1"},
        },
        headers=posting(headers, "result-valid"),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)
    assert body["data"]["status"] == "committed"
    assert body["data"]["profile_id"]
    assert repo.get_job(job_id)["state"] == "succeeded"

    profile = repo.current_profile(document.id)
    assert profile is not None and profile.id == body["data"]["profile_id"]
    assert profile.source_revision == document.current_revision
    assert profile.model_route == "fixture"


# ---------------------------------------------------------------------------
# GET /jobs/{id} scoping
# ---------------------------------------------------------------------------
def test_job_polling_is_scoped_to_the_requester(client, repo, sessions, csrf):
    _, owner_headers, owner_cookies = login(sessions, csrf, "reviewer_1")
    queued = client.post(
        url("/scan"), json={}, headers=posting(owner_headers, "scan-scope"), cookies=owner_cookies
    )
    job_id = queued.json()["job_id"]

    ok = client.get(url(f"/jobs/{job_id}"), headers=owner_headers, cookies=owner_cookies)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    validate_envelope(body)
    assert body["data"]["job_id"] == job_id
    assert "lease_token" not in body["data"]

    # A different reviewer is not the requester: the job is indistinguishable from
    # one that does not exist, so existence is not leaked.
    _, other_headers, other_cookies = login(sessions, csrf, "reviewer_2")
    hidden = client.get(url(f"/jobs/{job_id}"), headers=other_headers, cookies=other_cookies)
    assert hidden.status_code == 404
    validate_envelope(hidden.json())

    missing = client.get(url("/jobs/job_does_not_exist"), headers=other_headers, cookies=other_cookies)
    assert missing.status_code == 404
    validate_envelope(missing.json())


def test_unknown_job_id_is_a_404_envelope(client, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1")
    response = client.get(url("/jobs/job_absent"), headers=headers, cookies=cookies)
    assert response.status_code == 404
    validate_envelope(response.json())


def test_worker_can_poll_a_job_it_holds(client, repo, sessions, csrf):
    document, version, job_id, _token = _leased_job(client, repo, sessions, csrf, actor="worker:w1")
    _, headers, cookies = login(sessions, csrf, "worker:w1")
    response = client.get(url(f"/jobs/{job_id}"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "leased"
    assert "lease_token" not in response.json()["data"]


# ---------------------------------------------------------------------------
# POST /backup (administrator only)
# ---------------------------------------------------------------------------
def test_backup_requires_an_administrator(client, repo, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "reviewer_1", Role.REVIEWER)
    response = client.post(
        url("/backup"), json={}, headers=posting(headers, "backup-1"), cookies=cookies
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_administrator_backup_creates_a_verified_copy(client, repo, db, sessions, csrf):
    _, headers, cookies = login(sessions, csrf, "admin_1", Role.ADMINISTRATOR)
    response = client.post(
        url("/backup"), json={}, headers=posting(headers, "backup-admin"), cookies=cookies
    )
    assert response.status_code == 200, response.text
    body = response.json()
    validate_envelope(body)
    assert body["data"]["verified"] is True
    assert body["data"]["integrity"] == "ok"
    backup_path = Path(db.path).parent / "backups" / body["data"]["backup_file"]
    assert backup_path.is_file()
    assert backup_path.stat().st_size == body["data"]["byte_size"]


# ---------------------------------------------------------------------------
# Surface hygiene
# ---------------------------------------------------------------------------
def test_group_routes_are_registered_under_the_instance_prefix(app):
    # ``app.routes`` holds one opaque included-router entry in this Starlette
    # version, so the route table is read from the generated OpenAPI paths.
    paths = set(app.openapi()["paths"])
    for suffix in (
        "/criteria/proposals",
        "/criteria/{version}/activate",
        "/scan",
        "/analysis/jobs",
        "/analysis/results",
        "/jobs/{job_id}",
        "/backup",
    ):
        assert f"/api/v1/instances/{{instance_id}}{suffix}" in paths, suffix
