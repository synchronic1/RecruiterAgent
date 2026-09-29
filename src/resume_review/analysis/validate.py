"""Validate a model-produced analysis payload before anything is committed.

Authority: PRD sections 7.1 (two-stage analysis), 7.2 (evidence validation
limits), 7.3 (result shape) and 11.2 (required invariants), plus the AGENTS.md
rule that model output is data and is validated against a versioned contract
before it reaches the database.

What this module can and cannot do
----------------------------------

Per PRD 7.2 the helper can verify **schema, permitted criterion IDs, source
revision, locator existence, and whether a quoted excerpt occurs in the
identified source span**. Those checks do **not** prove that an interpretation
is correct. Passing validation means the payload is well-formed, bound to the
inputs that were requested, and cites text that is really there; it does **not**
mean the assessment is semantically right. Human evaluation with visible
evidence is still required. Do not describe a validated result as "correct".

Purity
------

Validation is pure. It performs no I/O: it does not open files, does not touch
the database, does not import :mod:`resume_review.db` or
:mod:`resume_review.actions`, and never sets a review decision. A validated
payload is a *candidate* result; committing it is another layer's job.

The ``not_found`` result is valid and accepted. It means "the processed document
did not establish this criterion" (PRD 7.1). It is not "unqualified", it is
never a failure of validation, and it is never converted into a negative
finding.

Repairable vs. non-repairable problems
--------------------------------------

:class:`Problem.repairable` distinguishes two kinds of failure so the caller can
decide whether one bounded repair turn is legitimate:

* ``repairable=True`` for **structural / formatting** problems a single repair
  turn can fix without inventing anything: a missing field, a wrong type, a bad
  enum value such as an unknown ``result``, or a criterion ID the model invented
  (the model is told the permitted set, so it can drop or correct the ID).
* ``repairable=False`` for **semantic** problems, where asking the model to
  "fix" the output would invite it to fabricate support: a quote that does not
  occur in the cited span, a span ID that does not exist, a ``supported``
  assessment with no located evidence, or a payload computed for a different
  document/revision/criteria version.

The whitespace trap
-------------------

Both the quote and the cited span's text are normalized with the **same**
function -- :func:`resume_review.models.normalize_ws` (via ``Span.normalized``,
which calls the same function) -- before the containment test. Normalizing one
side only, or comparing raw strings, would let a legitimate quote fail over a
line wrap, or let a fabricated quote pass by inserting whitespace. We use the
shared normalizer rather than a private copy so the check cannot drift from the
extraction layer.

Collapsing whitespace alone is not enough. A quote that normalizes to the empty
string is a substring of *every* string, so a whitespace-only quote would
"match" any span and could carry a ``supported`` assessment with no real
excerpt. The containment test therefore also requires the normalized quote to be
non-empty; a blank quote is rejected as ``QUOTE_NOT_FOUND`` and is not
repairable, because asking the model to "fix" it would invite it to invent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import Code
from ..models import (
    AnalysisResult,
    Criterion,
    CriterionAssessment,
    CriterionResult,
    EvidenceItem,
    Span,
    SuggestedTask,
    jsonable,
    normalize_ws,
)

__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "ProblemCode",
    "Problem",
    "ValidationOutcome",
    "validate_analysis_result",
]

#: The analysis-result schema this build understands. The model is asked to emit
#: exactly this in ``schema_version``. Kept in sync with
#: ``openclaw_adapter.prompts.PROMPT_SCHEMA_VERSION`` and the request skeleton; a
#: test asserts the two agree so the prompt and the validator cannot drift apart.
ANALYSIS_SCHEMA_VERSION = "1.0"

#: Accepted criterion results (PRD 7.1). ``not_found`` is first-class and valid.
_VALID_RESULTS = frozenset(str(r.value) for r in CriterionResult)


class ProblemCode:
    """Stable problem codes emitted by :func:`validate_analysis_result`.

    Where a code already exists in :class:`resume_review.errors.Code` it is
    reused rather than re-spelled, so callers can route on one vocabulary.
    """

    SCHEMA_VERSION = Code.ANALYSIS_SCHEMA_INVALID
    SCHEMA_SHAPE = "ANALYSIS_SCHEMA_SHAPE"
    DOCUMENT_MISMATCH = "ANALYSIS_DOCUMENT_MISMATCH"
    REVISION_MISMATCH = Code.ANALYSIS_REVISION_MISMATCH
    CRITERIA_VERSION_MISMATCH = Code.ANALYSIS_STALE_RESULT
    UNKNOWN_CRITERION = Code.ANALYSIS_UNKNOWN_CRITERION
    DUPLICATE_CRITERION = "ANALYSIS_DUPLICATE_CRITERION"
    UNKNOWN_EVIDENCE = "ANALYSIS_UNKNOWN_EVIDENCE"
    DUPLICATE_EVIDENCE_ID = "ANALYSIS_DUPLICATE_EVIDENCE_ID"
    SPAN_NOT_FOUND = Code.EVIDENCE_SPAN_NOT_FOUND
    QUOTE_NOT_FOUND = Code.EVIDENCE_QUOTE_NOT_FOUND
    INVALID_RESULT = "ANALYSIS_INVALID_RESULT"
    SUPPORTED_WITHOUT_EVIDENCE = "ANALYSIS_SUPPORTED_WITHOUT_EVIDENCE"


@dataclass
class Problem:
    """One reason a payload was not accepted.

    ``path`` locates the offending field (``"criteria[2].result"``). ``detail``
    is a human-readable explanation carrying **identifiers only** -- never
    applicant quote text -- so the outcome is safe to log and to show.
    """

    code: str
    path: str
    detail: str
    repairable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
            "repairable": self.repairable,
        }


@dataclass
class ValidationOutcome:
    """Result of validating one payload.

    ``ok`` is true exactly when there are no problems. ``result`` carries the
    parsed :class:`~resume_review.models.AnalysisResult` **only** on success; a
    payload with any problem yields ``result=None``. A partially valid payload is
    never returned as a result, so a caller cannot accidentally commit one.
    """

    problems: list[Problem] = field(default_factory=list)
    result: AnalysisResult | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def repairable(self) -> bool:
        """Whether a single bounded repair turn is legitimate.

        True only when every problem is a structural/formatting one. A payload
        that is clean is trivially repairable (nothing to fix).
        """
        return all(p.repairable for p in self.problems)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "repairable": self.repairable,
            "problems": [p.to_dict() for p in self.problems],
            "result": jsonable(self.result) if self.result is not None else None,
        }


def _is_int(value: Any) -> bool:
    # bool is a subclass of int; a JSON true must not satisfy a revision field.
    return isinstance(value, int) and not isinstance(value, bool)


def _collect_evidence_refs(value: Any, path: str, problems: list[Problem]) -> list[str]:
    """Parse an ``evidence_ids`` field into a list of non-empty strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, path, "must be an array of evidence ids", True)
        )
        return []
    refs: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            problems.append(
                Problem(
                    ProblemCode.SCHEMA_SHAPE,
                    f"{path}[{index}]",
                    "evidence reference must be a non-empty string",
                    True,
                )
            )
            continue
        refs.append(entry)
    return refs


