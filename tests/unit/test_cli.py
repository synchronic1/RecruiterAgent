"""Tests for the product CLI (:mod:`resume_review.cli`).

The CLI is exercised in-process through :func:`resume_review.cli.main` with argv
lists, against a real migrated SQLite database, a real deployed bundle, and a real
on-disk job folder. No subprocess is spawned except one test that runs the
installed ``.venv/Scripts/resume-review.exe`` to prove the console script entry
point (which previously died with an ImportError) now works.

Everything runs offline and unprivileged (PRD section 6.4); the API factory is
stubbed for the serve tests so no port is ever bound.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import resume_review.cli as cli
from resume_review import SCHEMA_VERSION
from resume_review.bootstrap import workspace
from resume_review.db import Database, Repository
from resume_review.db.connection import DbConfig
from resume_review.util import seconds_from_now_iso

#: The envelope keys PRD 5.3 requires on every machine-readable result.
REQUIRED_ENVELOPE_KEYS = {
    "ok",
    "code",
    "instance_id",
    "data",
    "warnings",
    "request_id",
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated host registry so setup/status never touch the real host."""
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(tmp_path / "host-registry"))
    # No live analysis route is configured for the deterministic suite.
    for name in cli.LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _job_file(env: Path, text: str = "Operations Manager\nCoordinate crews.\n") -> Path:
    job = env / "job.txt"
    job.write_text(text, encoding="utf-8")
    return job


def _run_json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def _open_repo(root: Path) -> tuple[Database, Repository]:
    db = Database(DbConfig(path=workspace.db_path(root)))
    return db, Repository(db)


