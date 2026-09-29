"""Document and review-state endpoint tests (PRD 12.1).

Authority: PRD section 8.2 (table sort/filter/paginate), section 8.3 (bulk set),
section 10 (five independent state dimensions), section 11.2 (stable pagination,
separate revisions), section 12.2 (mutation rules), and acceptance tests AT-15
(sort/filter/paginate with correct totals and stable order), AT-17 (bulk affects an
immutable explicit set), and AT-18 (a stale write is refused with a visible
conflict, never overwritten).

Everything here runs through ``fastapi.testclient.TestClient`` against a real
migrated database, real session and CSRF stores, and a real workspace on disk.
Synthetic data only. No symlink or junction is created, so the symlink branch of
the path guard is exercised only through its unit contract, not here.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, create_app
from resume_review.api.documents import resolve_original_path
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import NotFound
from resume_review.models import Role
from resume_review.storage import PathEscape

INSTANCE_ID = "inst_test"
ORIGIN = "http://testserver"

ROUTE_MODULES = ("resume_review.api.documents",)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A real workspace laid out as ``<root>/.review/review.db`` (PRD section 4)."""
    root = tmp_path / "job"
    (root / ".review").mkdir(parents=True)
    (root / "resumes").mkdir(parents=True)
    return root


@pytest.fixture
def db(workspace: Path):
    database = Database(DbConfig(path=workspace / ".review" / "review.db"))
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
    return create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=config,
        route_modules=ROUTE_MODULES,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def auth(
    sessions: SessionStore,
    csrf: CsrfStore,
    role: Role = Role.REVIEWER,
):
    session = sessions.issue(INSTANCE_ID, "reviewer_1", role)
    headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return session, headers, cookies


def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def make_doc(repo: Repository, filename: str, rel_path: str | None = None, media: str = "pdf"):
    return repo.create_document(
        original_filename=filename,
        rel_path=rel_path or f"resumes/{filename}",
        media_type=media,
        size_bytes=12,
        content_sha256=f"sha_{filename}",
        fs_identity=f"fs_{filename}",
    )


