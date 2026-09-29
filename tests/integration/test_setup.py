"""Integration tests for workspace provisioning, ownership, and integrity.

Proves acceptance tests AT-01 (repeat setup), AT-02 (safe collision), AT-04 (asset
integrity), AT-32 (host topology) and AT-37 (owner locking) from PRD section 18.

Every fixture here is synthetic. No real applicant data, no real host registry, and
no real network topology is touched: the registry is redirected into ``tmp_path``
and the topology probe is monkeypatched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from resume_review import SCHEMA_VERSION
from resume_review.bootstrap import manifest as manifest_module
from resume_review.bootstrap import ownership, workspace
from resume_review.bootstrap.registry import HostRegistry
from resume_review.bootstrap.setup import setup_instance
from resume_review.errors import ResumeReviewError
from resume_review.storage import topology
from resume_review.util import now_iso


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostRegistry:
    """A host registry isolated inside the test's temporary directory."""
    registry_dir = tmp_path / "host-registry"
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(registry_dir))
    return HostRegistry()


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A synthetic reviewed application bundle.

    Deliberately not the repository's ``web/`` directory: the tests must be
    reproducible and must not depend on files other agents may be editing.
    """
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "assets").mkdir()
    (bundle / "templates" / "report.html").write_text("<html>review</html>", encoding="utf-8")
    (bundle / "assets" / "report.css").write_text("body{font-family:sans-serif}", encoding="utf-8")
    (bundle / "helper.py").write_text("print('helper')\n", encoding="utf-8")
    return bundle


@pytest.fixture
def provisioned(tmp_path: Path, registry: HostRegistry, bundle_dir: Path):
    """A freshly provisioned instance plus its root."""
    root = tmp_path / "Job - Operations Manager"
    result = setup_instance(root, "Operations Manager\nCoordinate subcontractors.", registry=registry, bundle_dir=bundle_dir)
    return root, result


def _seed_human_state(db_path: Path, instance_id: str) -> None:
    """Write synthetic human state directly, standing in for prior review work."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        stamp = now_iso()
        conn.execute(
            "INSERT INTO documents (id, instance_id, original_filename, current_rel_path, "
            "first_seen_rel_path, ingested_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("doc_synthetic", instance_id, "candidate-001.pdf", "candidate-001.pdf", "candidate-001.pdf", stamp, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO decisions (document_id, instance_id, disposition, decision_revision, "
            "actor, decided_at, created_at, updated_at) VALUES (?, ?, 'reject', 1, ?, ?, ?, ?)",
            ("doc_synthetic", instance_id, "reviewer@host", stamp, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO notes (id, instance_id, document_id, body, author, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("note_synthetic", instance_id, "doc_synthetic", "Called the listed reference.", "reviewer@host", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO review_tasks (id, instance_id, document_id, dedupe_key, title, state, "
            "closed_by, closed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?)",
            ("task_synthetic", instance_id, "doc_synthetic", "dedupe-1", "Verify certification", "reviewer@host", stamp, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO audit_events (instance_id, actor, actor_kind, event, created_at) "
            "VALUES (?, ?, 'human', ?, ?)",
            (instance_id, "reviewer@host", "decision.set", stamp),
        )
        conn.execute("UPDATE instances SET state_revision = 7")
        conn.commit()
    finally:
        conn.close()


def _counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "decisions", "notes", "review_tasks", "audit_events")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AT-01 -- repeat setup
# ---------------------------------------------------------------------------
def test_setup_provisions_the_documented_layout(tmp_path, registry, bundle_dir):
    root = tmp_path / "Job - Operations Manager"
    result = setup_instance(root, "Operations Manager", registry=registry, bundle_dir=bundle_dir)

    assert result.created is True
    assert result.instance_id.startswith("inst_")
    assert workspace.db_path(root).is_file()
    assert workspace.instance_manifest_path(root).is_file()
    assert workspace.job_manifest_path(root).is_file()

    # ensure_layout creates the reserved names and nothing else at the root.
    assert {p.name for p in root.iterdir()} == {".review", "Rejected", "Trash"}
    for sub in ("app", "app/templates", "app/assets", "extracted", "exports", "journals", "backups", "migrations", "locks", "tmp"):
        assert (workspace.review_dir(root) / Path(sub)).is_dir()


def test_instance_manifest_marker_and_ids_are_not_path_derived(tmp_path, registry, bundle_dir):
    root_a = tmp_path / "Job A"
    root_b = tmp_path / "Job B"
    a = setup_instance(root_a, "Job A", registry=registry, bundle_dir=bundle_dir)
    b = setup_instance(root_b, "Job B", registry=registry, bundle_dir=bundle_dir)

    assert a.instance_id != b.instance_id
    # Identity is opaque: not a hash of the root, not the folder name.
    assert a.instance_id != root_a.name
    assert root_a.name.lower() not in a.instance_id

    marker = workspace.read_instance_manifest(root_a)
    assert marker is not None
    assert marker["app"] == workspace.APP_MARKER
    assert marker["instance_id"] == a.instance_id
    assert int(marker["schema_version"]) == SCHEMA_VERSION
    assert str(root_a) not in str(marker)  # no absolute path in the folder manifest


def test_setup_twice_preserves_state_and_leaves_one_owner(provisioned, registry, bundle_dir):
    root, first = provisioned
    _seed_human_state(workspace.db_path(root), first.instance_id)
    before = _counts(workspace.db_path(root))

    second = setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir)

    assert second.instance_id == first.instance_id
    assert second.created is False
    assert second.state_revision_before == 7
    assert second.state_revision_after == 7
    assert _counts(workspace.db_path(root)) == before
    assert second.preserved is True

    # Exactly one owner: setup released the lock, and it is free to acquire.
    lock_path = workspace.owner_lock_path(root)
    assert ownership.is_locked(lock_path) is False
    owner = ownership.InstanceLock(lock_path, instance_id=first.instance_id)
    owner.acquire()
    assert owner.is_held() is True
    owner.release()


def first_job_text() -> str:
    return "Operations Manager\nCoordinate subcontractors."


def test_repeat_setup_does_not_rewrite_the_job_row_when_unchanged(provisioned, registry, bundle_dir):
    root, first = provisioned
    db = workspace.db_path(root)
    conn = sqlite3.connect(str(db))
    try:
        job_id = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0]
        updated_at = conn.execute("SELECT updated_at FROM jobs LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir)

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0] == job_id
        assert conn.execute("SELECT updated_at FROM jobs LIMIT 1").fetchone()[0] == updated_at
    finally:
        conn.close()


def test_changed_job_description_updates_in_place(provisioned, registry, bundle_dir):
    root, first = provisioned
    db = workspace.db_path(root)
    setup_instance(root, "Operations Manager\nNew requirement.", registry=registry, bundle_dir=bundle_dir)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT id, description_text FROM jobs").fetchall()
        assert len(rows) == 1
        assert "New requirement." in rows[0][1]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AT-02 -- safe collision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reserved", ["review.html", ".review", "Rejected", "Trash"])
def test_unrelated_reserved_entry_causes_collision(tmp_path, registry, bundle_dir, reserved):
    root = tmp_path / f"Job {reserved}"
    root.mkdir()
    target = root / reserved
    foreign_bytes = b"FOREIGN CONTENT -- must never be overwritten\n"
    if "." in reserved and reserved.endswith("html"):
        target.write_bytes(foreign_bytes)
    else:
        target.mkdir()
        (target / "keep.txt").write_bytes(foreign_bytes)

    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance(root, "Operations Manager", registry=registry, bundle_dir=bundle_dir)

    assert excinfo.value.code == "SETUP_COLLISION"
    assert excinfo.value.exit_code == 4  # EXIT_CONFLICT

    # Nothing was overwritten and nothing was adopted.
    if target.is_file():
        assert target.read_bytes() == foreign_bytes
    else:
        assert (target / "keep.txt").read_bytes() == foreign_bytes
    assert workspace.instance_manifest_path(root).exists() is False


def test_collision_report_names_the_conflict(tmp_path, registry, bundle_dir):
    root = tmp_path / "Job"
    root.mkdir()
    (root / "review.html").write_text("unrelated", encoding="utf-8")
    report = workspace.assert_no_collision(root)
    assert report.ok is False
    assert "review.html" in report.names

    # A folder we do own is not a conflict.
    owned_root = tmp_path / "Job Owned"
    setup_instance(owned_root, "Ops", registry=registry, bundle_dir=bundle_dir)
    owned_id = workspace.recognised_instance_id(owned_root)
    assert owned_id is not None
    assert workspace.assert_no_collision(owned_root, requested_instance_id=owned_id).ok is True


def test_collision_error_message_carries_no_absolute_path(tmp_path, registry, bundle_dir):
    root = tmp_path / "Job"
    root.mkdir()
    (root / "review.html").write_text("unrelated", encoding="utf-8")
    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance(root, "Ops", registry=registry, bundle_dir=bundle_dir)
    assert str(tmp_path) not in excinfo.value.message
    assert str(tmp_path) not in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# AT-04 -- asset integrity
# ---------------------------------------------------------------------------
def test_verify_manifest_passes_a_clean_bundle_and_reports_a_modified_one(tmp_path, bundle_dir):
    expected = manifest_module.build_manifest(bundle_dir)

    clean = manifest_module.verify_manifest(bundle_dir, expected)
    assert clean.ok is True
    assert clean.modified == []
    assert clean.actual_bundle_hash == expected["bundle_hash"]

    (bundle_dir / "templates" / "report.html").write_text("<html>tampered</html>", encoding="utf-8")
    tampered = manifest_module.verify_manifest(bundle_dir, expected)
    assert tampered.ok is False
    assert tampered.modified == ["templates/report.html"]
    assert tampered.expected_bundle_hash == expected["bundle_hash"]


def test_verify_manifest_reports_missing_and_extra_files(tmp_path, bundle_dir):
    expected = manifest_module.build_manifest(bundle_dir)
    (bundle_dir / "assets" / "report.css").unlink()
    (bundle_dir / "assets" / "extra.js").write_text("// added", encoding="utf-8")

    verdict = manifest_module.verify_manifest(bundle_dir, expected)
    assert verdict.ok is False
    assert verdict.missing == ["assets/report.css"]
    assert verdict.extra == ["assets/extra.js"]


def test_deploy_records_trusted_manifest_in_the_registry_first(provisioned, registry, bundle_dir):
    root, result = provisioned
    trusted = registry.get_trusted_manifest(result.instance_id)
    assert trusted is not None
    assert trusted["bundle_hash"] == manifest_module.build_manifest(bundle_dir)["bundle_hash"]
    assert registry.get(result.instance_id).trusted_bundle_hash == trusted["bundle_hash"]

    deployed = manifest_module.verify_deployed(root, registry, result.instance_id)
    assert deployed.ok is True


def test_setup_blocks_execution_when_a_deployed_template_is_modified(provisioned, registry, bundle_dir):
    root, first = provisioned
    deployed_template = workspace.app_dir(root) / "templates" / "report.html"
    assert deployed_template.is_file()
    deployed_template.write_text("<html>tampered</html>", encoding="utf-8")

    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir)

    assert excinfo.value.code == "MANIFEST_MISMATCH"
    # The tampered bytes were left in place for an operator to inspect.
    assert "tampered" in deployed_template.read_text(encoding="utf-8")


def test_redeploy_after_a_bundle_upgrade_is_verified(provisioned, registry, bundle_dir):
    root, first = provisioned
    (bundle_dir / "templates" / "report.html").write_text("<html>v2</html>", encoding="utf-8")

    second = setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir)

    assert second.deployed is True
    assert manifest_module.verify_deployed(root, registry, first.instance_id).ok is True
    assert "v2" in (workspace.app_dir(root) / "templates" / "report.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AT-37 -- owner locking
# ---------------------------------------------------------------------------
def test_second_owner_cannot_acquire_and_can_after_release(provisioned):
    root, first = provisioned
    lock_path = workspace.owner_lock_path(root)

    holder = ownership.InstanceLock(lock_path, instance_id=first.instance_id)
    holder.acquire()
    try:
        assert holder.is_held() is True
        assert ownership.is_locked(lock_path) is True
        info = holder.holder_info()
        assert info is not None and info.pid is not None
        assert info.instance_id == first.instance_id

        rival = ownership.InstanceLock(lock_path)
        with pytest.raises(ResumeReviewError) as excinfo:
            rival.acquire()
        assert excinfo.value.code == "INSTANCE_LOCKED_BY_OTHER_OWNER"
        assert rival.held is False
    finally:
        holder.release()

    # Normal restart after a controlled shutdown succeeds.
    assert ownership.is_locked(lock_path) is False
    restart = ownership.InstanceLock(lock_path)
    restart.acquire()
    assert restart.is_held() is True
    restart.release()


def test_setup_refuses_while_another_owner_holds_the_instance(provisioned, registry, bundle_dir):
    root, first = provisioned
    holder = ownership.InstanceLock(workspace.owner_lock_path(root), instance_id=first.instance_id)
    holder.acquire()
    try:
        with pytest.raises(ResumeReviewError) as excinfo:
            setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir)
        assert excinfo.value.code == "INSTANCE_LOCKED_BY_OTHER_OWNER"
    finally:
        holder.release()

    # The instance is usable again once the owner stops.
    assert setup_instance(root, first_job_text(), registry=registry, bundle_dir=bundle_dir).instance_id == first.instance_id


