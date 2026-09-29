"""Tests for the analysis-result validator (PRD 7.1-7.3, 11.2).

These exercise real code on synthetic data. No fixture is live; no model is
called. The payload shapes mirror the illustrative example in PRD 7.3.

The two traps named in the build task get their own tests:

* whitespace-only differences in a legitimate quote must still pass (the quote
  and the span are normalized with the same function), and
* a quote that occurs in a *different* span than the one cited must fail.
"""

from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from resume_review.analysis import (  # noqa: E402
    ANALYSIS_SCHEMA_VERSION,
    ProblemCode,
    ValidationOutcome,
    validate_analysis_result,
)
from resume_review.analysis import validate as validate_module  # noqa: E402
from resume_review.models import (  # noqa: E402
    Criterion,
    CriterionResult,
    Span,
)

DOCUMENT_ID = "doc_synth_001"
SOURCE_REVISION = 2
CRITERIA_VERSION = 3

#: Two spans with disjoint text. ``span_a`` deliberately contains a doubled
#: space and a newline so the whitespace normalization is actually exercised.
SPANS = [
    Span(
        span_id="span_a",
        text="Coordinated    subcontractors\non commercial renovations.",
        locator={"page": 1},
    ),
    Span(
        span_id="span_b",
        text="Managed a crew of twelve electricians.",
        locator={"page": 1},
    ),
]

CRITERIA = [
    Criterion(criterion_id="cr_01", version=CRITERIA_VERSION, definition="Coordinates subcontractors"),
    Criterion(criterion_id="cr_02", version=CRITERIA_VERSION, definition="Manages a crew"),
]


def base_payload() -> dict:
    """A clean, fully valid payload. Each test mutates its own deep copy."""
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "document_id": DOCUMENT_ID,
        "source_revision": SOURCE_REVISION,
        "criteria_version": CRITERIA_VERSION,
        "summary": {
            "text": "Reports commercial renovation coordination.",
            "evidence_ids": ["ev_1"],
        },
        "criteria": [
            {
                "criterion_id": "cr_01",
                "result": "supported",
                "explanation": "The resume describes subcontractor coordination.",
                "evidence_ids": ["ev_1"],
            }
        ],
        "evidence": [
            {
                "id": "ev_1",
                "span_id": "span_a",
                "locator": {"page": 1},
                "quote": "Coordinated subcontractors on commercial renovations.",
            }
        ],
        "suggested_tasks": [],
        "warnings": [],
    }


def run(payload: dict) -> ValidationOutcome:
    return validate_analysis_result(
        payload=payload,
        document_id=DOCUMENT_ID,
        source_revision=SOURCE_REVISION,
        criteria_version=CRITERIA_VERSION,
        criteria=CRITERIA,
        spans=SPANS,
    )


def codes(outcome: ValidationOutcome) -> list[str]:
    return [p.code for p in outcome.problems]


def problem_with(outcome: ValidationOutcome, code: str):
    matches = [p for p in outcome.problems if p.code == code]
    assert matches, f"expected a {code!r} problem, got {codes(outcome)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_clean_payload_passes():
    outcome = run(base_payload())
    assert outcome.ok is True
    assert outcome.problems == []
    assert outcome.repairable is True
    assert outcome.result is not None
    result = outcome.result
    assert result.schema_version == ANALYSIS_SCHEMA_VERSION
    assert result.document_id == DOCUMENT_ID
    assert result.source_revision == SOURCE_REVISION
    assert result.criteria_version == CRITERIA_VERSION
    assert result.summary_text == "Reports commercial renovation coordination."
    assert result.summary_evidence_ids == ["ev_1"]
    assert len(result.evidence) == 1
    assert result.evidence[0].id == "ev_1"
    assert result.evidence[0].span_id == "span_a"
    assert len(result.criteria) == 1
    assert result.criteria[0].criterion_id == "cr_01"
    assert result.criteria[0].result is CriterionResult.SUPPORTED
    assert result.criteria[0].evidence_ids == ["ev_1"]


def test_outcome_to_dict_round_trip_shape():
    outcome = run(base_payload())
    payload = outcome.to_dict()
    assert payload["ok"] is True
    assert payload["problems"] == []
    assert payload["result"]["document_id"] == DOCUMENT_ID
    assert payload["result"]["criteria"][0]["result"] == "supported"

    bad = base_payload()
    bad["criteria"][0]["result"] = "reject"
    bad_outcome = run(bad)
    bad_dict = bad_outcome.to_dict()
    assert bad_dict["ok"] is False
    assert bad_dict["result"] is None
    assert bad_dict["problems"][0]["code"] == ProblemCode.INVALID_RESULT
    assert bad_dict["problems"][0]["repairable"] is True


