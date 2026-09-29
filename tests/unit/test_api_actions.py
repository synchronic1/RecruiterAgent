"""API tests for the file-action endpoints (PRD 12.1, 12.2, 12.3, 13).

Authority: AGENTS.md constraints 3, 4, 5 and PRD section 13.

These tests drive the real HTTP surface against a real migrated database, a real
workspace on disk, and the real planner/executor through the endpoints. They assert
the properties the PRD calls out as easy to get lazily wrong at the API layer:

* a Reject decision with no approval moves nothing (apply-before-approve refuses);
* an approval is bound to the plan hash the human saw, so a plan mutated after
  approval is refused and nothing moves;
* an authorization that went stale (the decision changed after approval) is
  rejected, not repaired, and nothing moves;
* a model or worker identity can never create an approval;
* restore mints a new inverse plan rather than moving a file, and the original
  batch is left alone;
* no endpoint accepts a caller-supplied destination path.

Synthetic data only. The workspace root is registered in a temporary host registry
(the same authoritative mapping the CLI uses) so the endpoint module resolves it
without a caller-supplied value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import API_PREFIX, ApiConfig, create_app
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.bootstrap import HostRegistry
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code
from resume_review.models import REJECTED_DIR, REVIEW_DIR, Location, MediaType, Role
from resume_review.util import sha256_file

INSTANCE_ID = "inst_actions"
ORIGIN = "http://testserver"
NAME = "candidate-001.pdf"
DATA = b"%PDF-1.4\nsynthetic file-action test bytes\n"
ROUTE_MODULE = "resume_review.api.actions"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the host registry at a private temp directory for the whole test."""
    directory = tmp_path / "registry"
    directory.mkdir()
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(directory))
    return directory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    area = tmp_path / "Job - Operations Manager"
    area.mkdir(parents=True)
    return area


@pytest.fixture
def db(workspace: Path) -> Database:
    """A real migrated database, laid out under the documented ``.review`` folder."""
    review = workspace / REVIEW_DIR
    review.mkdir()
    database = Database(DbConfig(path=review / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database, workspace: Path) -> Repository:
    repository = Repository(db)
    repository.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    # Register the canonical root the way setup does; the endpoint resolves the
    # workspace from this trusted mapping, never from a request.
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
    return create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=config,
        route_modules=[ROUTE_MODULE],
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
):
    """Issue a session and its CSRF token; return (session, headers, cookies)."""
    session = sessions.issue(INSTANCE_ID, actor_ref, role)
    headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return session, headers, cookies


