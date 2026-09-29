"""Tests for the analysis pipeline (:mod:`resume_review.analysis.pipeline`).

These run the real pipeline against a real migrated SQLite database and a real
on-disk job folder. The only substituted piece is the model client, which is a
stub that returns scripted text and counts calls -- no live route is ever used
(PRD section 6.4 requires the deterministic suite to be offline).

Each PRD requirement named in the build task gets a test that would fail if the
behavior regressed:

* two-level caching, proven by a no-op rescan making zero model calls;
* a superseded result never becoming current;
* a parser failure keeping the document listed and staling its profile;
* ``not_found`` staying neutral and setting no decision;
* analysis never writing a human decision or an approval.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis import ANALYSIS_SCHEMA_VERSION
from resume_review.analysis.pipeline import (
    AdapterCompletionClient,
    Completion,
    Pipeline,
    materialize_result,
)
from resume_review.analysis.validate import validate_analysis_result
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.ingest import ExtractionCache, extract_txt
from resume_review.ingest.stabilize import Stabilizer
from resume_review.models import (
    CriterionAssessment,
    CriterionResult,
    DEFAULT_LIMITS,
    EvidenceItem,
    AnalysisResult,
    MediaType,
    ModelRoute,
    ProcessingState,
    ProfileRecord,
    ReviewState,
)
from resume_review.openclaw_adapter.policy import (
    RouteAttestation,
    RoutePolicy,
)
from resume_review.openclaw_adapter.prompts import PROMPT_VERSION
from resume_review.util import now_iso

QUOTE = "Coordinated subcontractors on commercial renovations."
RESUME_TEXT = (
    "Jamie Rivera\n"
    "Operations Manager\n"
    f"{QUOTE}\n"
    "Managed a crew of twelve electricians.\n"
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path):
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


def make_policy(route: ModelRoute = ModelRoute.LOCAL_ONLY) -> RoutePolicy:
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
        route=route,
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
    assert extracted is not None, "extraction must have been cached by the scan"
    return extracted.spans


def payload_for(request, *, quote: str = QUOTE, bad: bool = False) -> dict:
    criterion_ids = list(request.criterion_ids)
    criteria = []
    evidence = []
    if bad:
        # Semantically invalid: a quote that is not in the cited span.
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
            {"id": "ev_1", "span_id": request.span_ids[0], "locator": {}, "quote": quote}
        )
        for criterion_id in criterion_ids[1:]:
            criteria.append(
                {
                    "criterion_id": criterion_id,
                    "result": "not_found",
                    "explanation": "Not established in this document.",
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
        "suggested_tasks": [
            {
                "type": "verify",
                "title": "Confirm crew size",
                "criterion_id": criterion_ids[1] if len(criterion_ids) > 1 else criterion_ids[0],
                "detail": "A human should confirm the crew size.",
            }
        ]
        if criterion_ids
        else [],
        "warnings": [],
    }


class StubAdapter:
    """Scripted model client that counts calls. Never touches a network."""

    def __init__(self, script: list[str] | None = None, *, route: str = "local_only") -> None:
        self.calls = 0
        self.requests: list = []
        self._script = list(script) if script is not None else None
        self._route = route

    def complete(self, request, *, request_id: str | None = None) -> Completion:
        self.calls += 1
        self.requests.append(request)
        if self._script is not None:
            text = self._script[min(self.calls - 1, len(self._script) - 1)]
        else:
            text = json.dumps(payload_for(request))
        return Completion(
            text=text,
            request_id=request_id,
            route=self._route,
            run_started_at="2026-01-01T00:00:00+00:00",
            run_ended_at="2026-01-01T00:00:01+00:00",
            token_usage=99,
        )


def run_metrics(stub: StubAdapter) -> int:
    return stub.calls


def make_validated_result(repo: Repository, document_id: str, revision: int, spans, *, quote=QUOTE):
    criteria = repo.list_criteria(1, approved_only=True)
    payload = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "document_id": document_id,
        "source_revision": revision,
        "criteria_version": 1,
        "summary": {"text": "Neutral summary.", "evidence_ids": ["ev_1"]},
        "criteria": [
            {
                "criterion_id": "cr_01",
                "result": "supported",
                "explanation": "The document states this.",
                "evidence_ids": ["ev_1"],
            },
            {"criterion_id": "cr_02", "result": "not_found", "explanation": "", "evidence_ids": []},
        ],
        "evidence": [
            {"id": "ev_1", "span_id": spans[0].span_id, "locator": {}, "quote": quote}
        ],
        "suggested_tasks": [],
        "warnings": [],
    }
    outcome = validate_analysis_result(
        payload=payload,
        document_id=document_id,
        source_revision=revision,
        criteria_version=1,
        criteria=criteria,
        spans=spans,
    )
    assert outcome.ok, [p.code for p in outcome.problems]
    return outcome.result


RUN_META = {
    "prompt_version": PROMPT_VERSION,
    "model_route": "local_only",
    "validation_state": "valid",
    "actor": "helper",
}


# ---------------------------------------------------------------------------
# Full run and two-level caching
# ---------------------------------------------------------------------------
def test_scan_then_drain_commits_and_rescan_is_free(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    stub = StubAdapter()
    pipeline = make_pipeline(repo, tmp_workspace, stub)

    summary = pipeline.scan()
    assert summary.discovered == 1
    assert summary.created == 1
    assert summary.revisions_added == 1
    assert summary.analyses_enqueued == 1
    # The deterministic stages never call a model.
    assert stub.calls == 0

    document = repo.list_documents()[0]
    assert document.processing_state is ProcessingState.ANALYZING

    job = repo.list_jobs()[0]
    outcome = pipeline.analyze_job(job)
    assert outcome.status == "committed"
    assert outcome.model_calls == 1

    profile = repo.current_profile(document.id)
    assert profile is not None
    assert profile.is_current is True
    assert profile.source_revision == 1
    assert profile.criteria_version == 1
    assert profile.validation_state == "valid"
    assert profile.token_usage == 99

    refreshed = repo.get_document(document.id)
    assert refreshed.processing_state is ProcessingState.READY

    # not_found is stored as a neutral result on a criterion evidence row.
    evidence = repo.evidence_for_profile(profile.id)
    by_criterion = {row["criterion_id"]: row for row in evidence if row["criterion_id"]}
    assert by_criterion["cr_01"]["result"] == "supported"
    assert by_criterion["cr_02"]["result"] == "not_found"
    assert by_criterion["cr_02"]["claim_kind"] == "criterion"

    # No decision was set or changed by the analysis.
    assert repo.get_decision(document.id).disposition is ReviewState.UNREVIEWED

    # A no-op rescan reuses the extraction and the assessment: zero model calls.
    stub.calls = 0
    repeat = pipeline.scan()
    assert repeat.revisions_added == 0
    assert repeat.extractions == 0
    assert repeat.cache_hits == 1
    assert repeat.analyses_enqueued == 0
    assert repeat.reused_assessments == 1
    assert stub.calls == 0
    assert repo.current_profile(document.id).id == profile.id


# ---------------------------------------------------------------------------
# Superseded results
# ---------------------------------------------------------------------------
def test_superseded_result_never_becomes_current(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    stub = StubAdapter()
    pipeline = make_pipeline(repo, tmp_workspace, stub)

    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"
    first = repo.current_profile(document.id)
    assert first is not None

    spans = cached_spans(repo, document.id, 1)
    stale_result = make_validated_result(repo, document.id, 1, spans)

    # The document changes; a new revision is registered without a new assessment.
    new_bytes = (RESUME_TEXT + "Scheduling and forecasting.\n").encode("utf-8")
    repo.add_revision(
        document.id,
        hashlib.sha256(new_bytes).hexdigest(),
        len(new_bytes),
        "candidate.txt",
        parser_name=extract_txt.PARSER_NAME,
        parser_version=extract_txt.parser_version(),
    )
    assert repo.get_document(document.id).current_revision == 2

    # Committing a result bound to revision 1 must not displace the live profile.
    commit = pipeline.commit_analysis(stale_result, run_meta=RUN_META)
    assert commit.status == "superseded"
    assert commit.is_current is False
    assert commit.profile_id is not None and commit.profile_id != first.id

    current = repo.current_profile(document.id)
    assert current is not None and current.id == first.id

    row = repo.db.query_one("SELECT is_current, source_revision FROM profiles WHERE id = ?", (commit.profile_id,))
    assert int(row["is_current"]) == 0
    assert int(row["source_revision"]) == 1  # retained for history, not current


# ---------------------------------------------------------------------------
# Parser failure keeps the row
# ---------------------------------------------------------------------------
def test_parser_failure_keeps_document_listed_and_stales_profile(tmp_workspace: Path, repo: Repository):
    target = tmp_workspace / "candidate.txt"
    target.write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())

    pipeline.scan()
    document = repo.list_documents()[0]
    assert pipeline.analyze_job(repo.list_jobs()[0]).status == "committed"
    assert repo.current_profile(document.id).stale is False

    # The same path now holds bytes no parser can read (a non-DOCX ZIP archive).
    target.write_bytes(b"PK\x03\x04" + b"this is not a docx")
    summary = pipeline.scan()
    assert summary.parser_failures == 1

    listed = repo.list_documents()
    assert [doc.id for doc in listed] == [document.id], "the document must stay listed"

    refreshed = repo.get_document(document.id)
    assert refreshed.processing_state is ProcessingState.STALE

    profile = repo.current_profile(document.id)
    assert profile is not None and profile.stale is True, "previous profile stays visible, marked stale"

    tasks = repo.list_tasks(document_id=document.id)
    kinds = {task.task_type for task in tasks if task.state.value == "open"}
    assert "manual_review" in kinds


# ---------------------------------------------------------------------------
# Analysis cannot write a human decision or an approval
# ---------------------------------------------------------------------------
def test_analysis_never_writes_decision_or_approval_tables(tmp_workspace: Path, repo: Repository, monkeypatch):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("analysis attempted to write a human decision or approval table")

    monkeypatch.setattr(repo, "set_decision", _forbidden)
    monkeypatch.setattr(repo, "set_intent", _forbidden)
    monkeypatch.setattr(repo, "approve_batch", _forbidden)

    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    document = repo.list_documents()[0]

    def count(table: str) -> int:
        return int(repo.db.scalar(f"SELECT COUNT(*) FROM {table}", (), default=0) or 0)

    before = {table: count(table) for table in ("decisions", "action_intents", "action_batches")}

    outcome = pipeline.analyze_job(repo.list_jobs()[0])
    assert outcome.status == "committed"

    after = {table: count(table) for table in before}
    assert after == before
    assert repo.get_decision(document.id).disposition is ReviewState.UNREVIEWED


# ---------------------------------------------------------------------------
# Repair budget
# ---------------------------------------------------------------------------
def test_structural_error_uses_exactly_one_repair_turn(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)

    # First answer is not JSON at all (structural); the repair is valid.
    class RepairingStub(StubAdapter):
        def complete(self, request, *, request_id: str | None = None) -> Completion:
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                text = "not json at all"
            else:
                text = json.dumps(payload_for(request))
            return Completion(text=text, request_id=request_id, route=self._route, token_usage=1)

    stub = RepairingStub()
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()

    outcome = pipeline.analyze_job(repo.list_jobs()[0])
    assert outcome.status == "committed"
    assert outcome.model_calls == 2
    assert stub.calls == 2
    # The second request is a repair turn: it extends the first, not replaces it.
    assert len(stub.requests[1].messages) == len(stub.requests[0].messages) + 1


def test_non_repairable_error_skips_repair_and_goes_to_manual_review(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    stub = StubAdapter()
    # Build a payload whose only problems are semantic (a quote not in the span).
    original = StubAdapter.complete
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()
    job = repo.list_jobs()[0]

    def bad_complete(request, *, request_id=None):
        stub.calls += 1
        stub.requests.append(request)
        return Completion(
            text=json.dumps(payload_for(request, bad=True)),
            request_id=request_id,
            route="local_only",
        )

    stub.complete = bad_complete  # type: ignore[assignment]
    outcome = pipeline.analyze_job(job)
    assert outcome.status == "manual_review"
    assert outcome.model_calls == 1, "a non-repairable error must not spend a repair turn"
    document = repo.list_documents()[0]
    assert repo.get_document(document.id).processing_state is ProcessingState.MANUAL_REVIEW
    assert any(t.task_type == "manual_review" for t in repo.list_tasks(document_id=document.id))


def test_repeated_invalid_output_goes_terminal_not_infinite(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    stub = StubAdapter(script=["not json", "still not json"])
    pipeline = make_pipeline(repo, tmp_workspace, stub)
    pipeline.scan()
    job = repo.list_jobs()[0]

    outcome = pipeline.analyze_job(job)
    assert outcome.status == "manual_review"
    assert outcome.model_calls == 2
    # One structural repair inside one attempt; no third call, no retry loop.
    assert stub.calls == 2


# ---------------------------------------------------------------------------
# Adapter bridge and evidence materialization
# ---------------------------------------------------------------------------
def test_adapter_completion_client_maps_adapter_result():
    captured: dict = {}

    class FakeAsyncAdapter:
        async def analyze(self, request, *, conversation_user=None, request_id=None):
            captured["conversation_user"] = conversation_user
            captured["request_id"] = request_id
            return SimpleNamespace(
                text='{"ok": true}',
                request_id="run-1",
                route=ModelRoute.LOCAL_ONLY,
                provider_label="local llama",
                started_at="t0",
                ended_at="t1",
                token_usage=11,
            )

    client = AdapterCompletionClient(FakeAsyncAdapter())
    request = SimpleNamespace(document_id="doc_x", source_revision=1, criteria_version=1)
    completion = client.complete(request, request_id="req-7")
    assert completion.text == '{"ok": true}'
    assert completion.route == "local_only"
    assert completion.provider_label == "local llama"
    assert completion.token_usage == 11
    assert captured["request_id"] == "req-7"


def test_materialize_result_synthesizes_a_row_for_every_assessment():
    result = AnalysisResult(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        document_id="doc_synth",
        source_revision=1,
        criteria_version=1,
        summary_text="summary",
        summary_evidence_ids=["ev_1"],
        criteria=[
            CriterionAssessment(
                criterion_id="cr_01",
                result=CriterionResult.SUPPORTED,
                explanation="",
                evidence_ids=["ev_1"],
            ),
            CriterionAssessment(
                criterion_id="cr_02",
                result=CriterionResult.NOT_FOUND,
                explanation="",
                evidence_ids=[],
            ),
        ],
        evidence=[
            EvidenceItem(id="ev_1", span_id="lines_0001_0002", quote="q", locator={}),
        ],
    )
    materialize_result(result, [])
    rows = {item.criterion_id: item for item in result.evidence if item.criterion_id}
    assert rows["cr_01"].result is CriterionResult.SUPPORTED
    assert rows["cr_01"].validation == "verified"
    assert rows["cr_02"].result is CriterionResult.NOT_FOUND
    assert rows["cr_02"].claim_kind == "criterion"
    # The synthesized row cites no span; it carries the neutral result only.
    assert rows["cr_02"].span_id == ""


# ---------------------------------------------------------------------------
# Publish stage
# ---------------------------------------------------------------------------
def test_publish_snapshot_writes_a_report(tmp_workspace: Path, repo: Repository):
    (tmp_workspace / "candidate.txt").write_text(RESUME_TEXT, encoding="utf-8")
    activate_criteria(repo)
    pipeline = make_pipeline(repo, tmp_workspace, StubAdapter())
    pipeline.scan()
    pipeline.analyze_job(repo.list_jobs()[0])

    report_path = tmp_workspace / "report.html"
    result = pipeline.publish_snapshot(report_path=report_path)
    assert result.published is True
    assert report_path.is_file()
    assert report_path.stat().st_size > 0
