"""Adversarial tests for the analysis validator, pipeline and queue.

The catastrophe under test is a fabricated quote becoming an accepted
``supported`` assessment, and a stale result overwriting a current one. Every
test here makes real assertions over real code on synthetic data; the only
substituted piece is the model client, which is a scripted stub.

PRD authority: 6.3, 6.4, 7.1, 7.2, 11.2 and 14. AGENTS.md: model output is data,
analysis never writes a human decision, and ``not_found`` is never a negative.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis import ANALYSIS_SCHEMA_VERSION
from resume_review.analysis.pipeline import Completion, Pipeline
from resume_review.analysis.queue import AnalysisQueue, HostCapacity
from resume_review.analysis.validate import (
    ProblemCode,
    validate_analysis_result,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.ingest import ExtractionCache, extract_txt
from resume_review.ingest.stabilize import Stabilizer
from resume_review.models import (
    DEFAULT_LIMITS,
    Criterion,
    CriterionResult,
    ModelRoute,
    ProcessingState,
    ReviewState,
    Span,
)
from resume_review.openclaw_adapter.policy import RouteAttestation, RoutePolicy
from resume_review.openclaw_adapter.prompts import PROMPT_VERSION
from resume_review.util import now_iso

DOCUMENT_ID = "doc_adv_001"
SOURCE_REVISION = 2
CRITERIA_VERSION = 3

QUOTE = "Coordinated subcontractors on commercial renovations."
RESUME_TEXT = (
    "Jamie Rivera\n"
    "Operations Manager\n"
    f"{QUOTE}\n"
    "Managed a crew of twelve electricians.\n"
)

SPANS = [
    Span(
        span_id="span_a",
        text="Coordinated    subcontractors\non commercial renovations.",
        locator={"page": 1},
    ),
    Span(span_id="span_b", text="Managed a crew of twelve electricians.", locator={"page": 1}),
]
CRITERIA = [
    Criterion(criterion_id="cr_01", version=CRITERIA_VERSION, definition="Coordinates subcontractors"),
    Criterion(criterion_id="cr_02", version=CRITERIA_VERSION, definition="Manages a crew"),
]


def base_payload() -> dict[str, Any]:
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "document_id": DOCUMENT_ID,
        "source_revision": SOURCE_REVISION,
        "criteria_version": CRITERIA_VERSION,
        "summary": {"text": "Reports coordination.", "evidence_ids": ["ev_1"]},
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
                "quote": QUOTE,
            }
        ],
        "suggested_tasks": [],
        "warnings": [],
    }


def run(payload: dict[str, Any]):
    return validate_analysis_result(
        payload=payload,
        document_id=DOCUMENT_ID,
        source_revision=SOURCE_REVISION,
        criteria_version=CRITERIA_VERSION,
        criteria=CRITERIA,
        spans=SPANS,
    )


def codes(outcome) -> list[str]:
    return [p.code for p in outcome.problems]


def problem_with(outcome, code: str):
    matches = [p for p in outcome.problems if p.code == code]
    assert matches, f"expected {code!r}, got {codes(outcome)}"
    return matches[0]


# ===========================================================================
# Validator: fabrication
# ===========================================================================
def test_a_quote_not_in_the_cited_span_fails_hard():
    payload = base_payload()
    payload["evidence"][0]["quote"] = "Led a team of forty welders offshore."
    outcome = run(payload)
    assert outcome.ok is False
    assert outcome.result is None
    problem = problem_with(outcome, ProblemCode.QUOTE_NOT_FOUND)
    assert problem.repairable is False
    assert "welder" not in problem.detail, "problem detail must not echo applicant text"


def test_a_quote_from_a_different_span_fails():
    payload = base_payload()
    payload["evidence"][0]["span_id"] = "span_a"
    payload["evidence"][0]["quote"] = "Managed a crew of twelve electricians."
    outcome = run(payload)
    assert outcome.ok is False
    assert ProblemCode.QUOTE_NOT_FOUND in codes(outcome)


def test_a_quote_assembled_from_two_non_adjacent_parts_fails():
    payload = base_payload()
    # Both fragments occur in span_a, but not contiguously.
    payload["evidence"][0]["quote"] = "Coordinated renovations."
    outcome = run(payload)
    assert outcome.ok is False
    assert ProblemCode.QUOTE_NOT_FOUND in codes(outcome)
    assert outcome.result is None


def test_a_homoglyph_quote_fails():
    payload = base_payload()
    # Cyrillic 'о' (U+043E) and 'ѕ' (U+0455) stand in for Latin 'o' and 's'.
    homoglyph = QUOTE.replace("o", "о").replace("s", "ѕ")
    assert homoglyph != QUOTE
    payload["evidence"][0]["quote"] = homoglyph
    outcome = run(payload)
    assert outcome.ok is False
    assert ProblemCode.QUOTE_NOT_FOUND in codes(outcome)


def test_a_quote_that_matches_only_after_stripping_bidi_fails():
    # A bidi override embedded in the span must not let a quote that only
    # matches once that character is stripped pass containment.
    payload = base_payload()
    payload["evidence"][0]["quote"] = "Coordinated subcontractors on commercial renovations."
    span = Span(span_id="span_a", text="Coordinated‮subcontractors on commercial renovations.")
    outcome = validate_analysis_result(
        payload=payload,
        document_id=DOCUMENT_ID,
        source_revision=SOURCE_REVISION,
        criteria_version=CRITERIA_VERSION,
        criteria=CRITERIA,
        spans=[span, SPANS[1]],
    )
    assert outcome.ok is False
    assert ProblemCode.QUOTE_NOT_FOUND in codes(outcome)


def test_a_legitimate_quote_with_different_whitespace_passes():
    payload = base_payload()
    payload["evidence"][0]["quote"] = "Coordinated\xa0subcontractors\t on\n\n commercial   renovations."
    outcome = run(payload)
    assert outcome.ok is True, codes(outcome)


@pytest.mark.parametrize("blank", [" ", "\t", "\n", " ", "\x0c", "  \t "])
def test_a_whitespace_only_quote_is_not_evidence(blank: str):
    """A quote that normalizes to the empty string carries no excerpt.

    The empty string is a substring of every string, so without a guard a
    whitespace-only quote would satisfy containment for any span and could
    support a ``supported`` assessment with no real evidence.
    """
    payload = base_payload()
    payload["evidence"][0]["quote"] = blank
    outcome = run(payload)
    assert outcome.ok is False, f"a blank quote {blank!r} must not be accepted"
    assert outcome.result is None
    problem = problem_with(outcome, ProblemCode.QUOTE_NOT_FOUND)
    assert problem.repairable is False
    # A supported assessment leaning on that quote must also be rejected.
    assert ProblemCode.SUPPORTED_WITHOUT_EVIDENCE in codes(outcome)


def test_a_quote_of_a_real_short_excerpt_still_passes():
    # The guard is only about empty excerpts; a short real quote is legitimate.
    payload = base_payload()
    payload["evidence"][0]["quote"] = "subcontractors"
    assert run(payload).ok is True


def test_the_same_criterion_assessed_twice_is_rejected():
    """PRD 7.1: assess every supplied criterion exactly once.

    A repeated id would persist two assessments (and two synthesized evidence
    rows keyed ``criterion:cr_01``) for one criterion.
    """
    payload = base_payload()
    payload["criteria"] = [
        {"criterion_id": "cr_01", "result": "not_found", "explanation": "", "evidence_ids": []},
        {"criterion_id": "cr_01", "result": "not_found", "explanation": "", "evidence_ids": []},
    ]
    payload["summary"] = {"text": "Nothing.", "evidence_ids": []}
    payload["evidence"] = []
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.DUPLICATE_CRITERION)
    assert problem.repairable is True
    assert problem.path == "criteria[1].criterion_id"


# ===========================================================================
# Validator: structural and semantic classification
# ===========================================================================
def test_supported_with_zero_evidence_fails_not_repairable():
    payload = base_payload()
    payload["criteria"] = [
        {"criterion_id": "cr_01", "result": "supported", "explanation": "", "evidence_ids": []}
    ]
    payload["summary"] = {"text": "Nothing.", "evidence_ids": []}
    payload["evidence"] = []
    outcome = run(payload)
    assert outcome.ok is False
    problem = problem_with(outcome, ProblemCode.SUPPORTED_WITHOUT_EVIDENCE)
    assert problem.repairable is False
    assert outcome.repairable is False


def test_invented_criterion_id_fails_and_is_repairable():
    payload = base_payload()
    payload["criteria"][0]["criterion_id"] = "cr_invented_by_model"
    outcome = run(payload)
    assert outcome.ok is False
    assert problem_with(outcome, ProblemCode.UNKNOWN_CRITERION).repairable is True


def test_result_outside_the_four_permitted_values_fails():
    for bad in ("reject", "keep", "hire", "SUPPORTED", "qualified", ""):
        payload = base_payload()
        payload["criteria"][0]["result"] = bad
        outcome = run(payload)
        assert outcome.ok is False, bad
        assert ProblemCode.INVALID_RESULT in codes(outcome), bad


def test_stale_source_revision_fails_not_repairable():
    payload = base_payload()
    payload["source_revision"] = SOURCE_REVISION - 1
    outcome = run(payload)
    assert outcome.ok is False
    assert problem_with(outcome, ProblemCode.REVISION_MISMATCH).repairable is False
    assert outcome.result is None


def test_stale_criteria_version_fails_not_repairable():
    payload = base_payload()
    payload["criteria_version"] = CRITERIA_VERSION + 1
    outcome = run(payload)
    assert outcome.ok is False
    assert problem_with(outcome, ProblemCode.CRITERIA_VERSION_MISMATCH).repairable is False


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda p: p["evidence"][0].__setitem__("quote", "not present anywhere"), False),
        (lambda p: p["evidence"][0].__setitem__("span_id", "span_missing"), False),
        (lambda p: p["criteria"][0].__setitem__("evidence_ids", []), False),
        (lambda p: p["criteria"][0].__setitem__("result", "reject"), True),
        (lambda p: p["criteria"][0].__setitem__("criterion_id", "cr_bogus"), True),
        (lambda p: p.__setitem__("source_revision", 1), False),
    ],
)
def test_repairability_classification(mutation, expected: bool):
    payload = base_payload()
    mutation(payload)
    outcome = run(payload)
    assert outcome.ok is False
    assert outcome.repairable is expected, (codes(outcome), outcome.repairable)


def test_not_found_is_accepted_and_never_reinterpreted():
    payload = base_payload()
    payload["criteria"] = [
        {"criterion_id": "cr_01", "result": "not_found", "explanation": "", "evidence_ids": []},
        {"criterion_id": "cr_02", "result": "needs_manual_review", "explanation": "", "evidence_ids": []},
    ]
    payload["summary"] = {"text": "Neither criterion was established.", "evidence_ids": []}
    payload["evidence"] = []
    outcome = run(payload)
    assert outcome.ok is True, codes(outcome)
    assert outcome.result is not None
    results = {c.criterion_id: c.result for c in outcome.result.criteria}
    assert results["cr_01"] is CriterionResult.NOT_FOUND
    assert results["cr_02"] is CriterionResult.NEEDS_MANUAL_REVIEW
    assert outcome.problems == []


# ===========================================================================
# Pipeline fixtures
# ===========================================================================
@pytest.fixture
def db(tmp_path: Path):
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_adv", __version__, SCHEMA_VERSION)
    return repository


def make_policy() -> RoutePolicy:
    attestation = RouteAttestation(
        attested_by="ops",
        attested_at=now_iso(),
        no_shell=True,
        no_write_or_edit=True,
        no_browser_control=True,
        no_messaging=True,
        no_credential_read=True,
        no_unrestricted_file_read=True,
        no_cross_session=True,
        no_agent_spawning=True,
        trusted_instruction_workspace=True,
    )
    return RoutePolicy(
        route=ModelRoute.LOCAL_ONLY,
        restricted=True,
        attestation=attestation,
        provider_label="deterministic test route",
    )


def activate_criteria(repo: Repository) -> None:
    repo.create_or_update_job("Operations Manager", "Coordinate crews and subcontractors.")
    repo.create_criteria_proposal("cr_01", "Coordinates subcontractors on commercial work.")
    repo.create_criteria_proposal("cr_02", "Manages a crew of electricians.")
    repo.activate_criteria_version(1, "ops-admin")
    assert repo.active_criteria_version() == 1


def make_pipeline(repo: Repository, root: Path, adapter) -> Pipeline:
    stabilizer = Stabilizer(
        interval_seconds=0.0,
        required_observations=2,
        sleep=lambda _seconds: None,
        limits=DEFAULT_LIMITS,
    )
    return Pipeline(
        repository=repo,
        root=root,
        adapter=adapter,
        route_policy=make_policy(),
        stabilizer=stabilizer,
    )


def cached_spans(repo: Repository, document_id: str, revision: int):
    row = repo.get_revision(document_id, revision)
    assert row is not None
    cache = ExtractionCache(repo.db.connect(), repo.instance_id)
    extracted = cache.get(row["content_sha256"], row["parser_name"], row["parser_version"])
    assert extracted is not None
    return extracted.spans


def payload_for(request, *, bad: bool = False) -> dict[str, Any]:
    criterion_ids = list(request.criterion_ids)
    criteria: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if bad:
        criteria.append(
            {
                "criterion_id": criterion_ids[0],
                "result": "supported",
                "explanation": "claimed",
                "evidence_ids": ["ev_1"],
            }
        )
        evidence.append(
            {"id": "ev_1", "span_id": request.span_ids[0], "locator": {}, "quote": "not in the document"}
        )
    elif criterion_ids:
        criteria.append(
            {
                "criterion_id": criterion_ids[0],
                "result": "supported",
                "explanation": "The document states this.",
                "evidence_ids": ["ev_1"],
            }
        )
        evidence.append(
            {"id": "ev_1", "span_id": request.span_ids[0], "locator": {}, "quote": QUOTE}
        )
        for criterion_id in criterion_ids[1:]:
            criteria.append(
                {
                    "criterion_id": criterion_id,
                    "result": "not_found",
                    "explanation": "Not established.",
                    "evidence_ids": [],
                }
            )
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "document_id": request.document_id,
        "source_revision": request.source_revision,
        "criteria_version": request.criteria_version,
        "summary": {"text": "Neutral summary.", "evidence_ids": ["ev_1"] if evidence else []},
        "criteria": criteria,
        "evidence": evidence,
        "suggested_tasks": [],
        "warnings": [],
    }


class StubAdapter:
    def __init__(self, script: list[str] | None = None) -> None:
        self.calls = 0
        self._script = list(script) if script is not None else None

    def complete(self, request, *, request_id: str | None = None) -> Completion:
        self.calls += 1
        if self._script is not None:
            text = self._script[min(self.calls - 1, len(self._script) - 1)]
        else:
            text = json.dumps(payload_for(request))
        return Completion(
            text=text,
            request_id=request_id,
            route="local_only",
            run_started_at="2026-01-01T00:00:00+00:00",
            run_ended_at="2026-01-01T00:00:01+00:00",
            token_usage=7,
        )


RUN_META = {
    "prompt_version": PROMPT_VERSION,
    "model_route": "local_only",
    "validation_state": "valid",
    "actor": "helper",
}


def make_validated_result(repo, document_id, revision, spans, *, criteria_version=1, quote=QUOTE):
    criteria = repo.list_criteria(criteria_version, approved_only=True)
    payload = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "document_id": document_id,
        "source_revision": revision,
        "criteria_version": criteria_version,
        "summary": {"text": "Neutral summary.", "evidence_ids": ["ev_1"]},
        "criteria": [
            {
                "criterion_id": "cr_01",
                "result": "supported",
                "explanation": "The document states this.",
                "evidence_ids": ["ev_1"],
            }
        ],
        "evidence": [{"id": "ev_1", "span_id": spans[0].span_id, "locator": {}, "quote": quote}],
        "suggested_tasks": [],
        "warnings": [],
    }
    outcome = validate_analysis_result(
        payload=payload,
        document_id=document_id,
        source_revision=revision,
        criteria_version=criteria_version,
        criteria=criteria,
        spans=spans,
    )
    assert outcome.ok, codes(outcome)
    return outcome.result


# ===========================================================================
# Pipeline: superseded result race
# ===========================================================================
def test_revision_change_supersedes_a_pending_result(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"
    first = repo.current_profile(document.id)
    assert first is not None

    spans = cached_spans(repo, document.id, 1)
    stale = make_validated_result(repo, document.id, 1, spans)

    new_bytes = (RESUME_TEXT + "Budget forecasting.\n").encode("utf-8")
    repo.add_revision(
        document.id,
        hashlib.sha256(new_bytes).hexdigest(),
        len(new_bytes),
        "candidate.txt",
        parser_name=extract_txt.PARSER_NAME,
        parser_version=extract_txt.parser_version(),
    )
    assert repo.get_document(document.id).current_revision == 2

    commit = pipeline.commit_analysis(stale, run_meta=RUN_META)
    assert commit.status == "superseded"
    assert commit.is_current is False
    current = repo.current_profile(document.id)
    assert current is not None and current.id == first.id
    row = repo.db.query_one("SELECT is_current FROM profiles WHERE id = ?", (commit.profile_id,))
    assert int(row["is_current"]) == 0


def test_criteria_version_change_supersedes_a_pending_result(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"
    first = repo.current_profile(document.id)
    assert first is not None and first.criteria_version == 1

    spans = cached_spans(repo, document.id, 1)
    stale = make_validated_result(repo, document.id, 1, spans, criteria_version=1)

    # A new criterion is proposed and activated; the active version advances.
    repo.create_criteria_proposal("cr_03", "Schedules field crews.")
    repo.activate_criteria_version(2, "ops-admin")
    assert repo.active_criteria_version() == 2

    commit = pipeline.commit_analysis(stale, run_meta=RUN_META)
    assert commit.status == "superseded"
    assert commit.is_current is False
    current = repo.current_profile(document.id)
    assert current is not None and current.id == first.id, "the stale result must not displace the live one"
    assert current.criteria_version == 1


# ===========================================================================
# Pipeline: caching, parser failure, human-state purity
# ===========================================================================
def test_no_op_rescan_makes_zero_model_calls(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    stub = StubAdapter()
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()
    assert stub.calls == 0
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"
    assert stub.calls == 1

    stub.calls = 0
    repeat = pipeline.scan()
    assert stub.calls == 0, "a rescan of unchanged bytes must call no model"
    assert repeat.reused_assessments == 1
    assert repeat.analyses_enqueued == 0


def test_parser_failure_keeps_row_and_stales_profile(tmp_workspace: Path, repo: Repository):
    target = tmp_workspace / "candidate.txt"
    target.write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"

    target.write_bytes(b"PK\x03\x04" + b"not a docx")
    summary = pipeline.scan()
    assert summary.parser_failures == 1

    assert [d.id for d in repo.list_documents()] == [document.id], "the row must stay visible"
    profile = repo.current_profile(document.id)
    assert profile is not None and profile.stale is True
    assert repo.get_document(document.id).processing_state is ProcessingState.STALE
    assert any(t.task_type == "manual_review" for t in repo.list_tasks(document_id=document.id))


def test_analysis_writes_no_human_state(tmp_workspace: Path, repo: Repository, monkeypatch):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("analysis wrote a human decision/approval table")

    for name in ("set_decision", "set_intent", "approve_batch", "create_batch"):
        monkeypatch.setattr(repo, name, _forbidden)

    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    document = repo.list_documents()[0]

    def count(table: str) -> int:
        return int(repo.db.scalar(f"SELECT COUNT(*) FROM {table}", (), default=0) or 0)

    tables = ("decisions", "action_intents", "action_batches")
    before = {t: count(t) for t in tables}
    outcome = pipeline.analyze_job(repo.list_jobs()[0])
    assert outcome.status == "committed"
    assert {t: count(t) for t in tables} == before
    assert repo.get_decision(document.id).disposition is ReviewState.UNREVIEWED


def test_pipeline_source_never_calls_a_human_state_writer():
    import resume_review.analysis.pipeline as pipeline_module
    import resume_review.analysis.queue as queue_module

    forbidden = {"set_decision", "set_intent", "approve_batch", "create_batch", "set_document_location"}
    for module in (pipeline_module, queue_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not (calls & forbidden), f"{module.__name__} calls {calls & forbidden}"
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level and node.module == "actions"
        }
        assert not imports, f"{module.__name__} imports actions"


def test_not_found_never_sets_a_decision(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)

    def only_not_found(request, *, request_id=None):
        return Completion(
            text=json.dumps(
                {
                    "schema_version": ANALYSIS_SCHEMA_VERSION,
                    "document_id": request.document_id,
                    "source_revision": request.source_revision,
                    "criteria_version": request.criteria_version,
                    "summary": {"text": "Nothing established.", "evidence_ids": []},
                    "criteria": [
                        {"criterion_id": cid, "result": "not_found", "explanation": "", "evidence_ids": []}
                        for cid in request.criterion_ids
                    ],
                    "evidence": [],
                    "suggested_tasks": [],
                    "warnings": [],
                }
            ),
            request_id=request_id,
            route="local_only",
        )

    stub = StubAdapter()
    stub.complete = only_not_found  # type: ignore[assignment]
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"

    profile = repo.current_profile(document.id)
    assert profile is not None
    results = [
        row["result"] for row in repo.evidence_for_profile(profile.id) if row["criterion_id"]
    ]
    assert set(results) == {"not_found"}
    assert repo.get_decision(document.id).disposition is ReviewState.UNREVIEWED


# ===========================================================================
# Queue discipline
# ===========================================================================
def test_queue_admits_only_one_bulk_request_per_instance():
    capacity = HostCapacity(max_in_flight=4, reserved_for_chat=1)
    assert capacity.try_acquire("inst_a", "analysis") is True
    assert capacity.try_acquire("inst_a", "analysis") is False, "one in flight per instance"
    assert capacity.try_acquire("inst_b", "analysis") is True, "a different instance may run"
    # The interactive lane is still available to the busy instance.
    assert capacity.try_acquire_interactive("inst_a") is True
    capacity.release_interactive("inst_a")
    capacity.release("inst_a", "analysis")
    capacity.release("inst_b", "analysis")
    assert capacity.in_flight() == 0


def test_queue_uses_at_most_three_attempts_and_one_repair_per_attempt(
    tmp_workspace: Path, repo: Repository
):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    # Every answer is structurally broken, so each attempt spends its single repair.
    stub = StubAdapter(script=["not json", "still not json"])
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()
    job = repo.list_jobs()[0]

    outcome = pipeline.analyze_job(job)
    assert outcome.status == "manual_review"
    assert outcome.model_calls == 2, "one initial call plus at most one repair"
    assert stub.calls == 2

    # Re-running the same job cannot loop: one repair per attempt, no more.
    stub.calls = 0
    again = pipeline.analyze_job(job)
    assert again.model_calls == 2
    assert stub.calls == 2


class _FakePipeline:
    def __init__(self, handler) -> None:
        self._handler = handler

    def analyze_job(self, job):
        return self._handler(job)


def _enqueue(repo: Repository, key: str) -> str:
    return repo.enqueue_job(
        key,
        "analysis",
        input_versions={"source_revision": 1, "criteria_version": 1},
        priority=200,
    )


def test_cancel_does_not_discard_a_committed_result(repo: Repository):
    from resume_review.analysis.pipeline import AnalysisOutcome

    job_id = _enqueue(repo, "analysis:doc:r1:c1")
    queue = AnalysisQueue(
        repository=repo,
        pipeline=_FakePipeline(lambda _job: AnalysisOutcome(status="committed", profile_id="p1")),
        capacity=HostCapacity(max_in_flight=2, reserved_for_chat=1),
    )
    assert queue.process_next().status == "committed"
    assert repo.get_job(job_id)["state"] == "succeeded"
    queue.cancel(job_id)
    job = repo.get_job(job_id)
    assert job["state"] == "succeeded"
    assert job["result_ref"] == "p1"


def test_lease_is_reaped_and_reclaimable(repo: Repository):
    from resume_review.analysis.pipeline import AnalysisOutcome

    job_id = _enqueue(repo, "analysis:doc:r1:c1")
    claimed = repo.claim_job("worker-crashed", 0.0, kinds=["analysis"])
    assert claimed is not None and claimed["id"] == job_id

    queue = AnalysisQueue(
        repository=repo,
        pipeline=_FakePipeline(lambda _job: AnalysisOutcome(status="committed")),
        capacity=HostCapacity(max_in_flight=2, reserved_for_chat=1),
    )
    assert queue.reap_expired_leases() == 1
    assert repo.get_job(job_id)["state"] == "queued"
    assert queue.process_next().status == "committed"