def with_key(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Regular files under the workspace, excluding the durable ``.review`` journal."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == REVIEW_DIR:
            continue
        if path.is_file():
            out[rel.as_posix()] = path.read_bytes()
    return out


def seed_rejected_document(repo: Repository, workspace: Path):
    """Place a file, register it, and mark it Reject through the repository."""
    path = workspace / NAME
    path.write_bytes(DATA)
    document = repo.create_document(
        original_filename=NAME,
        rel_path=NAME,
        media_type=MediaType.PDF,
        size_bytes=len(DATA),
        content_sha256=sha256_file(path),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor="reviewer_1")
    return document


def plan_via_api(client, headers, cookies, document_id: str, *, key: str = "plan-1") -> dict:
    response = client.post(
        url("/actions/plan"),
        json={"document_ids": [document_id]},
        headers=with_key(headers, key),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def approve_via_api(
    client, headers, cookies, batch_id: str, plan_hash: str, *, key: str = "approve-1"
):
    return client.post(
        url(f"/actions/{batch_id}/approve"),
        json={"plan_hash": plan_hash, "expected_revision": 0},
        headers=with_key(headers, key),
        cookies=cookies,
    )


def apply_via_api(
    client, headers, cookies, batch_id: str, *, revision: int = 0, key: str = "apply-1"
):
    return client.post(
        url(f"/actions/{batch_id}/apply"),
        json={"expected_revision": revision},
        headers=with_key(headers, key),
        cookies=cookies,
    )


def reject_destination(document_id: str) -> str:
    return f"{REJECTED_DIR}/{document_id}/{NAME}"


# ---------------------------------------------------------------------------
# The module is on the default discovery path
# ---------------------------------------------------------------------------
def test_module_registers_on_the_default_discovery_path(
    repo: Repository, sessions: SessionStore, csrf: CsrfStore, config: ApiConfig
) -> None:
    application = create_app(repo, sessions=sessions, csrf_store=csrf, config=config)
    paths = set(application.openapi()["paths"])
    assert API_PREFIX + "/actions/plan" in paths
    assert API_PREFIX + "/actions/{batch_id}/approve" in paths
    assert API_PREFIX + "/actions/{batch_id}/apply" in paths
    assert API_PREFIX + "/actions/{batch_id}/cancel" in paths
    assert API_PREFIX + "/actions/{batch_id}/restore-plan" in paths
    assert API_PREFIX + "/documents/{document_id}/action-intent" in paths


# ---------------------------------------------------------------------------
# Apply before approve is refused
# ---------------------------------------------------------------------------
def test_apply_before_approve_is_refused_and_moves_nothing(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]
    before = tree_snapshot(workspace)

    response = apply_via_api(client, headers, cookies, batch_id)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == Code.APPROVAL_REQUIRED
    # A Reject decision is not approval; nothing moved.
    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).is_file()
    assert not (workspace / reject_destination(document.id)).exists()
    assert repo.get_document(document.id).location == Location.ACTIVE
    assert repo.get_batch(batch_id)["execution_state"] == "planned"


# ---------------------------------------------------------------------------
# Approval is bound to the plan hash
# ---------------------------------------------------------------------------
def test_approval_requires_the_exact_plan_hash(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]
    before = tree_snapshot(workspace)

    wrong = approve_via_api(client, headers, cookies, batch_id, "0" * 40, key="approve-wrong")
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["error"]["code"] == Code.PLAN_HASH_MISMATCH
    assert repo.get_batch(batch_id)["execution_state"] == "planned"
    assert tree_snapshot(workspace) == before

    right = approve_via_api(client, headers, cookies, batch_id, data["plan"]["plan_hash"])
    assert right.status_code == 200, right.text
    assert right.json()["data"]["execution_state"] == "approved"
    assert repo.get_batch(batch_id)["execution_state"] == "approved"


def test_a_plan_mutated_after_approval_is_refused_and_moves_nothing(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]
    assert approve_via_api(
        client, headers, cookies, batch_id, data["plan"]["plan_hash"]
    ).status_code == 200
    # Tamper with the stored plan after approval: the plan being executed is no
    # longer the plan the approval was bound to.
    with repo.db.write(actor="attacker", event="test.tamper") as conn:
        conn.execute(
            "UPDATE action_batches SET plan_json = replace(plan_json, ?, ?) WHERE id = ?",
            (NAME, "candidate-999.pdf", batch_id),
        )
    before = tree_snapshot(workspace)

    response = apply_via_api(client, headers, cookies, batch_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == Code.APPROVAL_REQUIRED
    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).is_file()
    assert not (workspace / reject_destination(document.id)).exists()


# ---------------------------------------------------------------------------
# Stale authorization is rejected, not repaired
# ---------------------------------------------------------------------------
def test_stale_authorization_is_rejected_and_moves_nothing(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]
    assert approve_via_api(
        client, headers, cookies, batch_id, data["plan"]["plan_hash"]
    ).status_code == 200
    # The human changes their mind after approval: the authorization is now stale.
    repo.set_decision(document.id, "hold", expected_revision=1, actor="reviewer_1")
    before = tree_snapshot(workspace)

    response = apply_via_api(client, headers, cookies, batch_id)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == Code.PLAN_STALE
    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).is_file()
    assert repo.get_document(document.id).location == Location.ACTIVE


