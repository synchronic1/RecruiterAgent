"""Adversarial verification of the CLI, the layering test, packaging, and the
real-corpus gate.

This module does not restate the interface; it tries to break four claims.

1.  **The CLI cannot manufacture human approval.** It drives ``apply-actions``
    with no approval, an expired approval, an approval bound to a different
    plan, and an approval that belongs to another batch, and asserts every one
    is refused with exit code 4 and that no file moves. It also asserts the CLI
    exposes no ``approve`` command, cannot be handed an actor or an approval in
    request data, and never receives an executor credential.
2.  **The layering test is not vacuous.** It copies ``src/`` to a temp directory
    outside the repository, plants one forbidden import for each rule, and shows
    the checker fails for each. The repository tree itself is never modified.
3.  **Packaging.** The console-script target imports, the console script runs,
    the declared ``package-data`` globs resolve, and every asset a non-editable
    install needs is discoverable.
4.  **The corpus gate is closed by default.** A subprocess run of
    ``tests/corpus`` installs a ``sys.addaudithook`` recorder and asserts that
    *no* file under ``resume/`` was opened. A positive control proves the
    recorder actually observes a resume read.

Plus a table driving each documented process exit code and comparing it with
the observed one.

Nothing here reads real applicant text into a fixture or writes it anywhere.
The audit run uses the corpus conftest's own default state (flag unset).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import resume_review.cli as cli
from resume_review.bootstrap import workspace
from resume_review.db import Database, Repository
from resume_review.db.connection import DbConfig
from resume_review.util import seconds_from_now_iso

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PLUGIN_DIR = REPO_ROOT / "tests" / "corpus"
CORPUS_ROOT = REPO_ROOT / "resume"


# ===========================================================================
# Fixtures and helpers
# ===========================================================================
@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated host registry so setup/status never touch the real host."""
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(tmp_path / "host-registry"))
    for name in cli.LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def installed(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> SimpleNamespace:
    """A provisioned instance, created through the real ``setup`` command."""
    root = cli_env / "workspace"
    job = cli_env / "job.txt"
    job.write_text("Operations Manager\nCoordinate crews.\n", encoding="utf-8")
    code = cli.main(
        ["setup", "--folder", str(root), "--job", str(job), "--json"]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == int(cli.ExitCode.OK), payload
    assert payload["ok"] is True
    return SimpleNamespace(env=cli_env, root=root, instance_id=payload["instance_id"])


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def _open_repo(root: Path) -> tuple[Database, Repository]:
    db = Database(DbConfig(path=workspace.db_path(root)))
    return db, Repository(db)


def _scan_and_reject(
    installed: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    names: tuple[str, ...] = ("alex.txt",),
) -> dict[str, str]:
    """Write text resumes, scan them, and mark each Reject. Returns ids by name."""
    for name in names:
        (installed.root / name).write_text(
            f"{name}\nOperations Manager\nManaged twelve electricians.\n",
            encoding="utf-8",
        )
    code, payload = _run(
        capsys, ["scan", "--instance", installed.instance_id, "--json"]
    )
    assert code == int(cli.ExitCode.OK), payload
    db, repo = _open_repo(installed.root)
    ids: dict[str, str] = {}
    for name in names:
        doc = repo.get_document_by_path(name)
        assert doc is not None
        repo.set_decision(doc.id, "reject", 0, "local-owner")
        ids[name] = doc.id
    db.close()
    return ids


def _plan(
    installed: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    doc_id: str,
    *,
    tag: str = "a",
) -> tuple[str, str]:
    request = installed.env / f"request-{tag}-{uuid.uuid4().hex[:8]}.json"
    request.write_text(
        json.dumps({"document_ids": [doc_id], "intents": {doc_id: "move_rejected"}}),
        encoding="utf-8",
    )
    code, payload = _run(
        capsys,
        [
            "plan-actions",
            "--instance",
            installed.instance_id,
            "--request",
            str(request),
            "--json",
        ],
    )
    assert code == int(cli.ExitCode.OK), payload
    return payload["data"]["batch_id"], payload["data"]["plan"]["plan_hash"]


def _apply(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str], batch_id: str, *extra: str
) -> tuple[int, dict]:
    return _run(
        capsys,
        [
            "apply-actions",
            "--instance",
            installed.instance_id,
            "--batch",
            batch_id,
            *extra,
            "--json",
        ],
    )


def _approve(root: Path, batch_id: str, plan_hash: str, *, expires_in: float = 900.0) -> None:
    db, repo = _open_repo(root)
    repo.approve_batch(batch_id, "local-owner", plan_hash, seconds_from_now_iso(expires_in))
    db.close()


def _location(root: Path, doc_id: str) -> str:
    db, repo = _open_repo(root)
    doc = repo.get_document(doc_id)
    db.close()
    assert doc is not None
    return doc.location.value


def _no_file_was_moved(root: Path) -> bool:
    """True when no regular file sits under the Rejected or Trash trees.

    The reserved directories exist from ``ensure_layout``; a refusal must leave
    them empty, which a bare ``.exists()`` check cannot distinguish.
    """
    for reserved in ("Rejected", "Trash"):
        tree = root / reserved
        if tree.is_dir() and any(path.is_file() for path in tree.rglob("*")):
            return False
    return True


# ===========================================================================
# 1. The CLI cannot manufacture approval
# ===========================================================================
def test_apply_without_approval_is_refused_and_moves_nothing(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    batch_id, _ = _plan(installed, capsys, ids["alex.txt"])
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.CONFLICT), payload
    assert payload["code"] == "APPROVAL_REQUIRED"
    assert payload["ok"] is False
    assert _location(installed.root, ids["alex.txt"]) == "active"
    assert _no_file_was_moved(installed.root)
    assert (installed.root / "alex.txt").is_file()


def test_plan_actions_records_no_approval_whatsoever(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    batch_id, _ = _plan(installed, capsys, ids["alex.txt"])
    db, repo = _open_repo(installed.root)
    batch = repo.get_batch(batch_id)
    db.close()
    assert batch is not None
    assert batch["execution_state"] == "planned"
    assert not batch.get("approval_actor")
    assert not batch.get("approval_time")
    assert not batch.get("approval_expires_at")


def test_expired_approval_is_refused(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    batch_id, plan_hash = _plan(installed, capsys, ids["alex.txt"])
    _approve(installed.root, batch_id, plan_hash, expires_in=-60.0)
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.CONFLICT), payload
    assert payload["code"] == "APPROVAL_EXPIRED"
    assert _location(installed.root, ids["alex.txt"]) == "active"


def test_approval_for_a_different_plan_is_refused(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """An approval is bound to a plan hash; a changed plan is a different plan."""
    ids = _scan_and_reject(installed, capsys)
    batch_id, plan_hash = _plan(installed, capsys, ids["alex.txt"])
    _approve(installed.root, batch_id, plan_hash)

    # Tamper with the stored plan after approval but keep the stored plan_hash.
    db_path = workspace.db_path(installed.root)
    conn = sqlite3.connect(str(db_path))
    raw = conn.execute(
        "SELECT plan_json FROM action_batches WHERE id = ?", (batch_id,)
    ).fetchone()[0]
    plan = json.loads(raw)
    assert plan["operations"], plan
    original_destination = plan["operations"][0]["destination"]
    plan["operations"][0]["destination"] = "Rejected/other/subdir/alex.txt"
    conn.execute(
        "UPDATE action_batches SET plan_json = ? WHERE id = ?",
        (json.dumps(plan), batch_id),
    )
    conn.commit()
    conn.close()
    assert plan["operations"][0]["destination"] != original_destination

    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.CONFLICT), payload
    assert payload["code"] == "APPROVAL_REQUIRED"
    assert _location(installed.root, ids["alex.txt"]) == "active"
    assert _no_file_was_moved(installed.root)
    assert (installed.root / "alex.txt").is_file()


def test_approval_does_not_transfer_to_a_different_batch(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys, names=("alex.txt", "bob.txt"))
    batch_a, hash_a = _plan(installed, capsys, ids["alex.txt"], tag="a")
    batch_b, hash_b = _plan(installed, capsys, ids["bob.txt"], tag="b")
    assert batch_a != batch_b and hash_a != hash_b

    _approve(installed.root, batch_a, hash_a)  # approve only batch A

    code, payload = _apply(installed, capsys, batch_b)
    assert code == int(cli.ExitCode.CONFLICT), payload
    assert payload["code"] == "APPROVAL_REQUIRED"
    assert _location(installed.root, ids["bob.txt"]) == "active"


def test_approving_a_mismatched_hash_records_nothing(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """``approve_batch`` must reject a hash that is not the stored plan's."""
    ids = _scan_and_reject(installed, capsys)
    batch_id, plan_hash = _plan(installed, capsys, ids["alex.txt"])
    db, repo = _open_repo(installed.root)
    with pytest.raises(Exception) as excinfo:
        repo.approve_batch(batch_id, "local-owner", "hash-that-is-not-the-plan", seconds_from_now_iso(900))
    db.close()
    assert getattr(excinfo.value, "code", "") == "PLAN_HASH_MISMATCH"
    # No approval leaked; the executor still refuses.
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.CONFLICT)
    assert payload["code"] == "APPROVAL_REQUIRED"
    # And the real hash still works, proving the batch is otherwise approvable.
    _approve(installed.root, batch_id, plan_hash)
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.OK), payload


def test_dry_run_without_approval_is_also_refused(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    batch_id, _ = _plan(installed, capsys, ids["alex.txt"])
    code, payload = _apply(installed, capsys, batch_id, "--dry-run")
    assert code == int(cli.ExitCode.CONFLICT), payload
    assert payload["code"] == "APPROVAL_REQUIRED"


def test_replayed_apply_after_completion_moves_nothing_again(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    batch_id, plan_hash = _plan(installed, capsys, ids["alex.txt"])
    _approve(installed.root, batch_id, plan_hash)
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.OK), payload
    assert payload["data"]["counts"]["moved"] == 1
    moved_to = installed.root / "Rejected" / ids["alex.txt"] / "alex.txt"
    assert moved_to.is_file()
    before = moved_to.read_bytes()

    # A replay is a no-op: the batch is terminal and the file is not touched.
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.OK), payload
    assert payload["data"]["state"] == "completed"
    assert moved_to.read_bytes() == before
    assert not (installed.root / "alex.txt").exists()


def test_cli_exposes_no_approve_command() -> None:
    parser = cli.build_parser()
    choices = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
    assert not any("approve" in name for name in choices), choices
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["approve", "--instance", "x", "--batch", "b"])
    assert int(excinfo.value.code) == int(cli.ExitCode.INVALID_INPUT)


