"""End-to-end review journey over the real phase-2 stack.

Authority: PRD sections 6.3 (analysis pipeline), 10.1 (file behaviour after
approval), 13.1 (plan -> approve -> apply), 13.2 (filesystem safety) and 13.3
(crash reconciliation).

Every test here builds a synthetic instance from scratch (``setup_instance`` +
synthetic fixtures only -- never real applicant data) and drives the real
modules: bootstrap, discovery, extraction, the analysis pipeline, the planner,
the approval-gated executor, and reconciliation. Nothing is asserted about an
interface because the interface exists; each claim is checked against what the
system actually did, on disk and in the database.

The one substituted piece is the model client. PRD section 6.4 forbids a live
route in the deterministic suite, so a local :class:`StubAdapter` returns
scripted JSON and counts calls. It is an *explicit stub*: it cites a quote drawn
from the document's own extracted span, so the deterministic evidence-validation
path exercised here is the real one (a fabricated quote is a separate,
adversarial test). This stub is what makes the analysis step a MOCK: the network
call, the adapter's request/response shaping, and any live-route behaviour are
NOT exercised by these tests.

The journey covers, in order:

1. bootstrap a workspace and run discovery + extraction on synthetic documents;
2. run the pipeline with the stubbed adapter and commit profiles + evidence;
3. prove evidence survives the round trip from the database back to its source span;
4. record human decisions, including a reject and a hold;
5. plan, approve, and apply: the rejected file lands under ``Rejected/<doc-id>/``
   with unchanged bytes and the held file does not move;
6. show the five state dimensions are independent -- a row reads ``decision=reject``
   with ``location=active`` before the move and ``location=rejected`` after it;
7. rescan and prove it registers no new revision and makes ZERO model calls;
8. crash the executor between the move and the commit, then reconcile;
9. adversarial: a fabricated quote commits no assessment; a destination occupied
   between plan and apply stops the batch and never clobbers the occupant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures import synth  # noqa: E402  (path set up above)

from resume_review.actions import plan_actions  # noqa: E402
from resume_review.actions.executor import apply_batch  # noqa: E402
from resume_review.actions.planner import SkipReason  # noqa: E402
from resume_review.actions.recovery import Recovery, plan_recovery  # noqa: E402
from resume_review.analysis import ANALYSIS_SCHEMA_VERSION  # noqa: E402
from resume_review.analysis.pipeline import Completion, Pipeline  # noqa: E402
from resume_review.bootstrap import workspace  # noqa: E402
from resume_review.bootstrap.registry import HostRegistry  # noqa: E402
from resume_review.bootstrap.setup import setup_instance  # noqa: E402
from resume_review.db import Repository  # noqa: E402
from resume_review.db.connection import Database, DbConfig  # noqa: E402
from resume_review.errors import Code  # noqa: E402
from resume_review.ingest import ExtractionCache  # noqa: E402
from resume_review.ingest.stabilize import Stabilizer  # noqa: E402
from resume_review.models import (  # noqa: E402
    DEFAULT_LIMITS,
    ExecutionState,
    Location,
    ModelRoute,
    OperationState,
    PendingIntent,
    Principal,
    ProcessingState,
    ReviewState,
    Role,
    normalize_ws,
)
from resume_review.openclaw_adapter.policy import (  # noqa: E402
    RouteAttestation,
    RoutePolicy,
)
from resume_review.util import now_iso, seconds_from_now_iso  # noqa: E402

ACTOR = "reviewer@example.test"
JOB_TEXT = "Operations Manager\nCoordinate crews and subcontractors."
BAD_QUOTE = "This sentence appears in no source document at all."


# ---------------------------------------------------------------------------
# Fixtures: a real provisioned instance with a real migrated database
# ---------------------------------------------------------------------------
@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostRegistry:
    """A host registry isolated inside the test's temporary directory."""
    monkeypatch.setenv("RESUME_REVIEW_REGISTRY_DIR", str(tmp_path / "host-registry"))
    return HostRegistry()


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A synthetic reviewed application bundle (never the repository's ``web/``)."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "assets").mkdir()
    (bundle / "templates" / "report.html").write_text("<html>review</html>", encoding="utf-8")
    (bundle / "assets" / "report.css").write_text("body{font-family:sans-serif}", encoding="utf-8")
    (bundle / "helper.py").write_text("print('helper')\n", encoding="utf-8")
    return bundle


@pytest.fixture
def instance(tmp_path: Path, registry: HostRegistry, bundle_dir: Path) -> SimpleNamespace:
    """A freshly provisioned workspace plus an open database and repository."""
    root = tmp_path / "Job - Operations Manager"
    result = setup_instance(root, JOB_TEXT, registry=registry, bundle_dir=bundle_dir)
    assert result.created is True
    database = Database(DbConfig(path=workspace.db_path(root)))
    repository = Repository(database)
    assert repository.instance_id == result.instance_id
    try:
        yield SimpleNamespace(root=root, db=database, repo=repository, instance_id=result.instance_id)
    finally:
        database.close()


# ---------------------------------------------------------------------------
# The explicit stub model client (PRD constraint 10 / section 6.4)
# ---------------------------------------------------------------------------
class StubAdapter:
    """Scripted model client that counts calls and never touches a network.

    It is deliberately an explicit stub, not a mock of the adapter's own logic:
    the pipeline's validation, materialization, and commit paths it feeds are the
    real ones. Its quote comes from ``quote_by_document``, which the test fills
    with a substring of each document's real extracted span -- so the evidence it
    cites is genuinely locatable, and the pipeline's quote check is exercised for
    real. ``bad_quote=True`` feeds a quote that is in no span instead.
    """

    def __init__(self, *, quote_by_document: dict[str, str] | None = None) -> None:
        self.calls = 0
        self.requests: list = []
        self.quote_by_document = dict(quote_by_document or {})
        self.bad_quote = False

    def complete(self, request, *, request_id: str | None = None) -> Completion:
        self.calls += 1
        self.requests.append(request)
        quote = BAD_QUOTE if self.bad_quote else self.quote_by_document.get(
            request.document_id, "Operations Manager"
        )
        text = json.dumps(_payload_for(request, quote=quote))
        return Completion(
            text=text,
            request_id=request_id,
            route=ModelRoute.LOCAL_ONLY.value,
            run_started_at="2026-01-01T00:00:00+00:00",
            run_ended_at="2026-01-01T00:00:01+00:00",
            token_usage=42,
        )


def _payload_for(request, *, quote: str) -> dict:
    """A schema-shaped analysis payload citing one located quote."""
    criterion_ids = list(request.criterion_ids)
    criteria = []
    evidence = []
    if criterion_ids:
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
        "suggested_tasks": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resume_text(name: str) -> str:
    """Synthetic resume text; four non-blank lines -> exactly one TXT span."""
    return (
        f"{name}\n"
        "Operations Manager\n"
        "Coordinated subcontractors on commercial renovations.\n"
        "Managed a crew of twelve electricians.\n"
    )


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
        provider_label="deterministic e2e stub route",
    )


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


