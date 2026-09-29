"""Plain-text extraction adapter.

Authority: PRD section 6.1 (TXT is a supported input) and section 6.3 (source
spans carry stable IDs and line ranges).

TXT has no pagination and no internal structure, so the only honest locator is a
line range. Spans are consecutive line blocks rather than one span per line:
line-level spans would produce thousands of addressable units for a long resume,
while one giant span would make an evidence locator useless to a reviewer. A
block of :data:`LINES_PER_BLOCK` lines is the compromise, and its ID is derived
from the locator alone so it is stable for the same content hash and parser
version.
"""

from __future__ import annotations

import platform
from pathlib import Path

from ..models import DEFAULT_LIMITS, ExtractedDocument, MediaType, ResourceLimits, Span
from ..security.untrusted import neutralize_unicode, strip_control

__all__ = ["PARSER_NAME", "parser_version", "extract", "LINE_BLOCK_LINES", "decode_text", "DECODE_ORDER"]

PARSER_NAME = "resume_review.ingest.extract_txt"

#: Attempted in order. ``utf-8-sig`` first so a BOM is consumed rather than
#: becoming a spurious character; ``utf-16`` before ``cp1252`` because its BOM
#: makes it unambiguous; ``latin-1`` last because it can decode anything and is
#: therefore only a rescue for a file that sniffing already called text.
DECODE_ORDER: tuple[str, ...] = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")

LINE_BLOCK_LINES = 40


def parser_version() -> str:
    """The decoder is the standard library, so its version is the interpreter's."""
    return f"python-{platform.python_version()}"


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes with the documented encoding order.

    Returns ``(text, encoding)``. ``latin-1`` cannot fail, so this function does
    not raise; a wrong encoding is far less damaging here than a failed document,
    and the chosen encoding is recorded on the result.
    """
    for encoding in DECODE_ORDER:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _clean(text: str) -> str:
    """Remove controls and defuse bidi overrides while preserving line breaks.

    The same hygiene the untrusted-content layer applies: layout-spoofing control
    characters must not reach a reviewer's screen or a model's context.
    """
    return neutralize_unicode(strip_control(text, keep_newlines=True))


def _blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    """Group 1-based line numbers into blocks of non-blank text.

    Blank lines are skipped from the span body but still advance the line
    counter, so a locator keeps pointing at the real line number in the file.
    """
    out: list[tuple[int, int, str]] = []
    current_start: int | None = None
    current_lines: list[str] = []
    current_end = 0

    def flush() -> None:
        nonlocal current_start, current_lines, current_end
        if current_start is None:
            return
        body = "\n".join(line for line in current_lines if line.strip())
        if body:
            out.append((current_start, current_end, body))
        current_start = None
        current_lines = []
        current_end = 0

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if current_start is None:
            current_start = number
        current_lines.append(line)
        current_end = number
        if len(current_lines) >= LINE_BLOCK_LINES:
            flush()

    flush()
    return out


def extract(
    snapshot_path: str | Path,
    *,
    limits: ResourceLimits | None = None,
) -> ExtractedDocument:
    """Extract line-block spans from a plain-text snapshot.

    Never raises for a malformed document: decoding failures are impossible by
    construction (the last codec is total), and read failures return ``failed``.
    """
    del limits  # the character limit is enforced centrally by the dispatcher
    path = Path(snapshot_path)
    warnings: list[str] = []

    try:
        data = path.read_bytes()
    except OSError:
        return ExtractedDocument(
            media_type=MediaType.TXT,
            parser_name=PARSER_NAME,
            parser_version=parser_version(),
            state="failed",
            detail="EXTRACTION_FAILED",
            warnings=["EXTRACTION_FAILED"],
            hints={"code": "EXTRACTION_FAILED"},
        )

    text, encoding = decode_text(data)
    lines = text.splitlines()

    spans: list[Span] = []
    for start, end, body in _blocks(lines):
        cleaned = _clean(body)
        if not cleaned.strip():
            continue
        spans.append(
            Span(
                span_id=f"lines_{start:04d}_{end:04d}",
                text=cleaned,
                locator={"line_start": start, "line_end": end},
                kind="line_block",
            )
        )

    if not spans:
        warnings.append("EMPTY_DOCUMENT")

    return ExtractedDocument(
        media_type=MediaType.TXT,
        parser_name=PARSER_NAME,
        parser_version=parser_version(),
        state="ok",
        detail="ok",
        spans=spans,
        page_count=None,
        warnings=warnings,
        hints={
            "encoding": encoding,
            "line_count": len(lines),
            "byte_count": len(data),
        },
    )