def validate_analysis_result(
    *,
    payload: Mapping[str, Any],
    document_id: str,
    source_revision: int,
    criteria_version: int,
    criteria: Sequence[Criterion],
    spans: Sequence[Span],
) -> ValidationOutcome:
    """Validate ``payload`` against the requested inputs and the frozen schema.

    Every failure is reported as a :class:`Problem`; validation never raises for
    bad model output. See the module docstring for what a passing outcome does
    and does not prove.
    """
    problems: list[Problem] = []

    if not isinstance(payload, Mapping):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, "", "payload must be a JSON object", True)
        )
        return ValidationOutcome(problems=problems, result=None)

    permitted = {c.criterion_id for c in (criteria or ())}
    span_by_id = {s.span_id: s for s in (spans or ())}

    # -- schema version -----------------------------------------------------
    raw_schema = payload.get("schema_version")
    if not isinstance(raw_schema, str) or not raw_schema:
        problems.append(
            Problem(
                ProblemCode.SCHEMA_SHAPE,
                "schema_version",
                "schema_version is missing or not a string",
                True,
            )
        )
    elif raw_schema != ANALYSIS_SCHEMA_VERSION:
        # A different contract version is not a formatting slip to be corrected
        # in a repair turn; the payload was produced under a contract we do not
        # understand, so it cannot be trusted.
        problems.append(
            Problem(
                ProblemCode.SCHEMA_VERSION,
                "schema_version",
                f"payload schema_version {raw_schema!r} does not match "
                f"the supported version {ANALYSIS_SCHEMA_VERSION!r}",
                False,
            )
        )

    # -- binding to the requested inputs ------------------------------------
    raw_document = payload.get("document_id")
    if not isinstance(raw_document, str) or not raw_document:
        problems.append(
            Problem(
                ProblemCode.SCHEMA_SHAPE,
                "document_id",
                "document_id is missing or not a string",
                True,
            )
        )
    elif raw_document != document_id:
        problems.append(
            Problem(
                ProblemCode.DOCUMENT_MISMATCH,
                "document_id",
                f"payload document_id {raw_document!r} does not match the "
                f"requested document_id {document_id!r}",
                False,
            )
        )

    raw_revision = payload.get("source_revision")
    if not _is_int(raw_revision):
        problems.append(
            Problem(
                ProblemCode.SCHEMA_SHAPE,
                "source_revision",
                "source_revision is missing or not an integer",
                True,
            )
        )
    elif raw_revision != source_revision:
        # Superseded input: retained for history maybe, never the current result.
        problems.append(
            Problem(
                ProblemCode.REVISION_MISMATCH,
                "source_revision",
                f"payload source_revision {raw_revision} does not match the "
                f"requested revision {source_revision}",
                False,
            )
        )

    raw_criteria_version = payload.get("criteria_version")
    if not _is_int(raw_criteria_version):
        problems.append(
            Problem(
                ProblemCode.SCHEMA_SHAPE,
                "criteria_version",
                "criteria_version is missing or not an integer",
                True,
            )
        )
    elif raw_criteria_version != criteria_version:
        problems.append(
            Problem(
                ProblemCode.CRITERIA_VERSION_MISMATCH,
                "criteria_version",
                f"payload criteria_version {raw_criteria_version} does not match "
                f"the requested criteria version {criteria_version}",
                False,
            )
        )

    # -- evidence: structure, span existence, and quote containment ----------
    evidence_items: list[EvidenceItem] = []
    declared_evidence_ids: set[str] = set()
    seen_evidence_ids: set[str] = set()
    located_evidence_ids: set[str] = set()
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, "evidence", "evidence must be an array", True)
        )
    else:
        for index, entry in enumerate(raw_evidence):
            path = f"evidence[{index}]"
            if not isinstance(entry, Mapping):
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, path, "evidence item must be an object", True)
                )
                continue

            evidence_id = entry.get("id")
            if not isinstance(evidence_id, str) or not evidence_id:
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.id",
                        "evidence id is missing or not a string",
                        True,
                    )
                )
                evidence_id = ""
            elif evidence_id in seen_evidence_ids:
                problems.append(
                    Problem(
                        ProblemCode.DUPLICATE_EVIDENCE_ID,
                        f"{path}.id",
                        f"evidence id {evidence_id!r} appears more than once",
                        True,
                    )
                )
            else:
                seen_evidence_ids.add(evidence_id)

            span_id = entry.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.span_id",
                        "span_id is missing or not a string",
                        True,
                    )
                )
                span_id = ""

            quote = entry.get("quote")
            if not isinstance(quote, str) or not quote:
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.quote",
                        "quote is missing or not a string",
                        True,
                    )
                )
                quote = ""

            locator = entry.get("locator", {})
            if locator is None:
                locator = {}
            if not isinstance(locator, Mapping):
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.locator",
                        "locator must be an object",
                        True,
                    )
                )
                locator = {}

            if evidence_id:
                declared_evidence_ids.add(evidence_id)

            span_ok = True
            quote_ok = True
            if span_id:
                span = span_by_id.get(span_id)
                if span is None:
                    # Citing a span the extractor never produced is a
                    # fabrication-class failure. Repair would mean inventing a
                    # span, so it is not repairable.
                    problems.append(
                        Problem(
                            ProblemCode.SPAN_NOT_FOUND,
                            f"{path}.span_id",
                            f"span_id {span_id!r} is not present in the supplied spans",
                            False,
                        )
                    )
                    span_ok = False
                elif quote:
                    # Anti-fabrication check: the quote must occur *in this
                    # span's text* after the shared whitespace normalization.
                    # Both sides use normalize_ws / Span.normalized. A quote that
                    # normalizes to the empty string carries no excerpt at all --
                    # and because the empty string is a substring of every string
                    # it would otherwise "match" any span, letting a whitespace-only
                    # quote support a `supported` assessment with no real evidence.
                    normalized_quote = normalize_ws(quote)
                    if not normalized_quote or normalized_quote not in span.normalized:
                        problems.append(
                            Problem(
                                ProblemCode.QUOTE_NOT_FOUND,
                                f"{path}.quote",
                                f"quote does not occur in the cited span {span_id!r} "
                                "after whitespace normalization",
                                False,
                            )
                        )
                        quote_ok = False

            if evidence_id and span_ok and quote_ok and span_id and quote:
                located_evidence_ids.add(evidence_id)

            evidence_items.append(
                EvidenceItem(
                    id=evidence_id,
                    span_id=span_id,
                    quote=quote,
                    locator=dict(locator),
                )
            )

    # -- summary ------------------------------------------------------------
    summary_text = ""
    summary_evidence_ids: list[str] = []
    raw_summary = payload.get("summary")
    if not isinstance(raw_summary, Mapping):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, "summary", "summary must be an object", True)
        )
    else:
        raw_text = raw_summary.get("text", "")
        if raw_text is None:
            raw_text = ""
        if not isinstance(raw_text, str):
            problems.append(
                Problem(ProblemCode.SCHEMA_SHAPE, "summary.text", "summary text must be a string", True)
            )
            raw_text = ""
        summary_text = raw_text
        summary_evidence_ids = _collect_evidence_refs(
            raw_summary.get("evidence_ids"), "summary.evidence_ids", problems
        )
        _check_evidence_refs(summary_evidence_ids, "summary.evidence_ids", declared_evidence_ids, problems)

    # -- criteria -----------------------------------------------------------
    assessments: list[CriterionAssessment] = []
    seen_criteria: set[str] = set()
    raw_criteria = payload.get("criteria")
    if not isinstance(raw_criteria, list):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, "criteria", "criteria must be an array", True)
        )
    else:
        for index, entry in enumerate(raw_criteria):
            path = f"criteria[{index}]"
            if not isinstance(entry, Mapping):
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, path, "criterion entry must be an object", True)
                )
                continue

            criterion_id_ok = True
            criterion_id = entry.get("criterion_id")
            if not isinstance(criterion_id, str) or not criterion_id:
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.criterion_id",
                        "criterion_id is missing or not a string",
                        True,
                    )
                )
                criterion_id = ""
                criterion_id_ok = False
            elif criterion_id not in permitted:
                # The model does not get to define what is examined (PRD 7.1);
                # an invented ID is a hard failure but repairable (drop it).
                criterion_id_ok = False
                problems.append(
                    Problem(
                        ProblemCode.UNKNOWN_CRITERION,
                        f"{path}.criterion_id",
                        f"criterion_id {criterion_id!r} is not in the permitted criteria set",
                        True,
                    )
                )

            if criterion_id and criterion_id_ok:
                if criterion_id in seen_criteria:
                    # PRD 7.1: assess each approved criterion exactly once. A
                    # repeated id would otherwise persist two assessments (and two
                    # synthesized evidence rows with the same key) for one
                    # criterion, so it is rejected. Repairable: the model can drop
                    # the duplicate without inventing anything.
                    problems.append(
                        Problem(
                            ProblemCode.DUPLICATE_CRITERION,
                            f"{path}.criterion_id",
                            f"criterion_id {criterion_id!r} is assessed more than once",
                            True,
                        )
                    )
                else:
                    seen_criteria.add(criterion_id)

            result: CriterionResult | None = None
            raw_result = entry.get("result")
            if not isinstance(raw_result, str) or raw_result not in _VALID_RESULTS:
                problems.append(
                    Problem(
                        ProblemCode.INVALID_RESULT,
                        f"{path}.result",
                        "result must be one of supported, not_found, unclear, needs_manual_review",
                        True,
                    )
                )
            else:
                result = CriterionResult(raw_result)

            explanation = entry.get("explanation", "")
            if explanation is None:
                explanation = ""
            if not isinstance(explanation, str):
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"{path}.explanation",
                        "explanation must be a string",
                        True,
                    )
                )
                explanation = ""

            evidence_ids = _collect_evidence_refs(
                entry.get("evidence_ids"), f"{path}.evidence_ids", problems
            )
            _check_evidence_refs(
                evidence_ids, f"{path}.evidence_ids", declared_evidence_ids, problems
            )

            # "supported" requires located evidence (PRD 7.1: relevant
            # applicant-reported evidence was located). Only evidence that
            # itself passed the span and quote checks counts as located; asking
            # the model to add support would invite fabrication, so this is not
            # repairable.
            if result is CriterionResult.SUPPORTED:
                if not any(eid in located_evidence_ids for eid in evidence_ids):
                    problems.append(
                        Problem(
                            ProblemCode.SUPPORTED_WITHOUT_EVIDENCE,
                            path,
                            "result is 'supported' but the criterion cites no evidence "
                            "that was located in the cited span",
                            False,
                        )
                    )

            if criterion_id and criterion_id_ok and result is not None:
                assessments.append(
                    CriterionAssessment(
                        criterion_id=criterion_id,
                        result=result,
                        explanation=explanation,
                        evidence_ids=evidence_ids,
                    )
                )

    # -- suggested tasks ----------------------------------------------------
    suggested_tasks: list[SuggestedTask] = []
    raw_tasks = payload.get("suggested_tasks")
    if raw_tasks is None:
        raw_tasks = []
    if not isinstance(raw_tasks, list):
        problems.append(
            Problem(
                ProblemCode.SCHEMA_SHAPE,
                "suggested_tasks",
                "suggested_tasks must be an array",
                True,
            )
        )
    else:
        for index, entry in enumerate(raw_tasks):
            path = f"suggested_tasks[{index}]"
            if not isinstance(entry, Mapping):
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, path, "suggested task must be an object", True)
                )
                continue
            task_type = entry.get("type", "general")
            if not isinstance(task_type, str) or not task_type:
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, f"{path}.type", "type must be a string", True)
                )
                task_type = "general"
            title = entry.get("title", "")
            if not isinstance(title, str):
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, f"{path}.title", "title must be a string", True)
                )
                title = ""
            detail = entry.get("detail", "")
            if detail is None:
                detail = ""
            if not isinstance(detail, str):
                problems.append(
                    Problem(ProblemCode.SCHEMA_SHAPE, f"{path}.detail", "detail must be a string", True)
                )
                detail = ""
            criterion_id = entry.get("criterion_id")
            if criterion_id is not None:
                if not isinstance(criterion_id, str) or not criterion_id:
                    problems.append(
                        Problem(
                            ProblemCode.SCHEMA_SHAPE,
                            f"{path}.criterion_id",
                            "criterion_id must be a non-empty string or null",
                            True,
                        )
                    )
                    criterion_id = None
                elif criterion_id not in permitted:
                    problems.append(
                        Problem(
                            ProblemCode.UNKNOWN_CRITERION,
                            f"{path}.criterion_id",
                            f"criterion_id {criterion_id!r} is not in the permitted criteria set",
                            True,
                        )
                    )
            suggested_tasks.append(
                SuggestedTask(
                    type=task_type,
                    title=title,
                    criterion_id=criterion_id,
                    detail=detail,
                )
            )

    # -- warnings -----------------------------------------------------------
    warnings: list[str] = []
    raw_warnings = payload.get("warnings")
    if raw_warnings is None:
        raw_warnings = []
    if not isinstance(raw_warnings, list):
        problems.append(
            Problem(ProblemCode.SCHEMA_SHAPE, "warnings", "warnings must be an array", True)
        )
    else:
        for index, entry in enumerate(raw_warnings):
            if not isinstance(entry, str):
                problems.append(
                    Problem(
                        ProblemCode.SCHEMA_SHAPE,
                        f"warnings[{index}]",
                        "warning must be a string",
                        True,
                    )
                )
                continue
            warnings.append(entry)

    if problems:
        # Never hand back a partially valid result: a caller must not be able to
        # commit a payload that failed any check.
        return ValidationOutcome(problems=problems, result=None)

    result = AnalysisResult(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        document_id=document_id,
        source_revision=source_revision,
        criteria_version=criteria_version,
        summary_text=summary_text,
        summary_evidence_ids=summary_evidence_ids,
        criteria=assessments,
        evidence=evidence_items,
        suggested_tasks=suggested_tasks,
        warnings=warnings,
    )
    return ValidationOutcome(problems=[], result=result)


def _check_evidence_refs(
    refs: Sequence[str],
    path: str,
    declared: set[str],
    problems: list[Problem],
) -> None:
    """Every referenced evidence id must exist in the payload's evidence list."""
    for index, ref in enumerate(refs):
        if ref not in declared:
            problems.append(
                Problem(
                    ProblemCode.UNKNOWN_EVIDENCE,
                    f"{path}[{index}]",
                    f"evidence id {ref!r} is not present in the payload evidence list",
                    True,
                )
            )
