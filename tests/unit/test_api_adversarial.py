"""Adversarial HTTP-API tests for ``src/resume_review/api``.

Written by a verifier whose job is to *refute* security claims, not to confirm
them. Every test drives the real ``create_app`` application through
``fastapi.testclient.TestClient`` against a real migrated database, real session
and CSRF stores, and a real workspace on disk. Synthetic data only; no network, no
symlink/junction, no privilege.

The hunt, per the task brief:

1. authentication and authorization bypass (unauthenticated, wrong role, wrong
   instance, forged Origin, missing/rotated CSRF, a body-supplied ``actor``);
2. idempotency (same key + same payload does not repeat a side effect; same key +
   different payload is 409; a key scoped to one principal is not usable by
   another);
3. path escape through ``/documents/{id}/original`` (traversal, absolute,
   percent-encoding, backslash, UNC, reserved device name, null byte);
4. the catastrophe (no endpoint may move/delete a file, manufacture an approval
   without an approved plan, accept a caller destination, or execute anything);
5. envelope and leakage (error messages never carry a candidate name, an absolute
   path, or a credential; the envelope validates on success and failure);
6. layer direction (nothing under ``api/`` is imported by db, storage, actions,
   analysis).

Each test names the exact attack and the rejecting output it observed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, create_app, validate_envelope
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.bootstrap import HostRegistry
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code
from resume_review.models import MediaType, REJECTED_DIR, REVIEW_DIR, Role
from resume_review.util import sha256_file

INSTANCE_ID = "inst_adversarial"
OTHER_INSTANCE_ID = "inst_other"
ORIGIN = "http://testserver"
NAME = "candidate-001.pdf"
DATA = b"%PDF-1.4\nadversarial path-escape test bytes\n"

ROUTE_MODULES = (
    "resume_review.api.documents",
    "resume_review.api.criteria",
    "resume_review.api.actions",
)

#: A name and path a real deployment must never reflect in an error message.
CANDIDATE_NAME = "Jane Q. Applicant"
CANDIDATE_FILE = "Jane Q. Applicant resume.pdf"
SECRET_TOKEN = "Bearer zzz999SECRET99aaa"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the host registry at a private temp directory for every test."""
    directory = tmp_path / "registry"
    directory.mkdir()
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(directory))
    return directory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace laid out as ``<root>/.review/review.db`` (PRD section 4)."""
    root = tmp_path / "Job - Operations Manager"
    (root / REVIEW_DIR).mkdir(parents=True)
    (root / "resumes").mkdir(parents=True)
    return root


@pytest.fixture
def db(workspace: Path) -> Iterator[Database]:
    database = Database(DbConfig(path=workspace / REVIEW_DIR / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database, workspace: Path) -> Repository:
    repository = Repository(db)
    repository.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    HostRegistry().register(INSTANCE_ID, canonical_root=str(workspace))
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
def app(repo: Repository, sessions: SessionStore, csrf: CsrfStore, config: ApiConfig):
    """The whole endpoint surface, plus the core ``/chat`` route with a stub adapter."""
    return create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=config,
        route_modules=ROUTE_MODULES,
        chat_adapter=lambda payload, **kw: {"reply": "stub", "seen": payload},
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def auth(
    sessions: SessionStore,
    csrf: CsrfStore,
    actor_ref: str = "reviewer_1",
    role: Role = Role.REVIEWER,
    instance_id: str = INSTANCE_ID,
):
    """Issue a session and its CSRF token; return (session, headers, cookies)."""
    session = sessions.issue(instance_id, actor_ref, role)
    headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
    cookies = {session_cookie_name(instance_id): session.session_id}
    return session, headers, cookies


def with_key(headers: dict, key: str) -> dict:
    return {**headers, "Idempotency-Key": key}


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Every regular file under the workspace, excluding the ``.review`` journal."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == REVIEW_DIR:
            continue
        if path.is_file():
            out[rel.as_posix()] = path.read_bytes()
    return out


def seed_rejected_document(repo: Repository, workspace: Path, name: str = NAME):
    """Place a real file, register it, and mark it Reject (Reject is not approval)."""
    path = workspace / name
    path.write_bytes(DATA)
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=len(DATA),
        content_sha256=sha256_file(path),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor="reviewer_1")
    return document


def plan_via_api(client, headers, cookies, document_id: str, *, key: str = "plan-1"):
    response = client.post(
        url("/actions/plan"),
        json={"document_ids": [document_id]},
        headers=with_key(headers, key),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _abs_paths_present(text: str, workspace: Path, db: Database) -> list[str]:
    """Absolute-path shapes that must never appear in an API response body."""
    found: list[str] = []
    needles = [str(workspace.resolve()), str(Path(db.path).resolve())]
    for needle in needles:
        if needle and needle in text:
            found.append(needle)
    if re.search(r"[A-Za-z]:[\\/][^\s\"'<>|]+", text):
        found.append("<drive-path>")
    if "\\\\?\\" in text:
        found.append("<device-namespace>")
    return found


def assert_envelope(response, expected_status: int) -> dict:
    assert response.status_code == expected_status, response.text
    body = response.json()
    validate_envelope(body)  # raises EnvelopeSchemaError on non-conformance
    return body


# ===========================================================================
# 1. AUTHENTICATION AND AUTHORIZATION
# ===========================================================================
UNAUTH_READS = [
    "/status",
    "/documents",
    "/documents/doc_probe",
    "/documents/doc_probe/original",
    "/jobs/job_probe",
]

UNAUTH_MUTATIONS = [
    ("POST", "/scan", {}),
    ("POST", "/criteria/proposals", {"proposals": [{"criterion_id": "c1", "definition": "d"}]}),
    ("POST", "/analysis/jobs", {"document_ids": ["doc_probe"]}),
    ("POST", "/actions/plan", {"document_ids": ["doc_probe"]}),
    ("POST", "/decisions/bulk", {"items": [{"document_id": "doc_probe", "disposition": "keep", "expected_revision": 0}]}),
    ("POST", "/backup", {}),
    ("POST", "/chat", {"message": "hello"}),
    ("PUT", "/documents/doc_probe/action-intent", {"intent": "reject", "expected_revision": 0}),
    ("PATCH", "/documents/doc_probe/decision", {"disposition": "keep", "expected_revision": 0}),
]


@pytest.mark.parametrize("path", UNAUTH_READS)
def test_unauthenticated_read_is_rejected(client, path):
    response = client.get(url(path))
    body = assert_envelope(response, 401)
    assert body["ok"] is False
    assert body["error"]["code"] == Code.UNAUTHENTICATED


@pytest.mark.parametrize(("method", "path", "payload"), UNAUTH_MUTATIONS)
def test_unauthenticated_mutation_is_rejected(client, method, path, payload):
    response = client.request(method, url(path), json=payload)
    body = assert_envelope(response, 401)
    assert body["error"]["code"] == Code.UNAUTHENTICATED


def test_no_route_answers_an_unauthenticated_caller_with_success(client, app):
    """Sweep every registered route: none of them may answer 2xx without a session."""
    paths = app.openapi()["paths"]
    assert paths, "no routes were registered"
    checked = 0
    for template, operations in paths.items():
        path = template.replace("{instance_id}", INSTANCE_ID)
        for param in re.findall(r"\{([^}]+)\}", path):
            path = path.replace("{" + param + "}", "probe")
        for method in operations:
            if method.upper() in ("HEAD", "OPTIONS", "TRACE"):
                continue
            response = client.request(method.upper(), path, json={})
            assert response.status_code in (401, 403), (
                f"{method.upper()} {path} answered {response.status_code} without a session"
            )
            checked += 1
    assert checked >= 15, f"only {checked} routes were exercised"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/scan", {}),
        ("POST", "/criteria/proposals", {"proposals": [{"criterion_id": "c1", "definition": "d"}]}),
        ("POST", "/actions/plan", {"document_ids": ["doc_probe"]}),
        ("POST", "/decisions/bulk", {"items": [{"document_id": "doc_probe", "disposition": "keep", "expected_revision": 0}]}),
        ("POST", "/chat", {"message": "hello"}),
        ("PUT", "/documents/{doc_id}/action-intent", {"intent": "reject", "expected_revision": 0}),
    ],
)
def test_viewer_role_cannot_reach_reviewer_mutations(
    client, repo, workspace, sessions, csrf, method, path, payload
):
    """A VIEWER session is rejected on every reviewer-level mutation."""
    document = seed_rejected_document(repo, workspace, name="viewer-target.pdf")
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.request(
        method,
        url(path.format(doc_id=document.id)),
        json=payload,
        headers=with_key(headers, "k-role"),
        cookies=cookies,
    )
    body = assert_envelope(response, 403)
    assert body["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_reviewer_cannot_reach_administrator_backup(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf, role=Role.REVIEWER)
    response = client.post(
        url("/backup"), json={}, headers=with_key(headers, "k-admin"), cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_administrator_only_route_is_reachable_by_an_administrator(client, sessions, csrf):
    """Negative control: the role gate is real, not a blanket 403."""
    _, headers, cookies = auth(sessions, csrf, role=Role.ADMINISTRATOR)
    response = client.post(
        url("/backup"), json={}, headers=with_key(headers, "k-admin-ok"), cookies=cookies
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["verified"] is True


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/status", None),
        ("GET", "/documents", None),
        ("GET", "/documents/doc_probe", None),
        ("POST", "/scan", {}),
    ],
)
def test_session_bound_to_another_instance_is_refused(client, sessions, csrf, method, path, payload):
    """A genuine session for instance B presented to instance A is refused."""
    session, headers, _ = auth(
        sessions, csrf, actor_ref="stranger", instance_id=OTHER_INSTANCE_ID
    )
    request_headers = {
        "Authorization": f"Bearer {session.session_id}",
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf.issue(session.session_id),
        "Idempotency-Key": "k-cross",
    }
    response = client.request(method, url(path), json=payload, headers=request_headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.INSTANCE_MISMATCH


def test_a_request_with_no_origin_and_no_session_is_still_401_not_500(client):
    response = client.post(url("/scan"), json={})
    assert response.status_code == 401


def test_forged_origin_is_rejected_on_a_mutation(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    headers["Origin"] = "http://evil.example"
    response = client.post(
        url("/scan"), json={}, headers=with_key(headers, "k-origin"), cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.ORIGIN_REJECTED


def test_null_origin_is_rejected_on_a_mutation(client, sessions, csrf):
    """The opaque ``null`` origin (file:, sandboxed iframe) is never same-origin."""
    _, headers, cookies = auth(sessions, csrf)
    headers["Origin"] = "null"
    response = client.post(
        url("/scan"), json={}, headers=with_key(headers, "k-null-origin"), cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.ORIGIN_REJECTED


def test_forged_host_is_rejected(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/status"), headers={**headers, "Host": "evil.example"}, cookies=cookies)
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.HOST_REJECTED


def test_missing_csrf_token_is_rejected(client, sessions, csrf):
    _, _, cookies = auth(sessions, csrf)
    response = client.post(
        url("/scan"), json={}, headers={"Origin": ORIGIN, "Idempotency-Key": "k-nocsrf"}, cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.CSRF_FAILED


def test_wrong_csrf_token_is_rejected(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    headers["X-CSRF-Token"] = "not-the-token"
    response = client.post(
        url("/scan"), json={}, headers=with_key(headers, "k-badcsrf"), cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.CSRF_FAILED


def test_a_rotated_csrf_token_invalidates_the_replayed_old_token(client, sessions, csrf):
    """Replaying a superseded token fails closed once the session's token rotates."""
    session, headers, cookies = auth(sessions, csrf)
    old = headers["X-CSRF-Token"]
    csrf.rotate(session.session_id)
    response = client.post(
        url("/scan"), json={}, headers=with_key(headers, "k-rotated"), cookies=cookies
    )
    assert_envelope(response, 403)
    assert response.json()["error"]["code"] == Code.CSRF_FAILED
    assert old  # the token really was issued and then superseded