def activate_criteria(repo: Repository) -> None:
    """Approve a two-criterion version; the pipeline refuses to run without one."""
    repo.create_or_update_job("Operations Manager", "Coordinate crews and subcontractors.")
    repo.create_criteria_proposal("cr_01", "Coordinates subcontractors on commercial work.")
    repo.create_criteria_proposal("cr_02", "Manages a crew of electricians.")
    repo.activate_criteria_version(1, "ops-admin")
    assert repo.active_criteria_version() == 1


def reviewer(repo: Repository) -> Principal:
    return Principal(
        actor_ref=ACTOR,
        role=Role.REVIEWER,
        session_id="sess_e2e",
        instance_id=repo.instance_id,
    )


def persist_and_approve(repo: Repository, plan) -> None:
    repo.create_batch(plan, created_by=ACTOR)
    repo.create_file_operations(plan.batch_id, plan.operations)
    repo.approve_batch(
        plan.batch_id, actor=ACTOR, plan_hash=plan.plan_hash, expires_at=seconds_from_now_iso(900)
    )


def cached_spans(repo: Repository, document_id: str, revision: int):
    row = repo.get_revision(document_id, revision)
    assert row is not None
    cache = ExtractionCache(repo.db.connect(), repo.instance_id)
    extracted = cache.get(row["content_sha256"], row["parser_name"], row["parser_version"])
    assert extracted is not None, "the scan must have cached extraction for this revision"
    return list(extracted.spans)


def derived_quote(repo: Repository, document_id: str, revision: int, *, tokens: int = 4) -> str:
    """A quote built from the first tokens of the document's own first span.

    ``normalize_ws(quote)`` is then a prefix of ``normalize_ws(span.text)``, so the
    quote is genuinely locatable -- the test cites real extracted text rather than
    a hard-coded string that might drift from the parser.
    """
    spans = cached_spans(repo, document_id, revision)
    assert spans, "extraction produced no spans"
    words = spans[0].text.split()[:tokens]
    assert words
    return " ".join(words)