def test_request_data_cannot_supply_an_actor_or_an_approval(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _scan_and_reject(installed, capsys)
    for field in ("actor", "requested_by", "approval", "approved_by"):
        request = installed.env / f"bad-{field}.json"
        request.write_text(
            json.dumps({"document_ids": [ids["alex.txt"]], field: "local-owner"}),
            encoding="utf-8",
        )
        code, payload = _run(
            capsys,
            [
                "plan-actions",
                "--instance",
                installed.instance_id,
                "--request",
                str(request),
                "--json",
            ],
        )
        assert code == int(cli.ExitCode.INVALID_INPUT), (field, payload)
        assert payload["code"] == "INVALID_INPUT"
        assert payload["error"]["detail"].get("field") == field


def test_apply_uses_a_fixed_principal_not_request_data() -> None:
    """The apply handler cannot be steered by a caller-supplied identity or hash."""
    source = inspect.getsource(cli._cmd_apply_actions)
    assert "_local_principal(" in source
    assert "args.actor" not in source
    assert "args.approval" not in source
    assert cli.LOCAL_ACTOR and not cli.LOCAL_ACTOR.startswith(("agent:", "worker:"))


def test_cli_receives_no_executor_credential() -> None:
    """No CLI argument, handler, or output channel carries an executor secret.

    The only credential-adjacent name is the analysis route's secret *file path*;
    it is passed to the adapter as ``secret_path`` and is never a value the CLI
    parses, prints, or records as an approver.
    """
    parser = cli.build_parser()
    dests: set[str] = set()
    for action in parser._subparsers._group_actions[0].choices.values():  # type: ignore[attr-defined]
        dests.update(a.dest for a in action._actions)
    forbidden = {"token", "secret", "credential", "password", "approval_token", "executor"}
    assert not (dests & forbidden), dests & forbidden

    source = inspect.getsource(cli)
    assert "secret_path=" in source
    assert "secret=" not in source.replace("secret_path=", "")
    # Every live-route variable the CLI names is a file path or a label, by name.
    assert all(name.endswith(("_FILE", "_URL", "_ID", "_ROUTE", "_ATTESTATION", "_RECORD")) for name in cli.LIVE_ENV)
    # With no route configured, no adapter (and no credential) is constructed.
    assert cli._route_from_env() is None


def test_no_cli_command_can_make_the_analysis_agent_execute_actions() -> None:
    """``summarize`` builds an inference client; it never reaches the executor."""
    summarize = inspect.getsource(cli._cmd_summarize)
    assert "apply_batch" not in summarize
    assert "executor" not in summarize
    plan = inspect.getsource(cli._cmd_plan_actions)
    assert "apply_batch" not in plan
    # apply-actions is the only entry point that reaches the executor.
    reach_executor = [
        name
        for name, handler in cli._HANDLERS.items()
        if "apply_batch" in inspect.getsource(handler)
    ]
    assert reach_executor == ["apply-actions"], reach_executor


# ===========================================================================
# 2. Layering test integrity (non-vacuous, proven by planted violations)
# ===========================================================================
def _load_layering_module() -> object:
    path = REPO_ROOT / "tests" / "unit" / "test_layering.py"
    spec = importlib.util.spec_from_file_location("_adv_layering_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_layering_walk_finds_a_plausible_module_count() -> None:
    module = _load_layering_module()
    modules = module.discovered_modules()  # type: ignore[attr-defined]
    assert len(modules) >= module.MINIMUM_MODULES  # type: ignore[attr-defined]
    assert len(modules) > 0
    for layer in (
        "resume_review.db",
        "resume_review.storage",
        "resume_review.actions",
        "resume_review.analysis",
        "resume_review.security",
    ):
        assert any(module._under(name, layer) for name in modules), layer  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "importer_root, forbidden_root",
    [
        ("resume_review.db", "resume_review.api"),
        ("resume_review.storage", "resume_review.api"),
        ("resume_review.actions", "resume_review.openclaw_adapter"),
        ("resume_review.analysis", "resume_review.actions"),
        ("resume_review.storage", "resume_review.db"),
        ("resume_review.storage", "resume_review.actions"),
        ("resume_review.security", "resume_review.api"),
        ("resume_review.security", "resume_review.actions"),
    ],
)
def test_layering_checker_fails_on_each_planted_violation(
    importer_root: str,
    forbidden_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy the source tree OUTSIDE the repository and plant one violation.

    The repository tree is never touched: the copy is the only thing mutated, so
    there is nothing to restore. If the checker passed here it would be vacuous.
    """
    module = _load_layering_module()
    edges = module.FORBIDDEN_EDGES  # type: ignore[attr-defined]
    assert (importer_root, forbidden_root) in edges

    copy_root = tmp_path / "outside_repo" / "src"
    shutil.copytree(REPO_ROOT / "src", copy_root)

    monkeypatch.setattr(module, "SOURCE_ROOT", copy_root)
    monkeypatch.setattr(module, "PACKAGE_ROOT", copy_root / "resume_review")
    module.build_import_graph.cache_clear()  # type: ignore[attr-defined]

    # Control: the untouched copy has no violation for this rule.
    module.test_forbidden_import_edges(importer_root, forbidden_root)  # type: ignore[attr-defined]

    modules = module.discovered_modules()  # type: ignore[attr-defined]
    victim_name = next(
        name for name in modules if module._under(name, importer_root)  # type: ignore[attr-defined]
    )
    victim = modules[victim_name]
    original = victim.read_text(encoding="utf-8")
    victim.write_text(
        original + f"\nfrom {forbidden_root} import _planted_probe\n",
        encoding="utf-8",
    )
    module.build_import_graph.cache_clear()  # type: ignore[attr-defined]

    with pytest.raises(AssertionError) as excinfo:
        module.test_forbidden_import_edges(importer_root, forbidden_root)  # type: ignore[attr-defined]
    message = str(excinfo.value)
    assert forbidden_root in message
    assert victim_name in message

    # And the checker finds it with an import nested inside a function too.
    victim.write_text(
        original
        + "\ndef _nested():\n"
        + f"    from {forbidden_root} import _planted_nested\n",
        encoding="utf-8",
    )
    module.build_import_graph.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        module.test_forbidden_import_edges(importer_root, forbidden_root)  # type: ignore[attr-defined]

    victim.write_text(original, encoding="utf-8")


def test_layering_checker_resolves_relative_and_nested_imports() -> None:
    module = _load_layering_module()
    collect = module.collect_imports  # type: ignore[attr-defined]
    # A function-local relative import, resolved to an absolute dotted name.
    imported = collect("def f():\n    from ..api import routes\n", "resume_review.db.repository")
    assert {i.target for i in imported} >= {"resume_review.api", "resume_review.api.routes"}
    # ``from pkg import sub`` records the subpackage, not only the member.
    imported = collect("from resume_review import api\n", "resume_review.util")
    assert any(i.target == "resume_review.api" for i in imported)
    # TYPE_CHECKING and try/except bodies are both visited.
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from resume_review.actions import executor\n"
        "try:\n"
        "    from resume_review.actions import planner\n"
        "except ImportError:\n"
        "    pass\n"
    )
    targets = {i.target for i in collect(source, "resume_review.analysis.chat")}
    assert {"resume_review.actions", "resume_review.actions.executor", "resume_review.actions.planner"} <= targets


# ===========================================================================
# 3. Packaging
# ===========================================================================
def test_console_script_target_imports_and_is_callable() -> None:
    import importlib

    module = importlib.import_module("resume_review.cli")
    assert callable(module.main)


def test_console_script_executable_runs() -> None:
    exe_name = "resume-review.exe" if os.name == "nt" else "resume-review"
    exe = Path(sys.executable).with_name(exe_name)
    if not exe.exists():
        pytest.skip(f"console script not installed at {exe}")
    proc = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    for command in ("setup", "apply-actions", "plan-actions", "repair"):
        assert command in proc.stdout
    assert "exit codes" in proc.stdout


def test_package_data_globs_resolve_to_real_files() -> None:
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]
    assert package_data, "no package-data declared"
    base = REPO_ROOT / "src" / "resume_review"

    matched: set[str] = set()
    for pattern in package_data["resume_review"]:
        hits = [p for p in base.glob(pattern) if p.is_file()]
        assert hits, f"glob {pattern!r} matched nothing under {base}"
        matched.update(p.relative_to(base).as_posix() for p in hits)

    assert "templates/report.html" in matched
    assert "templates/report.css" in matched
    assert "templates/report.js" in matched
    assert any(name.startswith("migrations/") and name.endswith(".sql") for name in matched)
    assert any(name.startswith("schemas/") and name.endswith(".json") for name in matched)


def test_setuptools_file_discovery_would_ship_every_required_asset() -> None:
    """Offline stand-in for a wheel build: apply the same discovery rules.

    The venv carries no build backend (no setuptools/wheel/build), so a
    wheel cannot be built offline; installing one needs build isolation and a
    network fetch. This test reimplements setuptools' rules -- ``packages.find``
    over ``src`` (every directory holding an ``__init__.py``) plus the declared
    ``package-data`` globs -- and asserts the assets a non-editable install needs
    are in the set. A real ``pip wheel`` build was performed separately, with
    build isolation, and its contents verified; see the task report.
    """
    src = REPO_ROOT / "src"
    package_dirs = [p for p in src.rglob("*") if p.is_dir() and (p / "__init__.py").is_file()]
    assert any(p.name == "resume_review" for p in package_dirs), package_dirs

    shipped: set[str] = set()
    for package in package_dirs:
        for module in package.glob("*.py"):
            shipped.add(module.relative_to(src).as_posix())
    package_base = src / "resume_review"
    for pattern in (
        "templates/*.html",
        "templates/*.css",
        "templates/*.js",
        "migrations/*.sql",
        "schemas/*.json",
    ):
        for hit in package_base.glob(pattern):
            if hit.is_file():
                shipped.add(hit.relative_to(src).as_posix())

    for required in (
        "resume_review/cli.py",
        "resume_review/templates/report.html",
        "resume_review/templates/report.css",
        "resume_review/templates/report.js",
        "resume_review/migrations/0001_initial.sql",
    ):
        assert required in shipped, required
    assert any(n.startswith("resume_review/schemas/") and n.endswith(".json") for n in shipped)


def test_packaged_resources_resolve_through_importlib() -> None:
    import importlib.resources as resources

    root = resources.files("resume_review")
    for relative in (
        "templates/report.html",
        "templates/report.css",
        "templates/report.js",
        "migrations/0001_initial.sql",
        "schemas/analysis_result.schema.json",
    ):
        assert root.joinpath(*relative.split("/")).is_file(), relative

    from resume_review.reporting import load_assets

    loaded = load_assets()
    assert loaded["css"].strip() and loaded["js"].strip()


# ===========================================================================
# 4. The corpus gate is closed by default (observed file access)
# ===========================================================================
def _audit_pytest(
    targets: list[str], out_json: Path, plugin_dir: Path, audit_target: Path
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("RESUME_REVIEW_REAL_CORPUS", None)
    plugin_path = str(plugin_dir)
    env["PYTHONPATH"] = plugin_path + os.pathsep + env.get("PYTHONPATH", "")
    env["RR_AUDIT_TARGET"] = str(audit_target)
    env["RR_AUDIT_OUT"] = str(out_json)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            "-p",
            "corpus_audit_plugin",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_corpus_gate_opens_no_resume_file_by_default(tmp_path: Path) -> None:
    out_json = tmp_path / "audit.json"
    proc = _audit_pytest(
        [str(REPO_ROOT / "tests" / "corpus")], out_json, CORPUS_PLUGIN_DIR, CORPUS_ROOT
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The corpus tests were collected and skipped, not silently dropped.
    assert "skipped" in proc.stdout
    records = json.loads(out_json.read_text(encoding="utf-8"))
    assert records == [], f"the default suite opened files under resume/: {records}"


def test_audit_recorder_actually_observes_a_read_under_the_target(tmp_path: Path) -> None:
    """Positive control: the recorder is not vacuous.

    Without this, the assertion above would pass even if the hook recorded
    nothing for any run. The control uses a synthetic target directory, so it
    never touches the real (PII) corpus: it proves the hook observes an ``open``
    under whatever directory it is pointed at.
    """
    target = tmp_path / "watched"
    target.mkdir()
    payload = target / "sample.txt"
    payload.write_text("synthetic control data, not a resume\n", encoding="utf-8")

    probe = tmp_path / "test_audit_positive_control.py"
    probe.write_text(
        "def test_reads_a_watched_path():\n"
        f"    with open(r'{payload}', 'rb') as fh:\n"
        "        fh.read(1)\n",
        encoding="utf-8",
    )
    out_json = tmp_path / "audit_probe.json"
    proc = _audit_pytest([str(probe)], out_json, CORPUS_PLUGIN_DIR, target)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    records = json.loads(out_json.read_text(encoding="utf-8"))
    paths = {r["path"] for r in records}
    assert str(payload) in paths, records


# ===========================================================================
# 5. Exit codes: documented vs observed
# ===========================================================================
def test_each_documented_exit_code_is_observed(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0 -- success
    code, _ = _run(capsys, ["status", "--instance", installed.instance_id, "--json"])
    assert code == int(cli.ExitCode.OK)

    # 2 -- invalid input (unreadable request file)
    code, payload = _run(capsys, ["status", "--instance", "inst_absent", "--json"])
    assert code == int(cli.ExitCode.INVALID_INPUT)
    assert payload["code"] == "INSTANCE_NOT_FOUND"

    # 3 -- permission failure (a helper holds the instance lock)
    from resume_review.bootstrap.ownership import InstanceLock

    lock = InstanceLock(
        workspace.owner_lock_path(installed.root), instance_id=installed.instance_id
    )
    lock.acquire()
    try:
        code, payload = _run(capsys, ["stop", "--instance", installed.instance_id, "--json"])
    finally:
        lock.release()
    assert code == int(cli.ExitCode.PERMISSION), payload
    assert payload["code"] == "INSTANCE_LOCKED_BY_OTHER_OWNER"

    # 4 -- conflict (missing approval)
    ids = _scan_and_reject(installed, capsys, names=("carol.txt",))
    batch_id, _ = _plan(installed, capsys, ids["carol.txt"])
    code, payload = _apply(installed, capsys, batch_id)
    assert code == int(cli.ExitCode.CONFLICT)
    assert payload["code"] == "APPROVAL_REQUIRED"

    # 5 -- dependency failure (no analysis route)
    code, payload = _run(capsys, ["summarize", "--instance", installed.instance_id, "--json"])
    assert code == int(cli.ExitCode.DEPENDENCY)
    assert payload["code"] == "ROUTE_UNAVAILABLE"

    # 1 -- unexpected internal failure (a handler bug must not leak a traceback)
    def _boom(args: object, request_id: str) -> object:
        raise RuntimeError("synthetic handler failure")

    monkeypatch.setitem(cli._HANDLERS, "repair", _boom)
    code, payload = _run(capsys, ["repair", "--instance", installed.instance_id, "--json"])
    assert code == int(cli.ExitCode.UNEXPECTED)
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["error"]["detail"]["reason"] == "RuntimeError"


def test_unsupported_storage_is_exit_6_and_refuses_to_initialize(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A UNC (network) workspace root is refused before anything is created."""
    backslash = chr(92)
    unc = backslash * 2 + "nonexistent-host-zzq" + backslash + "share" + backslash + "ws"
    job = cli_env / "job.txt"
    job.write_text("Operations Manager\n", encoding="utf-8")
    code, payload = _run(capsys, ["setup", "--folder", unc, "--job", str(job), "--json"])
    assert code == int(cli.ExitCode.UNSUPPORTED_STORAGE), payload
    assert payload["code"] == "NETWORK_FILESYSTEM_DATABASE"
    assert payload["error"]["detail"]["kind"] == "network"


def test_refused_downgrade_is_exit_2(
    installed: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = workspace.db_path(installed.root)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE schema_migrations SET version = version + 10")
    conn.commit()
    conn.close()

    job = installed.env / "job.txt"
    job.write_text("Operations Manager\nCoordinate crews.\n", encoding="utf-8")
    code, payload = _run(
        capsys,
        ["setup", "--folder", str(installed.root), "--job", str(job), "--json"],
    )
    assert code == int(cli.ExitCode.INVALID_INPUT), payload
    assert payload["code"] == "DOWNGRADE_REFUSED"


def test_exit_codes_match_the_frozen_error_table() -> None:
    from resume_review.errors import ExitCode as ErrorExitCode

    for name in (
        "OK",
        "UNEXPECTED",
        "INVALID_INPUT",
        "PERMISSION",
        "CONFLICT",
        "DEPENDENCY",
        "UNSUPPORTED_STORAGE",
    ):
        assert int(getattr(cli.ExitCode, name)) == int(getattr(ErrorExitCode, name))