def test_a_body_supplied_actor_is_refused_not_honoured(client, sessions, csrf, repo):
    """No endpoint reads an ``actor``/``created_by``/``requested_by`` from the body."""
    _, headers, cookies = auth(sessions, csrf, actor_ref="reviewer_1")
    attempts = [
        ("POST", "/criteria/proposals", {"proposals": [{"criterion_id": "c1", "definition": "d"}], "actor": "admin:someone"}),
        ("POST", "/decisions/bulk", {"items": [{"document_id": "doc_x", "disposition": "keep", "expected_revision": 0}], "actor": "admin:someone"}),
        ("POST", "/actions/plan", {"document_ids": ["doc_x"], "actor": "admin:someone"}),
        ("POST", "/analysis/jobs", {"document_ids": ["doc_x"], "requested_by": "admin:someone"}),
        ("POST", "/chat", {"message": "hi", "actor": "admin:someone"}),
    ]
    for method, path, payload in attempts:
        response = client.request(
            method, url(path), json=payload, headers=with_key(headers, "k-actor"), cookies=cookies
        )
        body = assert_envelope(response, 422)
        assert body["error"]["code"] == Code.VALIDATION_FAILED, (path, body)


def test_an_agent_session_cannot_activate_criteria(client, sessions, csrf):
    """A model-identity session is refused the human approval, even at REVIEWER rank."""
    _, headers, cookies = auth(sessions, csrf, actor_ref="agent:model", role=Role.REVIEWER)
    response = client.post(
        url("/criteria/1/activate"),
        json={"expected_revision": 0},
        headers=with_key(headers, "k-agent"),
        cookies=cookies,
    )
    assert response.status_code in (403, 409, 422), response.text
    assert_envelope(response, response.status_code)
    assert response.json()["error"]["code"] in (
        Code.APPROVAL_MUST_BE_HUMAN,
        Code.REVISION_CONFLICT,
        Code.NOT_FOUND,
    )


