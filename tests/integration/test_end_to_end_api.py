"""End-to-end tests for the HTTP API surface (PRD section 12).

Authority: PRD section 12 (the helper API, the instance-scoped path, the mutation
rules, and the standard envelope), section 13 (plan -> approve -> apply), section
10 (the five independent state dimensions), and AGENTS.md constraints 3, 4, and 8.

This is the first test that drives the *real* FastAPI application -- the same
``create_app`` factory the helper ships -- through ``fastapi.testclient`` over a
*real* instance provisioned by the real ``bootstrap.setup_instance`` on a
throwaway folder: the real database, the real repositories, the real planner, the
real approval-gated executor, and the real envelope. Nothing in the api layer is
mocked or stubbed.

Two honest boundaries are stated here rather than hidden:

* There is no HTTP login route in this phase (authentication is a separate
  surface, PRD section 15.1). "Authenticate" therefore means the real host account
  store (``AccountStore.authenticate``, scrypt-backed) followed by the real
  ``SessionStore.issue``; the resulting instance-scoped session cookie and CSRF
  token are what every request below presents. The credential check is the real
  one; only its HTTP transport is absent, and that absence is reported.
* Documents are registered through the real repository rather than by a scan,
  because the flow under test begins at an existing document set (``GET
  /documents``). Discovery, extraction, and analysis are exercised elsewhere
  (``tests/integration/test_end_to_end_review.py``); nothing here asserts them.

Everything else -- status, listing, PATCH decision, the audit trail, plan,
approve, apply, restore-plan, and the error envelopes -- is driven over HTTP and
asserted against what the system actually did, on disk and in the database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from resume_review.api import ApiConfig, create_app
from resume_review.api.envelope import validate_envelope
from resume_review.auth import (
    AccountStore,
    CsrfStore,
    SessionStore,
    session_cookie_name,
)
from resume_review.bootstrap import HostRegistry, workspace
from resume_review.bootstrap.setup import setup_instance
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.errors import Code
from resume_review.models import REJECTED_DIR, REVIEW_DIR, MediaType, Role
from resume_review.util import sha256_file

JOB_TEXT = "Operations Manager\nCoordinate crews and subcontractors."
ORIGIN = "http://testserver"
PASSWORD = "correct-horse-battery-staple"
ALPHA = "alpha.txt"
BRAVO = "bravo.txt"
ALPHA_BYTES = b"alpha synthetic resume\nOperations Manager\n"
BRAVO_BYTES = b"bravo synthetic resume\nOperations Manager\n"
REVIEWER = "reviewer_1"


# ---------------------------------------------------------------------------
# Fixtures: a real provisioned instance, a real app, a real session
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the protected host registry inside the test's temporary directory."""
    directory = tmp_path / "host-registry"
    directory.mkdir()
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(directory))
    return directory


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A synthetic reviewed bundle; never the repository's real ``web/`` tree."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "report.html").write_text("<html>review</html>", encoding="utf-8")
    return bundle


@pytest.fixture
def instance(tmp_path: Path, bundle_dir: Path) -> SimpleNamespace:
    """A fresh workspace provisioned by the real setup, plus an open db/repository."""
    root = tmp_path / "Job - Operations Manager"
    result = setup_instance(root, JOB_TEXT, registry=HostRegistry(), bundle_dir=bundle_dir)
    assert result.created is True
    database = Database(DbConfig(path=workspace.db_path(root)))
    repository = Repository(database)
    assert repository.instance_id == result.instance_id
    try:
        yield SimpleNamespace(
            root=root, db=database, repo=repository, instance_id=result.instance_id
        )
    finally:
        database.close()


@pytest.fixture
def accounts(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "host-secrets" / "accounts.json")


@pytest.fixture
def sessions() -> SessionStore:
    return SessionStore()


@pytest.fixture
def csrf() -> CsrfStore:
    return CsrfStore()