# ---------------------------------------------------------------------------
# Trap 1: whitespace normalization (both sides, same function)
# ---------------------------------------------------------------------------
def test_whitespace_only_differences_in_legitimate_quote_pass():
    payload = base_payload()
    # Same words, wildly different whitespace than the span text.
    payload["evidence"][0]["quote"] = (
        "Coordinated\xa0subcontractors\t on\n\n commercial   renovations."
    )
    outcome = run(payload)
    assert outcome.ok is True, codes(outcome)


def test_lookalike_quote_with_one_different_word_fails():
    payload = base_payload()
    payload["evidence"][0]["quote"] = "Coordinated contractors on commercial renovations."
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.QUOTE_NOT_FOUND)
    assert problem.repairable is False
    assert problem.path == "evidence[0].quote"


def test_reordered_quote_fails():
    payload = base_payload()
    payload["evidence"][0]["quote"] = "commercial renovations on subcontractors Coordinated"
    outcome = run(payload)
    assert outcome.ok is False
    assert ProblemCode.QUOTE_NOT_FOUND in codes(outcome)


# ---------------------------------------------------------------------------
# Trap 2: a quote must occur in the span it is cited against
# ---------------------------------------------------------------------------
def test_quote_from_another_span_is_rejected():
    payload = base_payload()
    # This text really exists -- in span_b -- not in the cited span_a.
    payload["evidence"][0]["span_id"] = "span_a"
    payload["evidence"][0]["quote"] = "Managed a crew of twelve electricians."
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.QUOTE_NOT_FOUND)
    assert problem.repairable is False
    assert problem.path == "evidence[0].quote"


def test_quote_matches_only_when_the_cited_span_is_correct():
    payload = base_payload()
    payload["evidence"][0]["span_id"] = "span_b"
    payload["evidence"][0]["quote"] = "Managed a crew of twelve electricians."
    outcome = run(payload)
    assert outcome.ok is True, codes(outcome)


def test_span_not_present_fails_and_is_not_repairable():
    payload = base_payload()
    payload["evidence"][0]["span_id"] = "span_does_not_exist"
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.SPAN_NOT_FOUND)
    assert problem.repairable is False
    assert problem.path == "evidence[0].span_id"


# ---------------------------------------------------------------------------
# Criteria: permitted IDs and valid results
# ---------------------------------------------------------------------------
def test_invented_criterion_id_fails_and_is_repairable():
    payload = base_payload()
    payload["criteria"][0]["criterion_id"] = "cr_model_invented"
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.UNKNOWN_CRITERION)
    assert problem.repairable is True
    assert problem.path == "criteria[0].criterion_id"


def test_invalid_result_value_fails_and_is_repairable():
    payload = base_payload()
    payload["criteria"][0]["result"] = "reject"
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.INVALID_RESULT)
    assert problem.repairable is True


def test_suggested_task_with_invented_criterion_fails():
    payload = base_payload()
    payload["suggested_tasks"] = [
        {"type": "verify", "title": "Check dates", "criterion_id": "cr_bogus", "detail": ""}
    ]
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.UNKNOWN_CRITERION)
    assert problem.path == "suggested_tasks[0].criterion_id"
    assert problem.repairable is True


# ---------------------------------------------------------------------------
# supported requires located evidence; not_found / unclear do not
# ---------------------------------------------------------------------------
def test_supported_with_no_evidence_fails_and_is_not_repairable():
    payload = base_payload()
    payload["criteria"] = [
        {"criterion_id": "cr_01", "result": "supported", "explanation": "", "evidence_ids": []}
    ]
    payload["summary"] = {"text": "No evidence gathered.", "evidence_ids": []}
    payload["evidence"] = []
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.SUPPORTED_WITHOUT_EVIDENCE)
    assert problem.repairable is False
    assert problem.path == "criteria[0]"


def test_supported_with_only_a_failed_quote_is_not_supported():
    payload = base_payload()
    payload["evidence"][0]["quote"] = "A sentence that is not in the span at all."
    outcome = run(payload)
    assert outcome.ok is False
    found = set(codes(outcome))
    assert ProblemCode.QUOTE_NOT_FOUND in found
    assert ProblemCode.SUPPORTED_WITHOUT_EVIDENCE in found
    assert problem_with(outcome, ProblemCode.SUPPORTED_WITHOUT_EVIDENCE).repairable is False