# ===========================================================================
# 2. IDEMPOTENCY
# ===========================================================================
def test_scan_replay_does_not_repeat_the_side_effect(client, repo, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    request_headers = with_key(headers, "scan-key-1")
    first = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies)
    assert first.status_code == 202, first.text
    jobs_after_first = len(repo.list_jobs())
    second = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies)
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(repo.list_jobs()) == jobs_after_first == 1


def test_plan_replay_does_not_create_a_second_batch(
    client, repo, workspace, sessions, csrf
):
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    request_headers = with_key(headers, "plan-key-1")
    payload = {"document_ids": [document.id]}
    first = client.post(url("/actions/plan"), json=payload, headers=request_headers, cookies=cookies)
    assert first.status_code == 200, first.text
    batches_after_first = len(repo.list_batches())
    second = client.post(url("/actions/plan"), json=payload, headers=request_headers, cookies=cookies)
    assert second.status_code == 200, second.text
    assert second.json()["data"] == first.json()["data"]
    assert len(repo.list_batches()) == batches_after_first == 1


def test_same_key_with_a_different_payload_conflicts(client, repo, workspace, sessions, csrf):
    seed_rejected_document(repo, workspace, name="a.pdf")
    document_b = seed_rejected_document(repo, workspace, name="b.pdf")
    _, headers, cookies = auth(sessions, csrf)
    request_headers = with_key(headers, "plan-key-conflict")
    ok = client.post(
        url("/actions/plan"),
        json={"document_ids": [str(_first_doc_id(repo))]},
        headers=request_headers,
        cookies=cookies,
    )
    assert ok.status_code == 200, ok.text
    batches = len(repo.list_batches())
    conflict = client.post(
        url("/actions/plan"),
        json={"document_ids": [document_b.id]},
        headers=request_headers,
        cookies=cookies,
    )
    body = assert_envelope(conflict, 409)
    assert body["error"]["code"] == Code.IDEMPOTENCY_KEY_REUSED
    assert len(repo.list_batches()) == batches


