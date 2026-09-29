"""Regression contract for connected dashboard list refresh.

The browser helper is run under Node with its real refresh loop and a no-DOM
render seam.  Its responses originate from the real authenticated FastAPI list
and status endpoints over a synthetic 101-document instance.
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
INSTANCE_ID = "inst_connected_ui_contract"
ORIGIN = "http://testserver"


def _url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def _run_browser_refresh(envelopes: list[dict[str, object]]) -> dict[str, object]:
    """Execute the production refresh loop with recorded local API envelopes.

    Rendering is replaced only at the two calls after list collection.  The test
    therefore exercises production request construction, pagination, duplicate
    detection, revision consistency, and all-or-nothing row assignment without
    requiring a browser DOM implementation.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the connected dashboard contract check")
    script = """
import fs from 'node:fs';
const assetPath = process.argv[1];
const envelopes = JSON.parse(fs.readFileSync(0, 'utf8'));
let source = fs.readFileSync(assetPath, 'utf8');
source = source.replace(
  '    renderHeader();\\n    recompute();\\n    announce("Refreshed from the helper. No inference ran and nothing was approved.");',
  '    /* Rendering intentionally omitted by the Node contract harness. */'
);
source += '\\nexport { refreshFromHelper, DOM };\\n';
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const { refreshFromHelper, DOM } = await import(moduleUrl);
const calls = [];
let cursor = 0;
DOM.doc = null;
DOM.window = {
  fetch: async (url) => {
    calls.push(String(url));
    const envelope = envelopes[cursor++];
    if (!envelope) throw new Error('unexpected request');
    return { ok: true, status: 200, json: async () => envelope };
  },
};
DOM.state.mode = 'connected';
DOM.state.instanceId = 'inst_connected_ui_contract';
DOM.state.apiBase = '/api/v1/instances';
DOM.state.payload = { schema_version: '1.0', mode: 'connected', instance: {}, counts: {}, documents: [] };
DOM.state.rows = [];
DOM.state.filteredRows = [];
DOM.state.filteredTotal = 0;
await refreshFromHelper();
console.log(JSON.stringify({
  calls,
  rows: DOM.state.rows.length,
  row_ids: DOM.state.rows.map((row) => row.document_id),
  revision: DOM.state.payload.instance.state_revision,
}));
"""
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(ROOT / "web" / "assets" / "report.js"),
        ],
        input=json.dumps(envelopes),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _create_document(repo: Repository, number: int) -> None:
    name = f"synthetic-{number:03d}.pdf"
    repo.create_document(
        original_filename=name,
        rel_path=f"incoming/{name}",
        media_type="pdf",
        size_bytes=100 + number,
        content_sha256=f"synthetic-sha-{number}",
        fs_identity=f"synthetic-identity-{number}",
    )


def test_connected_refresh_uses_documents_pages_and_rejects_a_changed_revision(tmp_path: Path) -> None:
    database = Database(DbConfig(path=tmp_path / "connected.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    for number in range(101):
        _create_document(repo, number)

    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(INSTANCE_ID, "synthetic-reviewer", Role.REVIEWER)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf.issue(session.session_id)}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    app = create_app(repo, sessions=sessions, csrf_store=csrf, config=ApiConfig(allowed_origins=(ORIGIN,)))
    try:
        with TestClient(app) as client:
            status = client.get(_url("/status"), headers=headers, cookies=cookies)
            first = client.get(
                _url("/documents"),
                params={"page": 1, "page_size": 100, "sort": "ingested_at", "direction": "asc"},
                headers=headers,
                cookies=cookies,
            )
            second = client.get(
                _url("/documents"),
                params={"page": 2, "page_size": 100, "sort": "ingested_at", "direction": "asc"},
                headers=headers,
                cookies=cookies,
            )
        assert status.status_code == first.status_code == second.status_code == 200
        assert len(first.json()["data"]["documents"]) == 100
        assert first.json()["data"]["has_more"] is True
        assert len(second.json()["data"]["documents"]) == 1
        assert second.json()["data"]["has_more"] is False

        refreshed = _run_browser_refresh([status.json(), first.json(), second.json()])
        assert refreshed["rows"] == 101
        assert len(set(refreshed["row_ids"])) == 101
        assert refreshed["revision"] == first.json()["state_revision"]
        assert any("page=1" in call and "page_size=100" in call for call in refreshed["calls"])
        assert any("page=2" in call and "page_size=100" in call for call in refreshed["calls"])

        changed_second = json.loads(json.dumps(second.json()))
        changed_second["state_revision"] = int(first.json()["state_revision"]) + 1
        rejected = _run_browser_refresh([status.json(), first.json(), changed_second])
        # The loop sees both pages but never publishes a partial first page when
        # their state revisions disagree.
        assert rejected["rows"] == 0
    finally:
        database.close()
