"""Dashboard requisition writer contract against the local authenticated API."""

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
INSTANCE_ID = "inst_requisition_ui_contract"
ORIGIN = "http://testserver"


def _browser_write_body(values: dict[str, object]) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard/API contract check")
    script = """
import { pathToFileURL } from 'node:url';
const { requisitionWriteBody } = await import(pathToFileURL(process.argv[1]));
console.log(JSON.stringify(requisitionWriteBody(JSON.parse(process.argv[2]))));
"""
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(ROOT / "web" / "assets" / "report.js"),
            json.dumps(values),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def test_dashboard_requisition_body_persists_and_round_trips_through_api(tmp_path: Path) -> None:
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(INSTANCE_ID, "synthetic-reviewer", Role.REVIEWER)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf.issue(session.session_id)}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
    )
    try:
        with TestClient(app) as client:
            initial = client.get(_url("/requisition"), headers=headers, cookies=cookies)
            assert initial.status_code == 200, initial.text
            assert initial.json()["data"] == {"requisition": None}
            revision = initial.json()["state_revision"]

            body = _browser_write_body(
                {
                    "title": "  Synthetic Operations Lead  ",
                    "description_text": "  Coordinate synthetic site schedules.  ",
                    "source_reference": " https://example.test/jobs/synthetic ",
                    "expected_revision": revision,
                }
            )
            assert body == {
                "title": "Synthetic Operations Lead",
                "description_text": "Coordinate synthetic site schedules.",
                "source_reference": "https://example.test/jobs/synthetic",
                "expected_revision": revision,
            }
            saved = client.put(_url("/requisition"), json=body, headers=headers, cookies=cookies)
            assert saved.status_code == 200, saved.text
            assert saved.json()["data"]["requisition"]["title"] == body["title"]

            loaded = client.get(_url("/requisition"), headers=headers, cookies=cookies)
            assert loaded.status_code == 200, loaded.text
            record = loaded.json()["data"]["requisition"]
            assert record is not None
            assert {key: record[key] for key in ("title", "description_text", "source_reference")} == {
                key: body[key] for key in ("title", "description_text", "source_reference")
            }
    finally:
        database.close()