def document_id_for(repo: Repository, rel_path: str) -> str:
    document = repo.get_document_by_path(rel_path)
    assert document is not None, f"{rel_path} was not discovered"
    return document.id


def audit_count(repo: Repository) -> int:
    return int(repo.db.scalar("SELECT COUNT(*) FROM audit_events", (), default=0) or 0)


# ---------------------------------------------------------------------------
# Step 1-7: the full deterministic journey
# ---------------------------------------------------------------------------
def test_full_deterministic_review_journey(instance: SimpleNamespace) -> None:
    root, repo = instance.root, instance.repo

    # -- 1. bootstrap, discovery, extraction (synthetic fixtures only) -----
    synth.write_txt(root / "alpha.txt", resume_text("alpha"))
    synth.write_txt(root / "bravo.txt", resume_text("bravo"))
    synth.write_txt(root / "charlie.txt", resume_text("charlie"))
    synth.write_pdf(
        root / "delta.pdf",
        pages=[["Coordinated subcontractors on commercial renovations.", "Operations Manager"]],
    )
    activate_criteria(repo)

    stub = StubAdapter()
    pipeline = make_pipeline(repo, root, stub)

    summary = pipeline.scan()
    assert summary.discovered == 4
    assert summary.created == 4
    assert summary.revisions_added == 4
    assert summary.extractions == 4
    assert summary.cache_hits == 0
    assert summary.analyses_enqueued == 4
    # The deterministic stages never call a model.
    assert stub.calls == 0

    # -- 2. run the bounded analysis step against the stub ----------------
    alpha = document_id_for(repo, "alpha.txt")
    bravo = document_id_for(repo, "bravo.txt")
    charlie = document_id_for(repo, "charlie.txt")
    delta = document_id_for(repo, "delta.pdf")

    for document in repo.list_documents():
        stub.quote_by_document[document.id] = derived_quote(
            repo, document.id, document.current_revision
        )

    jobs = repo.list_jobs(state="queued")
    assert len(jobs) == 4
    outcomes = [pipeline.analyze_job(job) for job in jobs]
    assert [o.status for o in outcomes] == ["committed"] * 4
    assert all(o.model_calls == 1 for o in outcomes)
    assert stub.calls == 4

    # -- 3. evidence survives the round trip back to its source span ------
    for document in repo.list_documents():
        profile = repo.current_profile(document.id)
        assert profile is not None, f"{document.original_filename} produced no current profile"
        assert profile.is_current is True
        assert profile.validation_state == "valid"
        assert profile.source_revision == document.current_revision

        spans = cached_spans(repo, document.id, document.current_revision)
        span_map = {span.span_id: span for span in spans}
        evidence = repo.evidence_for_profile(profile.id)
        located = [e for e in evidence if e.get("validation") == "verified" and e.get("span_id")]
        assert located, f"{document.original_filename} committed no located evidence"
        for item in located:
            # The committed quote still occurs in the span it cites, after read back.
            assert normalize_ws(item["quote"]) in span_map[item["span_id"]].normalized
        # not_found stays neutral: it is a row, never a negative finding.
        neutral = [e for e in evidence if e.get("criterion_id") == "cr_02"]
        assert neutral and neutral[0]["result"] == "not_found"

    # Read the committed evidence back through a *second* connection.
    alpha_profile = repo.current_profile(alpha)
    assert alpha_profile is not None
    fresh = Database(DbConfig(path=workspace.db_path(root)))
    try:
        reread = Repository(fresh).evidence_for_profile(alpha_profile.id)
    finally:
        fresh.close()
    assert any(
        e.get("validation") == "verified" and normalize_ws(e["quote"]) in _alpha_span_text(repo, alpha)
        for e in reread
    )

    # -- 4. human decisions: one reject, one hold, one keep ---------------
    assert repo.set_decision(alpha, "reject", expected_revision=0, actor=ACTOR).disposition is ReviewState.REJECT
    assert repo.set_decision(bravo, "hold", expected_revision=0, actor=ACTOR).disposition is ReviewState.HOLD
    assert repo.set_decision(charlie, "keep", expected_revision=0, actor=ACTOR).disposition is ReviewState.KEEP

    # -- 5-6. plan, approve, apply; the five dimensions stay independent --
    alpha_bytes = (root / "alpha.txt").read_bytes()
    bravo_bytes = (root / "bravo.txt").read_bytes()

    plan = plan_actions(
        repo,
        document_ids=[alpha, bravo, charlie, delta],
        requested_by=ACTOR,
        criteria_version=repo.active_criteria_version(),
        root=root,
    )
    # Only the reject is planned. Hold, keep, and unreviewed emit no operation.
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.document_id == alpha
    assert operation.kind is PendingIntent.MOVE_REJECTED
    assert operation.destination == f"Rejected/{alpha}/alpha.txt"
    skipped = {s.document_id: s.reason for s in plan.skipped}
    assert skipped[bravo] == SkipReason.NO_OP_HOLD
    assert skipped[charlie] == SkipReason.NO_OP_KEEP_ACTIVE
    assert skipped[delta] == SkipReason.NO_OP_UNREVIEWED
    assert bravo not in {op.document_id for op in plan.operations}

    persist_and_approve(repo, plan)

    # Interface-visible state before the move: reject, but still active on disk.
    before = repo.get_document(alpha)
    assert before.location is Location.ACTIVE
    assert before.current_rel_path == "alpha.txt"
    assert repo.get_decision(alpha).disposition is ReviewState.REJECT
    assert repo.get_intent(alpha).intent is PendingIntent.NONE
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.APPROVED.value
    assert (root / "alpha.txt").exists()
    assert not (root / operation.destination).exists()

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )
    assert outcome.ok is True
    assert outcome.state == ExecutionState.COMPLETED.value
    assert outcome.counts["moved"] == 1
    assert outcome.remaining == 0

    # The rejected file is exactly where the plan said, byte-for-byte.
    assert not (root / "alpha.txt").exists()
    assert (root / operation.destination).read_bytes() == alpha_bytes
    # The held file did not move and was not touched.
    assert (root / "bravo.txt").exists()
    assert (root / "bravo.txt").read_bytes() == bravo_bytes
    assert not (root / f"Rejected/{bravo}/bravo.txt").exists()
    # Keep and unreviewed files stay put too.
    assert (root / "charlie.txt").exists()
    assert (root / "delta.pdf").exists()

    # Five dimensions, read separately after the move.
    after = repo.get_document(alpha)
    assert after.location is Location.REJECTED
    assert after.current_rel_path == operation.destination
    assert after.location_version == before.location_version + 1
    assert repo.get_decision(alpha).disposition is ReviewState.REJECT
    assert repo.get_intent(alpha).intent is PendingIntent.NONE
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.COMPLETED.value
    rows = repo.list_file_operations(plan.batch_id)
    assert len(rows) == 1
    assert rows[0].state is OperationState.COMMITTED

    # -- 7. a rescan registers no revision and makes ZERO model calls -----
    stub.calls = 0
    rescan = pipeline.scan()
    assert rescan.discovered == 3  # the rejected file is now under the excluded Rejected/
    assert rescan.revisions_added == 0
    assert rescan.analyses_enqueued == 0
    assert rescan.cache_hits == 3
    assert rescan.reused_assessments == 3
    assert stub.calls == 0
    # The human decision and the location survived the rescan.
    assert repo.get_decision(alpha).disposition is ReviewState.REJECT
    assert repo.get_document(alpha).location is Location.REJECTED


