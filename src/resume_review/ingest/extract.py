"""Extraction dispatcher and the content-hash extraction cache.

Authority: PRD sections 6.1, 6.3 and 11.1.

    "Process incrementally. Cache extraction by content hash and parser version;
     cache assessments by document revision, approved criteria version, prompt
     version, model route/version where known, and analysis schema version.
     Same-document retries must not create duplicate profiles or tasks."

Two responsibilities:

1. **Dispatch** a sniffed media type to the one adapter that handles it, then
   apply the resource limits centrally so every format behaves identically.
2. **Cache** the result under ``(content_sha256, parser_name, parser_version)``
   in the ``extraction_cache`` table, so a rescan of unchanged bytes never
   re-parses and a retry never produces a second set of spans.

Conventions every adapter follows, so the pipeline can branch without parsing
prose:

* ``detail`` carries the stable reason code for ``failed``/``unsupported``
  states and ``ok`` otherwise; ``hints["code"]`` repeats it for callers that
  read hints.
* Warnings are strings of the form ``CODE`` or ``CODE:detail``.
* An adapter never raises for a malformed document.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..errors import Code
from ..models import (
    DEFAULT_LIMITS,
    ExtractedDocument,
    MediaType,
    ResourceLimits,
    Span,
    jsonable,
)
from ..util import now_iso
from . import extract_docx, extract_pdf, extract_txt

__all__ = [
    "CHARACTER_LIMIT_EXCEEDED",
    "UNSUPPORTED_FORMAT",
    "CACHE_SCHEMA_VERSION",
    "extract_document",
    "cache_key",
    "serialize_extracted",
    "deserialize_extracted",
    "ExtractionCache",
    "extract_with_cache",
]

CHARACTER_LIMIT_EXCEEDED = "CHARACTER_LIMIT_EXCEEDED"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"

#: Bumped when the span-ID scheme or the serialized shape changes, so a stale
#: cache row can never be mistaken for a current extraction.
CACHE_SCHEMA_VERSION = "ingest-1"

_ADAPTERS = {
    MediaType.PDF: extract_pdf.extract,
    MediaType.DOCX: extract_docx.extract,
    MediaType.TXT: extract_txt.extract,
}


def _coerce_media_type(value: MediaType | str) -> MediaType:
    if isinstance(value, MediaType):
        return value
    try:
        return MediaType(str(value))
    except ValueError:
        return MediaType.UNKNOWN


def _apply_char_limit(document: ExtractedDocument, limits: ResourceLimits) -> ExtractedDocument:
    """Enforce ``max_extracted_chars`` as a visible partial, never a silent cut.

    Whole spans are kept while they fit and dropped afterwards, so a span is
    never truncated mid-sentence: a mangled span would make legitimate evidence
    fail validation for a reason the reviewer cannot see. At least one span is
    always kept, so an oversized single span produces an extraction rather than an
    empty one. The total, the kept count, and the dropped span count are recorded
    on the result and in a ``CHARACTER_LIMIT_EXCEEDED`` warning.
    """
    limit = limits.max_extracted_chars
    if limit <= 0 or document.state not in ("ok", "partial"):
        return document

    total = document.char_count
    if total <= limit:
        return document

    kept: list[Span] = []
    used = 0
    dropped = 0
    for span in document.spans:
        if kept and used + len(span.text) > limit:
            dropped += 1
            continue
        kept.append(span)
        used += len(span.text)

    document.spans = kept
    document.state = "partial"
    document.warnings.append(f"{CHARACTER_LIMIT_EXCEEDED}:{total}:{used}")
    document.hints["char_limit"] = {
        "max_extracted_chars": limit,
        "total_chars": total,
        "kept_chars": used,
        "dropped_spans": dropped,
    }
    return document


def extract_document(
    snapshot_path: str | Path,
    media_type: MediaType | str,
    *,
    sniff: Any = None,
    limits: ResourceLimits | None = None,
) -> ExtractedDocument:
    """Extract spans from a stabilized snapshot according to its media type.

    ``sniff`` is an optional :class:`~resume_review.ingest.sniff.SniffResult`; it
    is used only to give an unsupported result a precise reason. The dispatcher
    never raises: an unsupported type and a malformed document both return an
    ``ExtractedDocument`` the pipeline can persist and route.
    """
    effective = limits or DEFAULT_LIMITS
    resolved = _coerce_media_type(media_type)

    adapter = _ADAPTERS.get(resolved)
    if adapter is None:
        reason = UNSUPPORTED_FORMAT
        detail = "unsupported"
        if sniff is not None:
            reason = getattr(sniff, "reason_code", None) or UNSUPPORTED_FORMAT
            detail = getattr(sniff, "detail", "unsupported") or "unsupported"
        return ExtractedDocument(
            media_type=resolved,
            parser_name="none",
            parser_version="none",
            state="unsupported",
            detail=reason,
            warnings=[f"{reason}:{detail}"],
            hints={"code": reason, "sniff_detail": detail},
        )

    document = adapter(Path(snapshot_path), limits=effective)
    document.media_type = resolved
    return _apply_char_limit(document, effective)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def serialize_extracted(document: ExtractedDocument) -> str:
    """Canonical JSON for the extraction cache.

    Sorted keys and no insignificant whitespace, so the same extraction always
    serializes to the same bytes and a cache row can be compared by hash.
    """
    return json.dumps(jsonable(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deserialize_extracted(payload: str) -> ExtractedDocument:
    """Rebuild an :class:`ExtractedDocument` from a cache row.

    Unknown keys are ignored and a missing ``spans`` list degrades to empty, so a
    row written by an older build cannot crash a scan.
    """
    raw = json.loads(payload)
    spans = [
        Span(
            span_id=str(item.get("span_id", "")),
            text=str(item.get("text", "")),
            locator=dict(item.get("locator") or {}),
            kind=str(item.get("kind", "text")),
        )
        for item in raw.get("spans", [])
    ]
    try:
        media_type = MediaType(str(raw.get("media_type", "unknown")))
    except ValueError:
        media_type = MediaType.UNKNOWN
    return ExtractedDocument(
        media_type=media_type,
        parser_name=str(raw.get("parser_name", "")),
        parser_version=str(raw.get("parser_version", "")),
        state=str(raw.get("state", "failed")),
        detail=str(raw.get("detail", "")),
        spans=spans,
        page_count=raw.get("page_count"),
        warnings=[str(w) for w in raw.get("warnings", [])],
        hints=dict(raw.get("hints") or {}),
    )


def cache_key(content_sha256: str, parser_name: str, parser_version: str) -> str:
    """Stable digest of the cache identity.

    The database's uniqueness is on the raw columns; this digest exists so an
    in-memory or on-disk memo can be keyed by one string.
    """
    from ..models import sha256_hex

    return sha256_hex("\x1f".join([CACHE_SCHEMA_VERSION, content_sha256, parser_name, parser_version]))


class ExtractionCache:
    """Durable extraction cache over the ``extraction_cache`` table.

    The caller owns the connection and its transactions. The helper runs the only
    writer, so cache writes are not wrapped in a nested transaction here.
    """

    def __init__(self, conn: sqlite3.Connection, instance_id: str) -> None:
        self._conn = conn
        self._instance_id = instance_id

    def get(
        self, content_sha256: str, parser_name: str, parser_version: str
    ) -> ExtractedDocument | None:
        row = self._conn.execute(
            "SELECT payload_json FROM extraction_cache "
            "WHERE instance_id = ? AND content_sha256 = ? AND parser_name = ? AND parser_version = ?",
            (self._instance_id, content_sha256, parser_name, parser_version),
        ).fetchone()
        if row is None:
            return None
        try:
            document = deserialize_extracted(str(row[0]))
        except (ValueError, TypeError):
            # A corrupt row is a cache miss, never a hard failure.
            return None
        self._touch(content_sha256, parser_name, parser_version)
        return document

    def put(self, content_sha256: str, document: ExtractedDocument) -> None:
        now = now_iso()
        self._conn.execute(
            "INSERT INTO extraction_cache "
            "(instance_id, content_sha256, parser_name, parser_version, payload_json, "
            " span_count, char_count, page_count, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instance_id, content_sha256, parser_name, parser_version) DO UPDATE SET "
            "payload_json = excluded.payload_json, span_count = excluded.span_count, "
            "char_count = excluded.char_count, page_count = excluded.page_count, "
            "last_used_at = excluded.last_used_at",
            (
                self._instance_id,
                content_sha256,
                document.parser_name,
                document.parser_version,
                serialize_extracted(document),
                len(document.spans),
                document.char_count,
                document.page_count,
                now,
                now,
            ),
        )

    def _touch(self, content_sha256: str, parser_name: str, parser_version: str) -> None:
        self._conn.execute(
            "UPDATE extraction_cache SET last_used_at = ? "
            "WHERE instance_id = ? AND content_sha256 = ? AND parser_name = ? AND parser_version = ?",
            (now_iso(), self._instance_id, content_sha256, parser_name, parser_version),
        )


def extract_with_cache(
    cache: ExtractionCache,
    snapshot_path: str | Path,
    media_type: MediaType | str,
    content_sha256: str,
    *,
    sniff: Any = None,
    limits: ResourceLimits | None = None,
) -> tuple[ExtractedDocument, bool]:
    """Return ``(document, cache_hit)``, extracting only on a miss.

    The cache is keyed by the hash of the bytes in the snapshot, not by the path
    or the document ID, so a renamed file with identical bytes is a hit and a
    changed file is always a miss.
    """
    resolved = _coerce_media_type(media_type)
    adapters = _ADAPTERS.get(resolved)
    if adapters is not None:
        probe_parser_name = {
            MediaType.PDF: extract_pdf.PARSER_NAME,
            MediaType.DOCX: extract_docx.PARSER_NAME,
            MediaType.TXT: extract_txt.PARSER_NAME,
        }[resolved]
        probe_parser_version = {
            MediaType.PDF: extract_pdf.parser_version,
            MediaType.DOCX: extract_docx.parser_version,
            MediaType.TXT: extract_txt.parser_version,
        }[resolved]()
        cached = cache.get(content_sha256, probe_parser_name, probe_parser_version)
        if cached is not None:
            return cached, True

    document = extract_document(snapshot_path, resolved, sniff=sniff, limits=limits)
    if resolved in _ADAPTERS:
        cache.put(content_sha256, document)
    return document, False
