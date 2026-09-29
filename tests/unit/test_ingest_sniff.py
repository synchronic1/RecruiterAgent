"""Sniffing tests: magic-byte typing for every supported and refused format.

Authority: PRD section 6.1, AT-10.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures import synth  # noqa: E402  (path set up above)

from resume_review.errors import Code  # noqa: E402
from resume_review.ingest import sniff_bytes, sniff_path  # noqa: E402
from resume_review.models import MediaType  # noqa: E402


def test_sniff_utf8_text(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "a.txt", "Avery Sample\nsynthetic\n", encoding="utf-8")
    result = sniff_path(path)
    assert result.media_type is MediaType.TXT
    assert result.detail == "text"
    assert result.encoding == "utf-8"
    assert result.extension == "txt"
    assert result.extension_disagrees is False
    assert result.supported is True


def test_sniff_utf16_text_with_bom(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "b.txt", "Avery Sample\n", encoding="utf-16")
    result = sniff_path(path)
    assert result.media_type is MediaType.TXT
    assert result.encoding == "utf-16"


def test_sniff_utf16_without_bom_is_detected_by_pattern(tmp_path: Path) -> None:
    path = tmp_path / "le.txt"
    data = "Avery Sample placeholder\n" * 4
    path.write_bytes(data.encode("utf-16-le"))
    result = sniff_path(path)
    assert result.media_type is MediaType.TXT
    assert result.encoding == "utf-16-le"


def test_sniff_cp1252_text(tmp_path: Path) -> None:
    path = synth.write_txt(tmp_path / "c.txt", "Caf\xe9 Sample\n", encoding="cp1252")
    result = sniff_path(path)
    assert result.media_type is MediaType.TXT
    assert result.encoding == "cp1252"


def test_sniff_pdf_by_magic_not_extension(tmp_path: Path) -> None:
    path = synth.write_pdf(tmp_path / "renamed.txt")
    result = sniff_path(path)
    assert result.media_type is MediaType.PDF
    assert result.detail == "pdf"
    # The extension said text; the bytes said PDF. That disagreement is reported.
    assert result.extension == "txt"
    assert result.extension_disagrees is True


def test_sniff_docx_requires_both_archive_members(tmp_path: Path) -> None:
    good = synth.write_docx(tmp_path / "resume.docx")
    assert sniff_path(good).media_type is MediaType.DOCX

    # A ZIP with only word/document.xml is not a DOCX.
    import zipfile

    near = tmp_path / "near.docx"
    with zipfile.ZipFile(near, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    result = sniff_path(near)
    assert result.media_type is MediaType.UNSUPPORTED
    assert result.detail == "zip_unreadable" or result.detail == "zip_archive"


def test_sniff_legacy_doc_is_unsupported(tmp_path: Path) -> None:
    path = synth.write_ole_doc(tmp_path / "old.doc")
    result = sniff_path(path)
    assert result.media_type is MediaType.UNSUPPORTED
    assert result.detail == "ole_cfb"
    assert result.reason_code == Code.UNSUPPORTED_FORMAT
    assert result.supported is False


def test_sniff_zip_that_is_not_docx_is_unsupported_archive(tmp_path: Path) -> None:
    path = synth.write_zip_archive(tmp_path / "bundle.zip")
    result = sniff_path(path)
    assert result.media_type is MediaType.UNSUPPORTED
    assert result.detail == "zip_archive"
    assert "notes.txt" in result.container_entries


@pytest.mark.parametrize(
    "writer, detail",
    [
        (synth.write_rtf, "rtf"),
        (synth.write_html, "html"),
        (synth.write_binary, "binary"),
    ],
)
def test_sniff_refused_formats(tmp_path: Path, writer, detail: str) -> None:
    path = writer(tmp_path / f"file-{detail}.x")
    result = sniff_path(path)
    assert result.media_type is MediaType.UNSUPPORTED
    assert result.detail == detail
    assert result.reason_code == Code.UNSUPPORTED_FORMAT


def test_encrypted_pdf_detected_without_decoding(tmp_path: Path) -> None:
    path = synth.write_encrypted_pdf(tmp_path / "secret.pdf")
    result = sniff_path(path)
    assert result.media_type is MediaType.PDF
    assert result.encrypted is True
    # Encrypted is the manual-review state; scan-only would be a second, wrong one.
    assert result.scan_only is False
    assert result.requires_manual_review is True


def test_scan_only_pdf_detected_without_decoding(tmp_path: Path) -> None:
    path = synth.write_scan_only_pdf(tmp_path / "scan.pdf")
    result = sniff_path(path)
    assert result.media_type is MediaType.PDF
    assert result.encrypted is False
    assert result.scan_only is True
    assert result.requires_manual_review is True


def test_text_pdf_is_not_flagged_scan_only(tmp_path: Path) -> None:
    path = synth.write_pdf(tmp_path / "text.pdf")
    result = sniff_path(path)
    assert result.media_type is MediaType.PDF
    assert result.scan_only is False
    assert result.requires_manual_review is False


def test_extension_agreement_is_reported(tmp_path: Path) -> None:
    path = synth.write_pdf(tmp_path / "resume.pdf")
    assert sniff_path(path).extension_disagrees is False

    # An extension we know nothing about cannot disagree with anything.
    unknown = tmp_path / "resume.zzz"
    unknown.write_bytes(b"%PDF-1.4\n")
    assert sniff_path(unknown).extension_disagrees is False


def test_docx_named_pdf_disagrees(tmp_path: Path) -> None:
    path = synth.write_docx(tmp_path / "resume.pdf")
    result = sniff_path(path)
    assert result.media_type is MediaType.DOCX
    assert result.extension_disagrees is True


def test_sniff_bytes_matches_sniff_path_for_pdf(tmp_path: Path) -> None:
    path = synth.write_pdf(tmp_path / "pages.pdf")
    from_bytes = sniff_bytes(path.read_bytes(), filename="pages.pdf")
    from_path = sniff_path(path)
    assert from_bytes.media_type is from_path.media_type
    assert from_bytes.scan_only == from_path.scan_only
    assert from_bytes.encrypted == from_path.encrypted


def test_sniff_empty_file_is_text(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    result = sniff_path(path)
    assert result.media_type is MediaType.TXT
    assert result.detail == "empty"


def test_sniff_missing_file_reports_missing(tmp_path: Path) -> None:
    result = sniff_path(tmp_path / "gone.pdf")
    assert result.media_type is MediaType.UNKNOWN
    assert result.reason_code == Code.FILE_MISSING
