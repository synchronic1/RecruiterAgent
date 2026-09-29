"""resume-review: a folder-local, evidence-backed resume review workspace.

The folder is the application instance and the unit of portability. One job folder
owns one SQLite database, one generated report, one set of criteria, one review
history, and one helper process.

Layering (enforced by ``tests/unit/test_layering.py``)::

    models, errors          pure data and error contracts; no I/O
    storage                 filesystem primitives; imports models/errors only
    db                      persistence; imports models/errors/storage
    ingest, analysis,       domain services; import db/storage/models
    actions, reporting,
    auth, openclaw_adapter
    api, cli, bootstrap     entry points; may import everything below

Nothing below ``api`` imports ``api``.
"""

from __future__ import annotations

__all__ = ["__version__", "SCHEMA_VERSION", "APP_NAME"]

APP_NAME = "resume-review"
__version__ = "0.1.0"

#: Highest migration version this build understands. A database recording a
#: higher number is from a newer build and must be refused, not opened
#: (PRD section 5.3: "reject unsupported downgrades rather than corrupting
#: newer data").
#:
#: This MUST equal the highest ``NNNN_*.sql`` under ``migrations/``. The runner
#: applies every migration it finds and then compares the resulting database
#: version against this constant, so a build that ships a migration without
#: raising this value refuses to open even the database it just created.
#: tests/unit/test_migration_version.py holds the two in step.
SCHEMA_VERSION = 2