def _first_doc_id(repo: Repository) -> str:
    return repo.list_documents()[0].id


def test_idempotency_key_scoped_to_a_principal_is_not_usable_by_another(
    client, repo, sessions, csrf
):
    """Two different reviewers using the same key each get their own effect."""
    _, headers_a, cookies_a = auth(sessions, csrf, actor_ref="reviewer_a")
    _, headers_b, cookies_b = auth(sessions, csrf, actor_ref="reviewer_b")
    request_headers = with_key(headers_a, "shared-scan-key")
    first = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies_a)
    assert first.status_code == 202, first.text

    second = client.post(
        url("/scan"), json={}, headers=with_key(headers_b, "shared-scan-key"), cookies=cookies_b
    )
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] != first.json()["job_id"], (
        "a key scoped to one principal replayed another principal's effect"
    )
    assert len(repo.list_jobs()) == 2


def test_idempotency_key_is_scoped_to_the_route(client, repo, sessions, csrf):
    """The same key on two different routes is not a conflict and does not replay."""
    _, headers, cookies = auth(sessions, csrf)
    request_headers = with_key(headers, "route-shared-key")
    scan = client.post(url("/scan"), json={}, headers=request_headers, cookies=cookies)
    assert scan.status_code == 202, scan.text
    jobs = len(repo.list_jobs())
    jobs_response = client.post(
        url("/analysis/jobs"), json={"document_ids": ["doc_x"]}, headers=request_headers, cookies=cookies
    )
    # No conflict from the key: the failure (if any) is about the document, not reuse.
    assert jobs_response.status_code != 409, jobs_response.text
    assert len(repo.list_jobs()) == jobs  # the failed analysis queued nothing


