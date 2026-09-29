"""Schema migrations.

Authority: PRD sections 5.2 (step 4: "Never reset it because a template changed"),
5.3 ("make a verified backup before migration; reject unsupported downgrades
rather than corrupting newer data") and AT-05.

Rules:

* Migrations are forward-only, numbered, and applied inside a transaction.
* A previously applied migration whose bytes changed is a hard failure. Silently
  re-running edited DDL is how schema drift and data loss happen.
* A database recording a *higher* version than this build understands is refused
  outright. Opening it would risk writing older-shaped rows into a newer schema.
* Migration never deletes applicant-adjacent data. A migration that needs to
  restructure a table must copy, verify, then drop the old table explicitly.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import SCHEMA_VERSION
from ..errors import ResumeReviewError
from ..util import now_iso

__all__ = [
    "Migration",
    "discover_migrations",
    "apply_migrations",
    "current_version",
    "assert_downgrade_allowed",
]

_NAME_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_\-]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


class MigrationError(ResumeReviewError):
    code = "SCHEMA_VERSION_UNSUPPORTED"
    http_status = 409


def default_migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def iter_statements(sql: str) -> "list[str]":
    """Split a migration file into complete SQL statements.

    Uses :func:`sqlite3.complete_statement` so that semicolons inside string
    literals or trigger bodies do not split a statement incorrectly. Comment-only
    fragments are discarded.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        chunk = "".join(buffer)
        if sqlite3.complete_statement(chunk):
            stripped = _strip_leading_comments(chunk).strip()
            if stripped:
                statements.append(chunk.strip())
            buffer = []

    tail = _strip_leading_comments("".join(buffer)).strip()
    if tail:
        statements.append("".join(buffer).strip())
    return statements


def _strip_leading_comments(chunk: str) -> str:
    """Drop leading ``--`` line comments so a comment-only fragment is ignored."""
    lines = chunk.splitlines()
    index = 0
    while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("--")):
        index += 1
    return "\n".join(lines[index:])


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Load every ``NNNN_name.sql`` in ``directory``, ordered by version.

    Duplicate version numbers are refused: two migrations claiming version 3 would
    make the applied set ambiguous and the order unreproducible.
    """
    root = Path(directory) if directory else default_migrations_dir()
    if not root.is_dir():
        raise MigrationError(
            "The migration directory is missing from this installation.",
            code="MANIFEST_MISSING",
            detail={"directory_present": False},
        )

    found: dict[int, Migration] = {}
    for entry in sorted(root.glob("*.sql")):
        match = _NAME_RE.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                "Two migrations declare the same version number.",
                detail={"version": version},
            )
        data = entry.read_bytes()
        found[version] = Migration(
            version=version,
            name=match.group(2),
            path=entry,
            sql=data.decode("utf-8"),
            checksum=hashlib.sha256(data).hexdigest(),
        )

    if not found:
        raise MigrationError(
            "No migrations were found in the installation.",
            code="MANIFEST_MISSING",
        )
    return [found[v] for v in sorted(found)]


def _applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is None:
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def current_version(conn: sqlite3.Connection) -> int:
    applied = _applied_versions(conn)
    return max(applied) if applied else 0


def assert_downgrade_allowed(conn: sqlite3.Connection) -> None:
    """Refuse to open a database written by a newer build."""
    found = current_version(conn)
    if found > SCHEMA_VERSION:
        raise MigrationError(
            "This workspace was written by a newer version of the application. "
            "Downgrades are refused rather than risking corruption of newer data. "
            "Upgrade the installation instead.",
            code="DOWNGRADE_REFUSED",
            http_status=409,
            detail={"database_version": found, "application_version": SCHEMA_VERSION},
        )


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    directory: Path | None = None,
    backup_hook: "callable | None" = None,  # type: ignore[valid-type]
) -> dict[str, object]:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`.

    ``backup_hook`` is called once, before the first pending migration, so the
    caller can take a verified backup. It is required for upgrades (as opposed to
    a fresh database) by the CLI layer; this function calls it when given.
    """
    migrations = discover_migrations(directory)
    applied = _applied_versions(conn)

    for migration in migrations:
        if migration.version in applied:
            recorded = applied[migration.version]
            if recorded != migration.checksum:
                raise MigrationError(
                    "A previously applied migration has been modified. Refusing to "
                    "continue, because the installed schema can no longer be reproduced.",
                    code="MANIFEST_MISMATCH",
                    http_status=409,
                    detail={"version": migration.version},
                )

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        assert_downgrade_allowed(conn)
        return {"applied": [], "version": current_version(conn), "pending": 0}

    if backup_hook is not None and applied:
        backup_hook()

    newly_applied: list[int] = []
    for migration in pending:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Statements are executed one at a time rather than through
            # `executescript`, which implicitly COMMITs any open transaction and
            # would silently break the atomicity this loop depends on.
            for statement in iter_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, now_iso(), migration.checksum),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MigrationError(
                "A schema migration failed; the database was left unchanged.",
                code="SCHEMA_VERSION_UNSUPPORTED",
                http_status=409,
                detail={"version": migration.version, "sqlite_error": exc.__class__.__name__},
            ) from exc
        newly_applied.append(migration.version)

    # Keep the cached copy on `instances` consistent with reality.
    version = current_version(conn)
    conn.execute("UPDATE instances SET schema_version = ? WHERE schema_version != ?", (version, version))

    return {
        "applied": newly_applied,
        "version": version,
        "pending": 0,
        "migrations_available": [m.version for m in migrations],
    }


def verify_applied(conn: sqlite3.Connection, *, directory: Path | None = None) -> list[str]:
    """Return human-readable problems with the recorded migration set."""
    problems: list[str] = []
    migrations = {m.version: m for m in discover_migrations(directory)}
    applied = _applied_versions(conn)

    for version, checksum in sorted(applied.items()):
        known = migrations.get(version)
        if known is None:
            problems.append(f"version {version} is applied but not present in this installation")
        elif known.checksum != checksum:
            problems.append(f"version {version} checksum differs from the installed migration")
    return problems