# ---------------------------------------------------------------------------
# Status and detail
# ---------------------------------------------------------------------------
def test_status_reports_versions_counts_and_snapshot(client, sessions, csrf, repo):
    make_doc(repo, "alpha.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/status"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["instance_id"] == INSTANCE_ID
    assert data["health"]["ok"] is True
    assert data["versions"]["schema_version"] == SCHEMA_VERSION
    assert data["versions"]["report_schema_version"] == "1.0"
    assert data["counts"]["total"] == 1
    assert data["counts"]["unreviewed"] == 1
    assert data["queue"]["total"] == 0
    assert data["snapshot"]["exists"] is False
    # No filesystem path leaks through the status surface.
    assert str(repo.db.path) not in response.text


def test_document_detail_keeps_state_dimensions_separate(client, sessions, csrf, repo):
    doc = make_doc(repo, "beta.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["document_id"] == doc.id
    # Five independent dimensions, each present and not collapsed into one status.
    assert data["processing_state"] == "discovered"
    assert data["review_state"] == "unreviewed"
    assert data["location"] == "active"
    assert data["pending_intent"] == "none"
    assert data["file_actions"] == []
    assert data["notes"] == []
    assert data["tasks"] == []


def test_document_detail_unknown_id_is_404(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(url("/documents/doc_missing"), headers=headers, cookies=cookies)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Pagination / sort / totals (AT-15)
# ---------------------------------------------------------------------------
def test_pagination_totals_and_stable_order_across_pages(client, sessions, csrf, repo):
    docs = [make_doc(repo, f"cand_{i:02d}.pdf") for i in range(7)]
    _, headers, cookies = auth(sessions, csrf)

    seen: list[str] = []
    for page in (1, 2, 3):
        response = client.get(
            url("/documents"),
            params={"page": page, "page_size": 3, "sort": "name", "direction": "asc"},
            headers=headers,
            cookies=cookies,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 7
        assert data["page_count"] == 3
        assert data["page_size"] == 3
        assert data["has_more"] is (page * 3 < 7)
        seen.extend(item["document_id"] for item in data["documents"])

    # No duplicates, nothing dropped, exactly the set that was created.
    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert set(seen) == {d.id for d in docs}

    # Every display_name is NULL, so the deterministic document_id tie-breaker
    # alone decides the order (PRD 11.2). Two reads must agree.
    again: list[str] = []
    for page in (1, 2, 3):
        response = client.get(
            url("/documents"),
            params={"page": page, "page_size": 3, "sort": "name", "direction": "asc"},
            headers=headers,
            cookies=cookies,
        )
        again.extend(item["document_id"] for item in response.json()["data"]["documents"])
    assert again == sorted(seen)
    assert again == seen


def test_pagination_is_independent_of_page_size(client, sessions, csrf, repo):
    docs = [make_doc(repo, f"x_{i}.pdf") for i in range(5)]
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"),
        params={"page": 1, "page_size": 100, "sort": "name", "direction": "asc"},
        headers=headers,
        cookies=cookies,
    )
    data = response.json()["data"]
    assert data["total"] == 5
    assert data["page_count"] == 1
    assert data["has_more"] is False
    assert [d["document_id"] for d in data["documents"]] == sorted(d.id for d in docs)


def test_page_size_over_max_is_refused(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"), params={"page_size": 10_000}, headers=headers, cookies=cookies
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_sort_key_is_refused(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"), params={"sort": "current_rel_path; DROP TABLE"}, headers=headers, cookies=cookies
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_filter_value_is_refused(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"), params={"review_state": "definitely_not_a_state"}, headers=headers, cookies=cookies
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILTER_UNKNOWN_VALUE"


def test_filter_json_rejects_unknown_field(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"),
        params={"filter": json.dumps({"criterion:foo": "supported"})},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILTER_FIELD_NOT_ALLOWED"


def test_explicit_document_ids_filter_is_exact(client, sessions, csrf, repo):
    a = make_doc(repo, "keep_a.pdf")
    b = make_doc(repo, "keep_b.pdf")
    make_doc(repo, "other_c.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(
        url("/documents"),
        params={"document_ids": [a.id, b.id], "sort": "name"},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    assert {d["document_id"] for d in data["documents"]} == {a.id, b.id}


def test_review_state_filter_reflects_decisions(client, sessions, csrf, repo):
    doc = make_doc(repo, "decide.pdf")
    _, headers, cookies = auth(sessions, csrf)
    patch = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=headers,
        cookies=cookies,
    )
    assert patch.status_code == 200, patch.text
    listed = client.get(
        url("/documents"), params={"review_state": "keep"}, headers=headers, cookies=cookies
    )
    data = listed.json()["data"]
    assert data["total"] == 1
    assert data["documents"][0]["document_id"] == doc.id
    empty = client.get(
        url("/documents"), params={"review_state": "reject"}, headers=headers, cookies=cookies
    )
    assert empty.json()["data"]["total"] == 0


# ---------------------------------------------------------------------------
# Decision conflict (AT-18)
# ---------------------------------------------------------------------------
def test_stale_decision_write_is_refused_and_not_overwritten(client, sessions, csrf, repo):
    doc = make_doc(repo, "conflict.pdf")
    _, headers, cookies = auth(sessions, csrf)

    first = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=headers,
        cookies=cookies,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["decision_revision"] == 1
    assert first.json()["data"]["review_state"] == "keep"

    stale = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0},
        headers=headers,
        cookies=cookies,
    )
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    assert error["detail"]["current_revision"] == 1
    assert error["detail"]["current_value"] == "keep"

    # The refused write changed nothing.
    detail = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies).json()["data"]
    assert detail["review_state"] == "keep"
    assert detail["decision_revision"] == 1


def test_decision_rejects_missing_disposition(client, sessions, csrf, repo):
    doc = make_doc(repo, "nodisp.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"expected_revision": 0},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Bulk decisions: immutable explicit set (AT-17)
# ---------------------------------------------------------------------------
def test_bulk_decisions_affect_only_the_supplied_set(client, sessions, csrf, repo):
    a = make_doc(repo, "bulk_a.pdf")
    b = make_doc(repo, "bulk_b.pdf")
    c = make_doc(repo, "bulk_c.pdf")
    _, headers, cookies = auth(sessions, csrf)
    headers = {**headers, "Idempotency-Key": "bulk-1"}

    response = client.post(
        url("/decisions/bulk"),
        json={
            "items": [
                {"document_id": a.id, "disposition": "keep", "expected_revision": 0},
                {"document_id": b.id, "disposition": "reject", "expected_revision": 0},
            ]
        },
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["updated"] == 2
    assert set(data["document_ids"]) == {a.id, b.id}
    assert data["conflicts"] == []

    # A file that arrives after the request must not be swept into the set.
    d = make_doc(repo, "bulk_d.pdf")

    states = {}
    for doc_id in (a.id, b.id, c.id, d.id):
        body = client.get(url(f"/documents/{doc_id}"), headers=headers, cookies=cookies).json()
        states[doc_id] = (body["data"]["review_state"], body["data"]["decision_revision"])

    assert states[a.id] == ("keep", 1)
    assert states[b.id] == ("reject", 1)
    assert states[c.id] == ("unreviewed", 0)
    assert states[d.id] == ("unreviewed", 0)


def test_bulk_decisions_are_atomic_on_one_stale_revision(client, sessions, csrf, repo):
    a = make_doc(repo, "atomic_a.pdf")
    b = make_doc(repo, "atomic_b.pdf")
    _, headers, cookies = auth(sessions, csrf)
    idem = {**headers, "Idempotency-Key": "bulk-atomic-1"}

    primed = client.post(
        url("/decisions/bulk"),
        json={"items": [{"document_id": a.id, "disposition": "keep", "expected_revision": 0}]},
        headers=idem,
        cookies=cookies,
    )
    assert primed.status_code == 200, primed.text

    # a is now at revision 1, but the request says 0: nothing may be applied.
    response = client.post(
        url("/decisions/bulk"),
        json={
            "items": [
                {"document_id": b.id, "disposition": "keep", "expected_revision": 0},
                {"document_id": a.id, "disposition": "hold", "expected_revision": 0},
            ]
        },
        headers={**headers, "Idempotency-Key": "bulk-atomic-2"},
        cookies=cookies,
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    conflicts = error["detail"]["conflicts"]
    assert any(item["document_id"] == a.id and item["current_revision"] == 1 for item in conflicts)

    b_state = client.get(url(f"/documents/{b.id}"), headers=headers, cookies=cookies).json()["data"]
    assert b_state["review_state"] == "unreviewed"
    a_state = client.get(url(f"/documents/{a.id}"), headers=headers, cookies=cookies).json()["data"]
    assert a_state["review_state"] == "keep"


def test_bulk_decisions_requires_idempotency_key(client, sessions, csrf, repo):
    doc = make_doc(repo, "no_key.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/decisions/bulk"),
        json={"items": [{"document_id": doc.id, "disposition": "keep", "expected_revision": 0}]},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_bulk_decisions_replay_is_idempotent(client, sessions, csrf, repo):
    doc = make_doc(repo, "replay.pdf")
    _, headers, cookies = auth(sessions, csrf)
    idem = {**headers, "Idempotency-Key": "bulk-replay-1"}
    payload = {"items": [{"document_id": doc.id, "disposition": "keep", "expected_revision": 0}]}

    first = client.post(url("/decisions/bulk"), json=payload, headers=idem, cookies=cookies)
    assert first.status_code == 200, first.text
    second = client.post(url("/decisions/bulk"), json=payload, headers=idem, cookies=cookies)
    assert second.status_code == 200, second.text
    assert second.json()["data"] == first.json()["data"]
    # The decision revision advanced exactly once.
    state = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies).json()["data"]
    assert state["decision_revision"] == 1


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
def test_note_create_and_revisioned_update(client, sessions, csrf, repo):
    doc = make_doc(repo, "notes.pdf")
    _, headers, cookies = auth(sessions, csrf)
    created = client.post(
        url(f"/documents/{doc.id}/notes"),
        json={"body": "First look."},
        headers={**headers, "Idempotency-Key": "note-1"},
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    note = created.json()["data"]["note"]
    assert note["note_revision"] == 1
    assert note["author"] == "reviewer_1"

    edited = client.patch(
        url(f"/notes/{note['id']}"),
        json={"document_id": doc.id, "body": "Second look.", "expected_revision": 1},
        headers=headers,
        cookies=cookies,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["note"]["note_revision"] == 2

    stale = client.patch(
        url(f"/notes/{note['id']}"),
        json={"document_id": doc.id, "body": "Third.", "expected_revision": 1},
        headers=headers,
        cookies=cookies,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

    detail = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies).json()["data"]
    assert detail["notes"][0]["body"] == "Second look."


def test_note_foreign_document_scope_is_404(client, sessions, csrf, repo):
    doc = make_doc(repo, "notes_scope.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.patch(
        url("/notes/note_missing"),
        json={"document_id": doc.id, "body": "x", "expected_revision": 1},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def test_task_create_and_close(client, sessions, csrf, repo):
    doc = make_doc(repo, "tasks.pdf")
    _, headers, cookies = auth(sessions, csrf)
    created = client.post(
        url("/tasks"),
        json={"document_id": doc.id, "title": "Confirm the dates.", "severity": "attention"},
        headers={**headers, "Idempotency-Key": "task-1"},
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    task = created.json()["data"]["task"]
    assert created.json()["data"]["created"] is True
    assert task["origin"] == "human"
    assert task["state"] == "open"

    detail = client.get(url(f"/documents/{doc.id}"), headers=headers, cookies=cookies).json()["data"]
    assert detail["open_task_count"] == 1
    assert detail["task_warning"] is True

    closed = client.patch(
        url(f"/tasks/{task['id']}"),
        json={"resolution": "checked"},
        headers=headers,
        cookies=cookies,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["data"]["task"]["state"] == "closed"
    assert closed.json()["data"]["task"]["closed_by"] == "reviewer_1"


def test_task_create_requires_idempotency_key(client, sessions, csrf, repo):
    doc = make_doc(repo, "tasks_nokey.pdf")
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/tasks"),
        json={"document_id": doc.id, "title": "No key."},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_task_close_unknown_id_is_404(client, sessions, csrf):
    _, headers, cookies = auth(sessions, csrf)
    response = client.patch(
        url("/tasks/task_missing"),
        json={"resolution": "nope"},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The scoped original stream: success and every escape attempt fails closed
# ---------------------------------------------------------------------------
def _original_url(document_id: str) -> str:
    return url(f"/documents/{document_id}/original")


def test_original_stream_serves_the_document_without_leaking_a_path(
    client, sessions, csrf, repo, workspace
):
    payload = b"%PDF-1.4 synthetic resume bytes"
    (workspace / "resumes" / "real.pdf").write_bytes(payload)
    doc = make_doc(repo, "real.pdf")
    _, headers, cookies = auth(sessions, csrf)

    response = client.get(_original_url(doc.id), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert response.headers["content-type"].startswith("application/pdf")
    assert "attachment" in response.headers["content-disposition"]
    # The absolute workspace path must never appear in the response bytes or headers.
    assert str(workspace).encode() not in response.content
    assert str(workspace) not in response.text
    assert str(workspace) not in str(dict(response.headers))


@pytest.mark.parametrize(
    "hostile_rel_path",
    [
        "../outside.pdf",
        "resumes/../../outside.pdf",
        "C:\\Windows\\win.ini",
        "\\\\srv\\share\\resume.pdf",
        "/etc/passwd",
        "resumes/..\\..\\outside.pdf",
    ],
)
def test_original_stream_refuses_every_hostile_stored_path(
    client, sessions, csrf, repo, monkeypatch, hostile_rel_path
):
    doc = make_doc(repo, "hostile.pdf")
    _, headers, cookies = auth(sessions, csrf)

    def fake_get_document(_document_id: str):
        return dataclasses.replace(doc, current_rel_path=hostile_rel_path)

    monkeypatch.setattr(repo, "get_document", fake_get_document)

    response = client.get(_original_url(doc.id), headers=headers, cookies=cookies)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PATH_ESCAPE"
    # A refusal never echoes the hostile path back.
    assert "outside" not in response.text
    assert "win.ini" not in response.text
    assert "passwd" not in response.text


def test_original_stream_missing_file_is_404_not_a_path_leak(
    client, sessions, csrf, repo, workspace, monkeypatch
):
    doc = make_doc(repo, "ghost.pdf")
    _, headers, cookies = auth(sessions, csrf)

    def fake_get_document(_document_id: str):
        return dataclasses.replace(doc, current_rel_path="resumes/ghost.pdf")

    monkeypatch.setattr(repo, "get_document", fake_get_document)
    response = client.get(_original_url(doc.id), headers=headers, cookies=cookies)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "FILE_MISSING"
    assert str(workspace) not in response.text


@pytest.mark.parametrize(
    "hostile_id",
    ["..", "../..", "../../etc/passwd", "%2e%2e%2f%2e%2e%2fsecret.pdf"],
)
def test_original_stream_refuses_hostile_document_ids(client, sessions, csrf, hostile_id):
    _, headers, cookies = auth(sessions, csrf)
    response = client.get(_original_url(hostile_id), headers=headers, cookies=cookies)
    # A hostile id never selects a file: it is not a resolvable document, so the
    # only acceptable outcomes are 404 (no such document) or 422 (refused path).
    assert response.status_code in (404, 422), response.text
    assert response.status_code != 200


def test_resolve_original_path_contract_rejects_each_escape(workspace):
    # The pure helper is the same guard the endpoint uses; assert its contract.
    for hostile in ("../x.pdf", "a/../../x.pdf", "C:\\Windows\\x.pdf", "\\\\srv\\share\\x.pdf", "/etc/passwd"):
        with pytest.raises(PathEscape):
            resolve_original_path(workspace, hostile)
    with pytest.raises(NotFound):
        resolve_original_path(workspace, "resumes/absent.pdf")


def test_original_stream_requires_viewer_role(client, sessions, csrf, repo, workspace):
    (workspace / "resumes" / "role.pdf").write_bytes(b"%PDF role")
    doc = make_doc(repo, "role.pdf")
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.get(_original_url(doc.id), headers=headers, cookies=cookies)
    assert response.status_code == 200, response.text


def test_decision_requires_reviewer_role(client, sessions, csrf, repo):
    doc = make_doc(repo, "viewer_only.pdf")
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_INSUFFICIENT"


def test_mutation_requires_csrf_token(client, sessions, csrf, repo):
    doc = make_doc(repo, "csrf.pdf")
    _, headers, cookies = auth(sessions, csrf)
    no_csrf = {"Origin": ORIGIN}
    response = client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=no_csrf,
        cookies=cookies,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