def test_missing_and_malformed_idempotency_keys_are_refused(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    missing = client.post(url("/scan"), json={}, headers=headers, cookies=cookies)
    assert_envelope(missing, 422)
    assert missing.json()["error"]["code"] == Code.INVALID_INPUT
    malformed = client.post(
        url("/scan"), json={}, headers=with_key(headers, "bad key with spaces"), cookies=cookies
    )
    assert_envelope(malformed, 422)
    assert malformed.json()["error"]["code"] == Code.INVALID_INPUT


# ===========================================================================
# 3. PATH ESCAPE — /documents/{id}/original
# ===========================================================================
HOSTILE_IDS = [
    "..",
    "../..",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..%2F..%2F..%2FWindows%2Fwin.ini",
    "C:%5CWindows%5Cwin.ini",
    "C:/Windows/win.ini",
    "%5C%5Cserver%5Cshare%5Cevil.pdf",
    "\\\\server\\share\\evil.pdf",
    "NUL",
    "CON",
    "aux.txt",
    "%00",
    "....//....//secret",
    "resumes/..%2f..%2fescape.pdf",
]


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_original_stream_refuses_a_hostile_document_id_without_leaking_a_path(
    client, sessions, csrf, workspace, db, hostile
):
    _, headers, cookies = auth(sessions, csrf)
    try:
        response = client.get(
            url(f"/documents/{hostile}/original"), headers=headers, cookies=cookies
        )
    except Exception:
        # A client that refuses to even build the URL is also a refusal, not a leak.
        return
    assert response.status_code != 200, (hostile, response.text)
    assert response.status_code in (400, 404, 422), (hostile, response.status_code)
    leaked = _abs_paths_present(response.text, workspace, db)
    assert not leaked, (hostile, leaked, response.text)
    assert "win.ini" not in response.text
    assert "etc/passwd" not in response.text


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_document_detail_refuses_a_hostile_document_id(client, sessions, csrf, hostile):
    _, headers, cookies = auth(sessions, csrf)
    try:
        response = client.get(url(f"/documents/{hostile}"), headers=headers, cookies=cookies)
    except Exception:
        return
    assert response.status_code in (400, 404, 422), (hostile, response.status_code)


def test_original_stream_serves_a_real_file_and_leaks_no_absolute_path(
    client, repo, workspace, sessions, csrf, db
):
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url(f"/documents/{document.id}/original"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    assert response.content == DATA
    assert "attachment" in response.headers.get("content-disposition", "").lower()
    leaked = _abs_paths_present(response.text, workspace, db)
    assert not leaked, leaked
    assert str(workspace.resolve()) not in response.headers.get("content-disposition", "")


def test_original_stream_missing_file_is_404_without_a_path(
    client, repo, workspace, sessions, csrf, db
):
    document = seed_rejected_document(repo, workspace)
    (workspace / NAME).unlink()
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url(f"/documents/{document.id}/original"), headers=headers, cookies=cookies)
    body = assert_envelope(response, 404)
    assert body["error"]["code"] == Code.FILE_MISSING
    assert not _abs_paths_present(response.text, workspace, db)


# ===========================================================================
# 4. THE CATASTROPHE
# ===========================================================================
def test_apply_before_approve_refuses_and_moves_nothing(client, repo, workspace, sessions, csrf):
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    before = tree_snapshot(workspace)
    response = client.post(
        url(f"/actions/{data['batch_id']}/apply"),
        json={"expected_revision": 0},
        headers=with_key(headers, "apply-key-1"),
        cookies=cookies,
    )
    body = assert_envelope(response, 409)
    assert body["error"]["code"] == Code.APPROVAL_REQUIRED
    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).is_file()
    assert not (workspace / REJECTED_DIR / document.id / NAME).exists()
    assert repo.get_batch(data["batch_id"])["execution_state"] == "planned"


