"""Tests for the bounded, evidence-scoped folder chat pipeline.

Authority: PRD sections 9.1-9.4 (folder-scoped chat, query pipeline, proposed
filter contract, context isolation), section 14 (fail closed) and section 10
(the five independent state dimensions).

These tests use synthetic data exclusively and a stub adapter injected as a
dependency, so no live OpenClaw route is required. They assert the safety
boundaries the PRD makes non-negotiable:

* a partial answer carries its coverage and the caller can surface it;
* retrieval is hard-capped and never stuffs every resume into the prompt;
* an action request produces a plan proposal and moves nothing -- no batch, no
  approval, no intent, no filesystem change;
* a criteria proposal is recorded unapproved and never activates a version;
* an unusable route fails closed, with no fallback and no persisted exchange;
* the model's output can never become a citation, a criterion, or an action that
  the application trusts without resolving it against data it already holds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.chat import (
    ChatRetrievalBudget,
    FolderChat,
    detect_action_intent,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, ResumeReviewError
from resume_review.models import (
    AnalysisResult,
    CriterionResult,
    EvidenceItem,
    MediaType,
    ModelRoute,
    PendingIntent,
    ReviewState,
)
from resume_review.openclaw_adapter.client import AdapterResult
from resume_review.openclaw_adapter.policy import (
    RouteAttestation,
    RoutePolicy,
)
from resume_review.openclaw_adapter.prompts import PROMPT_VERSION


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_test", __version__, SCHEMA_VERSION)
    return repository


def make_result(request, text: str) -> AdapterResult:
    return AdapterResult(
        text=text,
        request_id="req_test",
        document_id=request.document_id,
        prompt_version=request.prompt_version,
        schema_version=request.schema_version,
        route=ModelRoute.LOCAL_ONLY,
        provider_label="test-local",
        agent_target="openclaw/analysis",
        endpoint_label="http://127.0.0.1:8080",
        started_at="2026-09-29T00:00:00+00:00",
        ended_at="2026-09-29T00:00:01+00:00",
        duration_ms=1000,
        http_status=200,
    )


class StubAdapter:
    """A deterministic stand-in for the OpenClaw adapter. Records every call."""

    def __init__(self, *, text: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._text = text
        self._error = error

    async def analyze(self, request, *, conversation_user=None, request_id=None):
        self.calls.append(
            {"request": request, "conversation_user": conversation_user, "request_id": request_id}
        )
        if self._error is not None:
            raise self._error
        return make_result(request, self._text or "")


def json_answer(
    answer: str,
    citations: list[dict] | None = None,
    criteria: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "answer": answer,
            "citations": citations or [],
            "criteria_proposals": criteria or [],
        }
    )


def add_document(
    repo: Repository,
    name: str,
    *,
    summary: str = "",
    quotes: tuple[str, ...] = (),
    criterion_id: str = "cr_01",
    result: CriterionResult = CriterionResult.SUPPORTED,
    decision: ReviewState | None = None,
    with_profile: bool = True,
):
    document = repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=1024,
        content_sha256=None,
        fs_identity=None,
    )
    repo.set_document_processing(document.id, "ready")
    if with_profile:
        evidence = [
            EvidenceItem(
                id=f"ev_{document.id}_{index}",
                span_id=f"span_{index}",
                quote=quote,
                criterion_id=criterion_id,
                result=result,
                claim_kind="criterion",
                validation="verified",
            )
            for index, quote in enumerate(quotes)
        ]
        analysis = AnalysisResult(
            schema_version="1.0",
            document_id=document.id,
            source_revision=1,
            criteria_version=1,
            summary_text=summary,
            evidence=evidence,
        )
        repo.insert_profile(
            analysis,
            {
                "prompt_version": PROMPT_VERSION,
                "model_route": ModelRoute.FIXTURE.value,
                "validation_state": "valid",
                "actor": "helper",
            },
        )
    if decision is not None:
        repo.set_decision(document.id, decision.value, 0, actor="reviewer_1")
    return document


def run(coro):
    return asyncio.run(coro)


def table_count(repo: Repository, table: str) -> int:
    return int(repo.db.scalar(f"SELECT COUNT(*) FROM {table}", (), default=0) or 0)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def test_partial_coverage_is_reported_and_only_selected_evidence_is_sent(repo: Repository) -> None:
    target = add_document(
        repo,
        "a.pdf",
        summary="Reports commercial construction coordination.",
        quotes=("Led commercial construction coordination for three sites.",),
    )
    add_document(repo, "b.pdf", summary="Plumbing maintenance.", quotes=("Repaired boilers.",))
    add_document(repo, "c.pdf", summary="Roofing.", quotes=("Installed roof tiles.",))

    adapter = StubAdapter(text=json_answer("One submission reports commercial construction work."))
    chat = FolderChat(repo, adapter, budget=ChatRetrievalBudget(max_documents=1))
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction experience"))

    coverage = answer.coverage
    assert coverage is not None
    assert coverage.documents_in_scope == 3
    assert coverage.documents_inspected == 1
    assert coverage.documents_not_inspected == 2
    assert coverage.partial is True
    assert coverage.retrieved_document_ids == (target.id,)
    # The partial fact is stated in a form the caller can render directly.
    assert "1 of 3" in coverage.message

    # Only the selected submission's evidence reached the prompt.
    prompt = adapter.calls[0]["request"].messages[-1].content
    assert target.id in prompt
    assert "Repaired boilers" not in prompt
    assert "Installed roof tiles" not in prompt


def test_coverage_is_not_partial_when_all_in_scope_are_inspected(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction coordination.",))
    add_document(repo, "b.pdf", quotes=("Commercial construction supervision.",))

    adapter = StubAdapter(text=json_answer("Both submissions were inspected."))
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert answer.coverage is not None
    assert answer.coverage.documents_in_scope == 2
    assert answer.coverage.documents_inspected == 2
    assert answer.coverage.partial is False


def test_unprocessed_submission_is_reported_not_inspected(repo: Repository) -> None:
    processed = add_document(repo, "a.pdf", quotes=("Commercial construction coordination.",))
    add_document(repo, "b.pdf", with_profile=False)

    adapter = StubAdapter(text=json_answer("One submission was inspected."))
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert answer.coverage is not None
    assert answer.coverage.documents_in_scope == 2
    assert answer.coverage.documents_inspected == 1
    assert answer.coverage.unprocessed_in_scope == 1
    assert answer.coverage.retrieved_document_ids == (processed.id,)


# ---------------------------------------------------------------------------
# Retrieval cap
# ---------------------------------------------------------------------------
def test_retrieval_is_capped_by_the_budget(repo: Repository) -> None:
    for index in range(5):
        add_document(
            repo,
            f"doc-{index}.pdf",
            quotes=(f"Commercial construction coordination number {index}.",),
        )

    adapter = StubAdapter(text=json_answer("Capped retrieval."))
    chat = FolderChat(repo, adapter, budget=ChatRetrievalBudget(max_documents=2))
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert answer.coverage is not None
    assert answer.coverage.documents_in_scope == 5
    assert answer.coverage.documents_inspected == 2
    assert len(answer.coverage.retrieved_document_ids) == 2
    assert answer.coverage.partial is True


def test_evidence_per_document_is_capped(repo: Repository) -> None:
    add_document(
        repo,
        "a.pdf",
        quotes=tuple(f"Commercial construction item {index}." for index in range(10)),
    )

    adapter = StubAdapter(text=json_answer("Capped evidence."))
    chat = FolderChat(
        repo, adapter, budget=ChatRetrievalBudget(max_evidence_per_document=3, max_documents=1)
    )
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert answer.coverage is not None
    assert answer.coverage.documents_inspected == 1
    prompt = adapter.calls[0]["request"].messages[-1].content
    assert prompt.count("EVIDENCE key=") == 3


# ---------------------------------------------------------------------------
# Action requests
# ---------------------------------------------------------------------------
def test_action_request_creates_a_plan_proposal_and_moves_nothing(repo: Repository) -> None:
    reject = add_document(repo, "reject.pdf", quotes=("Commercial construction.",), decision=ReviewState.REJECT)
    keep = add_document(repo, "keep.pdf", quotes=("Commercial construction.",), decision=ReviewState.KEEP)

    adapter = StubAdapter(text=json_answer("Moving files requires an approved plan."))
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="move the rejects to the rejected folder"))

    action = answer.proposed_action
    assert action is not None
    assert action.kind == PendingIntent.MOVE_REJECTED.value
    assert reject.id in action.document_ids
    assert keep.id not in action.document_ids
    # The boundary flags are explicit and false.
    assert action.requires_human_approval is True
    assert action.approval_created is False
    assert action.plan_persisted is False
    assert action.filesystem_changed is False

    # No batch, no file operation, no approval, no saved intent.
    assert repo.list_batches() == []
    assert repo.find_operations_in_state(
        ["planned", "intent_recorded", "file_moved", "committed", "needs_reconciliation"]
    ) == []
    assert repo.get_intent(reject.id).intent is PendingIntent.NONE
    assert repo.get_intent(keep.id).intent is PendingIntent.NONE
    approvals = repo.db.query(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event LIKE 'batch.approve%'"
    )
    assert int(approvals[0]["n"]) == 0
    # The human decision itself is untouched.
    assert repo.get_decision(reject.id).disposition is ReviewState.REJECT
    assert repo.get_decision(reject.id).decision_revision == 1


def test_trash_intent_targets_the_explicit_scope(repo: Repository) -> None:
    first = add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    second = add_document(repo, "b.pdf", quotes=("Commercial construction.",))

    adapter = StubAdapter(text=json_answer("Trash requires approval."))
    chat = FolderChat(repo, adapter)
    answer = run(
        chat.ask(
            reviewer="reviewer_1",
            question="trash these",
            scope_document_ids=[first.id],
        )
    )

    assert answer.proposed_action is not None
    assert answer.proposed_action.kind == PendingIntent.MOVE_TRASH.value
    assert answer.proposed_action.document_ids == (first.id,)
    assert second.id not in answer.proposed_action.document_ids
    assert repo.list_batches() == []


def test_action_detector_reads_the_question_not_the_model(repo: Repository) -> None:
    assert detect_action_intent("Which documents are in the trash?") is None
    assert detect_action_intent("move the rejects") is PendingIntent.MOVE_REJECTED
    assert detect_action_intent("explain the evidence for this criterion") is None
    assert detect_action_intent("restore the rejects to the active folder") is PendingIntent.RESTORE_ACTIVE


# ---------------------------------------------------------------------------
# Criteria proposals
# ---------------------------------------------------------------------------
def test_criteria_proposal_is_recorded_but_does_not_activate(repo: Repository) -> None:
    repo.create_or_update_job("Operations Manager", "Synthetic job description.")
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    before = repo.active_criteria_version()

    adapter = StubAdapter(
        text=json_answer(
            "Consider a new criterion.",
            criteria=[
                {
                    "criterion_id": "cr_new",
                    "definition": "Document reports experience coordinating subcontractors.",
                    "rationale": "Mentioned repeatedly in the scope.",
                    "label": None,
                }
            ],
        )
    )
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="Should we track subcontractor coordination?"))

    assert answer.criteria_proposed == ("cr_new",)
    # The proposal is stored unapproved and no version was activated.
    assert repo.active_criteria_version() == before == 0
    row = repo.db.query_one(
        "SELECT version, approved_at, approved_by, origin FROM criteria WHERE criterion_id = ?",
        ("cr_new",),
    )
    assert row is not None
    assert row["approved_at"] is None
    assert row["approved_by"] is None
    assert str(row["origin"]) == "agent_proposal"


def test_invalid_criteria_proposal_is_rejected_and_warned(repo: Repository) -> None:
    repo.create_or_update_job("Operations Manager", "Synthetic job description.")
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))

    adapter = StubAdapter(
        text=json_answer(
            "Ignore the malformed proposal.",
            criteria=[{"criterion_id": "bad id!", "definition": "x"}],
        )
    )
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="anything"))

    assert answer.criteria_proposed == ()
    assert any(w.code == "criteria_proposal_rejected" for w in answer.warnings)
    assert repo.active_criteria_version() == 0


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------
def test_unrestricted_route_fails_closed_before_any_write(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    policy = RoutePolicy(route=ModelRoute.LOCAL_ONLY, restricted=False)
    adapter = StubAdapter(text=json_answer("should never run"))

    chat = FolderChat(repo, adapter, route_policy=policy)
    with pytest.raises(ResumeReviewError) as excinfo:
        run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert excinfo.value.code == Code.ROUTE_NOT_RESTRICTED
    assert adapter.calls == []
    assert table_count(repo, "conversations") == 0
    assert table_count(repo, "messages") == 0


def test_adapter_failure_persists_nothing_and_does_not_fall_back(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    policy = RoutePolicy(route=ModelRoute.LOCAL_ONLY, restricted=False)

    def route_error() -> ResumeReviewError:
        return policy.route_failure_error(
            Code.ROUTE_UNAVAILABLE, message="The analysis route could not be reached."
        )

    adapter = StubAdapter(error=route_error())
    chat = FolderChat(repo, adapter, route_policy=_attested_policy())
    with pytest.raises(ResumeReviewError) as excinfo:
        run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    # LOCAL_ONLY reports a lost route as the point a fallback must be refused.
    assert excinfo.value.code == Code.LOCAL_ONLY_FALLBACK_BLOCKED
    assert len(adapter.calls) == 1
    assert table_count(repo, "messages") == 0


def test_malformed_model_output_fails_closed(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    adapter = StubAdapter(text="I think the answer is yes.")
    chat = FolderChat(repo, adapter)

    with pytest.raises(ResumeReviewError) as excinfo:
        run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert excinfo.value.code == Code.ADAPTER_BAD_RESPONSE
    assert table_count(repo, "messages") == 0


def _attested_policy() -> RoutePolicy:
    return RoutePolicy(
        route=ModelRoute.LOCAL_ONLY,
        restricted=True,
        attestation=RouteAttestation(
            attested_by="operator",
            attested_at="2026-09-29T00:00:00+00:00",
            no_shell=True,
            no_write_or_edit=True,
            no_browser_control=True,
            no_messaging=True,
            no_credential_read=True,
            no_unrestricted_file_read=True,
            no_cross_session=True,
            no_agent_spawning=True,
            trusted_instruction_workspace=True,
        ),
    )


# ---------------------------------------------------------------------------
# Untrusted model output and isolation
# ---------------------------------------------------------------------------
def test_unresolvable_citation_is_dropped(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction coordination.",))
    adapter = StubAdapter(
        text=json_answer(
            "Grounded answer.",
            citations=[{"document_id": "doc_does_not_exist", "evidence_key": "ev_x"}],
        )
    )
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert answer.citations == ()
    assert any(w.code == "citation_unverified" for w in answer.warnings)


def test_valid_citation_is_resolved_from_stored_evidence(repo: Repository) -> None:
    document = add_document(repo, "a.pdf", quotes=("Commercial construction coordination.",))
    profile = repo.current_profile(document.id)
    assert profile is not None
    evidence = repo.evidence_for_profile(profile.id)
    key = str(evidence[0]["evidence_key"])

    adapter = StubAdapter(
        text=json_answer(
            "Grounded answer.",
            citations=[{"document_id": document.id, "evidence_key": key}],
        )
    )
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert len(answer.citations) == 1
    assert answer.citations[0].document_id == document.id
    assert answer.citations[0].quote == "Commercial construction coordination."


def test_conversation_id_is_opaque_and_exchange_is_persisted(repo: Repository) -> None:
    add_document(repo, "candidate-name-in-filename.pdf", quotes=("Commercial construction.",))
    adapter = StubAdapter(text=json_answer("Stored answer."))
    chat = FolderChat(repo, adapter)

    first = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))
    second = run(chat.ask(reviewer="reviewer_1", question="anything else"))

    session_ref = adapter.calls[0]["conversation_user"]
    assert session_ref == first.conversation_id
    assert session_ref.startswith("conv_")
    # No candidate name, filename or path separator is provider-visible.
    assert "candidate-name" not in session_ref
    assert "/" not in session_ref and "\\" not in session_ref and " " not in session_ref
    # The same reviewer reuses the same opaque conversation.
    assert second.conversation_id == first.conversation_id

    messages = repo.list_messages(first.conversation_id)
    roles = [str(row["role"]) for row in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert messages[1]["coverage"] is not None
    assert messages[1]["payload"]["schema_version"] == "1.0"


def test_cross_reviewer_isolation_no_history_leak(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    adapter = StubAdapter(text=json_answer("A_SECRET_ANSWER from reviewer one."))
    chat = FolderChat(repo, adapter)

    first = run(chat.ask(reviewer="reviewer_1", question="A_SECRET_QUESTION commercial construction"))
    second = run(chat.ask(reviewer="reviewer_2", question="B question commercial construction"))

    assert first.conversation_id != second.conversation_id
    second_prompt = "".join(
        message.content for message in adapter.calls[1]["request"].messages
    )
    assert "A_SECRET_QUESTION" not in second_prompt
    assert "A_SECRET_ANSWER" not in second_prompt


def test_scope_document_from_another_instance_is_refused(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    adapter = StubAdapter(text=json_answer("never runs"))
    chat = FolderChat(repo, adapter)

    with pytest.raises(ResumeReviewError) as excinfo:
        run(
            chat.ask(
                reviewer="reviewer_1",
                question="commercial construction",
                scope_document_ids=["doc_foreign_123"],
            )
        )
    assert excinfo.value.code == Code.INVALID_INPUT
    assert adapter.calls == []


# ---------------------------------------------------------------------------
# Adversarial chat-boundary checks
# ---------------------------------------------------------------------------
def _snapshot(directory: Path) -> dict[str, str]:
    return {
        entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(directory.iterdir())
        if entry.is_file()
    }


def test_action_phrasings_change_no_bytes_on_disk_and_set_no_state(
    repo: Repository, tmp_path: Path
) -> None:
    workspace = tmp_path / "job-folder"
    workspace.mkdir()
    (workspace / "reject.pdf").write_bytes(b"reject-bytes")
    (workspace / "keep.pdf").write_bytes(b"keep-bytes")
    before = _snapshot(workspace)

    reject = add_document(
        repo, "reject.pdf", quotes=("Commercial construction.",), decision=ReviewState.REJECT
    )
    keep = add_document(
        repo, "keep.pdf", quotes=("Commercial construction.",), decision=ReviewState.KEEP
    )
    adapter = StubAdapter(text=json_answer("This needs an approved plan."))
    chat = FolderChat(repo, adapter)

    for question, expected in (
        ("move the rejects to the rejected folder", PendingIntent.MOVE_REJECTED),
        ("trash these", PendingIntent.MOVE_TRASH),
    ):
        answer = run(chat.ask(reviewer="reviewer_1", question=question))
        action = answer.proposed_action
        assert action is not None and action.kind == expected.value
        assert action.requires_human_approval is True
        assert action.approval_created is False
        assert action.plan_persisted is False
        assert action.filesystem_changed is False

    # Byte-identical: nothing moved, nothing created, nothing deleted.
    assert _snapshot(workspace) == before
    assert (workspace / "reject.pdf").read_bytes() == b"reject-bytes"
    # No batch, operation, intent, or approval was recorded.
    assert repo.list_batches() == []
    assert repo.find_operations_in_state(
        ["planned", "intent_recorded", "file_moved", "committed", "needs_reconciliation"]
    ) == []
    assert repo.get_intent(reject.id).intent is PendingIntent.NONE
    assert repo.get_intent(keep.id).intent is PendingIntent.NONE
    approvals = repo.db.query(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event LIKE 'batch.approve%'"
    )
    assert int(approvals[0]["n"]) == 0
    assert repo.get_decision(reject.id).disposition is ReviewState.REJECT


def test_repeating_the_same_criteria_proposal_does_not_fail_the_turn(repo: Repository) -> None:
    repo.create_or_update_job("Operations Manager", "Synthetic job description.")
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    proposal = [
        {
            "criterion_id": "cr_new",
            "definition": "Reports coordinating subcontractors.",
            "rationale": "Repeated in scope.",
            "label": None,
        }
    ]
    adapter = StubAdapter(text=json_answer("Proposal.", criteria=proposal))
    chat = FolderChat(repo, adapter)

    first = run(chat.ask(reviewer="reviewer_1", question="track subcontractor coordination"))
    assert first.criteria_proposed == ("cr_new",)
    assert repo.active_criteria_version() == 0

    # The model repeats the identical proposal on the next turn. A duplicate must
    # be rejected with a warning; it must never surface as an unhandled storage
    # error from model output.
    second = run(chat.ask(reviewer="reviewer_1", question="track subcontractor coordination again"))
    assert second.criteria_proposed == ()
    assert any(w.code == "criteria_proposal_rejected" for w in second.warnings)
    # Still exactly one unapproved proposal, and still no activated version.
    rows = repo.db.query(
        "SELECT version, approved_at FROM criteria WHERE criterion_id = ?", ("cr_new",)
    )
    assert len(rows) == 1
    assert rows[0]["approved_at"] is None
    assert repo.active_criteria_version() == 0


def test_prior_turns_replayed_are_the_newest_not_the_oldest(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    conversation = repo.get_or_create_conversation("reviewer_1")
    for index in range(150):
        repo.append_message(conversation["id"], "user", f"MSG-{index:03d} marker")

    chat = FolderChat(
        repo,
        StubAdapter(text=json_answer("x")),
        budget=ChatRetrievalBudget(max_history_messages=4),
    )
    replayed = chat._history(conversation["id"])
    assert [message.content for message in replayed] == [
        f"MSG-{index:03d} marker" for index in range(146, 150)
    ]


def test_all_folder_question_states_partial_coverage_when_capped(repo: Repository) -> None:
    for index in range(12):
        add_document(
            repo, f"doc-{index}.pdf", quotes=(f"Commercial construction item {index}.",)
        )
    adapter = StubAdapter(text=json_answer("Answered from a subset."))
    chat = FolderChat(repo, adapter, budget=ChatRetrievalBudget(max_documents=3))
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    coverage = answer.coverage
    assert coverage is not None
    assert (coverage.documents_in_scope, coverage.documents_inspected) == (12, 3)
    assert coverage.documents_not_inspected == 9
    assert coverage.partial is True
    # The partial fact reaches the model prompt, not only the caller.
    prompt = adapter.calls[0]["request"].messages[-1].content
    assert "3 of 12" in prompt
    assert "PARTIAL" in prompt


def test_provider_visible_identifiers_are_opaque(repo: Repository) -> None:
    add_document(repo, "Jane-Doe-resume.pdf", quotes=("Commercial construction.",))
    adapter = StubAdapter(text=json_answer("ok"))
    chat = FolderChat(repo, adapter)
    answer = run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    call = adapter.calls[0]
    session = call["conversation_user"]
    request = call["request"]
    assert session == answer.conversation_id == request.document_id
    for value in (str(session), str(request.document_id)):
        assert "Jane" not in value and "resume" not in value and ".pdf" not in value
        assert "/" not in value and "\\" not in value and " " not in value


def test_unavailable_route_fails_closed_with_exactly_one_attempt(repo: Repository) -> None:
    add_document(repo, "a.pdf", quotes=("Commercial construction.",))
    unavailable = ResumeReviewError(
        "The analysis route could not be reached.",
        code=Code.ROUTE_UNAVAILABLE,
        http_status=503,
    )
    primary = StubAdapter(error=unavailable)
    secondary = StubAdapter(text=json_answer("fallback answer"))
    chat = FolderChat(repo, primary)

    with pytest.raises(ResumeReviewError) as excinfo:
        run(chat.ask(reviewer="reviewer_1", question="commercial construction"))

    assert excinfo.value.code == Code.ROUTE_UNAVAILABLE
    # Exactly one configured route, one attempt, and no second endpoint contacted.
    assert len(primary.calls) == 1
    assert secondary.calls == []
    assert table_count(repo, "messages") == 0
