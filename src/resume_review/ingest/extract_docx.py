"""DOCX extraction adapter built on ``python-docx``.

Authority: PRD section 6.3.

    "Extract DOCX table content as well as paragraphs. Do not invent page numbers
     for formats where pagination is not reliably defined."

A DOCX has no pages until a layout engine renders it, so no page number is ever
produced. The locators are the document's own structure: paragraph index and
table/row/column. Table cells are extracted because a great many resumes put
employment dates, certifications, or skills in a table, and skipping them would
silently drop exactly the facts a criterion assessment needs.

Security note: ``python-docx`` opens the package and reads XML. This adapter
never resolves an external relationship, follows a hyperlink, evaluates a field,
or loads an embedded object.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

from ..models import DEFAULT_LIMITS, ExtractedDocument, MediaType, ResourceLimits, Span
from ..security.untrusted import neutralize_unicode, strip_control

__all__ = ["PARSER_NAME", "parser_version", "extract", "DOCX_FAILED"]

PARSER_NAME = "python-docx"

DOCX_FAILED = "EXTRACTION_FAILED"


def parser_version() -> str:
    """Version of the installed parser; part of the extraction cache key."""
    try:
        return importlib.metadata.version("python-docx")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - vendored build
        return "unknown"


def _clean(text: str) -> str:
    return neutralize_unicode(strip_control(text, keep_newlines=True))


def extract(
    snapshot_path: str | Path,
    *,
    limits: ResourceLimits | None = None,
) -> ExtractedDocument:
    """Extract paragraph spans and table-cell spans from a DOCX snapshot.

    Never raises for a malformed document: a package that ``python-docx`` cannot
    open returns ``failed`` with ``EXTRACTION_FAILED`` and no path in its detail.
    """
    del limits  # the character limit is enforced centrally by the dispatcher
    path = Path(snapshot_path)

    try:
        import docx
    except ImportError:  # pragma: no cover - dependency is declared
        return _failed("python_docx_unavailable")

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        return _failed(type(exc).__name__)

    spans: list[Span] = []
    warnings: list[str] = []

    try:
        for index, paragraph in enumerate(document.paragraphs):
            text = _clean(paragraph.text or "")
            if not text.strip():
                continue
            spans.append(
                Span(
                    span_id=f"para_{index:04d}",
                    text=text,
                    locator={"paragraph": index},
                    kind="paragraph",
                )
            )

        for table_index, table in enumerate(document.tables):
            spans.extend(_table_spans(table, table_index))
    except Exception as exc:
        # Partially-built span lists are discarded: a half-read document must not
        # be presented as a complete extraction.
        return _failed(type(exc).__name__)

    if not spans:
        warnings.append("EMPTY_DOCUMENT")

    return ExtractedDocument(
        media_type=MediaType.DOCX,
        parser_name=PARSER_NAME,
        parser_version=parser_version(),
        state="ok",
        detail="ok",
        spans=spans,
        page_count=None,  # never invented for DOCX
        warnings=warnings,
        hints={
            "paragraph_count": len(document.paragraphs),
            "table_count": len(document.tables),
        },
    )


def _table_spans(table: Any, table_index: int) -> list[Span]:
    """One span per non-empty cell, in row/column order.

    A horizontally merged cell is yielded once per row that owns it; the repeat is
    skipped by identifying the underlying XML element, so a merged value does not
    appear twice in the same row.
    """
    spans: list[Span] = []
    for row_index, row in enumerate(table.rows):
        seen_cells: set[int] = set()
        for col_index, cell in enumerate(row.cells):
            marker = id(cell._tc)  # the merged cell's own element
            if marker in seen_cells:
                continue
            seen_cells.add(marker)
            text = _clean(cell.text or "")
            if not text.strip():
                continue
            spans.append(
                Span(
                    span_id=f"tbl_{table_index:04d}_r{row_index:02d}_c{col_index:02d}",
                    text=text,
                    locator={"table": table_index, "row": row_index, "col": col_index},
                    kind="table_row",
                )
            )
    return spans


def _failed(detail: str) -> ExtractedDocument:
    return ExtractedDocument(
        media_type=MediaType.DOCX,
        parser_name=PARSER_NAME,
        parser_version=parser_version(),
        state="failed",
        detail=DOCX_FAILED,
        warnings=[DOCX_FAILED],
        hints={"code": DOCX_FAILED, "error_detail": detail},
    )