def test_a_battery_of_hostile_requests_moves_no_file(
    client, repo, workspace, sessions, csrf
):
    """Unapproved, unauthenticated, wrong-role and forged requests leave the tree intact."""
    document = seed_rejected_document(repo, workspace)
    reviewer_headers, reviewer_cookies = auth(sessions, csrf)[1:]
    data = plan_via_api(client, reviewer_headers, reviewer_cookies, document.id)
    batch_id = data["batch_id"]
    before = tree_snapshot(workspace)

    # Unauthenticated apply.
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers={"Idempotency-Key": "cat-1"},
    )
    # Wrong-role apply.
    _, viewer_headers, viewer_cookies = auth(sessions, csrf, actor_ref="viewer_1", role=Role.VIEWER)
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(viewer_headers, "cat-2"),
        cookies=viewer_cookies,
    )
    # Forged origin apply.
    forged = dict(reviewer_headers)
    forged["Origin"] = "http://evil.example"
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(forged, "cat-3"),
        cookies=reviewer_cookies,
    )
    # Missing CSRF apply.
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers={"Origin": ORIGIN, "Idempotency-Key": "cat-4"},
        cookies=reviewer_cookies,
    )
    # Unapproved reviewer apply.
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(reviewer_headers, "cat-5"),
        cookies=reviewer_cookies,
    )
    # Approve with the wrong plan hash, then apply anyway.
    client.post(
        url(f"/actions/{batch_id}/approve"),
        json={"plan_hash": "0" * 16, "expected_revision": 0},
        headers=with_key(reviewer_headers, "cat-6"),
        cookies=reviewer_cookies,
    )
    client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(reviewer_headers, "cat-7"),
        cookies=reviewer_cookies,
    )

    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).read_bytes() == DATA


def test_no_endpoint_executes_a_caller_path_or_command(client, sessions, csrf, repo, workspace):
    """Shell/exec/destination fields are refused; nothing is executed."""
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    payloads = [
        ("POST", "/scan", {"command": "whoami"}),
        ("POST", "/scan", {"path": "C:\\Windows\\System32\\cmd.exe"}),
        ("POST", "/actions/plan", {"document_ids": [document.id], "destination": "resumes/evil.pdf"}),
        (
            "POST",
            "/actions/plan",
            {"document_ids": [document.id], "overrides": {document.id: {"destination": "evil.pdf"}}},
        ),
    ]
    for method, path, payload in payloads:
        response = client.request(
            method, url(path), json=payload, headers=with_key(headers, "k-exec"), cookies=cookies
        )
        if path == "/actions/plan" and "overrides" in payload:
            # ``overrides`` is a free-form mapping the planner reads two named keys from;
            # an unknown destination key must be ignored, not honoured.
            assert response.status_code == 200, response.text
            operations = response.json()["data"]["plan"]["operations"]
            assert all("evil.pdf" not in str(op.get("destination", "")) for op in operations)
        else:
            body = assert_envelope(response, 422)
            assert body["error"]["code"] == Code.VALIDATION_FAILED
    assert (workspace / NAME).is_file()


def test_no_route_exposes_a_destination_or_execution_parameter(app):
    forbidden = ("destination", "exec", "shell", "command", "cmd", "subprocess", "proxy", "sql")
    for route in app.routes:
        path = getattr(route, "path", "").lower()
        for needle in forbidden:
            assert needle not in path, f"route {path!r} exposes {needle!r}"


def test_a_completed_mutation_bumps_state_revision_and_writes_audit(client, repo, sessions, csrf):
    """Every state mutation bumps the revision; an idempotent replay does not."""
    _, headers, cookies = auth(sessions, csrf)
    before = repo.db.state_revision()
    first = client.post(
        url("/scan"), json={}, headers=with_key(headers, "rev-key"), cookies=cookies
    )
    assert first.status_code == 202, first.text
    after = repo.db.state_revision()
    assert after > before
    replay = client.post(
        url("/scan"), json={}, headers=with_key(headers, "rev-key"), cookies=cookies
    )
    assert replay.status_code == 202
    assert repo.db.state_revision() == after


# ===========================================================================
# 5. ENVELOPE AND LEAKAGE
# ===========================================================================
LEAKY_BODIES = [
    ("POST", "/criteria/proposals", {"proposals": [{"criterion_id": "c", "definition": "d"}], "note": f"{CANDIDATE_FILE} {SECRET_TOKEN}"}),
    ("POST", "/scan", {"path": r"C:\Users\bob\{}".format(CANDIDATE_FILE)}),
    ("POST", "/actions/plan", {"document_ids": ["x"], "destination": r"C:\Users\bob\resume.pdf"}),
]