@pytest.fixture
def app(instance: SimpleNamespace, sessions: SessionStore, csrf: CsrfStore):
    """The real application, with default route discovery (documents/review/actions/chat)."""
    return create_app(
        instance.repo, sessions=sessions, csrf_store=csrf, config=ApiConfig()
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def login(instance: SimpleNamespace, accounts: AccountStore, sessions: SessionStore, csrf: CsrfStore):
    """Authenticate through the real account store, then issue a real session.

    This is the closest honest equivalent of signing in: the credential is checked
    against the scrypt verifier and the session carries that account's role. There
    is no HTTP login route yet, so this runs at the Python boundary.
    """

    def _login(
        *,
        actor_ref: str = REVIEWER,
        role: Role = Role.REVIEWER,
        display_name: str = "Dana Reviewer",
        password: str = PASSWORD,
    ) -> SimpleNamespace:
        accounts.create_user(display_name, password, role=role, actor_ref=actor_ref)
        user = accounts.authenticate(actor_ref, password)
        session = sessions.issue(instance.instance_id, user.actor_ref, user.role)
        headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
        cookies = {session_cookie_name(instance.instance_id): session.session_id}
        return SimpleNamespace(user=user, session=session, headers=headers, cookies=cookies)

    return _login


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def url(instance: SimpleNamespace, suffix: str) -> str:
    return f"/api/v1/instances/{instance.instance_id}{suffix}"


def with_key(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def envelope(response) -> dict:
    """Validate the body against the normative schema and echo its request id."""
    body = response.json()
    validate_envelope(body)  # raises EnvelopeSchemaError on any drift
    assert response.headers.get("X-Request-ID") == body.get("request_id")
    return body


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


def seed(repo: Repository, root: Path, name: str, data: bytes):
    """Register one synthetic submission through the real repository."""
    path = root / name
    path.write_bytes(data)
    return repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.TXT,
        size_bytes=len(data),
        content_sha256=sha256_file(path),
        fs_identity=None,
    )


def audit_rows(repo: Repository, *, event: str, entity_id: str) -> list:
    return repo.db.query(
        "SELECT * FROM audit_events WHERE event = ? AND entity_id = ? ORDER BY seq",
        (event, entity_id),
    )


def documents_by_id(client: TestClient, instance: SimpleNamespace, cookies) -> dict:
    body = envelope(client.get(url(instance, "/documents"), cookies=cookies))
    assert body["ok"] is True
    return {row["document_id"]: row for row in body["data"]["documents"]}


# ---------------------------------------------------------------------------
# The coherent end-to-end flow
# ---------------------------------------------------------------------------
def test_end_to_end_decision_plan_apply_restore_over_http(
    client: TestClient, instance: SimpleNamespace, login, app
) -> None:
    actor = login()
    root, repo = instance.root, instance.repo

    # -- authenticate -> GET /status ------------------------------------
    status = envelope(client.get(url(instance, "/status"), cookies=actor.cookies))
    assert status["ok"] is True
    assert status["instance_id"] == instance.instance_id
    assert status["data"]["counts"]["total"] == 0
    assert status["data"]["health"]["ok"] is True

    # -- seed two submissions (through the real repository) -------------
    alpha = seed(repo, root, ALPHA, ALPHA_BYTES)
    bravo = seed(repo, root, BRAVO, BRAVO_BYTES)

    # -- GET /documents: the five dimensions start independent ----------
    rows = documents_by_id(client, instance, actor.cookies)
    assert set(rows) == {alpha.id, bravo.id}
    first = rows[alpha.id]
    assert first["processing_state"] == "discovered"
    assert first["review_state"] == "unreviewed"
    assert first["location"] == "active"
    assert first["pending_intent"] == "none"
    assert first["file_actions"] == []

    # -- PATCH a decision with expected_revision ------------------------
    revision_before = repo.db.state_revision()
    decision = client.patch(
        url(instance, f"/documents/{alpha.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    decision_body = envelope(decision)
    assert decision.status_code == 200, decision.text
    assert decision_body["data"]["review_state"] == "reject"
    assert decision_body["data"]["decision_revision"] == 1
    # The mutation bumped state_revision by exactly one ...
    assert decision_body["state_revision"] == revision_before + 1
    assert repo.db.state_revision() == revision_before + 1
    # ... and wrote exactly one audit row, attributed to the session identity.
    rows = audit_rows(repo, event="decision.set", entity_id=alpha.id)
    assert len(rows) == 1
    assert rows[0]["actor"] == actor.user.actor_ref
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["request_id"] == decision_body["request_id"]

    # A second document, kept (so "exactly one file moved" is meaningful).
    kept = client.patch(
        url(instance, f"/documents/{bravo.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    assert envelope(kept)["data"]["review_state"] == "keep"

    # Dimensions still separate: a decision to reject has NOT moved anything.
    detail = envelope(
        client.get(url(instance, f"/documents/{alpha.id}"), cookies=actor.cookies)
    )["data"]
    assert detail["review_state"] == "reject"
    assert detail["location"] == "active"
    assert detail["pending_intent"] == "none"
    assert detail["processing_state"] == "discovered"
    assert detail["file_actions"] == []
    assert (root / ALPHA).is_file()

    # -- POST /actions/plan ---------------------------------------------
    snapshot_before_plan = tree_snapshot(root)
    plan = client.post(
        url(instance, "/actions/plan"),
        json={"document_ids": [alpha.id, bravo.id]},
        headers=with_key(actor.headers, "plan-1"),
        cookies=actor.cookies,
    )
    plan_body = envelope(plan)
    assert plan.status_code == 200, plan.text
    plan_data = plan_body["data"]
    batch_id = plan_data["batch_id"]
    operations = plan_data["plan"]["operations"]
    # Only the reject resolves into a move; the keep emits no operation.
    assert len(operations) == 1
    assert operations[0]["document_id"] == alpha.id
    assert operations[0]["kind"] == "move_rejected"
    assert operations[0]["destination"] == f"{REJECTED_DIR}/{alpha.id}/{ALPHA}"
    # Planning is not approval and moves nothing.
    assert tree_snapshot(root) == snapshot_before_plan
    assert repo.get_batch(batch_id)["execution_state"] == "planned"

    # The plan shows up as the action-execution dimension, distinct from intent.
    detail = envelope(
        client.get(url(instance, f"/documents/{alpha.id}"), cookies=actor.cookies)
    )["data"]
    assert detail["location"] == "active"
    assert detail["pending_intent"] == "none"
    assert detail["review_state"] == "reject"
    assert len(detail["file_actions"]) == 1
    assert detail["file_actions"][0]["state"] == "planned"

    # -- POST /actions/{id}/apply BEFORE approval: refused, no move -----
    early = client.post(
        url(instance, f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(actor.headers, "apply-early"),
        cookies=actor.cookies,
    )
    early_body = envelope(early)
    assert early.status_code == 409, early.text
    assert early_body["error"]["code"] == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == snapshot_before_plan
    assert (root / ALPHA).is_file()

    # -- POST /actions/{id}/approve -------------------------------------
    approve = client.post(
        url(instance, f"/actions/{batch_id}/approve"),
        json={"plan_hash": plan_data["plan"]["plan_hash"], "expected_revision": 0},
        headers=with_key(actor.headers, "approve-1"),
        cookies=actor.cookies,
    )
    approve_body = envelope(approve)
    assert approve.status_code == 200, approve.text
    assert approve_body["data"]["execution_state"] == "approved"
    assert approve_body["data"]["approval_actor"] == actor.user.actor_ref
    assert tree_snapshot(root) == snapshot_before_plan  # approval moves nothing

    # -- POST /actions/{id}/apply ---------------------------------------
    snapshot_before_apply = tree_snapshot(root)
    apply = client.post(
        url(instance, f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(actor.headers, "apply-1"),
        cookies=actor.cookies,
    )
    apply_body = envelope(apply)
    assert apply.status_code == 200, apply.text
    assert apply_body["data"]["state"] == "completed"
    assert apply_body["data"]["report"]["counts"]["moved"] == 1

    snapshot_after_apply = tree_snapshot(root)
    destination = f"{REJECTED_DIR}/{alpha.id}/{ALPHA}"
    # Exactly one file moved: alpha left its old path and arrived, byte-for-byte,
    # at the planned destination; bravo was never touched.
    assert set(snapshot_after_apply) - set(snapshot_before_apply) == {destination}
    assert set(snapshot_before_apply) - set(snapshot_after_apply) == {ALPHA}
    assert snapshot_after_apply[destination] == ALPHA_BYTES
    assert snapshot_after_apply[BRAVO] == BRAVO_BYTES
    assert not (root / ALPHA).exists()

    # Five dimensions after the move, read back over HTTP.
    detail = envelope(
        client.get(url(instance, f"/documents/{alpha.id}"), cookies=actor.cookies)
    )["data"]
    assert detail["location"] == "rejected"
    assert detail["current_rel_path"] == destination
    assert detail["review_state"] == "reject"  # unchanged by the move
    assert detail["processing_state"] == "discovered"  # unchanged by the move
    assert detail["pending_intent"] == "none"
    assert detail["file_actions"][0]["state"] == "committed"

    # -- POST /actions/{id}/restore-plan: a NEW plan, no move -----------
    snapshot_before_restore_plan = tree_snapshot(root)
    restore = client.post(
        url(instance, f"/actions/{batch_id}/restore-plan"),
        json={},
        headers=with_key(actor.headers, "restore-1"),
        cookies=actor.cookies,
    )
    restore_body = envelope(restore)
    assert restore.status_code == 200, restore.text
    restore_data = restore_body["data"]
    assert restore_data["batch_id"] != batch_id  # a new batch and a new plan
    assert restore_data["source_batch_id"] == batch_id
    assert restore_data["execution_state"] == "planned"
    restore_ops = restore_data["plan"]["operations"]
    assert len(restore_ops) == 1
    assert restore_ops[0]["kind"] == "restore_previous"
    assert restore_ops[0]["source"] == destination
    assert restore_ops[0]["destination"] == ALPHA
    # Planning the undo moved nothing, and the source batch is untouched.
    assert tree_snapshot(root) == snapshot_before_restore_plan
    assert repo.get_batch(batch_id)["execution_state"] == "completed"

    restore_batch = restore_data["batch_id"]

    # Nothing moves until the inverse plan is approved AND applied.
    restore_early = client.post(
        url(instance, f"/actions/{restore_batch}/apply"),
        json={"expected_revision": 0},
        headers=with_key(actor.headers, "restore-apply-early"),
        cookies=actor.cookies,
    )
    assert envelope(restore_early)["error"]["code"] == Code.APPROVAL_REQUIRED
    assert tree_snapshot(root) == snapshot_before_restore_plan

    restore_approve = client.post(
        url(instance, f"/actions/{restore_batch}/approve"),
        json={"plan_hash": restore_data["plan"]["plan_hash"], "expected_revision": 0},
        headers=with_key(actor.headers, "restore-approve"),
        cookies=actor.cookies,
    )
    assert envelope(restore_approve)["data"]["execution_state"] == "approved"
    assert tree_snapshot(root) == snapshot_before_restore_plan  # still nothing moved

    restore_apply = client.post(
        url(instance, f"/actions/{restore_batch}/apply"),
        json={"expected_revision": 0},
        headers=with_key(actor.headers, "restore-apply"),
        cookies=actor.cookies,
    )
    restore_apply_body = envelope(restore_apply)
    assert restore_apply_body["data"]["state"] == "completed"
    assert restore_apply_body["data"]["report"]["counts"]["moved"] == 1
    assert (root / ALPHA).read_bytes() == ALPHA_BYTES
    assert not (root / destination).exists()

    # The decision is independent of location: reject was never cleared by a move.
    detail = envelope(
        client.get(url(instance, f"/documents/{alpha.id}"), cookies=actor.cookies)
    )["data"]
    assert detail["location"] == "active"
    assert detail["current_rel_path"] == ALPHA
    assert detail["review_state"] == "reject"
    assert detail["file_actions"][-1]["state"] == "committed"


# ---------------------------------------------------------------------------
# A stale expected_revision is a 409 and never overwrites
# ---------------------------------------------------------------------------
def test_stale_expected_revision_is_a_409_and_does_not_overwrite(
    client: TestClient, instance: SimpleNamespace, login
) -> None:
    actor = login()
    document = seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)

    first = client.patch(
        url(instance, f"/documents/{document.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    assert envelope(first)["data"]["decision_revision"] == 1
    revision_after_first = instance.db.state_revision()

    stale = client.patch(
        url(instance, f"/documents/{document.id}/decision"),
        json={"disposition": "keep", "expected_revision": 0},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    stale_body = envelope(stale)
    assert stale.status_code == 409, stale.text
    assert stale_body["error"]["code"] == Code.REVISION_CONFLICT
    # The stale write neither bumped the revision nor overwrote the decision.
    assert stale_body["state_revision"] == revision_after_first
    assert instance.db.state_revision() == revision_after_first
    stored = instance.repo.get_decision(document.id)
    assert stored.disposition.value == "reject"
    assert stored.decision_revision == 1

    detail = envelope(
        client.get(url(instance, f"/documents/{document.id}"), cookies=actor.cookies)
    )["data"]
    assert detail["review_state"] == "reject"
    assert detail["decision_revision"] == 1


# ---------------------------------------------------------------------------
# No model text or worker identity can manufacture an approval or a move
# ---------------------------------------------------------------------------
class StubChatAdapter:
    """A stand-in model client, set on the runtime; the ``/chat`` route is the real one.

    It is an explicit stub (there is no live model in the deterministic suite). It
    records what the route forwarded and answers with a *proposal* to move a file.
    The point of the test is that such a proposal changes nothing by itself.
    """

    def __init__(self) -> None:
        self.turns: list[dict] = []

    def __call__(self, payload, *, principal, instance_id):
        self.turns.append(
            {
                "payload": dict(payload),
                "actor": principal.actor_ref,
                "instance_id": instance_id,
            }
        )
        return {
            "answer": "I would reject this, then move it under Rejected/.",
            "proposed_action": {
                "kind": "move_rejected",
                "document_id": payload.get("document_ids", [None])[0],
                "destination": "Rejected/please-do-not/alpha.txt",
            },
        }


def test_chat_text_cannot_create_an_approval_or_a_move(
    client: TestClient, instance: SimpleNamespace, login, app
) -> None:
    actor = login()
    document = seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)

    # With no chat adapter configured the route does not exist (404), exactly as an
    # unregistered path would.
    absent = client.post(
        url(instance, "/chat"),
        json={"message": "move it", "document_ids": [document.id]},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    assert envelope(absent)["error"]["code"] == Code.NOT_FOUND

    stub = StubChatAdapter()
    app.state.runtime.chat_adapter = stub

    revision_before = instance.db.state_revision()
    snapshot = tree_snapshot(instance.root)

    turn = client.post(
        url(instance, "/chat"),
        json={"message": "Please reject and move alpha.", "document_ids": [document.id]},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    turn_body = envelope(turn)
    assert turn.status_code == 200, turn.text
    assert turn_body["data"]["proposed_action"]["kind"] == "move_rejected"

    # The route forwarded the session identity and the bounded payload only.
    assert len(stub.turns) == 1
    assert stub.turns[0]["actor"] == actor.user.actor_ref
    assert stub.turns[0]["instance_id"] == instance.instance_id

    # A model proposal is data: no revision change, no batch, no move, no decision.
    assert turn_body["state_revision"] == revision_before
    assert instance.db.state_revision() == revision_before
    assert instance.repo.list_batches() == []
    assert tree_snapshot(instance.root) == snapshot
    assert (instance.root / ALPHA).is_file()
    assert instance.repo.get_decision(document.id).disposition.value == "unreviewed"


def test_apply_before_approve_over_http_is_refused(
    client: TestClient, instance: SimpleNamespace, login
) -> None:
    actor = login()
    document = seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)
    client.patch(
        url(instance, f"/documents/{document.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    plan = envelope(
        client.post(
            url(instance, "/actions/plan"),
            json={"document_ids": [document.id]},
            headers=with_key(actor.headers, "plan-a"),
            cookies=actor.cookies,
        )
    )
    batch_id = plan["data"]["batch_id"]
    snapshot = tree_snapshot(instance.root)

    applied = client.post(
        url(instance, f"/actions/{batch_id}/apply"),
        json={"expected_revision": 0},
        headers=with_key(actor.headers, "apply-a"),
        cookies=actor.cookies,
    )
    body = envelope(applied)
    assert applied.status_code == 409, applied.text
    assert body["error"]["code"] == Code.APPROVAL_REQUIRED
    assert tree_snapshot(instance.root) == snapshot
    assert (instance.root / ALPHA).is_file()
    assert instance.repo.get_batch(batch_id)["execution_state"] == "planned"


def test_a_non_human_identity_cannot_approve_over_http(
    client: TestClient, instance: SimpleNamespace, login, accounts: AccountStore, sessions: SessionStore, csrf: CsrfStore
) -> None:
    human = login()
    document = seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)
    client.patch(
        url(instance, f"/documents/{document.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0},
        headers=human.headers,
        cookies=human.cookies,
    )
    plan = envelope(
        client.post(
            url(instance, "/actions/plan"),
            json={"document_ids": [document.id]},
            headers=with_key(human.headers, "plan-h"),
            cookies=human.cookies,
        )
    )
    batch_id = plan["data"]["batch_id"]
    plan_hash = plan["data"]["plan"]["plan_hash"]
    snapshot = tree_snapshot(instance.root)

    for actor_ref, key in (("agent:planner", "agent-1"), ("worker:local-1", "worker-1")):
        agent = login(actor_ref=actor_ref, display_name=actor_ref)
        refused = client.post(
            url(instance, f"/actions/{batch_id}/approve"),
            json={"plan_hash": plan_hash, "expected_revision": 0},
            headers=with_key(agent.headers, key),
            cookies=agent.cookies,
        )
        body = envelope(refused)
        assert refused.status_code == 422, refused.text
        assert body["error"]["code"] == Code.APPROVAL_MUST_BE_HUMAN
        assert instance.repo.get_batch(batch_id)["execution_state"] == "planned"
        assert instance.repo.get_batch(batch_id)["approval_actor"] is None
        assert tree_snapshot(instance.root) == snapshot


# ---------------------------------------------------------------------------
# No caller-supplied destination, actor, or approval field is honoured
# ---------------------------------------------------------------------------
def test_smuggled_fields_are_rejected_and_move_nothing(
    client: TestClient, instance: SimpleNamespace, login
) -> None:
    actor = login()
    document = seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)
    snapshot = tree_snapshot(instance.root)

    plan_with_destination = client.post(
        url(instance, "/actions/plan"),
        json={"document_ids": [document.id], "destination": "Trash/elsewhere/x.txt"},
        headers=with_key(actor.headers, "smuggle-plan"),
        cookies=actor.cookies,
    )
    assert envelope(plan_with_destination)["error"]["code"] == Code.VALIDATION_FAILED

    plan_with_model_output = client.post(
        url(instance, "/actions/plan"),
        json={"document_ids": [document.id], "model_output": {"approved": True}},
        headers=with_key(actor.headers, "smuggle-model"),
        cookies=actor.cookies,
    )
    assert envelope(plan_with_model_output)["error"]["code"] == Code.VALIDATION_FAILED

    approve_with_approval = client.post(
        url(instance, "/actions/batch_x/approve"),
        json={
            "plan_hash": "0" * 40,
            "expected_revision": 0,
            "approval": True,
            "actor": "administrator",
        },
        headers=with_key(actor.headers, "smuggle-approve"),
        cookies=actor.cookies,
    )
    assert envelope(approve_with_approval)["error"]["code"] == Code.VALIDATION_FAILED

    decision_with_actor = client.patch(
        url(instance, f"/documents/{document.id}/decision"),
        json={"disposition": "reject", "expected_revision": 0, "actor": "administrator"},
        headers=actor.headers,
        cookies=actor.cookies,
    )
    assert envelope(decision_with_actor)["error"]["code"] == Code.VALIDATION_FAILED

    # None of the rejected requests created a batch, a decision, or a move.
    assert instance.repo.list_batches() == []
    assert instance.repo.get_decision(document.id).disposition.value == "unreviewed"
    assert tree_snapshot(instance.root) == snapshot


# ---------------------------------------------------------------------------
# Authentication and instance binding at the HTTP boundary
# ---------------------------------------------------------------------------
def test_unauthenticated_and_mis_scoped_requests_are_refused(
    client: TestClient, instance: SimpleNamespace, login
) -> None:
    actor = login()
    seed(instance.repo, instance.root, ALPHA, ALPHA_BYTES)

    anonymous = client.get(url(instance, "/status"))
    anonymous_body = envelope(anonymous)
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous_body["error"]["code"] == Code.UNAUTHENTICATED

    # A genuine session aimed at a different instance is refused (403), and the
    # request never reaches the handler.
    other = client.get(
        f"/api/v1/instances/inst_somewhere_else/status",
        cookies={session_cookie_name("inst_somewhere_else"): actor.session.session_id},
    )
    other_body = envelope(other)
    assert other.status_code == 403, other.text
    assert other_body["error"]["code"] == Code.INSTANCE_MISMATCH