def _alpha_span_text(repo: Repository, document_id: str) -> str:
    spans = cached_spans(repo, document_id, repo.get_document(document_id).current_revision)
    return " || ".join(span.normalized for span in spans)


# ---------------------------------------------------------------------------
# Step 8: crash between the move and the commit, then reconcile
# ---------------------------------------------------------------------------
def test_crash_between_move_and_commit_is_reconciled_not_repeated(
    instance: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = instance.root, instance.repo

    synth.write_txt(root / "crash_one.txt", resume_text("crash_one"))
    synth.write_txt(root / "crash_two.txt", resume_text("crash_two"))
    activate_criteria(repo)

    stub = StubAdapter()
    pipeline = make_pipeline(repo, root, stub)
    pipeline.scan()
    for document in repo.list_documents():
        stub.quote_by_document[document.id] = derived_quote(
            repo, document.id, document.current_revision
        )
    for job in repo.list_jobs(state="queued"):
        assert pipeline.analyze_job(job).status == "committed"

    one = document_id_for(repo, "crash_one.txt")
    two = document_id_for(repo, "crash_two.txt")
    for document_id in (one, two):
        repo.set_decision(document_id, "reject", expected_revision=0, actor=ACTOR)

    plan = plan_actions(
        repo,
        document_ids=[one, two],
        requested_by=ACTOR,
        criteria_version=repo.active_criteria_version(),
        root=root,
    )
    assert len(plan.operations) == 2
    first_op, second_op = plan.operations
    persist_and_approve(repo, plan)

    one_bytes = (root / "crash_one.txt").read_bytes()
    two_bytes = (root / "crash_two.txt").read_bytes()

    # Simulate a crash: the first filesystem move happens, its commit does not.
    real_set_location = repo.set_document_location
    state = {"crash": True}

    def crashing_set_location(document_id, rel_path, location, expected_location_version):
        if state["crash"]:
            state["crash"] = False
            raise RuntimeError("simulated crash after the move, before the commit")
        return real_set_location(document_id, rel_path, location, expected_location_version)

    monkeypatch.setattr(repo, "set_document_location", crashing_set_location)
    with pytest.raises(RuntimeError):
        apply_batch(repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso())
    # Subsequent calls (recovery, replay) delegate to the real method.
    assert state["crash"] is False

    # Reality after the crash: the file moved, the database did not commit it.
    assert not (root / "crash_one.txt").exists()
    assert (root / first_op.destination).read_bytes() == one_bytes
    assert repo.get_document(one).location is Location.ACTIVE
    first_row = next(r for r in repo.list_file_operations(plan.batch_id) if r.document_id == one)
    assert first_row.state is OperationState.FILE_MOVED
    # The second operation was never reached; its file is untouched.
    assert (root / "crash_two.txt").read_bytes() == two_bytes
    second_row = next(r for r in repo.list_file_operations(plan.batch_id) if r.document_id == two)
    assert second_row.state is OperationState.PLANNED
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.APPLYING.value

    revision_before = repo.db.state_revision()
    audit_before = audit_count(repo)

    # Dry run first: reports the reconciliation, mutates nothing.
    dry = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=True)
    assert dry.mutating is False
    assert dry.applied == 0
    conditions = {d.document_id: d.condition for d in dry.diagnoses}
    recoveries = {a.document_id: a.recovery for a in dry.actions}
    assert recoveries[one] == Recovery.COMMIT
    assert recoveries[two] == Recovery.RESUME
    assert repo.get_document(one).location is Location.ACTIVE  # unchanged by the dry run
    commit_diag = next(d for d in dry.diagnoses if d.document_id == one)
    assert commit_diag.safe_to_proceed_without_human is True
    assert commit_diag.evidence.get("namespace_owned") is True
    assert commit_diag.evidence.get("content_matches") is True
    assert commit_diag.evidence.get("identity_matches") is True

    # Real reconciliation: commit the performed move; do not repeat anything.
    repair = plan_recovery(repo, root=root, batch_id=plan.batch_id, dry_run=False)
    assert repair.applied == 1
    assert repair.counts.get(Recovery.COMMIT) == 1
    assert repair.counts.get(Recovery.RESUME) == 1

    assert repo.get_document(one).location is Location.REJECTED
    assert repo.get_document(one).current_rel_path == first_op.destination
    assert (
        next(r for r in repo.list_file_operations(plan.batch_id) if r.document_id == one).state
        is OperationState.COMMITTED
    )
    # The second operation is untouched: still planned, file still at the source.
    assert repo.get_document(two).location is Location.ACTIVE
    assert (root / "crash_two.txt").read_bytes() == two_bytes
    assert (
        next(r for r in repo.list_file_operations(plan.batch_id) if r.document_id == two).state
        is OperationState.PLANNED
    )

    # Both files intact; the move happened exactly once; history only grew.
    assert (root / first_op.destination).read_bytes() == one_bytes
    assert not (root / "crash_one.txt").exists()
    assert repo.db.state_revision() > revision_before
    assert audit_count(repo) > audit_before

    # A replayed apply resumes the untouched second move and never repeats the first.
    replay = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )
    assert replay.ok is True
    assert replay.state == ExecutionState.COMPLETED.value
    assert replay.counts["already_completed"] == 1  # the reconciled first operation
    assert replay.counts["moved"] == 1  # the second operation
    assert (root / first_op.destination).read_bytes() == one_bytes  # not moved a second time
    assert not (root / "crash_one.txt").exists()
    assert (root / second_op.destination).read_bytes() == two_bytes
    assert not (root / "crash_two.txt").exists()