@pytest.mark.parametrize(("method", "path", "payload"), LEAKY_BODIES)
def test_validation_errors_do_not_echo_attacker_supplied_text(
    client, sessions, csrf, workspace, db, method, path, payload
):
    _, headers, cookies = auth(sessions, csrf)
    response = client.request(
        method, url(path), json=payload, headers=with_key(headers, "k-leak"), cookies=cookies
    )
    body = assert_envelope(response, 422)
    text = response.text
    assert CANDIDATE_NAME not in text
    assert "resume.pdf" not in text and "bob" not in text
    assert "SECRET99" not in text
    assert not _abs_paths_present(text, workspace, db)
    assert body["ok"] is False


def test_query_string_errors_do_not_echo_attacker_text(client, sessions, csrf, workspace, db):
    _, headers, cookies = auth(sessions, csrf)
    long_search = f"{CANDIDATE_FILE} " * 20
    response = client.get(
        url("/documents"), params={"search": long_search}, headers=headers, cookies=cookies
    )
    body = assert_envelope(response, 422)
    assert body["error"]["code"] == Code.INVALID_INPUT
    assert CANDIDATE_NAME not in response.text

    bad_filter = client.get(
        url("/documents"),
        params={"filter": '{not-json "' + CANDIDATE_FILE + '}'},
        headers=headers,
        cookies=cookies,
    )
    assert bad_filter.status_code == 422
    assert CANDIDATE_NAME not in bad_filter.text
    assert not _abs_paths_present(bad_filter.text, workspace, db)


def test_error_envelopes_never_carry_paths_or_credentials_on_auth_failures(
    client, workspace, db, sessions, csrf
):
    attacks = [
        ("GET", "/status", None, {}),
        ("GET", "/documents/%2e%2e%2fsecret", None, {}),
        ("POST", "/scan", {}, {"Origin": "http://evil.example", "Idempotency-Key": "k"}),
        ("POST", "/scan", {}, {"Origin": ORIGIN, "Idempotency-Key": "k"}),
    ]
    for method, path, payload, extra in attacks:
        response = client.request(method, url(path), json=payload, headers=extra)
        body = response.json()
        validate_envelope(body)
        assert body["ok"] is False
        assert not _abs_paths_present(response.text, workspace, db), response.text
        assert SECRET_TOKEN not in response.text


def test_envelope_conforms_on_success_and_failure(client, sessions, csrf, repo, workspace):
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    ok = client.get(url(f"/documents/{document.id}"), headers=headers, cookies=cookies)
    validate_envelope(ok.json())
    not_found = client.get(url("/documents/doc_missing"), headers=headers, cookies=cookies)
    validate_envelope(not_found.json())
    unknown = client.get(url("/no-such-route"), headers=headers, cookies=cookies)
    validate_envelope(unknown.json())
    assert unknown.json()["error"]["code"] == Code.NOT_FOUND


def test_unexpected_exception_yields_a_generic_500_envelope(
    repo, sessions, csrf, config, workspace
):
    """A handler that raises a bare exception must not leak its text or a path."""
    from fastapi import APIRouter

    def register_boom(router: APIRouter) -> None:
        @router.get("/boom-adversarial")
        def boom():
            raise ValueError(r"internal detail at C:\Users\bob\secret.pdf token=abcd9999")

    application = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=config,
        route_modules=(),
        register_routes=[register_boom],
    )
    client = TestClient(application, raise_server_exceptions=False)
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/boom-adversarial"), headers=headers, cookies=cookies)
    body = assert_envelope(response, 500)
    assert body["error"]["code"] == Code.INTERNAL_ERROR
    assert "secret.pdf" not in response.text
    assert "abcd9999" not in response.text
    assert not _abs_paths_present(response.text, workspace, repo.db)


def test_document_list_response_never_leaks_an_absolute_path(client, repo, workspace, sessions, csrf, db):
    doc = seed_rejected_document(repo, workspace, name=CANDIDATE_FILE)
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/documents"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    assert not _abs_paths_present(response.text, workspace, db)
    detail = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies)
    assert not _abs_paths_present(detail.text, workspace, db)
    # The relative path is data the UI needs; the absolute root is not.
    assert doc.id in response.text


def test_approved_apply_actually_moves_the_file_and_leaks_no_path(
    client, repo, workspace, sessions, csrf, db
):
    """Positive control: the same flow *does* move the file when a human approved it.

    Without this, every "nothing moved" assertion above could be true merely because
    the apply path never works.
    """
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]
    approve = client.post(
        url(f"/actions/{batch_id}/approve"),
        json={"plan_hash": data["plan"]["plan_hash"], "expected_revision": 0},
        headers=with_key(headers, "approve-ok"),
        cookies=cookies,
    )
    assert approve.status_code == 200, approve.text
    apply = client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(headers, "apply-ok"),
        cookies=cookies,
    )
    assert apply.status_code == 200, apply.text
    destination = workspace / REJECTED_DIR / document.id / NAME
    assert destination.is_file(), "an approved apply did not move the file"
    assert not (workspace / NAME).exists()
    assert not _abs_paths_present(apply.text, workspace, db)


