"""Bounded opt-in smoke test over the real resume corpus.

This suite reads the real, gitignored HuggingFace resume datasets under
``resume/`` and exercises the deterministic pipeline end to end over a small,
explicitly bounded sample:

    discovery -> type sniffing -> extraction -> anti-fabrication validation

It is opt-in and never runs in the default suite. The gate lives in
``tests/corpus/conftest.py``: set ``RESUME_REVIEW_REAL_CORPUS=1`` and ensure the
corpus is present (``python resume/_download.py``). See ``docs/corpus.md``.

The work is bounded four ways, each reported when it bites:

* :data:`DISCOVERY_SAMPLE` candidate files for the incremental discovery pass;
* :data:`SNIFF_SAMPLE` files for type sniffing;
* :data:`EXTRACT_SAMPLE` documents (the smallest :data:`EXTRACT_MAX_BYTES`
  candidates) for extraction and for the validator check;
* :data:`TIME_BUDGET_SECONDS` of wall clock for the extraction loop.

Real applicant text is used in memory only. Nothing derived from the corpus is
written to a committed fixture, golden file, snapshot, or report.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

import pytest

from resume_review.analysis import (
    ANALYSIS_SCHEMA_VERSION,
    ProblemCode,
    validate_analysis_result,
)
from resume_review.ingest import (
    DiscoveredFile,
    SniffResult,
    extract_document,
    iter_discovery,
    sniff_path,
)
from resume_review.ingest.sniff import EXTENSION_FAMILIES
from resume_review.models import (
    Criterion,
    ExtractedDocument,
    MediaType,
    Span,
    normalize_ws,
)

pytestmark = pytest.mark.corpus

# -- bounds ------------------------------------------------------------------
#: Candidate files pulled from the incremental discovery pass.
DISCOVERY_SAMPLE = 200
#: Files sniffed for type detection.
SNIFF_SAMPLE = 24
#: Suffixes considered when assembling the sniffing sample.
SNIFF_SUFFIXES = frozenset({".pdf", ".txt", ".csv", ".docx"})
#: How many on-disk candidates to consider before picking the smallest.
EXTRACT_CANDIDATE_POOL = 40
#: How many documents to actually extract (and validate evidence against).
EXTRACT_SAMPLE = 6
#: Skip candidates larger than this, so a single file cannot dominate the run.
EXTRACT_MAX_BYTES = 2 * 1024 * 1024
#: Wall-clock budget for the extraction loop.
TIME_BUDGET_SECONDS = 90.0
#: A span must hold at least this many characters to be usable as evidence.
MIN_REAL_TEXT_CHARS = 40

_DIRS_TO_SKIP = frozenset({".cache", ".git", "__pycache__"})


# ---------------------------------------------------------------------------
# Bounded sampling helpers
# ---------------------------------------------------------------------------
def _bounded_files(
    root: Path,
    suffixes: frozenset[str],
    *,
    limit: int,
    max_bytes: int | None = None,
) -> list[Path]:
    """Return up to ``limit`` non-empty files under ``root`` with a wanted suffix.

    Deterministic (walker order is sorted) and bounded: the walk stops as soon as
    ``limit`` files are found, so a large dataset tree is never fully traversed.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _DIRS_TO_SKIP)
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in suffixes:
                continue
            path = Path(dirpath) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0 or (max_bytes is not None and size > max_bytes):
                continue
            found.append(path)
            if len(found) >= limit:
                return found
    return found