# ---------------------------------------------------------------------------
# Step 9a: a fabricated quote must not commit an assessment
# ---------------------------------------------------------------------------
def test_a_fabricated_quote_does_not_commit_an_assessment(instance: SimpleNamespace) -> None:
    root, repo = instance.root, instance.repo

    synth.write_txt(root / "liar.txt", resume_text("liar"))
    activate_criteria(repo)
    stub = StubAdapter()
    pipeline = make_pipeline(repo, root, stub)
    pipeline.scan()

    document = repo.get_document_by_path("liar.txt")
    assert document is not None
    stub.quote_by_document[document.id] = derived_quote(
        repo, document.id, document.current_revision
    )
    # The model now claims a quote that is in no span.
    stub.bad_quote = True

    outcome = pipeline.analyze_job(repo.list_jobs(state="queued")[0])
    assert outcome.status == "manual_review"
    # A non-repairable evidence failure spends no repair turn and commits nothing.
    assert outcome.model_calls == 1
    assert repo.current_profile(document.id) is None
    assert repo.get_document(document.id).processing_state is ProcessingState.MANUAL_REVIEW
    assert repo.evidence_for_profile("nonexistent") == []
    assert any(
        task.task_type == "manual_review" for task in repo.list_tasks(document_id=document.id)
    )
    # No decision was manufactured by the failed analysis.
    assert repo.get_decision(document.id).disposition is ReviewState.UNREVIEWED