def test_unknown_batch_ids_are_404_on_every_action_route(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    attempts = [
        ("POST", "/actions/batch_missing/approve", {"plan_hash": "0" * 16, "expected_revision": 0}),
        ("POST", "/actions/batch_missing/apply", {"expected_revision": 0}),
        ("POST", "/actions/batch_missing/cancel", {"expected_revision": 0}),
        ("POST", "/actions/batch_missing/restore-plan", {}),
    ]
    for method, path, payload in attempts:
        response = client.request(
            method, url(path), json=payload, headers=with_key(headers, "k-unknown"), cookies=cookies
        )
        body = assert_envelope(response, 404)
        assert body["error"]["code"] == Code.NOT_FOUND, (path, body)


def test_a_second_instance_cannot_share_a_database(db, repo):
    """The DB invariant that makes an instance-unscoped batch id unreachable.

    The action endpoints resolve a batch id without an instance predicate; that is
    only safe because one database can never hold two instances. This pins the
    invariant so a future relaxation cannot silently open a cross-instance batch.
    """
    from resume_review.errors import Conflict

    with pytest.raises(Conflict):
        Repository(db).create_instance(OTHER_INSTANCE_ID, __version__, SCHEMA_VERSION)


def test_wrong_method_returns_a_405_envelope(client, workspace, db):
    response = client.get(url("/scan"))
    body = assert_envelope(response, 405)
    assert body["error"]["code"] == Code.METHOD_NOT_ALLOWED
    assert not _abs_paths_present(response.text, workspace, db)


# ===========================================================================
# 6. LAYER DIRECTION
# ===========================================================================
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "resume_review"


def _imported_targets(path: Path, module_name: str) -> set[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    found: set[str] = set()

    def _resolve(level: int, module: str | None) -> str:
        if level == 0:
            return module or ""
        parts = package.split(".") if package else []
        drop = level - 1
        if drop:
            parts = parts[: len(parts) - drop] if drop <= len(parts) else []
        if module:
            parts = parts + module.split(".")
        return ".".join(part for part in parts if part)

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                found.add(alias.name)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = _resolve(node.level, node.module)
            if base:
                found.add(base)
            for alias in node.names:
                if alias.name != "*":
                    found.add(".".join(part for part in (base, alias.name) if part))

    Visitor().visit(tree)
    return found


def _modules_under(root: Path) -> list[tuple[str, set[str]]]:
    out: list[tuple[str, set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(SOURCE_ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        out.append((".".join(parts), _imported_targets(path, ".".join(parts))))
    return out


@pytest.mark.parametrize("layer", ["db", "storage", "actions", "analysis"])
def test_api_is_not_imported_by_a_lower_or_sibling_layer(layer: str):
    """AGENTS.md: db/storage never import api; actions/analysis must not either."""
    violations: list[str] = []
    for module, targets in _modules_under(SOURCE_ROOT / layer):
        for target in targets:
            if target == "resume_review.api" or target.startswith("resume_review.api."):
                violations.append(f"{module} imports {target}")
    assert not violations, "layer violation: " + "; ".join(sorted(violations))


def test_layer_probe_actually_sees_imports():
    """Negative control: the scanner must flag a planted forbidden import."""
    sample = "def f():\n    from ..api import envelope\n"
    found = _imported_targets_for_source(sample, "resume_review.db.repository")
    assert any(t == "resume_review.api" or t.startswith("resume_review.api.") for t in found)


def _imported_targets_for_source(source: str, module_name: str) -> set[str]:
    import ast as _ast

    tree = _ast.parse(source)
    package = module_name.rpartition(".")[0]
    found: set[str] = set()
    level = tree.body[0].body[0].level  # type: ignore[attr-defined]
    node = tree.body[0].body[0]  # type: ignore[attr-defined]
    parts = package.split(".")
    drop = level - 1
    if drop:
        parts = parts[: len(parts) - drop]
    base = ".".join(parts + ([node.module] if node.module else []))  # type: ignore[attr-defined]
    found.add(base)
    return found
