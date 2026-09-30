"""Desktop helper integration against synthetic files and recorded HTTP envelopes."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from resume_review.bootstrap import workspace
from resume_review.bootstrap.setup import setup_instance
from resume_review.db import Database, Repository
from resume_review.db.connection import DbConfig
from resume_review.errors import ResumeReviewError
from resume_review.helper import DesktopWorker, build_desktop_app
from resume_review.models import ModelRoute, Role
from resume_review.openclaw_adapter.client import OpenClawAdapter
from resume_review.openclaw_adapter.connection import load_connection


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(tmp_path / "registry"))
    job = tmp_path / "requisition.txt"
    job.write_text("Software engineer\nBuild Python services.", encoding="utf-8")
    root = tmp_path / "resumes"
    setup_instance(root, job.read_text(encoding="utf-8"))
    db = Database(DbConfig(path=workspace.db_path(root)))
    repo = Repository(db)
    yield root, repo
    db.close()


def profile(tmp_path, instance_id):
    secret = tmp_path / "gateway-secret"
    secret.write_text("synthetic-test-token-only", encoding="utf-8")
    secret.chmod(0o600)
    return {
        "schema_version": 1, "instance_id": instance_id,
        "base_url": "https://analysis.example.test", "agent_id": "recruiter-analysis",
        "secret_file": str(secret), "provider_label": "Approved test provider",
        "privacy_approved": True, "timeout_seconds": 5,
        "attestation": {
            "attested_by": "test-operator", "attested_at": "2026-09-29T00:00:00+00:00",
            **{key: True for key in (
                "no_shell", "no_write_or_edit", "no_browser_control", "no_messaging",
                "no_credential_read", "no_unrestricted_file_read", "no_cross_session",
                "no_agent_spawning", "trusted_instruction_workspace",
            )},
        },
    }


def config_for(tmp_path, root, repo, changes=None):
    raw = profile(tmp_path, repo.instance_id)
    if changes:
        raw.update(changes)
    path = tmp_path / "connection.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_connection(path, root=root, instance_id=repo.instance_id)


def test_profile_is_bound_and_secret_is_not_reported(installed, tmp_path):
    root, repo = installed
    config = config_for(tmp_path, root, repo)
    assert config.approved_https_origin == "https://analysis.example.test:443"
    assert config.route_policy.route is ModelRoute.APPROVED_PROVIDER
    assert "synthetic-test-token" not in json.dumps(config.describe()) + repr(config)
    assert str(tmp_path) not in json.dumps(config.describe())


@pytest.mark.parametrize("changes", [
    {"instance_id": "inst_another"}, {"privacy_approved": False},
    {"privacy_approved": "true"}, {"base_url": "http://analysis.example.test"},
    {"base_url": "https://analysis.example.test/new?agent=main"},
    {"base_url": "https://analysis.example.test:invalid"},
    {"base_url": "https://user:password@analysis.example.test"},
    {"unexpected": "browser-proxy"},
])
def test_unsafe_profile_refused(installed, tmp_path, changes):
    root, repo = installed
    with pytest.raises(ResumeReviewError):
        config_for(tmp_path, root, repo, changes)


def test_applicant_owned_config_and_credentials_refused(installed, tmp_path):
    root, repo = installed
    raw = profile(tmp_path, repo.instance_id)
    applicant_profile = root / "connection.json"
    applicant_profile.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ResumeReviewError):
        load_connection(applicant_profile, root=root, instance_id=repo.instance_id)
    secret = root / "secret.txt"
    secret.write_text("synthetic", encoding="utf-8")
    with pytest.raises(ResumeReviewError):
        config_for(tmp_path, root, repo, {"secret_file": str(secret)})


def test_incomplete_or_string_attestation_refused(installed, tmp_path):
    root, repo = installed
    raw = profile(tmp_path, repo.instance_id)
    for value in (False, "true"):
        attestation = {**raw["attestation"], "no_shell": value}
        with pytest.raises(ResumeReviewError):
            config_for(tmp_path, root, repo, {"attestation": attestation})


def test_exact_origin_and_local_only_boundaries(installed, tmp_path):
    root, repo = installed
    config = config_for(tmp_path, root, repo, {"base_url": "https://8.8.8.8"})
    with pytest.raises(ResumeReviewError):
        replace(config, approved_https_origin=None)
    with pytest.raises(ResumeReviewError):
        replace(config, base_url="https://9.9.9.9")
    with pytest.raises(ResumeReviewError):
        replace(config, route_policy=replace(config.route_policy, route=ModelRoute.LOCAL_ONLY))


def pair_client(app, client):
    ticket = app.state.desktop_pairing.create(actor_ref="reviewer_test", role=Role.ADMINISTRATOR, port=80)
    response = client.get("/pair", params={"token": ticket.token}, follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/pair", params={"token": ticket.token}, follow_redirects=False).status_code == 401
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return page, {"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf}


def test_connected_launch_auth_assets_and_csrf(installed):
    root, repo = installed
    app = build_desktop_app(repo, root)
    base = f"/api/v1/instances/{repo.instance_id}"
    # Deliberately do not enter lifespan: this test drives the worker manually.
    with httpx.Client():
        client = TestClient(app, base_url="http://127.0.0.1")
        assert client.get(base + "/review").status_code == 401
        assert client.get(base + "/assets/report.js").status_code == 401
        assert client.get(base + "/connection").status_code == 401
        page, headers = pair_client(app, client)
        assert '"mode":"connected"' in page.text
        assert "synthetic-test-token" not in page.text
        assert "connect-src 'self'" in page.headers["content-security-policy"]
        assert client.get(base + "/assets/report.js").status_code == 200
        assert client.get(base + "/assets/not-a-file").status_code == 404
        assert client.post(base + "/scan", json={}, headers={"Origin": "http://127.0.0.1"}).status_code == 403
        assert client.post(base + "/scan", json={}, headers={**headers, "Origin": "https://foreign.example"}).status_code == 403
        assert client.get(base + "/connection").json()["data"]["configured"] is False
        assert client.post(base + "/chat", json={"message": "Hello"}, headers=headers).status_code == 404
    app.state.desktop_worker.repository.db.close()


def response_content(body):
    user = body["messages"][-1]["content"]
    if "BOUND REQUEST FIELDS" in user:
        meta = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
        return {
            **meta, "summary": {"text": "Synthetic application reviewed.", "evidence_ids": []},
            "criteria": [{"criterion_id": "cr_python", "result": "not_found", "explanation": "Not established.", "evidence_ids": []}],
            "evidence": [], "suggested_tasks": [], "warnings": [],
        }
    return {"schema_version": "1.0", "answer": "Synthetic feedback processed.", "citations": [], "criteria_proposals": []}


def test_dashboard_to_worker_to_hosted_envelopes(installed, tmp_path):
    root, repo = installed
    original = root / "synthetic-engineer.txt"
    original.write_text("Synthetic engineer\nBuilt Python services.\n", encoding="utf-8")
    before = original.read_bytes()
    repo.create_criteria_proposal("cr_python", "Evidence of building Python services.")
    repo.activate_criteria_version(1, "human:test")
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append((request, body))
        assert request.headers["authorization"] == "Bearer synthetic-test-token-only"
        assert request.url == "https://analysis.example.test/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(response_content(body))}}]})

    config = replace(config_for(tmp_path, root, repo), transport=httpx.MockTransport(respond))
    app = build_desktop_app(repo, root, config)
    worker = app.state.desktop_worker
    client = TestClient(app, base_url="http://127.0.0.1")
    _, headers = pair_client(app, client)
    base = f"/api/v1/instances/{repo.instance_id}"
    scan = client.post(base + "/scan", json={}, headers={**headers, "Idempotency-Key": "synthetic-scan"})
    assert scan.status_code == 202
    assert worker.step()
    assert not calls, "Scanning must not call the model"
    document = repo.list_documents()[0]
    repo.set_decision(document.id, "keep", expected_revision=0, actor="reviewer_test")
    queued = client.post(base + "/analysis/jobs", json={"document_ids": [document.id]}, headers={**headers, "Idempotency-Key": "synthetic-analysis"})
    assert queued.status_code == 202
    assert worker.step()
    job_id = queued.json()["data"]["jobs"][0]["job_id"]
    assert client.get(base + "/jobs/" + job_id).json()["data"]["state"] == "succeeded"
    assert repo.current_profile(document.id) is not None
    assert len(calls) == 1
    assert calls[0][1]["model"] == "openclaw/recruiter-analysis"
    assert "tools" not in calls[0][1]
    assert str(root) not in json.dumps(calls[0][1])
    # No raw PDF, path, actor credentials or model-controlled actions are transmitted.
    direct = client.post(base + "/chat", json={"message": "Explain the missing evidence."}, headers=headers)
    assert direct.status_code == 200
    assert direct.json()["data"]["answer"] == "Synthetic feedback processed."
    queued_chat = client.post(base + "/chat", json={"message": "Review this next.", "mode": "queue"}, headers={**headers, "Idempotency-Key": "synthetic-chat"})
    assert queued_chat.status_code == 202
    assert len(calls) == 2
    assert worker.step()
    assert len(calls) == 3
    assert client.get(base + "/jobs/" + queued_chat.json()["job_id"]).json()["data"]["state"] == "succeeded"
    assert repo.get_decision(document.id).disposition.value == "keep"
    assert original.read_bytes() == before
    assert repo.db.scalar("SELECT COUNT(*) FROM action_batches") == 0
    assert client.get(base + "/connection").json()["data"]["state"] == "ready"
    worker.repository.db.close()


def test_mock_probe_is_not_live_and_redirect_is_not_followed(installed, tmp_path):
    root, repo = installed
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.example/steal"})

    config = replace(config_for(tmp_path, root, repo), transport=httpx.MockTransport(redirect))
    verification = asyncio.run(OpenClawAdapter(config).verify_route())
    assert verification.ok is False and verification.is_live is False
    assert seen == ["https://analysis.example.test/v1/models"]


def test_offline_queue_survives_worker_restart(installed):
    root, repo = installed
    job_id = repo.enqueue_job("synthetic-deferred", "analysis", input_versions={"requested_by": "reviewer_test"})
    worker = DesktopWorker(repo, root, None)
    assert worker.step() is False
    assert repo.get_job(job_id)["attempts"] == 0
    restarted = DesktopWorker(repo, root, None)
    assert restarted.step() is False
    assert repo.get_job(job_id)["state"] == "queued"


def test_worker_starts_and_stops_with_helper_lifespan(installed):
    root, repo = installed
    app = build_desktop_app(repo, root)
    with TestClient(app, base_url="http://127.0.0.1"):
        assert app.state.desktop_worker.thread.is_alive()
        from resume_review.bootstrap.ownership import is_locked

        assert is_locked(workspace.owner_lock_path(root))
    assert not app.state.desktop_worker.thread.is_alive()


def test_second_helper_cannot_take_the_same_folder(installed):
    root, repo = installed
    first = build_desktop_app(repo, root)
    second = build_desktop_app(repo, root)
    with TestClient(first, base_url="http://127.0.0.1"):
        with pytest.raises(ResumeReviewError, match="Another owner"):
            with TestClient(second, base_url="http://127.0.0.1"):
                pass
        assert first.state.desktop_worker.thread.is_alive()
    second.state.desktop_worker.repository.db.close()


def test_transient_hosted_failure_is_bounded_and_safe(installed, tmp_path):
    root, repo = installed
    original = root / "synthetic.txt"
    original.write_text("Synthetic engineer\nPython services.\n", encoding="utf-8")
    offline = DesktopWorker(repo, root, None)
    repo.enqueue_job("scan-for-retry", "scan")
    assert offline.step()
    document = repo.list_documents()[0]
    repo.create_criteria_proposal("cr_python", "Python services.")
    repo.activate_criteria_version(1, "human:test")
    repo.set_decision(document.id, "hold", expected_revision=0, actor="reviewer_test")
    job_id = repo.enqueue_job("analysis-for-retry", "analysis", document_id=document.id, input_versions={"source_revision": document.current_revision, "criteria_version": 1})
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(503, text="synthetic error containing sensitive text must not be exposed")

    config = replace(config_for(tmp_path, root, repo), transport=httpx.MockTransport(unavailable))
    worker = DesktopWorker(repo, root, config)
    for attempt in range(1, 4):
        assert worker.step()
        assert repo.get_job(job_id)["attempts"] == attempt
        if attempt < 3:
            assert worker.step() is False, "Backoff must prevent an immediate retry"
            worker._next_attempt.clear()  # advance the test's retry clock without waiting
    assert repo.get_job(job_id)["state"] == "failed"
    assert worker.step() is False
    assert len(requests) == 3
    assert "sensitive text" not in json.dumps(worker.describe())
    assert repo.get_decision(document.id).disposition.value == "hold"
    assert original.exists()


def test_real_persisted_analysis_runs_after_worker_recreation(installed, tmp_path):
    root, repo = installed
    (root / "synthetic.txt").write_text("Synthetic engineer\nPython services.\n", encoding="utf-8")
    repo.enqueue_job("resume-scan", "scan")
    assert DesktopWorker(repo, root, None).step()
    document = repo.list_documents()[0]
    repo.create_criteria_proposal("cr_python", "Python services.")
    repo.activate_criteria_version(1, "human:test")
    job_id = repo.enqueue_job("resume-analysis", "analysis", document_id=document.id, input_versions={"source_revision": document.current_revision, "criteria_version": 1})
    calls = []

    def respond(request):
        calls.append(request)
        answer = response_content(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    config = replace(config_for(tmp_path, root, repo), transport=httpx.MockTransport(respond))
    first_db = Database(DbConfig(path=repo.db.path), instance_id=repo.instance_id)
    first = DesktopWorker(Repository(first_db), root, config)
    first.repository.db.close()
    restarted_db = Database(DbConfig(path=repo.db.path), instance_id=repo.instance_id)
    restarted = DesktopWorker(Repository(restarted_db), root, config)
    try:
        assert restarted.step()
        assert repo.get_job(job_id)["state"] == "succeeded"
        assert repo.current_profile(document.id) is not None
        assert len(calls) == 1
        assert restarted.step() is False
    finally:
        restarted_db.close()


def test_connecting_does_not_remove_hosted_boundaries(installed, tmp_path):
    root, repo = installed
    original = config_for(tmp_path, root, repo)
    secret = original.secret_path
    secret.write_text("synthetic-rotated-token", encoding="utf-8")
    assert original.read_secret() == "synthetic-rotated-token"
    assert copy.deepcopy(original).read_secret() == "synthetic-rotated-token"


def test_desktop_criteria_ui_posts_real_contract_and_requires_human_approval(installed):
    root, repo = installed
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the desktop criteria UI contract")
    script = r'''
import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const path = process.argv[1];
let source = fs.readFileSync(path, 'utf8');
source += '\nexport { DOM, loadDesktopCriteria, proposeDesktopCriteria, approveDesktopCriteria };';
const ui = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const elements = new Map();
const element = () => ({ textContent: '', value: '', disabled: false, children: [],
  replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); } });
for (const id of ['rr-criteria-input','rr-criteria-status','rr-criteria-draft-list','rr-criteria-propose','rr-criteria-approve']) elements.set(id, element());
elements.get('rr-criteria-input').value = 'Evidence of Python service development.';
ui.DOM.doc = { getElementById: id => elements.get(id) || null, createElement: element };
ui.DOM.state.mode = 'connected'; ui.DOM.state.role = 'administrator';
ui.DOM.state.instanceId = input.instance_id; ui.DOM.state.apiBase = '/api/v1/instances';
ui.DOM.state.csrfToken = 'synthetic-csrf';
const calls = [];
let confirm = false;
ui.DOM.window = { confirm: () => confirm, fetch: async (url, init) => {
  calls.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : null, headers: init.headers });
  return { ok: true, status: 200, json: async () => input.envelope };
}};
await ui.loadDesktopCriteria();
if (input.mode === 'propose') await ui.proposeDesktopCriteria();
if (input.mode === 'approve') {
  await ui.approveDesktopCriteria();
  if (calls.some(call => call.method === 'POST')) throw new Error('approval occurred without confirmation');
  confirm = true;
  await ui.approveDesktopCriteria();
}
console.log(JSON.stringify({ calls, status: elements.get('rr-criteria-status').textContent,
  displayed: elements.get('rr-criteria-draft-list').children.map(item => item.textContent) }));
'''
    app = build_desktop_app(repo, root)
    client = TestClient(app, base_url="http://127.0.0.1")
    _, headers = pair_client(app, client)
    base = f"/api/v1/instances/{repo.instance_id}"

    def run_ui(mode, envelope):
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, str(Path(__file__).resolve().parents[2] / "web/assets/report.js")],
            input=json.dumps({"instance_id": repo.instance_id, "mode": mode, "envelope": envelope}),
            text=True, capture_output=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)

    assert client.get(base + "/criteria").json()["data"]["active_version"] == 0
    draft_ui = run_ui("propose", client.get(base + "/criteria").json())
    proposal_call = next(call for call in draft_ui["calls"] if call["method"] == "POST")
    proposal = client.post(proposal_call["url"], json=proposal_call["body"], headers={**headers, "Idempotency-Key": proposal_call["headers"]["Idempotency-Key"]})
    assert proposal.status_code == 200
    assert repo.active_criteria_version() == 0, "Saving a draft is not approval"
    pending = client.get(base + "/criteria").json()
    approval_ui = run_ui("approve", pending)
    assert any("Draft: Evidence of Python" in text for text in approval_ui["displayed"])
    approval_call = next(call for call in approval_ui["calls"] if call["method"] == "POST")
    assert approval_call["body"] == {"expected_revision": pending["state_revision"]}
    activated = client.post(approval_call["url"], json=approval_call["body"], headers={**headers, "Idempotency-Key": approval_call["headers"]["Idempotency-Key"]})
    assert activated.status_code == 200
    assert repo.active_criteria_version() == 1
    assert client.get(base + "/criteria").json()["data"]["pending_version"] is None
    app.state.desktop_worker.repository.db.close()
