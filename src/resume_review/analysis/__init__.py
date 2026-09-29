"""Analysis: two-stage, evidence-backed assessment (PRD section 7).

Stage one (neutral factual extraction) is deterministic and lives in
:mod:`resume_review.ingest`. Stage two (job-specific criterion assessment) is
produced by a restricted model route and arrives here as an untrusted payload.

This package currently exposes the validator. :mod:`validate` is pure: it reads
a payload plus the requested inputs and returns an outcome. It does not import
:mod:`resume_review.db` or :mod:`resume_review.actions`, performs no I/O, and
never sets a review decision.
"""

from __future__ import annotations

from .validate import (
    ANALYSIS_SCHEMA_VERSION,
    Problem,
    ProblemCode,
    ValidationOutcome,
    validate_analysis_result,
)

__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "Problem",
    "ProblemCode",
    "ValidationOutcome",
    "validate_analysis_result",
]
