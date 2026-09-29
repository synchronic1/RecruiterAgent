"""Browser bulk-decision request contract against the real local API.

Synthetic data only.  This test executes the exported dashboard request builder
under Node, then submits the resulting body through authenticated FastAPI routes.
It guards the boundary where checkbox selection becomes reviewer-owned decisions;
no file action is planned or applied here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, create_app
from resume_review.auth import CsrfStore, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.models import Role


ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = "inst_bulk_ui_contract"
ORIGIN = "http://testserver"


def _browser_bulk_body(selection: dict[str, object], disposition: str) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard/API contract check")
    script = """
import { pathToFileURL } from 'node:url';
const { bulkDecisionBody } = await import(pathToFileURL(process.argv[1]));
console.log(JSON.stringify(bulkDecisionBody(JSON.parse(process.argv[2]), process.argv[3])));
"""
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(ROOT / "web" / "assets" / "report.js"),
            json.dumps(selection),
            disposition,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _browser_single_body(decision: str, expected_revision: int) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard/API contract check")
    script = """
import { pathToFileURL } from 'node:url';
const { decisionWriteBody } = await import(pathToFileURL(process.argv[1]));
console.log(JSON.stringify(decisionWriteBody(JSON.parse(process.argv[2]))));
"""
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(ROOT / "web" / "assets" / "report.js"),
            json.dumps({"decision": decision, "expectedRevision": expected_revision}),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _document(repo: Repository, name: str):
    return repo.create_document(
        original_filename=name,
        rel_path=f"synthetic/{name}",
        media_type="pdf",
        size_bytes=24,
        content_sha256=f"sha_{name}",
        fs_identity=f"identity_{name}",
    )


def _url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def test_bulk_checkbox_selection_builds_atomic_reviewer_decisions_without_file_moves(tmp_path: Path) -> None:
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    first = _document(repo, "synthetic-first.pdf")
    second = _document(repo, "synthetic-second.pdf")
    unselected = _document(repo, "synthetic-unselected.pdf")

    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(INSTANCE_ID, "synthetic-reviewer", Role.REVIEWER)
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf.issue(session.session_id),
        "Idempotency-Key": "bulk-ui-contract-success",
    }
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
    )
    try:
        selected = {
            "pairs": [
                {"document_id": first.id, "decision_revision": 0},
                {"document_id": second.id, "decision_revision": 0},
            ]
        }
        body = _browser_bulk_body(selected, "reject")
        assert body == {
            "items": [
                {"document_id": first.id, "disposition": "reject", "expected_revision": 0},
                {"document_id": second.id, "disposition": "reject", "expected_revision": 0},
            ]
        }

        with TestClient(app) as client:
            response = client.post(_url("/decisions/bulk"), json=body, headers=headers, cookies=cookies)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["updated"] == 2

            # The explicit set changes only review decisions.  It never moves a
            # file or creates a pending file intent, even for a reject decision.
            for document_id in (first.id, second.id):
                detail = client.get(_url(f"/documents/{document_id}"), headers=headers, cookies=cookies).json()["data"]
                assert detail["review_state"] == "reject"
                assert detail["location"] == "active"
                assert detail["pending_intent"] == "none"
            untouched = client.get(_url(f"/documents/{unselected.id}"), headers=headers, cookies=cookies).json()["data"]
            assert untouched["review_state"] == "unreviewed"
            assert untouched["location"] == "active"

            # One stale selected row makes the entire later request fail.  The
            # otherwise-current unselected-to-date row must not be partially saved.
            stale_body = _browser_bulk_body(
                {
                    "pairs": [
                        {"document_id": first.id, "decision_revision": 0},
                        {"document_id": unselected.id, "decision_revision": 0},
                    ]
                },
                "hold",
            )
            stale = client.post(
                _url("/decisions/bulk"),
                json=stale_body,
                headers={**headers, "Idempotency-Key": "bulk-ui-contract-stale"},
                cookies=cookies,
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
            after = client.get(_url(f"/documents/{unselected.id}"), headers=headers, cookies=cookies).json()["data"]
            assert after["review_state"] == "unreviewed"
            assert after["location"] == "active"
    finally:
        database.close()


def test_single_decision_builder_uses_the_patch_contract_without_a_file_move(tmp_path: Path) -> None:
    database = Database(DbConfig(path=tmp_path / "single-review.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    selected = _document(repo, "synthetic-selected.pdf")
    untouched = _document(repo, "synthetic-untouched.pdf")

    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(INSTANCE_ID, "synthetic-reviewer", Role.REVIEWER)
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf.issue(session.session_id),
    }
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
    )
    try:
        body = _browser_single_body("keep", 0)
        assert body == {"disposition": "keep", "expected_revision": 0}
        with TestClient(app) as client:
            response = client.patch(
                _url(f"/documents/{selected.id}/decision"),
                json=body,
                headers=headers,
                cookies=cookies,
            )
            assert response.status_code == 200, response.text
            selected_detail = client.get(
                _url(f"/documents/{selected.id}"), headers=headers, cookies=cookies
            ).json()["data"]
            untouched_detail = client.get(
                _url(f"/documents/{untouched.id}"), headers=headers, cookies=cookies
            ).json()["data"]
            assert selected_detail["review_state"] == "keep"
            assert selected_detail["location"] == "active"
            assert selected_detail["pending_intent"] == "none"
            assert untouched_detail["review_state"] == "unreviewed"
            assert untouched_detail["location"] == "active"
    finally:
        database.close()