# ---------------------------------------------------------------------------
# Approval is human-only
# ---------------------------------------------------------------------------
def test_a_non_human_principal_cannot_approve(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]

    # An agent identity with the reviewer role plans freely but cannot approve.
    _, agent_headers, agent_cookies = auth(sessions, csrf, actor_ref="agent:planner")
    before = tree_snapshot(workspace)
    response = approve_via_api(
        client, agent_headers, agent_cookies, batch_id, data["plan"]["plan_hash"], key="agent-1"
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == Code.APPROVAL_MUST_BE_HUMAN
    assert repo.get_batch(batch_id)["execution_state"] == "planned"
    assert repo.get_batch(batch_id)["approval_actor"] is None
    assert tree_snapshot(workspace) == before

    # A worker identity is refused the same way.
    _, worker_headers, worker_cookies = auth(sessions, csrf, actor_ref="worker:local-1")
    worker = approve_via_api(
        client, worker_headers, worker_cookies, batch_id, data["plan"]["plan_hash"], key="worker-1"
    )
    assert worker.status_code == 422, worker.text
    assert worker.json()["error"]["code"] == Code.APPROVAL_MUST_BE_HUMAN
    assert repo.get_batch(batch_id)["execution_state"] == "planned"


# ---------------------------------------------------------------------------
# Restore is a new inverse plan, not a move
# ---------------------------------------------------------------------------
def test_restore_plan_mints_an_inverse_plan_without_moving(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    original_batch = data["batch_id"]
    assert approve_via_api(
        client, headers, cookies, original_batch, data["plan"]["plan_hash"]
    ).status_code == 200

    applied = apply_via_api(client, headers, cookies, original_batch)
    assert applied.status_code == 200, applied.text
    moved = workspace / reject_destination(document.id)
    assert moved.is_file()
    before = tree_snapshot(workspace)

    response = client.post(
        url(f"/actions/{original_batch}/restore-plan"),
        json={},
        headers=with_key(headers, "restore-1"),
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    restore = body["data"]
    # A new batch and a new plan; the source batch is untouched.
    assert restore["batch_id"] != original_batch
    assert restore["source_batch_id"] == original_batch
    assert restore["execution_state"] == "planned"
    operations = restore["plan"]["operations"]
    assert len(operations) == 1
    assert operations[0]["kind"] == "restore_previous"
    assert operations[0]["source"] == reject_destination(document.id)
    assert operations[0]["destination"] == NAME
    # Nothing was moved by planning the undo, and the original batch is unchanged.
    assert tree_snapshot(workspace) == before
    assert moved.is_file()
    assert repo.get_batch(original_batch)["execution_state"] == "completed"
    # The inverse plan is not approved: it must travel the same gate.
    assert repo.get_batch(restore["batch_id"])["execution_state"] == "planned"


def test_restore_plan_for_an_unknown_batch_is_not_found(client, repo, workspace, sessions, csrf):
    seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/actions/batch_missing/restore-plan"),
        json={},
        headers=with_key(headers, "restore-missing"),
        cookies=cookies,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == Code.NOT_FOUND


# ---------------------------------------------------------------------------
# No endpoint accepts a caller-supplied destination
# ---------------------------------------------------------------------------
def test_no_endpoint_accepts_a_caller_supplied_destination(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)

    plan_with_destination = client.post(
        url("/actions/plan"),
        json={"document_ids": [document.id], "destination": "Trash/elsewhere/x.pdf"},
        headers=with_key(headers, "smuggle-plan"),
        cookies=cookies,
    )
    assert plan_with_destination.status_code == 422, plan_with_destination.text
    assert plan_with_destination.json()["error"]["code"] == Code.VALIDATION_FAILED

    plan_with_actor = client.post(
        url("/actions/plan"),
        json={"document_ids": [document.id], "actor": "administrator"},
        headers=with_key(headers, "smuggle-actor"),
        cookies=cookies,
    )
    assert plan_with_actor.status_code == 422, plan_with_actor.text
    assert plan_with_actor.json()["error"]["code"] == Code.VALIDATION_FAILED

    intent_with_destination = client.put(
        url(f"/documents/{document.id}/action-intent"),
        json={"intent": "move_rejected", "expected_revision": 0, "destination": "Trash/x"},
        headers=with_key(headers, "smuggle-intent"),
        cookies=cookies,
    )
    assert intent_with_destination.status_code == 422, intent_with_destination.text

    # A plan was never created by any rejected request.
    assert repo.list_batches() == []
    assert (workspace / NAME).is_file()


def test_no_action_route_has_a_destination_path_parameter(app) -> None:
    action_paths = [
        path
        for path in app.openapi()["paths"]
        if path.startswith(API_PREFIX + "/actions") or "/action-intent" in path
    ]
    assert action_paths
    for path in action_paths:
        params = {
            part.strip("{}")
            for part in path.split("/")
            if part.startswith("{") and part.endswith("}")
        }
        assert params <= {"instance_id", "document_id", "batch_id"}, path


# ---------------------------------------------------------------------------
# Pending intent
# ---------------------------------------------------------------------------
def test_action_intent_saves_and_cancels_without_moving(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)

    saved = client.put(
        url(f"/documents/{document.id}/action-intent"),
        json={"intent": "move_rejected", "expected_revision": 0},
        headers=with_key(headers, "intent-1"),
        cookies=cookies,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["data"]["intent"] == "move_rejected"
    assert body["data"]["intent_revision"] == 1
    assert body["data"]["state"] == "saved"
    assert (workspace / NAME).is_file()

    stale = client.put(
        url(f"/documents/{document.id}/action-intent"),
        json={"intent": "move_trash", "expected_revision": 0},
        headers=with_key(headers, "intent-stale"),
        cookies=cookies,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == Code.REVISION_CONFLICT

    cancelled = client.put(
        url(f"/documents/{document.id}/action-intent"),
        json={"intent": "none", "expected_revision": 1},
        headers=with_key(headers, "intent-cancel"),
        cookies=cookies,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["state"] == "cancelled"
    assert cancelled.json()["data"]["intent_revision"] == 2
    assert (workspace / NAME).is_file()


def test_action_intent_rejects_an_unsupported_value(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    response = client.put(
        url(f"/documents/{document.id}/action-intent"),
        json={"intent": "delete_everything", "expected_revision": 0},
        headers=with_key(headers, "intent-bad"),
        cookies=cookies,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == Code.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def test_cancel_stops_unstarted_work_and_apply_then_refuses(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    data = plan_via_api(client, headers, cookies, document.id)
    batch_id = data["batch_id"]

    cancelled = client.post(
        url(f"/actions/{batch_id}/cancel"),
        json={"expected_revision": 0},
        headers=with_key(headers, "cancel-1"),
        cookies=cookies,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["execution_state"] == "canceled"
    assert repo.get_batch(batch_id)["execution_state"] == "canceled"

    before = tree_snapshot(workspace)
    # Pass the batch's current revision so the executor (not the optimistic guard)
    # answers: a canceled batch is reported as a terminal state, never re-executed.
    revision = repo.get_batch(batch_id)["execution_revision"]
    applied = apply_via_api(client, headers, cookies, batch_id, revision=revision)
    assert applied.status_code == 409, applied.text
    assert applied.json()["error"]["code"] == Code.BATCH_ALREADY_STARTED
    assert tree_snapshot(workspace) == before
    assert (workspace / NAME).is_file()


def test_cancel_of_an_unknown_batch_is_not_found(client, repo, workspace, sessions, csrf):
    seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/actions/batch_missing/cancel"),
        json={"expected_revision": 0},
        headers=with_key(headers, "cancel-missing"),
        cookies=cookies,
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Identity and role gates
# ---------------------------------------------------------------------------
def test_a_viewer_cannot_plan(client, repo: Repository, workspace: Path, sessions, csrf) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf, role=Role.VIEWER)
    response = client.post(
        url("/actions/plan"),
        json={"document_ids": [document.id]},
        headers=with_key(headers, "viewer-plan"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.ROLE_INSUFFICIENT


def test_a_mutation_without_csrf_is_refused(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    session, headers, cookies = auth(sessions, csrf)
    del headers["X-CSRF-Token"]
    response = client.post(
        url("/actions/plan"),
        json={"document_ids": [document.id]},
        headers=with_key(headers, "no-csrf"),
        cookies=cookies,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == Code.CSRF_FAILED


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_a_repeated_plan_request_replays_without_a_second_batch(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    payload = {"document_ids": [document.id]}
    key_headers = with_key(headers, "plan-idem")

    first = client.post(url("/actions/plan"), json=payload, headers=key_headers, cookies=cookies)
    second = client.post(url("/actions/plan"), json=payload, headers=key_headers, cookies=cookies)

    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["data"]["batch_id"] == second.json()["data"]["batch_id"]
    assert len(repo.list_batches()) == 1


def test_reusing_an_idempotency_key_with_a_different_payload_conflicts(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    first_doc = seed_rejected_document(repo, workspace)
    second = repo.create_document(
        original_filename="candidate-002.pdf",
        rel_path="candidate-002.pdf",
        media_type=MediaType.PDF,
        size_bytes=len(DATA),
        content_sha256="0" * 64,
        fs_identity=None,
    )
    _, headers, cookies = auth(sessions, csrf)
    key_headers = with_key(headers, "plan-reuse")

    first = client.post(
        url("/actions/plan"), json={"document_ids": [first_doc.id]}, headers=key_headers, cookies=cookies
    )
    second_response = client.post(
        url("/actions/plan"), json={"document_ids": [second.id]}, headers=key_headers, cookies=cookies
    )

    assert first.status_code == 200, first.text
    assert second_response.status_code == 409, second_response.text
    assert second_response.json()["error"]["code"] == Code.IDEMPOTENCY_KEY_REUSED


def test_a_post_without_an_idempotency_key_is_refused(
    client, repo: Repository, workspace: Path, sessions, csrf
) -> None:
    document = seed_rejected_document(repo, workspace)
    _, headers, cookies = auth(sessions, csrf)
    response = client.post(
        url("/actions/plan"),
        json={"document_ids": [document.id]},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Workspace-root resolution
# ---------------------------------------------------------------------------
def test_the_registered_root_is_authoritative_over_the_database_location(
    tmp_path: Path, sessions: SessionStore, csrf: CsrfStore, config: ApiConfig
) -> None:
    """The endpoint resolves the workspace from the registry, not from the db path.

    The database lives directly in ``outer`` (so a path-derived root would be
    ``outer``) while the registry points at ``real_root``, which is the only place
    the file exists. A plan that resolves the file proves the trusted mapping won.
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    database = Database(DbConfig(path=outer / "review.db"))
    apply_migrations(database.connect())
    try:
        repository = Repository(database)
        repository.create_instance("inst_alt", __version__, SCHEMA_VERSION)
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        HostRegistry().register("inst_alt", canonical_root=str(real_root))

        (real_root / NAME).write_bytes(DATA)
        document = repository.create_document(
            original_filename=NAME,
            rel_path=NAME,
            media_type=MediaType.PDF,
            size_bytes=len(DATA),
            content_sha256=sha256_file(real_root / NAME),
            fs_identity=None,
        )
        repository.set_decision(document.id, "reject", expected_revision=0, actor="reviewer_1")
        application = create_app(
            repository,
            sessions=sessions,
            csrf_store=csrf,
            config=config,
            route_modules=[ROUTE_MODULE],
        )
        test_client = TestClient(application)
        # The instance id differs from the module-level fixture, so issue the
        # session and cookie for this instance explicitly.
        session = sessions.issue("inst_alt", "reviewer_1", Role.REVIEWER)
        headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
        cookies = {session_cookie_name("inst_alt"): session.session_id}

        response = test_client.post(
            "/api/v1/instances/inst_alt/actions/plan",
            json={"document_ids": [document.id]},
            headers=with_key(headers, "alt-plan"),
            cookies=cookies,
        )

        assert response.status_code == 200, response.text
        operations = response.json()["data"]["plan"]["operations"]
        assert len(operations) == 1
        assert operations[0]["source"] == NAME
    finally:
        database.close()
