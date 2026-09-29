"""Focused tests for report generation (PRD sections 8.5 and 9; AT-19, AT-20).

Synthetic data only. The tests prove the five things the reporting deliverable must
guarantee: the payload matches the documented shape, a crafted applicant value
cannot break out of the embedded JSON, interpolated values are HTML-escaped, the
rendered document makes no external asset request, and a blocked replacement keeps
the prior report byte-identical while reporting staleness with no temporary file
left behind.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import resume_review.reporting.publish as publish_mod
from resume_review import reporting
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import ResumeReviewError
from resume_review.reporting import (
    build_snapshot_payload,
    load_assets,
    publish_report,
    render_snapshot,
    write_snapshot,
)

INSTANCE_ID = "inst_report_test"
NOW = "2026-09-29T12:00:00.000000+00:00"

TOP_LEVEL_KEYS = {"schema_version", "mode", "generated_at", "instance", "counts", "documents"}
INSTANCE_KEYS = {
    "instance_id",
    "job_title",
    "app_version",
    "schema_version",
    "state_revision",
    "last_analysis_at",
    "storage_mode",
    "criteria_version",
    "criteria",
}
COUNT_KEYS = {
    "total",
    "filtered",
    "processed",
    "unreviewed",
    "keep",
    "reject",
    "hold",
    "manual_review",
    "pending_action",
    "needs_recheck",
    "open_tasks",
}
DOCUMENT_KEYS = {
    "document_id",
    "display_name",
    "original_filename",
    "current_rel_path",
    "media_type",
    "size_bytes",
    "ingested_at",
    "submitted_at",
    "processing_state",
    "processing_detail",
    "location",
    "location_version",
    "review_state",
    "decision_revision",
    "decision_needs_recheck",
    "recheck_reason",
    "disposition_frozen",
    "pending_intent",
    "intent_revision",
    "duplicate_content",
    "duplicate_of",
    "open_task_count",
    "task_warning",
    "summary_text",
    "summary_stale",
    "criteria",
    "evidence",
    "tasks",
    "notes",
    "decision_history",
    "file_actions",
    "document_link",
    "warnings",
}
EVIDENCE_KEYS = {"id", "criterion_id", "span_id", "quote", "locator", "validation"}
CRITERION_KEYS = {"criterion_id", "version", "definition", "label", "rationale"}
DOC_CRITERION_KEYS = {"criterion_id", "result", "explanation", "evidence_ids"}
TASK_KEYS = {"id", "title", "origin", "state", "severity", "criterion_id", "detail"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_db(tmp_path: Path) -> Database:
    workspace = tmp_path / "workspace"
    review_dir = workspace / ".review"
    review_dir.mkdir(parents=True)
    db = Database(DbConfig(path=review_dir / "resume_review.sqlite3"))
    apply_migrations(db.connect())
    return db


def _seed(db: Database) -> None:
    inserts: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO instances (id, schema_version, app_version, state_revision, "
            "storage_mode, host_label, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (INSTANCE_ID, 1, "0.1.0", 412, "local", None, NOW, NOW),
        ),
        (
            "INSERT INTO jobs (id, instance_id, title, description_text, description_sha256, "
            "criteria_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("job_1", INSTANCE_ID, "Operations Manager", "", "", 3, NOW, NOW),
        ),
        # Approved criterion.
        (
            "INSERT INTO criteria (instance_id, job_id, criterion_id, version, definition, "
            "rationale, evidence_rule, label, created_by, created_at, approved_by, approved_at, "
            "origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                INSTANCE_ID,
                "job_1",
                "cr_01",
                3,
                "Coordinate subcontractors on commercial sites",
                "Core to the role.",
                "",
                "required",
                "reviewer@host",
                NOW,
                "reviewer@host",
                NOW,
                "human",
            ),
        ),
        # Unapproved proposal: must never appear in the payload.
        (
            "INSERT INTO criteria (instance_id, job_id, criterion_id, version, definition, "
            "rationale, evidence_rule, label, created_by, created_at, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                INSTANCE_ID,
                "job_1",
                "cr_99",
                1,
                "Proposed but unapproved criterion",
                "",
                "",
                "preferred",
                "agent:proposal",
                NOW,
                "agent_proposal",
            ),
        ),
        (
            "INSERT INTO documents (id, instance_id, original_filename, display_name, "
            "current_rel_path, first_seen_rel_path, media_type, size_bytes, content_sha256, "
            "processing_state, processing_detail, location, location_version, "
            "decision_needs_recheck, duplicate_content, submitted_at, ingested_at, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "doc_a",
                INSTANCE_ID,
                "candidate-001.pdf",
                None,  # unknown display name, must stay null
                "candidate-001.pdf",
                "candidate-001.pdf",
                "pdf",
                182004,
                "sha_a",
                "ready",
                None,
                "active",
                1,
                0,
                0,
                None,  # unknown submitted_at, must stay null
                NOW,
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO documents (id, instance_id, original_filename, display_name, "
            "current_rel_path, first_seen_rel_path, media_type, size_bytes, content_sha256, "
            "processing_state, processing_detail, location, location_version, "
            "decision_needs_recheck, duplicate_content, submitted_at, ingested_at, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "doc_b",
                INSTANCE_ID,
                "resume with space.pdf",
                "Jordan Example",
                "sub folder/café résumé.pdf",
                "sub folder/café résumé.pdf",
                "pdf",
                None,
                "sha_b",
                "manual_review",
                "No extractable text was found by the scanner pass.",
                "active",
                1,
                0,
                1,
                None,
                NOW,
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO documents (id, instance_id, original_filename, display_name, "
            "current_rel_path, first_seen_rel_path, media_type, size_bytes, content_sha256, "
            "processing_state, processing_detail, location, location_version, "
            "decision_needs_recheck, duplicate_content, submitted_at, ingested_at, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "doc_c",
                INSTANCE_ID,
                "candidate-003.docx",
                "Alex Sample",
                "candidate-003.docx",
                "candidate-003.docx",
                "docx",
                900,
                "sha_c",
                "error",
                "parser exploded",
                "missing",
                2,
                1,
                0,
                "2026-09-01T00:00:00.000000+00:00",
                NOW,
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO decisions (document_id, instance_id, disposition, decision_revision, "
            "actor, decided_at, needs_recheck, disposition_frozen, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("doc_a", INSTANCE_ID, "keep", 2, "reviewer@host", NOW, 0, 0, NOW, NOW),
        ),
        (
            "INSERT INTO decisions (document_id, instance_id, disposition, decision_revision, "
            "actor, decided_at, needs_recheck, disposition_frozen, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("doc_b", INSTANCE_ID, "reject", 1, "reviewer@host", NOW, 0, 1, NOW, NOW),
        ),
        (
            "INSERT INTO action_intents (document_id, instance_id, intent, intent_revision, "
            "state, requester, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("doc_b", INSTANCE_ID, "move_rejected", 1, "saved", "reviewer@host", NOW, NOW),
        ),
        (
            "INSERT INTO profiles (id, instance_id, document_id, source_revision, "
            "criteria_version, prompt_version, schema_version, model_route, summary_text, "
            "validation_state, is_fixture, is_current, stale, generated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "prof_a",
                INSTANCE_ID,
                "doc_a",
                1,
                3,
                "prompt.1",
                "1.0",
                "fixture",
                "Reports commercial renovation coordination experience.",
                "valid",
                1,
                1,
                0,
                NOW,
            ),
        ),
        (
            "INSERT INTO evidence (instance_id, profile_id, document_id, evidence_key, "
            "claim_kind, criterion_id, result, span_id, locator_json, quote, validation, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                INSTANCE_ID,
                "prof_a",
                "doc_a",
                "ev_001",
                "criterion",
                "cr_01",
                "supported",
                "page_1_block_4",
                '{"page": 1}',
                "Coordinated subcontractors on commercial renovations.",
                "verified",
                NOW,
            ),
        ),
        (
            "INSERT INTO evidence (instance_id, profile_id, document_id, evidence_key, "
            "claim_kind, criterion_id, result, span_id, locator_json, quote, validation, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                INSTANCE_ID,
                "prof_a",
                "doc_a",
                "ev_002",
                "summary",
                None,
                None,
                "page_1_block_1",
                "{}",
                "Operations Manager with 12 years of experience.",
                "verified",
                NOW,
            ),
        ),
        (
            "INSERT INTO review_tasks (id, instance_id, document_id, task_type, criterion_id, "
            "dedupe_key, origin, title, detail, state, severity, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task_1",
                INSTANCE_ID,
                "doc_a",
                "general",
                None,
                "dedupe_1",
                "human",
                "Confirm availability date",
                "",
                "open",
                "normal",
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO review_tasks (id, instance_id, document_id, task_type, criterion_id, "
            "dedupe_key, origin, title, detail, state, severity, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task_2",
                INSTANCE_ID,
                "doc_a",
                "general",
                None,
                "dedupe_2",
                "human",
                "Already resolved",
                "",
                "closed",
                "normal",
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO review_tasks (id, instance_id, document_id, task_type, criterion_id, "
            "dedupe_key, origin, title, detail, state, severity, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task_3",
                INSTANCE_ID,
                "doc_b",
                "verify_certification",
                "cr_01",
                "dedupe_3",
                "agent",
                "Verify certification",
                "The document names a certificate that needs verification.",
                "open",
                "attention",
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO notes (id, instance_id, document_id, body, author, note_revision, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("note_1", INSTANCE_ID, "doc_a", "Strong coordination examples.", "reviewer@host", 1, NOW, NOW),
        ),
        (
            "INSERT INTO action_batches (id, instance_id, plan_json, plan_hash, criteria_version, "
            "execution_state, execution_revision, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("batch_1", INSTANCE_ID, "{}", "hash_1", 3, "completed", 1, "reviewer@host", NOW, NOW),
        ),
        (
            "INSERT INTO file_operations (id, instance_id, batch_id, document_id, sequence, kind, "
            "source_rel_path, destination_rel_path, expected_sha256, source_revision, "
            "decision_revision, intent_revision, location_version, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "op_1",
                INSTANCE_ID,
                "batch_1",
                "doc_b",
                0,
                "move_rejected",
                "sub folder/café résumé.pdf",
                "Rejected/doc_b/café résumé.pdf",
                "sha_b",
                1,
                1,
                1,
                1,
                "committed",
                NOW,
                NOW,
            ),
        ),
        (
            "INSERT INTO audit_events (instance_id, actor, actor_kind, event, entity_type, "
            "entity_id, affected_ids_json, prior_json, new_json, outcome, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                INSTANCE_ID,
                "reviewer@host",
                "human",
                "decision.set",
                "decision",
                "doc_a",
                "[]",
                None,
                json.dumps({"disposition": "keep", "actor": "reviewer@host", "decision_revision": 2}),
                "ok",
                NOW,
            ),
        ),
    ]

    with db.bare_transaction() as conn:
        for sql, params in inserts:
            conn.execute(sql, params)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = _make_db(tmp_path)
    _seed(database)
    return database


@pytest.fixture
def payload(db: Database) -> dict:
    return build_snapshot_payload(db, mode="snapshot", generated_at=NOW)


@pytest.fixture
def assets() -> dict[str, str]:
    return {
        "css": ".app-header { font-family: sans-serif; }",
        "js": "console.log('snapshot renderer');",
    }


@pytest.fixture
def report_path(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / "review.html"


# ---------------------------------------------------------------------------
# Payload shape and contract rules
# ---------------------------------------------------------------------------
def test_payload_matches_documented_shape(payload: dict) -> None:
    assert set(payload) == TOP_LEVEL_KEYS
    assert set(payload["instance"]) == INSTANCE_KEYS
    assert set(payload["counts"]) == COUNT_KEYS
    assert payload["schema_version"] == "1.0"
    assert payload["mode"] == "snapshot"

    for criterion in payload["instance"]["criteria"]:
        assert set(criterion) == CRITERION_KEYS

    assert len(payload["documents"]) == 3
    for document in payload["documents"]:
        assert set(document) == DOCUMENT_KEYS
        for item in document["criteria"]:
            assert set(item) == DOC_CRITERION_KEYS
        for item in document["evidence"]:
            assert set(item) == EVIDENCE_KEYS
        for item in document["tasks"]:
            assert set(item) == TASK_KEYS


def test_only_approved_criteria_appear(payload: dict) -> None:
    criterion_ids = {c["criterion_id"] for c in payload["instance"]["criteria"]}
    assert criterion_ids == {"cr_01"}
    assert payload["instance"]["criteria_version"] == 3


def test_unknown_values_are_null_not_zero_or_empty(payload: dict) -> None:
    doc_a = next(d for d in payload["documents"] if d["document_id"] == "doc_a")
    assert doc_a["display_name"] is None
    assert doc_a["submitted_at"] is None
    assert doc_a["processing_detail"] is None
    assert doc_a["recheck_reason"] is None
    assert doc_a["duplicate_of"] is None

    doc_b = next(d for d in payload["documents"] if d["document_id"] == "doc_b")
    assert doc_b["size_bytes"] is None
    assert doc_b["summary_text"] is None  # no profile for doc_b
    # A criterion with no assessment is null, never "not_found" or a negative.
    results = {c["criterion_id"]: c["result"] for c in doc_b["criteria"]}
    assert results == {"cr_01": None}


def test_counts_are_whole_instance_and_filtered(payload: dict) -> None:
    counts = payload["counts"]
    assert counts["total"] == 3
    assert counts["filtered"] == 3
    assert counts["unreviewed"] == 1  # doc_c has no decision row
    assert counts["keep"] == 1
    assert counts["reject"] == 1
    assert counts["hold"] == 0
    assert counts["manual_review"] == 1  # doc_b
    assert counts["pending_action"] == 1  # doc_b save intent
    assert counts["needs_recheck"] == 1  # doc_c
    assert counts["open_tasks"] == 2  # task_1 and task_3


def test_filtered_scope_keeps_whole_instance_total(db: Database) -> None:
    scoped = build_snapshot_payload(db, document_ids=["doc_b"], generated_at=NOW)
    assert scoped["counts"]["total"] == 3
    assert scoped["counts"]["filtered"] == 1
    assert [d["document_id"] for d in scoped["documents"]] == ["doc_b"]


def test_last_analysis_at_null_when_no_profiles(tmp_path: Path) -> None:
    database = _make_db(tmp_path)
    with database.bare_transaction() as conn:
        conn.execute(
            "INSERT INTO instances (id, schema_version, app_version, state_revision, "
            "storage_mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (INSTANCE_ID, 1, "0.1.0", 0, "local", NOW, NOW),
        )
    empty = build_snapshot_payload(database, generated_at=NOW)
    assert empty["instance"]["last_analysis_at"] is None
    assert empty["documents"] == []
    assert empty["counts"]["total"] == 0


def test_document_link_is_percent_encoded_per_segment(payload: dict) -> None:
    doc_b = next(d for d in payload["documents"] if d["document_id"] == "doc_b")
    assert doc_b["document_link"] == "./sub%20folder/caf%C3%A9%20r%C3%A9sum%C3%A9.pdf"
    doc_a = next(d for d in payload["documents"] if d["document_id"] == "doc_a")
    assert doc_a["document_link"] == "./candidate-001.pdf"


def test_no_aggregate_score_or_rank_anywhere(payload: dict) -> None:
    forbidden = {"score", "rank", "suitability", "fit", "fit_score", "rating", "best_match"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert key.lower() not in forbidden, f"payload emitted {key!r}"
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)


def test_evidence_and_tasks_and_history_are_attached(payload: dict) -> None:
    doc_a = next(d for d in payload["documents"] if d["document_id"] == "doc_a")
    assert doc_a["review_state"] == "keep"
    assert doc_a["decision_revision"] == 2
    assert doc_a["summary_stale"] is False
    assert doc_a["open_task_count"] == 1
    assert doc_a["task_warning"] is False
    assert {e["id"] for e in doc_a["evidence"]} == {"ev_001", "ev_002"}
    criterion = doc_a["criteria"][0]
    assert criterion["result"] == "supported"
    assert criterion["evidence_ids"] == ["ev_001"]
    assert doc_a["decision_history"][0]["disposition"] == "keep"
    assert doc_a["notes"][0]["body"] == "Strong coordination examples."
    # Tasks are open-first so a closed item is not mistaken for work owed.
    assert [t["id"] for t in doc_a["tasks"]] == ["task_1", "task_2"]

    doc_b = next(d for d in payload["documents"] if d["document_id"] == "doc_b")
    assert doc_b["task_warning"] is True
    assert doc_b["disposition_frozen"] is True
    assert doc_b["pending_intent"] == "move_rejected"
    assert doc_b["file_actions"][0]["kind"] == "move_rejected"
    assert doc_b["file_actions"][0]["state"] == "committed"


def test_warnings_are_code_keyed_and_never_echo_detail(payload: dict) -> None:
    doc_b = next(d for d in payload["documents"] if d["document_id"] == "doc_b")
    assert any(w["code"] == "SCAN_ONLY_DOCUMENT" for w in doc_b["warnings"])

    doc_c = next(d for d in payload["documents"] if d["document_id"] == "doc_c")
    codes = {w["code"] for w in doc_c["warnings"]}
    assert {"FILE_MISSING", "EXTRACTION_FAILED", "DECISION_NEEDS_RECHECK"} <= codes
    # A raw pipeline detail string is never copied into a warning message.
    for warning in doc_c["warnings"]:
        assert "parser exploded" not in warning["message"]


# ---------------------------------------------------------------------------
# Rendering and embedding safety
# ---------------------------------------------------------------------------
def _embedded_json(html: str) -> str:
    marker = f'<script id="{reporting.snapshot.PAYLOAD_ELEMENT_ID}" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return html[start:end]


def test_embedded_json_cannot_be_closed_by_applicant_text(payload: dict, assets: dict[str, str]) -> None:
    hostile = "line</script><script>alert(1)</script> tail end"
    payload["documents"][0]["notes"].append(
        {"id": "note_x", "body": hostile, "author": "reviewer@host", "updated_at": NOW}
    )
    payload["documents"][0]["display_name"] = "x></script><img src=x>"

    html = render_snapshot(payload, assets)
    embedded = _embedded_json(html)

    # The script-closing sequence cannot appear inside the embedded element, and
    # the Unicode line separators are escaped so a JS parser cannot be confused.
    assert "</script>" not in embedded
    assert "<" not in embedded
    assert " " not in embedded
    assert " " not in embedded

    restored = json.loads(embedded)
    note = restored["documents"][0]["notes"][-1]
    assert note["body"] == hostile  # exact round-trip, no lossy escaping
    assert restored["mode"] == "snapshot"
    assert restored["documents"][0]["display_name"] == "x></script><img src=x>"


def test_mode_is_forced_to_snapshot(payload: dict, assets: dict[str, str]) -> None:
    payload["mode"] = "connected"
    html = render_snapshot(payload, assets)
    assert json.loads(_embedded_json(html))["mode"] == "snapshot"
    assert 'data-mode="snapshot"' in html


def test_interpolated_values_are_html_escaped(payload: dict, assets: dict[str, str]) -> None:
    payload["instance"]["job_title"] = "<b>Ops & Eng</b>"
    payload["generated_at"] = '"><script>alert(1)</script>'

    html = render_snapshot(payload, assets)

    assert "&lt;b&gt;Ops &amp; Eng&lt;&#x2F;b&gt;" in html
    assert "<b>Ops & Eng</b>" not in html
    # The crafted timestamp cannot break out of its attribute.
    assert '"><script>' not in html
    assert "&quot;&gt;&lt;script&gt;" in html


def test_rendered_document_makes_no_external_request(payload: dict) -> None:
    assets = {
        "css": ".app { color: black; }",
        "js": "console.log('no network here');",
    }
    html = render_snapshot(payload, assets)

    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script src" not in html
    assert "@import" not in html
    assert "url(" not in html

    assert '<meta http-equiv="Content-Security-Policy"' in html
    assert "default-src 'none'" in html
    assert "connect-src 'none'" in html


def test_snapshot_header_shows_timestamp_revision_and_credential_free_link(
    payload: dict, assets: dict[str, str]
) -> None:
    html = render_snapshot(payload, assets)
    assert NOW in html
    assert "State revision" in html and "412" in html
    assert "Open connected review" in html

    with_url = render_snapshot(payload, assets, connected_url="http://127.0.0.1:8765/instance")
    assert "token" not in with_url.lower()

    # With no host known, the link is present but marked unavailable, and never
    # carries a guessed address or a credential.
    assert 'href="#"' in html


def test_load_assets_requires_both_files(tmp_path: Path) -> None:
    empty = tmp_path / "assets"
    empty.mkdir()
    with pytest.raises(ResumeReviewError) as excinfo:
        load_assets(empty)
    assert excinfo.value.code == "MANIFEST_MISSING"
    assert excinfo.value.detail["missing"] == ["report.css", "report.js"]

    (empty / "report.css").write_text("body{}", encoding="utf-8")
    with pytest.raises(ResumeReviewError):
        load_assets(empty)

    (empty / "report.js").write_text("// js", encoding="utf-8")
    loaded = load_assets(empty)
    assert loaded == {"css": "body{}", "js": "// js"}


def test_write_snapshot_publishes_and_reports_metadata(
    tmp_path: Path, payload: dict, assets: dict[str, str]
) -> None:
    target = tmp_path / "workspace" / "review.html"
    result = write_snapshot(target, payload, assets=assets)

    assert result.published is True
    assert result.stale is False
    assert result.state_revision == 412
    assert result.generated_at == NOW
    assert result.byte_size == target.stat().st_size
    assert result.warnings == []
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert _leftover_temp_files(target.parent) == []


# ---------------------------------------------------------------------------
# Atomic publication and stale-snapshot handling (AT-20)
# ---------------------------------------------------------------------------
def _leftover_temp_files(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_blocked_replacement_keeps_prior_report_and_reports_staleness(
    tmp_path: Path, payload: dict, assets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workspace" / "review.html"
    first = write_snapshot(target, payload, assets=assets)
    assert first.published is True
    prior_bytes = target.read_bytes()

    def _blocked(src: object, dst: object) -> None:
        raise PermissionError("the report is open in another program")

    monkeypatch.setattr(publish_mod.os, "replace", _blocked)

    payload["counts"]["total"] = 999
    second = write_snapshot(target, payload, assets=assets)

    assert second.published is False
    assert second.stale is True
    assert second.state_revision == 412
    # The previous valid report is untouched, byte for byte.
    assert target.read_bytes() == prior_bytes

    codes = {w.code for w in second.warnings}
    assert "SNAPSHOT_PUBLISH_FAILED" in codes
    assert "SNAPSHOT_STALE" in codes
    stale = next(w for w in second.warnings if w.code == "SNAPSHOT_STALE")
    assert stale.detail["reason"] == "locked_or_permission_denied"
    # No absolute path or applicant content leaks into the warning.
    assert str(tmp_path) not in stale.message
    # The temporary file is not left behind on the failure path either.
    assert _leftover_temp_files(target.parent) == []


def test_failed_first_publish_is_not_reported_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workspace" / "review.html"

    def _blocked(src: object, dst: object) -> None:
        raise PermissionError("blocked")

    monkeypatch.setattr(publish_mod.os, "replace", _blocked)
    result = publish_report(target, "<!doctype html>", state_revision=1, generated_at=NOW)

    assert result.published is False
    assert result.stale is False  # there was no prior report to go stale
    assert result.byte_size == 0
    assert {w.code for w in result.warnings} == {"SNAPSHOT_PUBLISH_FAILED"}
    assert _leftover_temp_files(target.parent) == []


def test_temporary_filename_never_contains_applicant_content(
    tmp_path: Path, payload: dict, assets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workspace" / "review.html"
    seen: list[str] = []
    real_replace = publish_mod.os.replace

    def _spy(src: object, dst: object) -> None:
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(publish_mod.os, "replace", _spy)
    write_snapshot(target, payload, assets=assets)

    assert seen, "publication did not go through an atomic replace"
    assert seen[0].startswith(".review.html.")
    assert seen[0].endswith(".tmp")
    assert "Jordan" not in seen[0]
    assert "caf" not in seen[0]


# ---------------------------------------------------------------------------
# Seam with the concurrently-authored browser client (web/assets/report.js)
# ---------------------------------------------------------------------------
_COUNT_IDS = {
    "rr-count-total",
    "rr-count-processed",
    "rr-count-unreviewed",
    "rr-count-keep",
    "rr-count-reject",
    "rr-count-hold",
    "rr-count-manual-review",
    "rr-count-pending-action",
    "rr-count-needs-recheck",
    "rr-count-open-tasks",
    "rr-count-filtered",
    "rr-count-page",
    "rr-count-omitted",
    "rr-count-selected",
}


def _required_client_ids() -> set[str] | None:
    """Every element id ``report.js`` looks up, read from the shipped client."""
    try:
        client = (reporting.snapshot.asset_dir() / "report.js").read_text(encoding="utf-8")
    except OSError:
        return None
    ids = set(re.findall(r'byId\("(rr-[a-z0-9-]+)"\)', client))
    # The drawer-close ids are looked up through a loop, not a literal byId call.
    ids.update({"rr-chat-close", "rr-actions-close"})
    ids.update(_COUNT_IDS)
    return ids


def test_shell_provides_every_element_the_client_addresses(
    payload: dict, assets: dict[str, str]
) -> None:
    required = _required_client_ids()
    if required is None:
        pytest.skip("web/assets/report.js is not installed in this checkout")

    html = render_snapshot(payload, assets)
    missing = sorted(identifier for identifier in required if f'id="{identifier}"' not in html)
    assert missing == [], f"snapshot shell is missing client-addressed ids: {missing}"


def test_renders_with_the_real_installed_assets(payload: dict) -> None:
    try:
        real_assets = load_assets()
    except ResumeReviewError:
        pytest.skip("web/assets is not installed in this checkout")

    html = render_snapshot(payload, real_assets)

    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "<script type=\"module\">" in html
    # The shipped client is inlined, not linked, and brings no network reference.
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script src" not in html
    assert json.loads(_embedded_json(html))["documents"][0]["document_id"] in {"doc_a", "doc_b", "doc_c"}