def _extract_candidates(root: Path) -> list[Path]:
    """The smallest bounded set of small, real documents to extract.

    Prefers PDFs (the real resume format); falls back to plain text if the
    corpus happens to hold none. Files larger than :data:`EXTRACT_MAX_BYTES` are
    excluded so neither time nor memory can run away.
    """
    pool = _bounded_files(
        root, frozenset({".pdf"}), limit=EXTRACT_CANDIDATE_POOL, max_bytes=EXTRACT_MAX_BYTES
    )
    if not pool:
        pool = _bounded_files(
            root,
            frozenset({".txt", ".csv"}),
            limit=EXTRACT_CANDIDATE_POOL,
            max_bytes=EXTRACT_MAX_BYTES,
        )

    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return EXTRACT_MAX_BYTES

    pool.sort(key=_size)
    return pool[:EXTRACT_SAMPLE]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discovery_over_bounded_real_sample(corpus_root: Path) -> None:
    """The incremental walker copes with the real tree and yields well-formed,
    root-contained candidates, including at least one supported extension."""
    discovered: list[DiscoveredFile] = []
    skipped = 0
    for entry in iter_discovery(corpus_root):
        if isinstance(entry, DiscoveredFile):
            discovered.append(entry)
            if len(discovered) >= DISCOVERY_SAMPLE:
                break
        else:
            skipped += 1

    print(
        f"corpus smoke: discovery sampled {len(discovered)} candidate file(s) "
        f"and {skipped} skipped entr(ies) (cap {DISCOVERY_SAMPLE})"
    )
    assert discovered, "discovery found no candidate files under the real corpus"

    for entry in discovered:
        assert entry.size_bytes > 0, f"zero-byte candidate: {entry.rel_path}"
        assert not os.path.isabs(entry.rel_path)
        assert not entry.rel_path.startswith("..")
        # mtime is never promoted to a submission date (PRD 6.2).
        assert entry.submitted_at is None

    extensions = {entry.extension for entry in discovered}
    # Classify with the sniffer's own extension map, so a text-bearing CSV or MD
    # counts as a supported input family just as a .txt would. The bounded prefix
    # of the tree happens to reach cache metadata and tabular files first.
    families = {EXTENSION_FAMILIES.get(extension) for extension in extensions}
    assert {"pdf", "docx", "txt"} & families, (
        "no supported input family in the discovery sample: "
        f"extensions={sorted(extensions)} families={sorted(f for f in families if f)}"
    )


# ---------------------------------------------------------------------------
# Type sniffing
# ---------------------------------------------------------------------------
def test_type_sniffing_over_bounded_real_sample(corpus_root: Path) -> None:
    """Sniffing reads magic bytes, never raises, and recognises the real bytes."""
    files = _bounded_files(corpus_root, SNIFF_SUFFIXES, limit=SNIFF_SAMPLE)
    if not files:
        pytest.skip("no .pdf/.txt/.csv/.docx file found in the real corpus")

    counts: Counter[str] = Counter()
    supported = 0
    pdf_seen = 0
    pdf_hits = 0
    for path in files:
        result: SniffResult = sniff_path(path)
        counts[result.media_type.value] += 1
        assert result.size_bytes is not None and result.size_bytes > 0
        if result.supported:
            supported += 1
        if path.suffix.lower() == ".pdf":
            pdf_seen += 1
            if result.media_type is MediaType.PDF:
                pdf_hits += 1

    print(f"corpus smoke: sniffed {len(files)} real file(s): {dict(counts)}")
    assert supported >= 1, f"no supported media type in the sample: {dict(counts)}"
    if pdf_seen:
        assert pdf_hits >= 1, "no sampled real .pdf sniffed as a PDF"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def real_extractions(
    corpus_root: Path,
) -> list[tuple[Path, SniffResult, ExtractedDocument]]:
    """Extract a bounded, small set of real documents, under a wall-clock budget.

    Computed once per session. Reports how many candidates the budget caused it to
    skip; skips the whole thing only if the corpus holds no usable candidate or
    the budget expired before a single extraction.
    """
    candidates = _extract_candidates(corpus_root)
    if not candidates:
        pytest.skip(
            f"no PDF/TXT candidate at or below {EXTRACT_MAX_BYTES} bytes found in the real corpus"
        )

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    results: list[tuple[Path, SniffResult, ExtractedDocument]] = []
    budget_skipped = 0
    for path in candidates:
        if time.monotonic() > deadline:
            budget_skipped += 1
            continue
        result = sniff_path(path)
        document = extract_document(path, result.media_type)
        results.append((path, result, document))

    print(
        f"corpus smoke: extracted {len(results)} of {len(candidates)} bounded candidate(s) "
        f"within the {TIME_BUDGET_SECONDS:.0f}s budget; {budget_skipped} skipped by the budget"
    )
    if not results:
        pytest.skip(f"time budget {TIME_BUDGET_SECONDS:.0f}s exhausted before any extraction")
    return results


