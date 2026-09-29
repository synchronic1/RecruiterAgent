"""PDF extraction adapter built on ``pypdf``.

Authority: PRD section 6.3 and AT-10.

    "Source spans carry stable IDs, page numbers for PDFs. ... Do not invent page
     numbers for formats where pagination is not reliably defined."

A PDF knows its pages, so page numbers are the honest locator and each page is
one span with ID ``page_0001``. Two manual-review states live here, and both are
routed to a human rather than to an automatic rejection:

* ``ENCRYPTED_DOCUMENT``  — the document cannot be opened without a password;
* ``SCAN_ONLY_DOCUMENT``  — no page yields extractable text (an image-only scan).

Security note: this adapter reads text only. It never executes JavaScript,
launches a viewer action, follows a URI, or opens an embedded file. ``pypdf`` is
used strictly as a byte-level page-text reader.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

from ..models import DEFAULT_LIMITS, ExtractedDocument, MediaType, ResourceLimits, Span

__all__ = ["PARSER_NAME", "parser_version", "extract", "PDF_EMPTY_PAGE", "PDF_ENCRYPTED", "PDF_SCAN_ONLY"]

PARSER_NAME = "pypdf"

PDF_EMPTY_PAGE = "EMPTY_PAGE"
PDF_ENCRYPTED = "ENCRYPTED_DOCUMENT"
PDF_SCAN_ONLY = "SCAN_ONLY_DOCUMENT"
PDF_FAILED = "EXTRACTION_FAILED"
PDF_TOO_MANY_PAGES = "TOO_MANY_PAGES"


def parser_version() -> str:
    """Version of the actual installed parser, for the extraction cache key.

    A failed lookup must not break extraction, so it degrades to ``unknown`` and
    changes the cache key rather than raising on a machine with a vendored build.
    """
    try:
        return importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - vendored build
        return "unknown"


def _failed(detail: str, warnings: list[str]) -> ExtractedDocument:
    return ExtractedDocument(
        media_type=MediaType.PDF,
        parser_name=PARSER_NAME,
        parser_version=parser_version(),
        state="failed",
        detail=PDF_FAILED,
        warnings=warnings + [PDF_FAILED],
        hints={"code": PDF_FAILED, "error_detail": detail},
    )


def extract(
    snapshot_path: str | Path,
    *,
    limits: ResourceLimits | None = None,
) -> ExtractedDocument:
    """Extract one span per page, capped at ``limits.max_pages``.

    A page beyond the cap is not read at all, and the result is ``partial`` with
    a ``TOO_MANY_PAGES`` warning: an explicit, visible limit rather than a silent
    truncation. A document no page of which yields text is ``unsupported`` with
    ``SCAN_ONLY_DOCUMENT`` so the pipeline routes it to manual review.
    """
    effective = limits or DEFAULT_LIMITS
    path = Path(snapshot_path)

    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is declared
        return _failed("pypdf_unavailable", [])

    warnings: list[str] = []

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            # Attempt an empty-password open first: many "encrypted" PDFs use an
            # empty user password and are still readable. Never guess further.
            try:
                if reader.decrypt("") == 0:
                    return ExtractedDocument(
                        media_type=MediaType.PDF,
                        parser_name=PARSER_NAME,
                        parser_version=parser_version(),
                        state="unsupported",
                        detail=PDF_ENCRYPTED,
                        warnings=[PDF_ENCRYPTED],
                        hints={"code": PDF_ENCRYPTED, "encrypted": True},
                    )
            except Exception:
                return ExtractedDocument(
                    media_type=MediaType.PDF,
                    parser_name=PARSER_NAME,
                    parser_version=parser_version(),
                    state="unsupported",
                    detail=PDF_ENCRYPTED,
                    warnings=[PDF_ENCRYPTED],
                    hints={"code": PDF_ENCRYPTED, "encrypted": True},
                )

        page_count = len(reader.pages)
        spans: list[Span] = []
        empty_pages: list[int] = []

        limit_pages = effective.max_pages if effective.max_pages > 0 else page_count
        pages_read = min(page_count, limit_pages)

        for index in range(pages_read):
            page_number = index + 1
            try:
                page = reader.pages[index]
                text = page.extract_text() or ""
            except Exception:
                # One unreadable page must not lose the readable ones.
                empty_pages.append(page_number)
                warnings.append(f"{PDF_EMPTY_PAGE}:{page_number}")
                continue

            if not text.strip():
                empty_pages.append(page_number)
                warnings.append(f"{PDF_EMPTY_PAGE}:{page_number}")
                continue

            spans.append(
                Span(
                    span_id=f"page_{page_number:04d}",
                    text=text,
                    locator={"page": page_number},
                    kind="page",
                )
            )

        if page_count > pages_read:
            warnings.append(f"{PDF_TOO_MANY_PAGES}:{page_count}:{pages_read}")

        hints: dict[str, Any] = {
            "page_count": page_count,
            "pages_read": pages_read,
            "empty_pages": empty_pages,
        }

        if not spans:
            # No page produced text: a scan, an image-only export, or a document
            # whose text layer is absent. Manual review, never a rejection.
            hints["code"] = PDF_SCAN_ONLY
            return ExtractedDocument(
                media_type=MediaType.PDF,
                parser_name=PARSER_NAME,
                parser_version=parser_version(),
                state="unsupported",
                detail=PDF_SCAN_ONLY,
                page_count=page_count,
                warnings=[PDF_SCAN_ONLY] + warnings,
                hints=hints,
            )

        state = "partial" if page_count > pages_read else "ok"
        return ExtractedDocument(
            media_type=MediaType.PDF,
            parser_name=PARSER_NAME,
            parser_version=parser_version(),
            state=state,
            detail="ok",
            spans=spans,
            page_count=page_count,
            warnings=warnings,
            hints=hints,
        )
    except Exception as exc:
        # Malformed or truncated documents reach here. The detail must stay free
        # of paths: a resume's filename or folder is not error telemetry.
        return _failed(type(exc).__name__, warnings)
