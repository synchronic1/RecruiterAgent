"""Data-access package: the single ``Repository`` facade over the frozen schema.

Authority: PRD section 11 ("Database and persistence contracts") and section 12.2
("Mutation rules").

Every layer that persists or reads domain state goes through :class:`Repository`.
It composes two mixins so the API stays coherent while each concern keeps its own
file:

* :mod:`resume_review.db.repository` supplies instance, document, human-state,
  action-batch and durable-queue methods, plus the versioned-write exception
  :class:`RevisionConflict`.
* :mod:`resume_review.db.repository_analysis` supplies job requisition, criteria,
  profile, evidence, idempotency, conversation and audit methods.

Callers should read and write state through this package rather than importing the
mixins directly, so the seam between the two halves never leaks. Row mappers here
return dataclasses from :mod:`resume_review.models`, never a raw ``sqlite3.Row``.
"""

from __future__ import annotations

from .connection import Database, DbConfig, open_connection
from .repository import (
    Repository,
    RevisionConflict,
    decision_from_row,
    document_from_row,
    file_operation_from_row,
    intent_from_row,
    note_from_row,
    task_from_row,
)
from .repository_analysis import (
    AnalysisRepositoryMixin,
    coerce_enum,
    criterion_from_row,
    profile_from_row,
)

__all__ = [
    "Repository",
    "RevisionConflict",
    "AnalysisRepositoryMixin",
    "Database",
    "DbConfig",
    "open_connection",
    "coerce_enum",
    "document_from_row",
    "decision_from_row",
    "note_from_row",
    "task_from_row",
    "intent_from_row",
    "file_operation_from_row",
    "criterion_from_row",
    "profile_from_row",
]