def test_extraction_over_bounded_real_subset(
    real_extractions: list[tuple[Path, SniffResult, ExtractedDocument]],
) -> None:
    """Real documents extract without raising and yield addressable spans."""
    states: Counter[str] = Counter(document.state for _, _, document in real_extractions)
    text_docs = [document for _, _, document in real_extractions if document.text.strip()]
    print(
        f"corpus smoke: extraction states {dict(states)}; "
        f"{len(text_docs)} of {len(real_extractions)} document(s) produced text"
    )

    for path, _result, document in real_extractions:
        assert document.parser_name, f"no parser recorded for {path.name}"
        assert document.state in {"ok", "partial", "unsupported", "failed"}
        for span in document.spans:
            assert span.span_id, "extracted span has no stable id"
            assert isinstance(span.locator, dict)

    if not text_docs:
        pytest.skip("bounded real sample produced no text-bearing document (scan-only set?)")
    assert max(len(document.text) for document in text_docs) >= MIN_REAL_TEXT_CHARS


# ---------------------------------------------------------------------------
# Anti-fabrication validation on real extracted text
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def real_text_span(
    real_extractions: list[tuple[Path, SniffResult, ExtractedDocument]],
) -> Span:
    """A real extracted span long enough to serve as an evidence source."""
    for _path, _result, document in real_extractions:
        for span in document.spans:
            if len(span.normalized) >= MIN_REAL_TEXT_CHARS:
                return span
    pytest.skip(
        f"no real span of at least {MIN_REAL_TEXT_CHARS} characters in the bounded sample"
    )


def test_anti_fabrication_validator_on_real_extracted_text(real_text_span: Span) -> None:
    """A quote genuinely present in real extracted text validates; a fabricated
    quote does not, and the failure is non-repairable (PRD 7.2).

    The real text stays in memory: it is never written to a fixture or report.
    """
    span = real_text_span
    document_id = "doc_corpus_smoke"
    criteria = [
        Criterion(
            criterion_id="cr_corpus",
            version=1,
            definition="real-corpus smoke evidence check",
        )
    ]
    spans = [span]

    normalized = span.normalized
    real_quote = normalized[:80]
    assert real_quote, "normalized real span is empty"

    # A probe guaranteed absent from this span, extended until it truly is.
    fabricated = "zzq corpus fabrication probe"
    while normalize_ws(fabricated) in normalized:
        fabricated += "q"

    def payload(quote: str) -> dict:
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "document_id": document_id,
            "source_revision": 1,
            "criteria_version": 1,
            "summary": {"text": "", "evidence_ids": ["ev_1"]},
            "criteria": [
                {
                    "criterion_id": "cr_corpus",
                    "result": "supported",
                    "explanation": "",
                    "evidence_ids": ["ev_1"],
                }
            ],
            "evidence": [
                {
                    "id": "ev_1",
                    "span_id": span.span_id,
                    "locator": dict(span.locator),
                    "quote": quote,
                }
            ],
            "suggested_tasks": [],
            "warnings": [],
        }

    def validate(quote: str):
        return validate_analysis_result(
            payload=payload(quote),
            document_id=document_id,
            source_revision=1,
            criteria_version=1,
            criteria=criteria,
            spans=spans,
        )

    genuine = validate(real_quote)
    assert genuine.ok, [problem.to_dict() for problem in genuine.problems]

    fabricated_outcome = validate(fabricated)
    assert not fabricated_outcome.ok
    codes = [problem.code for problem in fabricated_outcome.problems]
    assert ProblemCode.QUOTE_NOT_FOUND in codes, codes
    quote_problem = next(
        problem for problem in fabricated_outcome.problems if problem.code == ProblemCode.QUOTE_NOT_FOUND
    )
    assert quote_problem.repairable is False