# ---------------------------------------------------------------------------
# Step 9b: a destination occupied between plan and apply stops the batch
# ---------------------------------------------------------------------------
def test_a_destination_occupied_between_plan_and_apply_stops_the_batch(
    instance: SimpleNamespace,
) -> None:
    root, repo = instance.root, instance.repo

    synth.write_txt(root / "one.txt", resume_text("one"))
    synth.write_txt(root / "two.txt", resume_text("two"))
    synth.write_txt(root / "three.txt", resume_text("three"))
    activate_criteria(repo)

    stub = StubAdapter()
    pipeline = make_pipeline(repo, root, stub)
    pipeline.scan()
    for document in repo.list_documents():
        stub.quote_by_document[document.id] = derived_quote(
            repo, document.id, document.current_revision
        )
    for job in repo.list_jobs(state="queued"):
        assert pipeline.analyze_job(job).status == "committed"

    ids = [document_id_for(repo, name) for name in ("one.txt", "two.txt", "three.txt")]
    for document_id in ids:
        repo.set_decision(document_id, "reject", expected_revision=0, actor=ACTOR)

    plan = plan_actions(
        repo,
        document_ids=ids,
        requested_by=ACTOR,
        criteria_version=repo.active_criteria_version(),
        root=root,
    )
    assert len(plan.operations) == 3
    persist_and_approve(repo, plan)

    first, second, third = plan.operations
    one_bytes = (root / "one.txt").read_bytes()
    two_bytes = (root / "two.txt").read_bytes()
    three_bytes = (root / "three.txt").read_bytes()

    # Between plan/approval and apply, another actor occupies op 2's destination.
    foreign = b"FOREIGN CONTENT -- placed by another actor; must never be overwritten\n"
    occupant = root / second.destination
    occupant.parent.mkdir(parents=True, exist_ok=True)
    occupant.write_bytes(foreign)

    outcome = apply_batch(
        repo, batch_id=plan.batch_id, actor=reviewer(repo), root=root, now=now_iso()
    )

    assert outcome.ok is False
    assert outcome.state == ExecutionState.PARTIAL.value
    assert outcome.code == Code.BATCH_PARTIAL
    assert outcome.counts["moved"] == 1
    assert outcome.remaining == 2

    # Op 1 completed and was not rolled back.
    assert not (root / "one.txt").exists()
    assert (root / first.destination).read_bytes() == one_bytes
    # Op 2 was stopped by the collision; the occupant was never clobbered.
    assert occupant.read_bytes() == foreign
    assert (root / "two.txt").read_bytes() == two_bytes
    # Op 3 was never reached.
    assert (root / "three.txt").read_bytes() == three_bytes
    assert not (root / third.destination).exists()

    rows = {row.document_id: row.state for row in repo.list_file_operations(plan.batch_id)}
    assert rows[ids[0]] is OperationState.COMMITTED
    assert rows[ids[1]] is OperationState.NEEDS_RECONCILIATION
    assert rows[ids[2]] is OperationState.PLANNED
    assert repo.get_batch(plan.batch_id)["execution_state"] == ExecutionState.PARTIAL.value
    # A blocked operation left a reconciliation task for a human.
    assert any(
        task.task_type == "reconciliation" for task in repo.list_tasks(document_id=ids[1], state="open")
    )
