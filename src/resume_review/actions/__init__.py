"""Plan -> approve -> apply, journal, and recovery (PRD sections 10, 12.3, 13).

This package owns the front half of the safety-critical file-management path:

* :mod:`resume_review.actions.planner` builds an immutable, canonical
  :class:`~resume_review.models.ActionPlan` from an explicit set of document IDs.
  Planning **moves nothing** and is **not approval**.
* :mod:`resume_review.actions.journal` is a durable, crash-safe, per-operation
  journal on disk. It records the operation intent before any file is touched and
  is a *recovery aid*, never the authority.

Layer rules (AGENTS.md): this package imports ``models``, ``errors``, ``util`` and
``storage`` only. It imports no HTTP layer and no model-adapter layer, never
performs a filesystem move, never renames or deletes a managed file, and imports
no copy-and-delete utility.
"""

from __future__ import annotations

from .journal import (
    JOURNAL_SCHEMA_VERSION,
    Journal,
    JournalOperation,
    JournalOrderError,
    JournalSnapshot,
    ReconciliationItem,
    ReconciliationReport,
    journal_dir,
    journal_path,
    load_journal,
    reconcile_with_database,
    reconcile_with_repo,
)
from .planner import PLAN_SCHEMA_VERSION, SkipReason, plan_actions

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "SkipReason",
    "plan_actions",
    "JOURNAL_SCHEMA_VERSION",
    "Journal",
    "JournalOperation",
    "JournalOrderError",
    "JournalSnapshot",
    "ReconciliationItem",
    "ReconciliationReport",
    "journal_dir",
    "journal_path",
    "load_journal",
    "reconcile_with_database",
    "reconcile_with_repo",
]