def test_not_found_with_no_evidence_is_valid_and_accepted():
    payload = base_payload()
    payload["criteria"] = [
        {"criterion_id": "cr_01", "result": "not_found", "explanation": "", "evidence_ids": []},
        {"criterion_id": "cr_02", "result": "unclear", "explanation": "", "evidence_ids": []},
    ]
    payload["summary"] = {"text": "Neither criterion was established.", "evidence_ids": []}
    payload["evidence"] = []
    outcome = run(payload)
    assert outcome.ok is True, codes(outcome)
    assert outcome.result is not None
    results = {c.criterion_id: c.result for c in outcome.result.criteria}
    assert results["cr_01"] is CriterionResult.NOT_FOUND
    assert results["cr_02"] is CriterionResult.UNCLEAR
    # not_found is never a failure and sets no decision.
    assert outcome.problems == []


# ---------------------------------------------------------------------------
# Binding: document / source revision / criteria version / schema version
# ---------------------------------------------------------------------------
def test_superseded_source_revision_fails():
    payload = base_payload()
    payload["source_revision"] = SOURCE_REVISION - 1
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.REVISION_MISMATCH)
    assert problem.repairable is False
    assert outcome.result is None


def test_superseded_criteria_version_fails():
    payload = base_payload()
    payload["criteria_version"] = CRITERIA_VERSION + 1
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.CRITERIA_VERSION_MISMATCH)
    assert problem.repairable is False


def test_document_id_mismatch_fails():
    payload = base_payload()
    payload["document_id"] = "doc_other"
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.DOCUMENT_MISMATCH)
    assert problem.repairable is False


def test_schema_version_mismatch_fails():
    payload = base_payload()
    payload["schema_version"] = "2.0"
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.SCHEMA_VERSION)
    assert problem.repairable is False


def test_missing_field_is_repairable_structural_problem():
    payload = base_payload()
    del payload["source_revision"]
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.SCHEMA_SHAPE)
    assert problem.repairable is True
    # Every problem is structural, so one repair turn is legitimate.
    assert outcome.repairable is True
    assert outcome.result is None


# ---------------------------------------------------------------------------
# Evidence references must exist; ids must be unambiguous
# ---------------------------------------------------------------------------
def test_dangling_evidence_reference_fails():
    payload = base_payload()
    payload["criteria"][0]["evidence_ids"] = ["ev_1", "ev_missing"]
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.UNKNOWN_EVIDENCE)
    assert problem.path == "criteria[0].evidence_ids[1]"
    assert problem.repairable is True


def test_dangling_summary_evidence_reference_fails():
    payload = base_payload()
    payload["summary"]["evidence_ids"] = ["ev_ghost"]
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.UNKNOWN_EVIDENCE)
    assert problem.path == "summary.evidence_ids[0]"


def test_duplicate_evidence_ids_fail():
    payload = base_payload()
    payload["evidence"].append(
        {
            "id": "ev_1",
            "span_id": "span_b",
            "locator": {"page": 1},
            "quote": "Managed a crew of twelve electricians.",
        }
    )
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.DUPLICATE_EVIDENCE_ID)
    assert problem.repairable is True


# ---------------------------------------------------------------------------
# Purity and contract sync
# ---------------------------------------------------------------------------
def _imports_of(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append((node.level, node.module or ""))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((0, alias.name))
    return found


def test_analysis_validate_is_pure():
    """No DB/actions imports, no absolute I/O modules, stdlib only."""
    imports = _imports_of(Path(validate_module.__file__))
    relative = {module for level, module in imports if level > 0}
    assert relative <= {"errors", "models"}, relative

    forbidden_absolute = {"os", "shutil", "sqlite3", "pathlib", "io"}
    for level, module in imports:
        if level > 0:
            continue
        assert not module.startswith("resume_review.db")
        assert not module.startswith("resume_review.actions")
        assert module not in forbidden_absolute, module


def test_schema_version_matches_prompt_skeleton():
    from resume_review.openclaw_adapter.prompts import PROMPT_SCHEMA_VERSION

    assert ANALYSIS_SCHEMA_VERSION == PROMPT_SCHEMA_VERSION


def test_validator_does_not_mutate_its_inputs():
    payload = base_payload()
    snapshot = copy.deepcopy(payload)
    run(payload)
    assert payload == snapshot