def test_lock_backend_is_a_real_os_lock(provisioned):
    root, _ = provisioned
    assert "flock" in ownership.lock_backend_name() or "LockFile" in ownership.lock_backend_name()
    # The lock file is not a PID file: ownership survives a stale timestamp, so the
    # holder's diagnostics never decide anything.
    holder = ownership.InstanceLock(workspace.owner_lock_path(root))
    holder.acquire()
    try:
        assert holder.is_held() is True
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# AT-32 -- host topology
# ---------------------------------------------------------------------------
def test_setup_refuses_a_network_topology_path(tmp_path, registry, bundle_dir, monkeypatch):
    def fake_probe(path):  # noqa: ANN001 - test double
        return topology.TopologyReport(
            kind=topology.TopologyKind.NETWORK,
            detail="A mapped network drive.",
            mount_point="Z:/",
            filesystem_type="win32:4",
            writable=True,
        )

    monkeypatch.setattr(topology, "probe_topology", fake_probe)
    root = tmp_path / "Job on a share"
    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance(root, "Ops", registry=registry, bundle_dir=bundle_dir)
    assert excinfo.value.code == "NETWORK_FILESYSTEM_DATABASE"
    assert excinfo.value.exit_code == 6  # EXIT_UNSUPPORTED_STORAGE


def test_unknown_topology_needs_explicit_confirmation(tmp_path, registry, bundle_dir, monkeypatch):
    def fake_probe(path):  # noqa: ANN001 - test double
        return topology.TopologyReport(
            kind=topology.TopologyKind.UNKNOWN, detail="Unclassifiable.", writable=True
        )

    monkeypatch.setattr(topology, "probe_topology", fake_probe)
    root = tmp_path / "Job unknown"

    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance(root, "Ops", registry=registry, bundle_dir=bundle_dir)
    assert excinfo.value.code == "UNSUPPORTED_STORAGE_TOPOLOGY"

    confirmed = setup_instance(
        root, "Ops", registry=registry, bundle_dir=bundle_dir, storage_confirm_unknown=True
    )
    assert confirmed.instance_id.startswith("inst_")
    assert any("topology" in w.lower() for w in confirmed.warnings)


# ---------------------------------------------------------------------------
# Root validation
# ---------------------------------------------------------------------------
def test_setup_rejects_a_relative_root(registry, bundle_dir):
    with pytest.raises(ResumeReviewError) as excinfo:
        setup_instance("relative/job", "Ops", registry=registry, bundle_dir=bundle_dir)
    assert excinfo.value.code == "INVALID_INPUT"
