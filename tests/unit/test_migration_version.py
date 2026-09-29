"""Guard against the schema-version constant drifting away from the migrations.

``apply_migrations`` applies every ``NNNN_*.sql`` it discovers and then compares
the resulting database version against ``resume_review.SCHEMA_VERSION``. A build
that ships a migration without raising the constant therefore refuses to open
even the database it has just created: the recorded version exceeds the declared
one and ``assert_downgrade_allowed`` raises ``DOWNGRADE_REFUSED``.

That is not a hypothetical. Migration ``0002`` was added while ``SCHEMA_VERSION``
stayed at ``1``, and the result was that every fresh instance failed to reopen --
eight tests failing across setup, repository, and migration coverage, all with the
same root cause and none of them naming it. The constant is a literal rather than
a computed value (importing the migration tree from the package root would invert
the import direction the db layer depends on), so the two are held in step here.

These tests are cheap and deliberately independent of the application fixtures:
they reason about the migration directory and one throwaway database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION
from resume_review.db.migrations import (
    apply_migrations,
    assert_downgrade_allowed,
    current_version,
    discover_migrations,
)


def test_schema_version_equals_the_highest_shipped_migration() -> None:
    """The declared version must be exactly the newest migration on disk.

    Equality, not "at least": a constant ahead of the migrations would claim a
    schema this build cannot produce, which is the same class of lie in the other
    direction.
    """
    migrations = discover_migrations()
    highest = max(m.version for m in migrations)
    assert SCHEMA_VERSION == highest, (
        f"SCHEMA_VERSION is {SCHEMA_VERSION} but the highest migration is {highest} "
        f"({migrations[-1].path.name}). Raise SCHEMA_VERSION, or remove migration "
        f"{highest}. Leaving them unequal makes every fresh database unopenable."
    )


def test_migration_versions_are_contiguous_from_one() -> None:
    """No gaps: a missing version means an upgrade path nobody can execute."""
    versions = [m.version for m in discover_migrations()]
    assert versions == list(range(1, len(versions) + 1)), (
        f"migration versions are not contiguous from 1: {versions}"
    )


def test_every_migration_is_named_and_checksummed() -> None:
    """Discovery must not silently skip a file that fails to match the convention."""
    discovered = discover_migrations()
    names = {m.path.name for m in discovered}
    on_disk = {p.name for p in discovered[0].path.parent.glob("*.sql")}
    assert on_disk == names, (
        "these .sql files in the migration directory were not discovered: "
        f"{sorted(on_disk - names)}"
    )
    for migration in discovered:
        assert migration.name
        assert len(migration.checksum) == 64
        assert migration.sql.strip()


def test_a_freshly_migrated_database_is_not_refused(tmp_path: Path) -> None:
    """The regression test for the actual failure.

    Migrate a brand-new database and then reopen it the way setup does. Before the
    constant was corrected this raised ``MigrationError`` with code
    ``DOWNGRADE_REFUSED`` against a database the same build had just written.
    """
    path = tmp_path / "fresh.db"
    conn = sqlite3.connect(path)
    try:
        result = apply_migrations(conn)
        assert result["applied"] == list(range(1, SCHEMA_VERSION + 1))
        assert current_version(conn) == SCHEMA_VERSION
        # Reopening is what setup does on the second run.
        assert_downgrade_allowed(conn)
        # And the applied checksums are on record for every migration.
        recorded = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [row[0] for row in recorded] == list(range(1, SCHEMA_VERSION + 1))
    finally:
        conn.close()

    reopened = sqlite3.connect(path)
    try:
        assert_downgrade_allowed(reopened)
        assert current_version(reopened) == SCHEMA_VERSION
    finally:
        reopened.close()


def test_a_database_from_a_newer_build_is_still_refused(tmp_path: Path) -> None:
    """The protection the constant exists for must survive the correction.

    A database recording a version above this build's must still be refused; the
    fix must not have flattened the check into a no-op.
    """
    path = tmp_path / "newer.db"
    conn = sqlite3.connect(path)
    try:
        apply_migrations(conn)
        # current_version() takes the max recorded schema_migrations row, so a
        # future build is simulated by recording one version it would have added.
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
            (SCHEMA_VERSION + 1, "from_the_future", "2026-01-01T00:00:00Z", "0" * 64),
        )
        conn.commit()
        with pytest.raises(Exception) as caught:
            assert_downgrade_allowed(conn)
        assert getattr(caught.value, "code", None) == "DOWNGRADE_REFUSED"
    finally:
        conn.close()
