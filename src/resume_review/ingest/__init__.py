"""Deterministic discovery and extraction (PRD section 6).

The pipeline stage that turns a folder of unknown files into addressable spans:

    discover -> stabilize -> sniff -> extract -> (analysis)

Nothing in this package calls a model. Every step is deterministic, and the two
non-deterministic outcomes a real folder produces -- a file that is still being
written and a file whose type cannot be read -- become *pending* or *manual
review* states rather than errors, because the PRD requires every submission to
stay visible (AT-06, AT-08, AT-10).

Public surface::

    from resume_review.ingest import (
        discover, iter_discovery, DiscoveryCensus, DiscoveredFile, SkippedEntry,
        Stabilizer, StableSnapshot, sniff_path, sniff_bytes, SniffResult,
        extract_document, extract_with_cache, ExtractionCache,
    )
"""

from __future__ import annotations

from .discover import (
    SKIP_EXCLUDED_DIR,
    SKIP_NOT_A_FILE,
    SKIP_REPARSE_POINT,
    SKIP_RESERVED_REPORT,
    SKIP_TEMPORARY_FILE,
    SKIP_UNREADABLE,
    DiscoveredFile,
    DiscoveryCensus,
    SkippedEntry,
    discover,
    iter_discovery,
)
from .extract import (
    CACHE_SCHEMA_VERSION,
    CHARACTER_LIMIT_EXCEEDED,
    ExtractionCache,
    cache_key,
    deserialize_extracted,
    extract_document,
    extract_with_cache,
    serialize_extracted,
)
from .sniff import SniffResult, sniff_bytes, sniff_path
from .stabilize import (
    REASON_NOT_REGULAR_FILE,
    REASON_REPARSE_POINT,
    REASON_SOURCE_CHANGED,
    REASON_SOURCE_MISSING,
    REASON_STABLE,
    REASON_STILL_GROWING,
    REASON_UNREADABLE,
    StableSnapshot,
    Stabilizer,
    StabilityVerdict,
)

__all__ = [
    # discovery
    "DiscoveredFile",
    "DiscoveryCensus",
    "SkippedEntry",
    "discover",
    "iter_discovery",
    "SKIP_EXCLUDED_DIR",
    "SKIP_REPARSE_POINT",
    "SKIP_RESERVED_REPORT",
    "SKIP_TEMPORARY_FILE",
    "SKIP_UNREADABLE",
    "SKIP_NOT_A_FILE",
    # stabilization
    "Stabilizer",
    "StableSnapshot",
    "StabilityVerdict",
    "REASON_STABLE",
    "REASON_STILL_GROWING",
    "REASON_SOURCE_CHANGED",
    "REASON_SOURCE_MISSING",
    "REASON_NOT_REGULAR_FILE",
    "REASON_REPARSE_POINT",
    "REASON_UNREADABLE",
    # sniffing
    "SniffResult",
    "sniff_bytes",
    "sniff_path",
    # extraction
    "extract_document",
    "extract_with_cache",
    "ExtractionCache",
    "serialize_extracted",
    "deserialize_extracted",
    "cache_key",
    "CACHE_SCHEMA_VERSION",
    "CHARACTER_LIMIT_EXCEEDED",
]
