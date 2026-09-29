"""Extraction tests: adapters, dispatcher, resource limits, and the cache.

Authority: PRD sections 6.1 and 6.3, AT-08, AT-10.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures import synth  # noqa: E402  (path set up above)

from resume_review.ingest import (  # noqa: E402
    CHARACTER_LIMIT_EXCEEDED,
    ExtractionCache,
    deserialize_extracted,
    extract_document,
    extract_with_cache,
    serialize_extracted,
)
from resume_review.models import DEFAULT_LIMITS, MediaType, ResourceLimits  # noqa: E402

_CACHE_DDL = """
CREATE TABLE extraction_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    span_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    UNIQUE (instance_id, content_sha256, parser_name, parser_version)
)
"""


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def test_txt_decode_order_and_encoding_recorded(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "u16.txt", "Avery Sample\ninvented\n", encoding="utf-16")
    document = extract_document(path, MediaType.TXT)
    assert document.state == "ok"
    assert document.hints["encoding"] == "utf-16"
    assert "Avery Sample" in document.text


def test_txt_spans_are_line_blocks_with_line_locators(tmp_path: Path) -> None:
    body = "\n".join(f"Synthetic line {n}" for n in range(1, 101))
    path = synth.write_txt(tmp_path / "lines.txt", body, encoding="utf-8")
    document = extract_document(path, MediaType.TXT)

    assert document.media_type is MediaType.TXT
    assert document.page_count is None
    assert document.spans, "expected at least one span"
    first = document.spans[0]
    assert first.locator == {"line_start": 1, "line_end": 40}
    assert first.span_id == "lines_0001_0040"
    assert first.kind == "line_block"
    # Blocks partition the content: the last line number of one is below the
    # first line number of the next.
    for earlier, later in zip(document.spans, document.spans[1:]):
        assert earlier.locator["line_end"] < later.locator["line_start"]


def test_txt_blank_lines_do_not_break_locators(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "gaps.txt", "alpha\n\n\nbeta\n", encoding="utf-8")
    document = extract_document(path, MediaType.TXT)
    assert len(document.spans) == 1
    assert document.spans[0].locator == {"line_start": 1, "line_end": 4}


def test_txt_empty_document_is_ok_with_warning(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "empty.txt", "   \n\n", encoding="utf-8")
    document = extract_document(path, MediaType.TXT)
    assert document.state == "ok"
    assert document.spans == []
    assert "EMPTY_DOCUMENT" in document.warnings


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def test_pdf_one_span_per_page_with_page_locator(tmp_path: Path) -> None:
    path = synth.write_pdf(
        tmp_path / "three.pdf",
        [["Page one text"], ["Page two text"], ["Page three text"]],
    )
    document = extract_document(path, MediaType.PDF)

    assert document.state == "ok"
    assert document.page_count == 3
    assert [s.span_id for s in document.spans] == ["page_0001", "page_0002", "page_0003"]
    assert [s.locator for s in document.spans] == [{"page": 1}, {"page": 2}, {"page": 3}]
    assert all(s.kind == "page" for s in document.spans)
    assert "Page two text" in document.spans[1].text
    assert document.parser_name == "pypdf"
    assert document.parser_version and document.parser_version != "unknown"


def test_pdf_page_cap_is_partial_not_silent(tmp_path: Path) -> None:
    pages = [[f"Synthetic page {n}"] for n in range(1, 61)]
    path = synth.write_pdf(tmp_path / "sixty.pdf", pages)
    limits = ResourceLimits(max_pages=5)

    document = extract_document(path, MediaType.PDF, limits=limits)

    assert document.state == "partial"
    assert len(document.spans) == 5
    assert document.page_count == 60
    assert any(w.startswith("TOO_MANY_PAGES:60:5") for w in document.warnings)
    assert document.hints["pages_read"] == 5


def test_pdf_scan_only_is_unsupported_manual_review(tmp_path: Path) -> None:
    path = synth.write_scan_only_pdf(tmp_path / "scan.pdf")
    document = extract_document(path, MediaType.PDF)
    assert document.state == "unsupported"
    assert document.detail == "SCAN_ONLY_DOCUMENT"
    assert document.hints["code"] == "SCAN_ONLY_DOCUMENT"
    assert document.page_count == 1


def test_pdf_encrypted_is_unsupported_manual_review(tmp_path: Path) -> None:
    path = synth.write_encrypted_pdf(tmp_path / "secret.pdf")
    document = extract_document(path, MediaType.PDF)
    assert document.state == "unsupported"
    assert document.detail == "ENCRYPTED_DOCUMENT"
    assert document.spans == []


def test_pdf_corrupt_returns_failed_without_raising(tmp_path: Path) -> None:
    path = synth.write_corrupt_pdf(tmp_path / "broken.pdf")
    document = extract_document(path, MediaType.PDF)
    assert document.state == "failed"
    assert document.detail == "EXTRACTION_FAILED"
    assert "EXTRACTION_FAILED" in document.warnings


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def test_docx_extracts_paragraphs_and_table_cells(tmp_path: Path) -> None:
    path = synth.write_docx(
        tmp_path / "tables.docx",
        paragraphs=["Avery Sample", "Synthetic profile"],
        tables=[(("Certification", "Year"), ("Sample State Certificate", "2021"))],
    )
    document = extract_document(path, MediaType.DOCX)

    assert document.state == "ok"
    assert document.page_count is None, "a page number must never be invented for DOCX"
    ids = [s.span_id for s in document.spans]
    assert "para_0000" in ids
    assert "tbl_0000_r00_c00" in ids
    assert "tbl_0000_r01_c01" in ids
    table_span = document.span_map()["tbl_0000_r01_c01"]
    assert table_span.locator == {"table": 0, "row": 1, "col": 1}
    assert table_span.text == "2021"
    assert table_span.kind == "table_row"
    assert document.hints["table_count"] == 1


def test_docx_broken_package_returns_failed_without_raising(tmp_path: Path) -> None:
    path = synth.write_broken_docx(tmp_path / "broken.docx")
    document = extract_document(path, MediaType.DOCX)
    assert document.state == "failed"
    assert document.detail == "EXTRACTION_FAILED"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def test_dispatch_of_unsupported_media_type(tmp_path: Path) -> None:
    from resume_review.ingest import sniff_path

    path = synth.write_zip_archive(tmp_path / "bundle.zip")
    sniff = sniff_path(path)
    document = extract_document(path, sniff.media_type, sniff=sniff)
    assert document.state == "unsupported"
    assert document.detail == "UNSUPPORTED_FORMAT"
    assert "UNSUPPORTED_FORMAT:zip_archive" in document.warnings


def test_dispatch_accepts_a_string_media_type(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "a.txt", "invented\n", encoding="utf-8")
    document = extract_document(path, "txt")
    assert document.media_type is MediaType.TXT


def test_character_limit_produces_partial_with_warning(tmp_path: Path) -> None:
    body = "\n".join(f"Synthetic line {n} padded padded padded padded" for n in range(1, 2001))
    path = synth.write_txt(tmp_path / "long.txt", body, encoding="utf-8")
    limits = ResourceLimits(max_extracted_chars=2_000)

    document = extract_document(path, MediaType.TXT, limits=limits)

    assert document.state == "partial"
    assert any(w.startswith(CHARACTER_LIMIT_EXCEEDED) for w in document.warnings)
    assert document.hints["char_limit"]["dropped_spans"] > 0
    # Spans are dropped whole, and the kept text actually respects the limit.
    assert 0 < document.char_count <= 2_000


def test_span_ids_are_deterministic_across_two_extractions(tmp_path: Path) -> None:
    path = synth.write_docx(tmp_path / "stable.docx")
    first = extract_document(path, MediaType.DOCX)
    second = extract_document(path, MediaType.DOCX)
    assert [(s.span_id, s.locator, s.text) for s in first.spans] == [
        (s.span_id, s.locator, s.text) for s in second.spans
    ]

    pdf = synth.write_pdf(tmp_path / "stable.pdf")
    one = extract_document(pdf, MediaType.PDF)
    two = extract_document(pdf, MediaType.PDF)
    assert [s.span_id for s in one.spans] == [s.span_id for s in two.spans]


def test_adapters_never_raise_on_garbage(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(bytes(range(256)) * 8)
    for media_type in (MediaType.PDF, MediaType.DOCX, MediaType.TXT, MediaType.UNSUPPORTED, MediaType.UNKNOWN):
        document = extract_document(garbage, media_type)
        assert document.state in ("ok", "partial", "failed", "unsupported")


def test_failure_detail_contains_no_absolute_path(tmp_path: Path) -> None:
    root_hint = str(tmp_path)
    for name, media_type in (("broken.pdf", MediaType.PDF), ("broken.docx", MediaType.DOCX)):
        path = (
            synth.write_corrupt_pdf(tmp_path / name)
            if media_type is MediaType.PDF
            else synth.write_broken_docx(tmp_path / name)
        )
        document = extract_document(path, media_type)
        haystack = " ".join([document.detail, *document.warnings, str(document.hints)])
        assert root_hint not in haystack


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def test_cache_serialize_round_trip(tmp_path: Path) -> None:
    path = synth.write_pdf(tmp_path / "round.pdf")
    document = extract_document(path, MediaType.PDF)
    restored = deserialize_extracted(serialize_extracted(document))
    assert restored.state == document.state
    assert restored.media_type is document.media_type
    assert [(s.span_id, s.locator, s.text) for s in restored.spans] == [
        (s.span_id, s.locator, s.text) for s in document.spans
    ]
    assert restored.warnings == document.warnings


def test_cache_hit_avoids_reparsing_and_miss_reparses(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_CACHE_DDL)
    cache = ExtractionCache(conn, "inst_test")

    path = synth.write_txt(tmp_path / "cached.txt", "invented content\n", encoding="utf-8")

    first, hit_first = extract_with_cache(cache, path, MediaType.TXT, "a" * 64)
    assert hit_first is False
    second, hit_second = extract_with_cache(cache, path, MediaType.TXT, "a" * 64)
    assert hit_second is True
    assert [s.span_id for s in second.spans] == [s.span_id for s in first.spans]

    # Different bytes (different hash) is a miss, and the row is refreshed.
    third, hit_third = extract_with_cache(cache, path, MediaType.TXT, "b" * 64)
    assert hit_third is False
    rows = conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0]
    assert rows == 2


def test_cache_row_columns_match_models(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_CACHE_DDL)
    cache = ExtractionCache(conn, "inst_test")
    path = synth.write_pdf(tmp_path / "row.pdf")
    document = extract_document(path, MediaType.PDF)
    cache.put("c" * 64, document)
    row = conn.execute(
        "SELECT span_count, char_count, page_count FROM extraction_cache"
    ).fetchone()
    assert row[0] == len(document.spans)
    assert row[1] == document.char_count
    assert row[2] == document.page_count


def test_default_limits_are_unmodified(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "small.txt", "invented\n", encoding="utf-8")
    document = extract_document(path, MediaType.TXT, limits=DEFAULT_LIMITS)
    assert document.state == "ok"


@pytest.mark.parametrize("media_type", [MediaType.PDF, MediaType.DOCX, MediaType.TXT])
def test_missing_snapshot_returns_failed(tmp_path: Path, media_type: MediaType) -> None:
    document = extract_document(tmp_path / "absent-source.bin", media_type)
    assert document.state == "failed"
