"""Exercise the dashboard request against the real API contracts, using no model."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review.api.app import ChatTurn, create_app
from resume_review.api.chat import ChatRequest
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.models import Role


ROOT = Path(__file__).resolve().parents[2]


def browser_request(message, document_ids):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard/API contract check")
    script = """
import { pathToFileURL } from 'node:url';
const { chatRequestBody } = await import(pathToFileURL(process.argv[1]));
const input = JSON.parse(process.argv[2]);
console.log(JSON.stringify(chatRequestBody(input.message, input.ids)));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script,
         str(ROOT / "web/assets/report.js"),
         json.dumps({"message": message, "ids": document_ids})],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def test_dashboard_feedback_reaches_authenticated_chat(tmp_path):
    body = browser_request("Please explain gaps in the project coordination evidence.", [])
    assert ChatTurn.model_validate(body).document_ids == []
    assert ChatRequest.model_validate(body).document_ids == []
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance("inst_feedback", "0.1.0", 2)
    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(repo.instance_id, "feedback_reviewer", Role.REVIEWER)
    calls = []

    async def fixture_chat(payload, *, principal, instance_id):
        calls.append((payload, principal.actor_ref, instance_id))
        return {"answer": "Synthetic response; no OpenClaw call was made."}

    app = create_app(repo, sessions=sessions, csrf_store=csrf, chat_adapter=fixture_chat)
    try:
        with TestClient(app) as client:
            client.cookies.set(session_cookie_name(repo.instance_id), session.session_id)
            response = client.post(
                f"/api/v1/instances/{repo.instance_id}/chat", json=body,
                headers={"Origin": "http://testserver", "X-CSRF-Token": csrf.issue(session.session_id)},
            )
        assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert calls[0][0]["message"] == body["message"]
        assert calls[0][0]["document_ids"] == []
        assert calls[0][2] == repo.instance_id
    finally:
        database.close()


def test_selected_chat_uses_the_api_document_scope():
    body = browser_request("Explain this evidence.", ["doc_a", "doc_b", "doc_a"])
    assert ChatRequest.model_validate(body).document_ids == ["doc_a", "doc_b"]
    assert set(body) == {"message", "document_ids"}
