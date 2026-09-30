"""Exercise the installed image offline with disposable synthetic PDF fixtures."""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

SKILL = Path("/opt/plow/skills/recruiteragent")
UPSTREAM_SHA256 = "5be521644ade0f041e83370ac457edc8ad85410e14265f1b1243807772de9a5b"


def gateway_check():
    probe = subprocess.run(["/opt/plow/probe"], capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0 and "PLOW_PROBE_OK" in probe.stdout, probe.stdout + probe.stderr
    env = dict(os.environ, PLOW_AGENT_TOKEN="synthetic-probe-only")
    result = subprocess.run(["openclaw", "skills", "info", "recruiteragent", "--json"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    skill = json.loads(result.stdout)
    assert all(skill[key] for key in ("eligible", "modelVisible", "userInvocable")), skill
    print(json.dumps({"ok": True, "gateway_probe": "PLOW_PROBE_OK",
                      "skill_eligible": skill["eligible"], "skill_model_visible": skill["modelVisible"],
                      "fixture_only": True, "live_plow_messaging_tested": False}))


def main():
    manifest = json.loads((SKILL / "package-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((SKILL / name).read_bytes()).hexdigest() == expected, name
    assert hashlib.sha256(Path("/opt/plow/agent-index-client.py").read_bytes()).hexdigest() == UPSTREAM_SHA256
    assert os.environ["AGENT_ID"] == "recruiteragent"
    subprocess.run([sys.executable, str(SKILL / "scripts/install.py"), "--verify-only"], check=True)
    demo = SKILL / "application/docs/review-artifacts"
    assert (demo / "recruiteragent-design-preview.html").is_file()
    assert len(list((demo / "synthetic-resumes").glob("*.pdf"))) == 200
    from reportlab.pdfgen import canvas
    with tempfile.TemporaryDirectory(prefix="recruiteragent-image-smoke-") as temporary:
        root = Path(temporary)
        job = root / "requisition.txt"
        job.write_text("Software Engineer\nPython and SQL experience.\n", encoding="utf-8")
        folder = root / "job"
        folder.mkdir()
        env = dict(os.environ, RESUME_REVIEW_REGISTRY_DIR=str(root / "registry"))
        for name in tuple(env):
            if name.startswith("RESUME_REVIEW_LIVE_"):
                del env[name]

        def run(*arguments):
            result = subprocess.run([sys.executable, "-I", "-m", "resume_review.cli",
                                     *arguments, "--json"], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr + result.stdout
            payload = json.loads(result.stdout)
            assert payload["ok"], payload
            return payload

        setup = run("setup", "--folder", str(folder), "--job", str(job))
        instance = setup["instance_id"]
        originals = {}
        for number in (1, 2):
            path = folder / f"synthetic-{number}.pdf"
            pdf = canvas.Canvas(str(path))
            pdf.drawString(72, 740, f"Synthetic Applicant {number}: Python and SQL engineer")
            pdf.save()
            originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        run("scan", "--instance", instance)
        run("status", "--instance", instance)
        run("render", "--instance", instance)
        repeated = run("setup", "--folder", str(folder), "--job", str(job))
        assert repeated["instance_id"] == instance
        with sqlite3.connect((folder / ".review/review.db").resolve().as_uri() + "?mode=ro", uri=True) as db:
            assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
        report = (folder / "review.html").read_text(encoding="utf-8")
        assert "RecruiterAgent" in report
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())
    print(json.dumps({"ok": True, "fixture_only": True, "inference_performed": False,
                      "originals_unchanged": True, "synthetic_preview_count": 200,
                      "setup_scan_status_render_repeat_setup": "passed",
                      "inherited_reporter_checksum": "verified"}))


if __name__ == "__main__":
    gateway_check() if sys.argv[1:] == ["--gateway"] else main()
