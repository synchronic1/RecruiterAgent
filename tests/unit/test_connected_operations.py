"""Connected action handlers exercised against real helper response envelopes.

All files are synthetic and live under pytest's temporary directory.
"""

from __future__ import annotations

import copy
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
from resume_review.models import MediaType, Role
from resume_review.util import sha256_file


ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = "inst_connected_operations"
ORIGIN = "http://testserver"


def _url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def _key(headers: dict[str, str], value: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": value}


def _document(repo: Repository, workspace: Path, name: str):
    path = workspace / name
    path.write_bytes((f"synthetic-{name}\n" * 8).encode())
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=path.stat().st_size,
        content_sha256=sha256_file(path),
        fs_identity=None,
    )
    repo.set_decision(document.id, "reject", expected_revision=0, actor="reviewer_1")
    return document


def _run_node(scenario: str, envelopes: list[dict], initial: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the connected operations contract")
    script = r"""
import fs from 'node:fs';
const assetPath = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let source = fs.readFileSync(assetPath, 'utf8');
source = source.replace(
  '    renderHeader();\n    recompute();\n    announce("Refreshed from the helper. No inference ran and nothing was approved.");',
  '    /* Rendering intentionally omitted by the Node contract harness. */'
);
source += '\nexport { DOM, buildPlan, approvePlan, applyPlan, cancelPlan, restorePlan, queueOperation, resetFinishedPlan };\n';
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const Report = await import(moduleUrl);
const { DOM } = Report;
const calls = [];
let cursor = 0;
const status = { textContent: '', hidden: false, dataset: {} };
const alert = { textContent: '', hidden: true, dataset: {} };
DOM.doc = {
  getElementById: (id) => id === 'rr-status' ? status : id === 'rr-alert' ? alert : null,
};
DOM.window = {
  confirm: () => input.confirm !== false,
  fetch: async (url, init) => {
    calls.push({
      url: String(url),
      method: init.method,
      body: init.body ? JSON.parse(init.body) : null,
      idempotency_key: init.headers['Idempotency-Key'] || null,
    });
    const item = input.envelopes[cursor++];
    if (!item) throw new Error('unexpected request');
    if (item.throw) throw new TypeError('synthetic transport interruption');
    return { ok: item.status >= 200 && item.status < 300, status: item.status, json: async () => item.body };
  },
};
DOM.state.mode = input.initial.mode || 'connected';
DOM.state.role = input.initial.role || 'reviewer';
DOM.state.instanceId = 'inst_connected_operations';
DOM.state.apiBase = '/api/v1/instances';
DOM.state.csrfToken = 'synthetic-csrf';
DOM.state.payload = { schema_version: '1.0', mode: 'connected', instance: {}, counts: {}, documents: [] };
DOM.state.rows = input.initial.rows || [];
DOM.state.filteredRows = DOM.state.rows;
DOM.state.filteredTotal = DOM.state.rows.length;
DOM.state.selection = { pairs: input.initial.selection || [], frozen: false, source: 'manual' };
DOM.state.plan = input.initial.plan || null;
DOM.state.actionBusy = false;
DOM.state.actionRetry = null;

if (input.scenario === 'build_approve') {
  await Report.buildPlan();
  await Report.approvePlan();
} else if (input.scenario === 'apply_restore') {
  await Report.applyPlan();
  await Report.restorePlan();
} else if (input.scenario === 'partial_apply') {
  await Report.applyPlan();
} else if (input.scenario === 'cancel') {
  await Report.cancelPlan();
  if (input.initial.reset_finished) Report.resetFinishedPlan();
} else if (input.scenario === 'queue') {
  await Report.queueOperation('/scan', 'Scan');
  await Report.queueOperation('/analysis/jobs', 'Summarize selected candidates');
} else if (input.scenario === 'analysis_zero') {
  await Report.queueOperation('/analysis/jobs', 'Summarize selected candidates');
} else if (input.scenario === 'retry') {
  await Report.buildPlan();
  await Report.buildPlan();
} else if (input.scenario === 'gates') {
  DOM.state.role = 'viewer';
  await Report.buildPlan();
  DOM.state.role = 'reviewer';
  DOM.state.mode = 'snapshot';
  await Report.queueOperation('/scan', 'Scan');
  DOM.state.mode = 'connected';
  DOM.state.selection = { pairs: [], frozen: false, source: 'manual' };
  await Report.queueOperation('/analysis/jobs', 'Summarize selected candidates');
  DOM.state.selection = { pairs: Array.from({ length: 201 }, (_, index) => ({ document_id: `doc_${index}`, decision_revision: 0 })), frozen: false, source: 'manual' };
  await Report.queueOperation('/analysis/jobs', 'Summarize selected candidates');
  DOM.state.selection = { pairs: input.initial.selection, frozen: false, source: 'manual' };
  DOM.window.confirm = () => false;
  DOM.state.plan = input.initial.plan;
  await Report.approvePlan();
  DOM.window.confirm = () => true;
  DOM.state.plan = null;
  await Promise.all([Report.buildPlan(), Report.buildPlan()]);
}
console.log(JSON.stringify({ calls, plan: DOM.state.plan, status: status.textContent, alert: alert.textContent }));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(ROOT / "web/assets/report.js")],
        input=json.dumps({"scenario": scenario, "envelopes": envelopes, "initial": initial}),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.fixture
def real_contract(tmp_path: Path):
    workspace = tmp_path / "job"
    (workspace / ".review").mkdir(parents=True)
    database = Database(DbConfig(path=workspace / ".review/review.db"))
    apply_migrations(database.connect())
    repo = Repository(database)
    repo.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    sessions, csrf = SessionStore(), CsrfStore()
    session = sessions.issue(INSTANCE_ID, "reviewer_1", Role.REVIEWER)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf.issue(session.session_id)}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    app = create_app(repo, sessions=sessions, csrf_store=csrf, config=ApiConfig(allowed_origins=(ORIGIN,)))

    try:
        with TestClient(app) as client:
            first = _document(repo, workspace, "first.pdf")
            plan_response = client.post(
                _url("/actions/plan"),
                json={"document_ids": [first.id]},
                headers=_key(headers, "server-plan"),
                cookies=cookies,
            )
            assert plan_response.status_code == 200, plan_response.text
            plan = plan_response.json()["data"]
            approve_response = client.post(
                _url(f"/actions/{plan['batch_id']}/approve"),
                json={"plan_hash": plan["plan"]["plan_hash"], "expected_revision": plan["execution_revision"]},
                headers=_key(headers, "server-approve"),
                cookies=cookies,
            )
            assert approve_response.status_code == 200, approve_response.text
            approved = {**plan, **approve_response.json()["data"], "plan": plan["plan"]}
            apply_response = client.post(
                _url(f"/actions/{plan['batch_id']}/apply"),
                json={"expected_revision": approved["execution_revision"]},
                headers=_key(headers, "server-apply"),
                cookies=cookies,
            )
            assert apply_response.status_code == 200, apply_response.text
            restore_response = client.post(
                _url(f"/actions/{plan['batch_id']}/restore-plan"),
                json={},
                headers=_key(headers, "server-restore"),
                cookies=cookies,
            )
            assert restore_response.status_code == 200, restore_response.text

            second = _document(repo, workspace, "second.pdf")
            cancel_plan_response = client.post(
                _url("/actions/plan"),
                json={"document_ids": [second.id]},
                headers=_key(headers, "server-plan-cancel"),
                cookies=cookies,
            )
            cancel_plan = cancel_plan_response.json()["data"]
            cancel_response = client.post(
                _url(f"/actions/{cancel_plan['batch_id']}/cancel"),
                json={"expected_revision": cancel_plan["execution_revision"]},
                headers=_key(headers, "server-cancel"),
                cookies=cookies,
            )
            assert cancel_response.status_code == 200, cancel_response.text

            # Create one analysis-eligible document and approved criterion.
            analysis_doc = _document(repo, workspace, "analysis.pdf")
            analysis_path = workspace / "analysis.pdf"
            repo.add_revision(
                analysis_doc.id,
                sha256_file(analysis_path),
                analysis_path.stat().st_size,
                "analysis.pdf",
            )
            repo.create_or_update_job("Synthetic role", "Synthetic requirements")
            criterion = repo.create_criteria_proposal(
                "c_synthetic", "Synthetic criterion", created_by="reviewer_1"
            )
            repo.activate_criteria_version(criterion.version, actor="reviewer_1")
            scan_response = client.post(
                _url("/scan"), json={}, headers=_key(headers, "server-scan"), cookies=cookies
            )
            analysis_response = client.post(
                _url("/analysis/jobs"),
                json={"document_ids": [analysis_doc.id]},
                headers=_key(headers, "server-analysis"),
                cookies=cookies,
            )
            assert scan_response.status_code == analysis_response.status_code == 202
            status_response = client.get(_url("/status"), headers=headers, cookies=cookies)
            documents_response = client.get(
                _url("/documents"),
                params={"page": 1, "page_size": 100, "sort": "ingested_at", "direction": "asc"},
                headers=headers,
                cookies=cookies,
            )

        yield {
            "plan": plan_response.json(),
            "approve": approve_response.json(),
            "approved": approved,
            "apply": apply_response.json(),
            "restore": restore_response.json(),
            "cancel_plan": cancel_plan,
            "cancel": cancel_response.json(),
            "scan": scan_response.json(),
            "analysis": analysis_response.json(),
            "analysis_document_id": analysis_doc.id,
            "status": status_response.json(),
            "documents": documents_response.json(),
        }
    finally:
        database.close()


def _ok(body: dict, status: int = 200) -> dict:
    return {"status": status, "body": body}


def test_build_approve_cancel_and_queue_use_exact_backend_contract(real_contract: dict) -> None:
    plan = real_contract["plan"]["data"]
    selection = [{"document_id": plan["plan"]["operations"][0]["document_id"], "decision_revision": 1}]
    built = _run_node(
        "build_approve",
        [_ok(real_contract["plan"]), _ok(real_contract["approve"])],
        {"selection": selection},
    )
    assert built["calls"][0]["body"] == {"document_ids": [selection[0]["document_id"]]}
    assert built["calls"][1]["body"] == {
        "plan_hash": plan["plan"]["plan_hash"],
        "expected_revision": plan["execution_revision"],
    }
    assert built["plan"]["execution_state"] == "approved"
    assert built["plan"]["plan"]["operations"] == plan["plan"]["operations"]

    canceled = _run_node(
        "cancel",
        [_ok(real_contract["cancel"])],
        {"plan": real_contract["cancel_plan"], "selection": selection},
    )
    assert canceled["calls"][0]["body"] == {
        "expected_revision": real_contract["cancel_plan"]["execution_revision"]
    }
    assert canceled["plan"]["execution_state"] == "canceled"

    reset = _run_node(
        "cancel",
        [_ok(real_contract["cancel"])],
        {"plan": real_contract["cancel_plan"], "selection": selection, "reset_finished": True},
    )
    assert reset["plan"] is None

    analysis_id = real_contract["analysis_document_id"]
    queued = _run_node(
        "queue",
        [_ok(real_contract["scan"], 202), _ok(real_contract["analysis"], 202)],
        {"selection": [{"document_id": analysis_id, "decision_revision": 1}]},
    )
    assert queued["calls"][0]["body"] == {}
    assert queued["calls"][1]["body"] == {"document_ids": [analysis_id]}
    assert "1 analysis job queued" in queued["status"]

    zero_envelope = copy.deepcopy(real_contract["analysis"])
    zero_envelope["data"]["queued"] = 0
    zero_envelope["data"]["jobs"] = []
    zero = _run_node(
        "analysis_zero",
        [_ok(zero_envelope)],
        {"selection": [{"document_id": analysis_id, "decision_revision": 1}]},
    )
    assert "No analysis jobs were queued" in zero["status"]


def test_apply_retains_report_restore_is_fresh_and_partial_evidence_survives(
    real_contract: dict,
) -> None:
    refresh = [_ok(real_contract["status"]), _ok(real_contract["documents"])]
    applied = _run_node(
        "apply_restore",
        [
            _ok(real_contract["apply"]),
            *refresh,
            _ok(real_contract["restore"]),
        ],
        {"plan": real_contract["approved"]},
    )
    apply_call = applied["calls"][0]
    assert apply_call["body"] == {
        "expected_revision": real_contract["approved"]["execution_revision"]
    }
    assert applied["calls"][-1]["body"] == {}
    assert applied["plan"]["batch_id"] != real_contract["approved"]["batch_id"]
    assert applied["plan"]["execution_state"] == "planned"
    assert applied["plan"]["plan"]["operations"] == real_contract["restore"]["data"]["plan"]["operations"]

    report = copy.deepcopy(real_contract["apply"]["data"]["report"])
    report.update({"ok": False, "state": "partial", "remaining": 1})
    report["counts"] = {"moved": 1, "blocked": 1}
    report["warnings"] = ["Synthetic partial execution warning."]
    partial_error = {
        "ok": False,
        "code": "BATCH_PARTIAL",
        "request_id": "partial",
        "error": {
            "code": "BATCH_PARTIAL",
            "message": "The batch completed only partially.",
            "retryable": False,
            "detail": {"batch_id": report["batch_id"], "report": report},
        },
        "warnings": [],
    }
    partial = _run_node(
        "partial_apply",
        [_ok(partial_error, 409), *refresh],
        {"plan": real_contract["approved"]},
    )
    assert partial["plan"]["execution_state"] == "partial"
    assert partial["plan"]["plan"] == real_contract["approved"]["plan"]
    assert partial["plan"]["execution_report"]["counts"] == {"moved": 1, "blocked": 1}
    assert "Some files may have moved" in partial["alert"]


def test_viewer_snapshot_decline_and_duplicate_clicks_do_not_mutate(real_contract: dict) -> None:
    plan = real_contract["plan"]["data"]
    selection = [{"document_id": plan["plan"]["operations"][0]["document_id"], "decision_revision": 1}]
    gated = _run_node(
        "gates",
        [_ok(real_contract["plan"])],
        {"plan": plan, "selection": selection},
    )
    # Viewer build, snapshot scan, and declined approval produce no request; the
    # two simultaneous build calls collapse to one in-flight mutation.
    assert len(gated["calls"]) == 1
    assert gated["calls"][0]["url"].endswith("/actions/plan")

    retried = _run_node(
        "retry",
        [{"throw": True}, _ok(real_contract["plan"])],
        {"selection": selection},
    )
    assert len(retried["calls"]) == 2
    assert retried["calls"][0]["body"] == retried["calls"][1]["body"]
    assert retried["calls"][0]["idempotency_key"] == retried["calls"][1]["idempotency_key"]
