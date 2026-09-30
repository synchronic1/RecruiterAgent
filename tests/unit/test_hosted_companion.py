"""Hosted owner bridge drives the real connected API without widget rendering."""
import re

import pytest
from fastapi.testclient import TestClient

from resume_review.bootstrap.setup import setup_instance
from resume_review.hosted import create_hosted_app

ORIGIN = "https://hosted.example.test"
SECRET = "ab" * 32
HEADERS = {"x-recruiteragent-bridge": SECRET, "x-recruiteragent-owner": "owner-test"}


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(tmp_path / "registry"))
    root = tmp_path / "applications"
    result = setup_instance(root, "Software engineer: Python services")
    preview = tmp_path / "demo.html"
    preview.write_text("<html>Synthetic preview only</html>", encoding="utf-8")
    app = create_hosted_app(bridge_secret=SECRET, public_origin=ORIGIN, preview_path=preview)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, result.instance_id, root


def test_owner_bridge_and_demo_are_private(hosted):
    client, instance, root = hosted
    assert client.get("/recruiteragent").status_code == 401
    assert client.get("/recruiteragent/demo").status_code == 401
    assert client.get("/recruiteragent", headers={**HEADERS, "x-recruiteragent-bridge": "wrong"}).status_code == 401
    landing = client.get("/recruiteragent", headers=HEADERS)
    assert landing.status_code == 200
    assert instance in landing.text and SECRET not in landing.text
    assert str(root) not in landing.text
    assert landing.headers["cache-control"] == "no-store"
    demo = client.get("/recruiteragent/demo", headers=HEADERS)
    assert demo.status_code == 200
    assert "connect-src 'none'" in demo.headers["content-security-policy"]


def test_connected_session_origin_csrf_and_owner_binding(hosted):
    client, instance, root = hosted
    base = f"/api/v1/instances/{instance}"
    assert client.get(base + "/documents", headers=HEADERS).status_code == 401
    page = client.get(base + "/review", headers=HEADERS)
    assert page.status_code == 200
    assert '"mode":"connected"' in page.text
    assert SECRET not in page.text
    assert "Secure" in page.headers["set-cookie"]
    assert "HttpOnly" in page.headers["set-cookie"]
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert client.get(base + "/assets/report.js", headers=HEADERS).status_code == 200
    assert client.get(base + "/connection", headers=HEADERS).json()["data"]["configured"] is False
    assert client.post(base + "/scan", json={}, headers=HEADERS).status_code == 403
    assert client.post(base + "/scan", json={}, headers={**HEADERS, "Origin": ORIGIN}).status_code == 403
    headers = {**HEADERS, "Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "hosted-scan-test"}
    assert client.post(base + "/scan", json={}, headers=headers).status_code == 202
    assert client.post(base + "/scan", json={}, headers={**headers, "Origin": "https://foreign.test"}).status_code == 403
    assert client.get(base + "/documents", headers={**HEADERS, "x-recruiteragent-owner": "another-owner"}).status_code == 403
    assert not (root / "Rejected").exists() or not list((root / "Rejected").iterdir())


def test_unknown_instance_is_not_a_filesystem_selector(hosted):
    client, instance, root = hosted
    response = client.get("/api/v1/instances/inst_abcdef/review", headers=HEADERS)
    assert response.status_code in (400, 404)
    assert str(root) not in response.text


@pytest.mark.parametrize("origin", ["http://public.example.test", "https://owner:secret@host.test", "https://host.test/path"])
def test_unsafe_public_origins_refused(origin):
    with pytest.raises(ValueError):
        create_hosted_app(bridge_secret=SECRET, public_origin=origin)