@pytest.fixture
def installed(env: Path, capsys: pytest.CaptureFixture[str]) -> SimpleNamespace:
    """A provisioned instance plus the setup result envelope."""
    root = env / "workspace"
    job = _job_file(env)
    code, payload = _run_json(
        capsys, ["setup", "--folder", str(root), "--job", str(job), "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    return SimpleNamespace(
        env=env, root=root, instance_id=payload["instance_id"], setup=payload
    )


def _scan_file(
    installed: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    name: str = "alex.txt",
    text: str = "Alex Doe\nOperations Manager\nManaged twelve electricians.\n",
) -> tuple[int, dict]:
    (installed.root / name).write_text(text, encoding="utf-8")
    return _run_json(capsys, ["scan", "--instance", installed.instance_id, "--json"])


# ---------------------------------------------------------------------------
# Entry point and exit-code table
# ---------------------------------------------------------------------------
def test_console_script_help_runs() -> None:
    """The declared console script must start instead of raising ImportError."""
    exe_name = "resume-review.exe" if os.name == "nt" else "resume-review"
    exe = Path(sys.executable).with_name(exe_name)
    if not exe.exists():
        pytest.skip(f"console script not installed at {exe}")
    proc = subprocess.run(
        [str(exe), "--help"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert "apply-actions" in proc.stdout
    assert "exit codes" in proc.stdout


def test_no_command_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([])
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert "COMMAND" in capsys.readouterr().out


def test_unknown_command_uses_argparse_exit_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["frobnicate"])
    assert excinfo.value.code == int(cli.ExitCode.INVALID_INPUT)


def test_exit_codes_match_the_error_table() -> None:
    from resume_review.errors import ExitCode as ErrorExitCode

    for name in ("OK", "UNEXPECTED", "INVALID_INPUT", "PERMISSION", "CONFLICT",
                 "DEPENDENCY", "UNSUPPORTED_STORAGE"):
        assert int(getattr(cli.ExitCode, name)) == int(getattr(ErrorExitCode, name))


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def test_setup_creates_instance_and_envelope(installed: SimpleNamespace) -> None:
    data = installed.setup
    assert REQUIRED_ENVELOPE_KEYS <= set(data)
    assert data["ok"] is True
    assert data["instance_id"] == installed.instance_id
    assert data["instance_id"].startswith("inst_")
    assert data["data"]["created"] is True
    # Root is described by a label, never an absolute path, in the envelope.
    assert data["data"]["root_label"] == "workspace"


def test_setup_rejects_missing_job_file(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(
        capsys,
        ["setup", "--folder", str(env / "ws"), "--job", str(env / "nope.txt"), "--json"],
    )
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "INVALID_INPUT"
    assert REQUIRED_ENVELOPE_KEYS <= set(payload)


def _frozen_schema_version_matches_migrations() -> bool:
    """Whether the frozen migration layer agrees with its own schema version.

    A migration file newer than ``SCHEMA_VERSION`` makes ``assert_downgrade_allowed``
    refuse every reopen, so repeat setup cannot be exercised. This is a defect in a
    file this task does not own (see ``blocked_on_frozen_file`` in the task report);
    the guard skips rather than silently accepting a broken repeat setup, and stops
    skipping the moment the version is corrected.
    """
    from resume_review import SCHEMA_VERSION
    from resume_review.db.migrations import discover_migrations

    return all(m.version <= SCHEMA_VERSION for m in discover_migrations())


def test_repeat_setup_preserves_human_state(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _frozen_schema_version_matches_migrations():
        pytest.skip(
            "frozen migration layer is inconsistent: an installed migration is newer "
            "than SCHEMA_VERSION, so assert_downgrade_allowed refuses repeat setup"
        )
    code, _ = _scan_file(installed, capsys)
    assert code == int(cli.ExitCode.OK)

    db, repo = _open_repo(installed.root)
    doc = repo.get_document_by_path("alex.txt")
    assert doc is not None
    repo.set_decision(doc.id, "hold", 0, "local-owner")
    db.close()

    job = _job_file(installed.env)
    code, payload = _run_json(
        capsys, ["setup", "--folder", str(installed.root), "--job", str(job), "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["instance_id"] == installed.instance_id
    assert payload["data"]["created"] is False
    assert payload["data"]["preserved"] is True
    assert payload["data"]["counts_after"]["decisions_set"] == 1

    # The decision survived the repeat setup.
    db, repo = _open_repo(installed.root)
    assert repo.get_decision(doc.id).disposition.value == "hold"
    db.close()


def test_setup_refuses_downgrade(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = workspace.db_path(installed.root)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE schema_migrations SET version = version + 10")
    conn.commit()
    conn.close()

    job = _job_file(installed.env)
    code, payload = _run_json(
        capsys, ["setup", "--folder", str(installed.root), "--job", str(job), "--json"]
    )
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "DOWNGRADE_REFUSED"
    assert payload["error"]["detail"]["database_version"] > SCHEMA_VERSION


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_is_json_by_default(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["status", "--instance", installed.instance_id])
    assert code == int(cli.ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert REQUIRED_ENVELOPE_KEYS <= set(payload)
    assert payload["instance_id"] == installed.instance_id
    assert "counts" in payload["data"]
    assert payload["data"]["counts"]["total"] == 0


def test_status_unknown_instance_is_invalid_input(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(capsys, ["status", "--instance", "inst_missing"])
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "INSTANCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# scan / render
# ---------------------------------------------------------------------------
def test_scan_registers_documents(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _scan_file(installed, capsys)
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["scan"]["discovered"] == 1
    assert payload["data"]["scan"]["created"] == 1
    assert payload["data"]["counts"]["total"] == 1
    # No approved criteria and no route: the document is visible for manual review.
    assert payload["data"]["scan"]["awaiting_criteria"] == 1


def test_render_writes_snapshot(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_json(
        capsys, ["render", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["published"] is True
    assert (installed.root / "review.html").is_file()
    assert payload["data"]["report_file"] == "review.html"


# ---------------------------------------------------------------------------
# plan-actions / apply-actions
# ---------------------------------------------------------------------------
def _prepare_rejectable(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> str:
    code, _ = _scan_file(installed, capsys)
    assert code == int(cli.ExitCode.OK)
    db, repo = _open_repo(installed.root)
    doc = repo.get_document_by_path("alex.txt")
    assert doc is not None
    repo.set_decision(doc.id, "reject", 0, "local-owner")
    db.close()
    return doc.id


def _write_request(installed: SimpleNamespace, payload: dict) -> Path:
    path = installed.env / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_plan_actions_builds_planned_batch(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    doc_id = _prepare_rejectable(installed, capsys)
    request = _write_request(
        installed, {"document_ids": [doc_id], "intents": {doc_id: "move_rejected"}}
    )
    code, payload = _run_json(
        capsys,
        ["plan-actions", "--instance", installed.instance_id, "--request", str(request), "--json"],
    )
    assert code == int(cli.ExitCode.OK)
    plan = payload["data"]["plan"]
    assert len(plan["operations"]) == 1
    assert plan["operations"][0]["kind"] == "move_rejected"
    batch_id = payload["data"]["batch_id"]
    assert payload["data"]["execution_state"] == "planned"

    db, repo = _open_repo(installed.root)
    batch = repo.get_batch(batch_id)
    assert batch is not None
    assert batch["execution_state"] == "planned"
    # The durable per-operation rows exist before any file is touched.
    assert len(repo.list_file_operations(batch_id)) == 1
    db.close()


def test_plan_actions_refuses_caller_actor(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    doc_id = _prepare_rejectable(installed, capsys)
    request = _write_request(installed, {"document_ids": [doc_id], "actor": "someone"})
    code, payload = _run_json(
        capsys,
        ["plan-actions", "--instance", installed.instance_id, "--request", str(request), "--json"],
    )
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "INVALID_INPUT"


def test_plan_actions_requires_document_ids(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _write_request(installed, {"document_ids": []})
    code, payload = _run_json(
        capsys,
        ["plan-actions", "--instance", installed.instance_id, "--request", str(request), "--json"],
    )
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "INVALID_INPUT"


def _planned_batch(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> tuple[str, str, str]:
    doc_id = _prepare_rejectable(installed, capsys)
    request = _write_request(
        installed, {"document_ids": [doc_id], "intents": {doc_id: "move_rejected"}}
    )
    code, payload = _run_json(
        capsys,
        ["plan-actions", "--instance", installed.instance_id, "--request", str(request), "--json"],
    )
    assert code == int(cli.ExitCode.OK)
    return payload["data"]["batch_id"], payload["data"]["plan"]["plan_hash"], doc_id


def test_apply_actions_refuses_without_recorded_approval(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CLI invocation cannot manufacture human approval (PRD 5.3, 13.1)."""
    batch_id, _, _ = _planned_batch(installed, capsys)
    code, payload = _run_json(
        capsys,
        ["apply-actions", "--instance", installed.instance_id, "--batch", batch_id, "--json"],
    )
    assert code == int(cli.ExitCode.CONFLICT)
    assert payload["code"] == "APPROVAL_REQUIRED"
    assert payload["ok"] is False
    # Nothing moved.
    db, repo = _open_repo(installed.root)
    doc = repo.get_document_by_path("alex.txt")
    assert doc is not None and doc.location.value == "active"
    db.close()


def test_apply_actions_succeeds_with_recorded_approval(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    batch_id, plan_hash, doc_id = _planned_batch(installed, capsys)

    db, repo = _open_repo(installed.root)
    repo.approve_batch(batch_id, "local-owner", plan_hash, seconds_from_now_iso(900))
    db.close()

    code, payload = _run_json(
        capsys,
        ["apply-actions", "--instance", installed.instance_id, "--batch", batch_id, "--json"],
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["ok"] is True
    assert payload["data"]["state"] == "completed"
    assert payload["data"]["counts"]["moved"] == 1

    db, repo = _open_repo(installed.root)
    doc = repo.get_document(doc_id)
    db.close()
    assert doc is not None
    assert doc.location.value == "rejected"
    assert (installed.root / "Rejected" / doc_id / "alex.txt").is_file()
    assert not (installed.root / "alex.txt").exists()


def test_apply_actions_unknown_batch_is_invalid_input(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(
        capsys,
        ["apply-actions", "--instance", installed.instance_id, "--batch", "batch_nope", "--json"],
    )
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# backup / repair / stop
# ---------------------------------------------------------------------------
def test_backup_creates_verified_copy(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(
        capsys, ["backup", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["verified"] is True
    backup_file = workspace.backups_dir(installed.root) / payload["data"]["backup_file"]
    assert backup_file.is_file()
    assert payload["data"]["byte_size"] > 0


def test_repair_dry_run_reports_no_mutation(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(
        capsys, ["repair", "--instance", installed.instance_id, "--dry-run", "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["mutating"] is False
    # Root is reported by label, never as an absolute path.
    assert payload["data"]["root"] == installed.root.name


def test_stop_reports_not_running(installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_json(
        capsys, ["stop", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["running"] is False


def test_stop_refuses_while_a_helper_holds_the_lock(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from resume_review.bootstrap.ownership import InstanceLock

    lock = InstanceLock(workspace.owner_lock_path(installed.root), instance_id=installed.instance_id)
    lock.acquire()
    try:
        code, payload = _run_json(
            capsys, ["stop", "--instance", installed.instance_id, "--json"]
        )
        # The frozen error table classifies a held lock as a permission failure.
        assert code == int(cli.ExitCode.PERMISSION)
        assert payload["code"] == "INSTANCE_LOCKED_BY_OTHER_OWNER"
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# summarize / start honest failures
# ---------------------------------------------------------------------------
def test_summarize_without_route_is_dependency_failure(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(
        capsys,
        ["summarize", "--instance", installed.instance_id, "--changed-only", "--json"],
    )
    assert code == int(cli.ExitCode.DEPENDENCY)
    assert payload["code"] == "ROUTE_UNAVAILABLE"
    assert REQUIRED_ENVELOPE_KEYS <= set(payload)


def test_start_reports_dependency_failure_when_api_absent(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_load_api_app", lambda: None)
    code, payload = _run_json(
        capsys, ["start", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.DEPENDENCY)
    assert payload["code"] == "ROUTE_UNAVAILABLE"


def test_start_serves_the_installed_app_without_binding_a_port(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an API factory is installed, start hands it a repository and serves it."""
    captured: dict = {}

    def _fake_factory(repository, **kwargs):
        from resume_review.api import create_app

        captured["repository"] = repository
        return create_app(repository, **kwargs)

    monkeypatch.setattr(cli, "_load_api_app", lambda: _fake_factory)
    monkeypatch.setattr(cli, "_serve", lambda app, *, host, port: captured.update(app=app, host=host, port=port))

    code, payload = _run_json(
        capsys, ["start", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.OK)
    assert payload["data"]["served"] is True
    assert payload["data"]["host"] == "127.0.0.1"
    assert captured["repository"].instance_id == installed.instance_id


# ---------------------------------------------------------------------------
# Envelope contract
# ---------------------------------------------------------------------------
def test_error_envelope_shape(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure still carries the full six-key envelope plus an error object."""
    code, payload = _run_json(capsys, ["status", "--instance", "inst_nope", "--json"])
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert REQUIRED_ENVELOPE_KEYS <= set(payload)
    assert payload["ok"] is False
    assert payload["error"]["code"] == payload["code"]
    assert payload["error"]["message"]
    assert payload["warnings"] == []
