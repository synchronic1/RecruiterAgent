"""Instance provisioning: the setup sequence from PRD section 5.2.

Authority: PRD section 5.2 (steps 1-9) and 5.3 ("On repeat setup, preserve IDs,
summaries, notes, reviewer decisions, completed tasks, and journals. On upgrade,
make a verified backup before migration; reject unsupported downgrades rather than
corrupting newer data."). Acceptance tests AT-01, AT-02, AT-04, AT-32, AT-37.

Starting the helper process is deliberately out of scope here (it is wired up in a
later phase); this module ends at "the instance exists, its database is current, its
bundle is deployed, and its manifest files are written".

Order matters and is enforced in :func:`setup_instance`. In particular the release
integrity check happens before the database is opened, and a verified backup happens
before any migration, because resetting a database "because the template changed" is
exactly the failure the PRD forbids.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from .. import __version__ as APP_VERSION
from ..db.connection import DbConfig, open_connection
from ..db.migrations import apply_migrations, assert_downgrade_allowed, current_version
from ..errors import InvalidInput, ResumeReviewError
from ..models import sha256_hex
from ..security.untrusted import safe_display_name
from ..storage import (
    SymlinkEscape,
    assert_supported_for_database,
    is_reparse_point,
)
from ..util import new_id, now_iso
from . import manifest as manifest_module
from . import registry as registry_module
from . import workspace
from .ownership import InstanceLock, lock_backend_name

__all__ = ["SetupResult", "setup_instance", "snapshot_counts", "MIN_FREE_BYTES"]

#: Refuse to provision a workspace that cannot hold a database plus extraction cache
#: without the disk filling during the first scan.
MIN_FREE_BYTES = 64 * 1024 * 1024

_COUNT_QUERIES: dict[str, str] = {
    "documents": "SELECT COUNT(*) FROM documents",
    "decisions_set": "SELECT COUNT(*) FROM decisions WHERE disposition != 'unreviewed'",
    "notes": "SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL",
    "tasks_open": "SELECT COUNT(*) FROM review_tasks WHERE state = 'open'",
    "tasks_closed": "SELECT COUNT(*) FROM review_tasks WHERE state IN ('closed', 'dismissed')",
    "profiles": "SELECT COUNT(*) FROM profiles",
    "intents": "SELECT COUNT(*) FROM action_intents",
    "audit_events": "SELECT COUNT(*) FROM audit_events",
}


@dataclass
class SetupResult:
    """Everything the caller and the CLI need to report a completed setup.

    ``counts_before``/``counts_after`` and the two revisions exist so a repeat run
    can *prove* it preserved human state rather than assert it (AT-01).
    """

    instance_id: str
    root: str
    db_path: str
    report_path: str
    storage_mode: str
    topology: dict[str, Any]
    created: bool
    migrated: bool
    warnings: list[str] = field(default_factory=list)
    next_action: str = ""
    counts_before: dict[str, int] = field(default_factory=dict)
    counts_after: dict[str, int] = field(default_factory=dict)
    state_revision_before: int = 0
    state_revision_after: int = 0
    app_version: str = APP_VERSION
    schema_version: int = SCHEMA_VERSION
    bundle_hash: str | None = None
    deployed: bool = False
    backup_path: str | None = None
    service_address: str | None = None
    lock_backend: str = ""

    @property
    def preserved(self) -> bool:
        """True when a repeat run left every counted dimension untouched."""
        return self.counts_before == self.counts_after and self.state_revision_before == self.state_revision_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "root_label": Path(self.root).name,
            "db_path": self.db_path,
            "report_path": self.report_path,
            "storage_mode": self.storage_mode,
            "topology": self.topology,
            "created": self.created,
            "migrated": self.migrated,
            "warnings": list(self.warnings),
            "next_action": self.next_action,
            "counts_before": dict(self.counts_before),
            "counts_after": dict(self.counts_after),
            "preserved": self.preserved,
            "state_revision_before": self.state_revision_before,
            "state_revision_after": self.state_revision_after,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "bundle_hash": self.bundle_hash,
            "deployed": self.deployed,
            "backup_path": self.backup_path,
            "service_address": self.service_address,
            "lock_backend": self.lock_backend,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def setup_instance(
    root: str | os.PathLike[str],
    job_description_text: str,
    *,
    storage_confirm_unknown: bool = False,
    instance_id: str | None = None,
    actor: str = "local",
    registry: registry_module.HostRegistry | None = None,
    bundle_dir: str | os.PathLike[str] | None = None,
    storage_mode: str = "local",
    min_free_bytes: int = MIN_FREE_BYTES,
) -> SetupResult:
    """Provision or re-provision one instance in ``root``.

    Idempotent: running it twice against a populated folder preserves identifiers,
    summaries, notes, decisions, tasks, and the audit trail, and leaves no owner
    holding the instance lock. The return value carries the before/after counts that
    prove it.
    """
    host_registry = registry or registry_module.HostRegistry()
    bundle = Path(bundle_dir) if bundle_dir is not None else default_bundle_dir()

    warnings: list[str] = []

    # -- 1. resolve and validate the root (PRD 5.2 step 1) -----------------
    root_path = _resolve_root(root)
    if is_reparse_point(root_path):
        raise SymlinkEscape(
            "The selected root is a link or reparse point, which cannot be a workspace root.",
            detail={"reason": "root_is_link"},
        )
    topology = assert_supported_for_database(root_path, allow_unknown=storage_confirm_unknown)
    if not topology.is_certain:
        warnings.append(
            "Storage topology could not be classified and was accepted only because it was "
            "confirmed explicitly."
        )
    if not topology.supports_live_database:
        warnings.append(
            "The workspace is not on a fixed local volume; WAL durability is weaker here."
        )

    # -- 2. access, space, collisions, existing ownership (PRD 5.2 step 2) --
    existing_id = workspace.recognised_instance_id(root_path)
    if existing_id is not None and instance_id is None:
        # Adopt the folder's own identity rather than minting a second one.
        instance_id = existing_id
    collision = workspace.assert_no_collision(root_path, requested_instance_id=instance_id)
    collision.raise_if_any()

    workspace.ensure_layout(root_path)
    _assert_writable_directory(root_path)
    _assert_free_space(root_path, min_free_bytes)

    lock = InstanceLock(
        workspace.owner_lock_path(root_path), instance_id=instance_id, app_version=APP_VERSION
    )
    try:
        lock.acquire()
    except ResumeReviewError:
        raise

    try:
        # -- 3. release integrity (PRD 5.2 step 3, AT-04) -------------------
        # The bundle counts as deployed only when a trusted manifest exists for it.
        # A bare .review/app directory proves nothing: ensure_layout creates those
        # directories on every run, so using their presence would make a fresh
        # install look like a tampered existing one.
        trusted_manifest = (
            host_registry.get_trusted_manifest(instance_id) if instance_id else None
        )
        app_present = trusted_manifest is not None
        if app_present:
            verdict = manifest_module.verify_deployed(root_path, host_registry, instance_id)
            if not verdict.ok:
                raise ResumeReviewError(
                    "The deployed application bundle does not match its trusted manifest. "
                    "Execution is blocked until an operator performs a controlled repair.",
                    code="MANIFEST_MISMATCH",
                    http_status=409,
                    detail=verdict.to_dict(),
                )

        # -- 4. database: initialise, or back up and migrate (step 4) --------
        db_file = workspace.db_path(root_path)
        conn = open_connection(DbConfig(path=db_file))
        backup_path: str | None = None
        migrated = False
        try:
            def _backup_hook() -> None:
                nonlocal backup_path
                backup_path = str(_backup_database(db_file, workspace.backups_dir(root_path)))

            if current_version(conn) > 0:
                # Refuse a database from a newer build before touching anything.
                assert_downgrade_allowed(conn)
            migration_report = apply_migrations(conn, backup_hook=_backup_hook)
            migrated = bool(migration_report.get("applied"))

            # Counts are taken after migration and before any row this run writes,
            # so a repeat setup can prove it changed nothing.
            counts_before = snapshot_counts(conn)
            revision_before = _state_revision(conn)

            # -- 5. instance and job rows (steps 4-5) -----------------------
            row = conn.execute("SELECT id, state_revision FROM instances LIMIT 1").fetchone()
            created = row is None
            if row is None:
                resolved_id = instance_id or new_id("instance")
                with _bare(conn):
                    conn.execute(
                        "INSERT INTO instances (id, schema_version, app_version, state_revision, "
                        "storage_mode, host_label, created_at, updated_at) "
                        "VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                        (
                            resolved_id,
                            current_version(conn),
                            APP_VERSION,
                            storage_mode,
                            _host_label(),
                            now_iso(),
                            now_iso(),
                        ),
                    )
            else:
                resolved_id = str(row["id"])
                if instance_id is not None and resolved_id != instance_id:
                    raise ResumeReviewError(
                        "This folder's database belongs to a different instance id than the "
                        "manifest names. No changes were made.",
                        code="INSTANCE_MISMATCH",
                        http_status=409,
                        detail={"reason": "database_instance_mismatch"},
                    )
                instance_id = resolved_id

            instance_id = resolved_id
            job_id, criteria_version = _register_job(conn, instance_id, job_description_text, actor)

            # Keep the cached schema version honest for an already-current database.
            conn.execute(
                "UPDATE instances SET schema_version = ? WHERE schema_version != ?",
                (current_version(conn), current_version(conn)),
            )

            counts_after = snapshot_counts(conn)
            revision_after = _state_revision(conn)
        finally:
            conn.close()

        # -- 6. deploy the versioned bundle (step 6) ------------------------
        current_manifest = manifest_module.build_manifest(bundle)
        deployed = False
        if not app_present:
            result = manifest_module.deploy_bundle(
                bundle, root_path, registry=host_registry, instance_id=instance_id
            )
            deployed = True
        else:
            trusted = host_registry.get_trusted_manifest(instance_id)
            if trusted is None or trusted.get("bundle_hash") != current_manifest["bundle_hash"]:
                # An upgrade: record the new trusted manifest first, then copy.
                result = manifest_module.deploy_bundle(
                    bundle, root_path, registry=host_registry, instance_id=instance_id
                )
                deployed = True
            else:
                result = manifest_module.DeployResult(
                    deployed_path=workspace.app_dir(root_path),
                    file_count=len(current_manifest["files"]),
                    bundle_hash=current_manifest["bundle_hash"],
                )

        # -- 7. register the instance and write the manifest files ----------
        host_registry.register(
            instance_id,
            canonical_root=root_path,
            service_address=None,
            storage_mode=storage_mode,
            trusted_bundle_hash=result.bundle_hash,
        )
        _write_instance_manifest(
            root_path,
            instance_id=instance_id,
            storage_mode=storage_mode,
            schema_version=current_version_from_db(db_file),
        )
        _write_job_manifest(
            root_path,
            instance_id=instance_id,
            job_id=job_id,
            title=_derive_title(job_description_text),
            description_text=job_description_text,
            criteria_version=criteria_version,
        )
    finally:
        # A controlled shutdown: the lock is released so a normal restart succeeds
        # (AT-37). The OS would release it on process death regardless.
        lock.release()

    return SetupResult(
        instance_id=instance_id or "",
        root=str(root_path),
        db_path=str(db_file),
        report_path=str(workspace.report_path(root_path)),
        storage_mode=storage_mode,
        topology=topology.to_dict(),
        created=created,
        migrated=migrated,
        warnings=warnings,
        next_action=(
            "Run a scan to register submissions, then open the connected page or the "
            "generated review.html snapshot."
        ),
        counts_before=counts_before,
        counts_after=counts_after,
        state_revision_before=revision_before,
        state_revision_after=revision_after,
        bundle_hash=result.bundle_hash,
        deployed=deployed,
        backup_path=backup_path,
        service_address=None,
        lock_backend=lock_backend_name(),
    )


# ---------------------------------------------------------------------------
# Root and environment checks
# ---------------------------------------------------------------------------
def packaged_bundle_dir() -> Path:
    """The deployable bundle shipped inside the package (``resume_review/bundle``).

    Its shape is the *deployed* layout, ``assets/`` and ``templates/`` side by side,
    and that shape is load-bearing twice over. ``report.html`` links
    ``../assets/report.css`` and ``../assets/report.js``, so a flat tree deploys a
    page whose stylesheet and script resolve to nothing; and ``manifest._mode_for``
    reads the ``assets/``/``templates/`` prefix to classify a file as an asset,
    calling a bare ``report.js`` an executable instead.
    """
    return Path(__file__).resolve().parents[1] / "bundle"


def default_bundle_dir() -> Path:
    """The reviewed application bundle shipped with this installation.

    The repository keeps the deployable report assets under ``web/``; a packaged
    install ships the same tree under ``resume_review/bundle/``. Both are the nested
    layout, so the deployed page's relative links resolve either way.
    """
    repo_web = Path(__file__).resolve().parents[3] / "web"
    if repo_web.is_dir():
        return repo_web
    return packaged_bundle_dir()


def _resolve_root(root: str | os.PathLike[str]) -> Path:
    raw = os.fspath(root)
    if not raw or not str(raw).strip():
        raise InvalidInput("A workspace folder must be selected.", code="INVALID_INPUT")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise InvalidInput(
            "The workspace folder must be an absolute path.",
            code="INVALID_INPUT",
            detail={"reason": "relative_root"},
        )
    return path.resolve(strict=False)


def _assert_writable_directory(root: Path) -> None:
    if not root.is_dir():
        raise ResumeReviewError(
            "The workspace root could not be created.",
            code="READ_ONLY_LOCATION",
            http_status=409,
        )
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise ResumeReviewError(
            "The workspace root is not readable and writable by this process.",
            code="READ_ONLY_LOCATION",
            http_status=409,
            detail={"reason": "access_denied"},
        )


def _assert_free_space(root: Path, minimum: int) -> None:
    try:
        usage = shutil.disk_usage(str(root))
    except OSError:  # pragma: no cover - platform quirk on a fresh volume
        return
    if usage.free < max(0, minimum):
        raise ResumeReviewError(
            "There is not enough free space on this volume to provision a workspace.",
            code="INSUFFICIENT_SPACE",
            http_status=409,
            detail={"required_bytes": int(minimum), "free_bytes": int(usage.free)},
        )


def _host_label() -> str:
    """A non-secret descriptor of the host, for diagnostics only."""
    import socket

    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover - defensive
        return ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
@contextmanager
def _bare(conn: sqlite3.Connection):
    """A setup-level transaction: no revision bump and no audit row.

    Setup is bootstrapping, not a domain mutation; the audit trail starts once a
    reviewer acts. This mirrors what the migration path is allowed to do.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def snapshot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts of the dimensions AT-01 requires a repeat setup to preserve."""
    counts: dict[str, int] = {}
    for name, sql in _COUNT_QUERIES.items():
        try:
            counts[name] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            counts[name] = 0
    return counts


def _state_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT state_revision FROM instances LIMIT 1").fetchone()
    return int(row[0]) if row else 0


def _register_job(
    conn: sqlite3.Connection, instance_id: str, description_text: str, actor: str
) -> tuple[str, int]:
    """Create the job row, or update it only when the description actually changed.

    Returning early on an unchanged description is what makes a repeat setup silent:
    no write, no revision change, no audit noise.
    """
    description = description_text or ""
    digest = sha256_hex(description)
    row = conn.execute("SELECT id, description_sha256, criteria_version FROM jobs LIMIT 1").fetchone()
    if row is not None and str(row["description_sha256"]) == digest:
        return str(row["id"]), int(row["criteria_version"])

    if row is None:
        job_id = new_id("job")
        with _bare(conn):
            conn.execute(
                "INSERT INTO jobs (id, instance_id, title, description_text, description_sha256, "
                "criteria_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (job_id, instance_id, _derive_title(description), description, digest, now_iso(), now_iso()),
            )
        return job_id, 0

    job_id = str(row["id"])
    criteria_version = int(row["criteria_version"])
    with _bare(conn):
        conn.execute(
            "UPDATE jobs SET description_text = ?, description_sha256 = ?, title = ?, updated_at = ? "
            "WHERE id = ?",
            (description, digest, _derive_title(description), now_iso(), job_id),
        )
    return job_id, criteria_version


def current_version_from_db(db_file: Path) -> int:
    conn = open_connection(DbConfig(path=db_file))
    try:
        return current_version(conn)
    finally:
        conn.close()


def _backup_database(db_file: Path, backups: Path) -> Path:
    """Take a verified copy through the SQLite backup API.

    A plain file copy of a live database can capture a torn page mid-write. The
    backup API holds the correct locks and produces a consistent snapshot; we then
    verify it by opening the copy and running an integrity check before trusting it.
    """
    backups.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").split(".")[0]
    target = backups / f"review-{stamp}-{new_id('tmp')[-8:]}.db"

    source = sqlite3.connect(str(db_file))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    check = sqlite3.connect(str(target))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
        ok = bool(result) and str(result[0]).lower() == "ok"
    finally:
        check.close()
    if not ok:
        try:
            target.unlink()
        except OSError:  # pragma: no cover - cleanup only
            pass
        raise ResumeReviewError(
            "The pre-migration backup did not verify, so migration was not attempted.",
            code="MANIFEST_MISMATCH",
            http_status=409,
            detail={"reason": "backup_integrity_failed"},
        )
    return target


# ---------------------------------------------------------------------------
# Generated manifests (PRD section 4)
# ---------------------------------------------------------------------------
def _derive_title(description_text: str) -> str:
    """First non-empty line of the job description, sanitised. Never invented."""
    for line in (description_text or "").splitlines():
        candidate = safe_display_name(line, fallback="")
        if candidate:
            return candidate[:120]
    return ""


def _write_instance_manifest(
    root: Path, *, instance_id: str, storage_mode: str, schema_version: int
) -> None:
    payload = {
        workspace.MANIFEST_MARKER_KEY: workspace.APP_MARKER,
        "manifest_version": workspace.INSTANCE_MANIFEST_VERSION,
        "instance_id": instance_id,
        "app_version": APP_VERSION,
        "schema_version": int(schema_version),
        "storage_mode": storage_mode,
        "root_label": root.name,
        "lock_backend": lock_backend_name(),
        "generated_at": now_iso(),
    }
    _write_json(workspace.instance_manifest_path(root), payload)


def _write_job_manifest(
    root: Path,
    *,
    instance_id: str,
    job_id: str,
    title: str,
    description_text: str,
    criteria_version: int,
) -> None:
    payload = {
        workspace.MANIFEST_MARKER_KEY: workspace.APP_MARKER,
        "manifest_version": workspace.INSTANCE_MANIFEST_VERSION,
        "instance_id": instance_id,
        "job_id": job_id,
        "title": title,
        "description_sha256": sha256_hex(description_text or ""),
        "criteria_version": int(criteria_version),
        "generated_at": now_iso(),
    }
    _write_json(workspace.job_manifest_path(root), payload)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    # O_BINARY keeps Windows from rewriting the JSON's newlines as CRLF.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
