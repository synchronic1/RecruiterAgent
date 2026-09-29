"""Content-type sniffing by magic bytes.

Authority: PRD section 6.1.

    "Text-bearing PDF, DOCX, and TXT. Sniff the actual file type rather than
     trusting only the extension. Legacy DOC, archives, application emails,
     password-protected files, corrupt files, and scan-only PDFs receive explicit
     unsupported or manual-review states. They remain visible and are never
     automatically rejected."

What this module does and does not do
-------------------------------------
It never decodes a document. It reads the leading bytes, a trailing window, and,
for a ZIP candidate, the archive directory — nothing else. The two PDF manual
review states are detected structurally:

* ``encrypted``   — the file declares ``/Encrypt`` in its trailer;
* ``scan_only``   — no page content stream contains a text-showing operator.

Both are advisory *manual-review* signals. They never produce a rejection, and
extraction makes the authoritative call with a real parser (see ``extract_pdf``).

The extension is reported alongside the detected type, and the disagreement
between the two is itself a fact worth recording: a ``.txt`` file containing PDF
bytes is either a mistake or a deliberate disguise, and the reviewer should see
which one the bytes say.
"""

from __future__ import annotations

import codecs
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import Code
from ..models import MediaType

__all__ = [
    "SniffResult",
    "EXTENSION_FAMILIES",
    "sniff_bytes",
    "sniff_path",
    "pdf_looks_encrypted",
    "pdf_looks_scan_only",
]


#: How far into the file the PDF header may legally appear (PDF 1.7, 7.5.2).
_PDF_HEADER_WINDOW = 1024

#: Windows read for header and trailer inspection. A PDF's encryption dictionary
#: lives in the trailer, so the tail must be read as well as the head.
_HEAD_BYTES = 64 * 1024
_TAIL_BYTES = 64 * 1024

#: A ZIP candidate needs its central directory, which is at the end. Only files
#: at or below this size are read whole; anything larger is reported as an
#: unreadable archive rather than silently truncated into a wrong verdict.
_MAX_ZIP_READ_BYTES = 64 * 1024 * 1024

#: Bounds on content-stream decompression. A pathological PDF must not turn a
#: type sniff into a memory amplifier.
_MAX_STREAM_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_STREAM_BYTES = 8 * 1024 * 1024

PDF_MAGIC = b"%PDF-"
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
RTF_MAGIC = b"{\\rtf"

_HTML_HINT = re.compile(rb"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]", re.IGNORECASE)
_ENCRYPT_NAME = re.compile(rb"/Encrypt[\s/<\[]")
_TEXT_OPERATOR = re.compile(rb"(?:\bBT\b|\bTj\b|\bTJ\b)")
_STREAM_BODY = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
#: Object streams use /Filter FlateDecode; we try the common ones only.
_FLATE = (b"/FlateDecode", b"/Fl")

#: Extension -> sniff family. ``None`` (an extension we do not recognise) can
#: never "disagree": there is no expectation to disagree with. ``unsupported``
#: covers the formats the PRD lists as explicitly out of scope.
EXTENSION_FAMILIES: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "docm": "unsupported",
    "txt": "txt",
    "text": "txt",
    "log": "txt",
    "md": "txt",
    "csv": "txt",
    "tsv": "txt",
    "doc": "unsupported",
    "dot": "unsupported",
    "rtf": "unsupported",
    "html": "unsupported",
    "htm": "unsupported",
    "mht": "unsupported",
    "msg": "unsupported",
    "eml": "unsupported",
    "zip": "unsupported",
    "7z": "unsupported",
    "rar": "unsupported",
    "gz": "unsupported",
    "tar": "unsupported",
    "odt": "unsupported",
    "ods": "unsupported",
    "odp": "unsupported",
    "xlsx": "unsupported",
    "xls": "unsupported",
    "pptx": "unsupported",
    "ppt": "unsupported",
    "pages": "unsupported",
    "bin": "unsupported",
    "dat": "unsupported",
    "exe": "unsupported",
    "dll": "unsupported",
    "js": "unsupported",
    "vbs": "unsupported",
    "py": "unsupported",
}


@dataclass
class SniffResult:
    """The type decision for one candidate file, plus the evidence behind it.

    ``detail`` is a stable token (``pdf``, ``docx``, ``text``, ``ole_cfb``,
    ``zip_archive``, ``rtf``, ``html``, ``binary``, ``empty``) so callers can
    branch on it without parsing prose. ``reason_code`` is set only when the
    file cannot be extracted at all; the encrypted/scan-only flags are manual
    review states, not failures, and leave ``media_type`` as ``pdf``.
    """

    media_type: MediaType
    detail: str
    extension: str = ""
    extension_disagrees: bool = False
    encoding: str | None = None
    encrypted: bool = False
    scan_only: bool = False
    reason_code: str | None = None
    size_bytes: int | None = None
    container_entries: tuple[str, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        """True when a deterministic adapter exists for this file."""
        return self.media_type in (MediaType.PDF, MediaType.DOCX, MediaType.TXT)

    @property
    def requires_manual_review(self) -> bool:
        """True for a type we recognise but cannot process without a human.

        Encrypted and scan-only PDFs land here: they stay visible, they are never
        automatically rejected (PRD section 6.1, AT-10).
        """
        return self.supported and (self.encrypted or self.scan_only)

    def to_dict(self) -> dict[str, object]:
        return {
            "media_type": str(self.media_type.value),
            "detail": self.detail,
            "extension": self.extension,
            "extension_disagrees": self.extension_disagrees,
            "encoding": self.encoding,
            "encrypted": self.encrypted,
            "scan_only": self.scan_only,
            "reason_code": self.reason_code,
            "size_bytes": self.size_bytes,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extension_of(filename: str | None) -> str:
    if not filename:
        return ""
    name = Path(filename).name
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].strip().lower()


def _family_of(result_media: MediaType, detail: str) -> str:
    del detail  # the media type already fixes the family
    if result_media is MediaType.PDF:
        return "pdf"
    if result_media is MediaType.DOCX:
        return "docx"
    if result_media is MediaType.TXT:
        return "txt"
    return "unsupported"


def _with_extension_verdict(
    result: SniffResult, filename: str | None, size_bytes: int | None
) -> SniffResult:
    extension = _extension_of(filename)
    implied = EXTENSION_FAMILIES.get(extension)
    detected = _family_of(result.media_type, result.detail)
    result.extension = extension
    result.extension_disagrees = implied is not None and implied != detected
    result.size_bytes = size_bytes
    return result


def _looks_like_utf16(data: bytes) -> str | None:
    """Detect BOM-less UTF-16 from the alternating-NUL pattern of the head.

    A UTF-16 document has NUL bytes by construction, so the "no NUL bytes" rule
    for 8-bit text cannot apply to it. The pattern (not the NUL count alone) is
    what distinguishes UTF-16 from binary.
    """
    window = data[:4096]
    if len(window) < 4:
        return None
    even_nul = sum(1 for i in range(0, len(window), 2) if window[i] == 0)
    odd_nul = sum(1 for i in range(1, len(window), 2) if window[i] == 0)
    pairs = len(window) // 2
    if pairs == 0:
        return None
    if odd_nul / pairs > 0.4 and even_nul / pairs < 0.05:
        return "utf-16-le"
    if even_nul / pairs > 0.4 and odd_nul / pairs < 0.05:
        return "utf-16-be"
    return None


def _decode_as_text(data: bytes) -> tuple[bool, str | None]:
    """Return ``(is_text, encoding)`` for a text candidate.

    UTF-8 (with or without BOM) and UTF-16 are recognised first because they are
    unambiguous. CP1252 is the Windows 8-bit default and accepts almost any byte
    sequence, so it is tried last; ``latin-1`` is deliberately *not* tried here
    since it never fails and would classify every binary blob as text. The
    extraction adapter uses ``latin-1`` only as a final rescue for a file sniff
    already called TXT.
    """
    if data.startswith(codecs.BOM_UTF8):
        try:
            data.decode("utf-8-sig")
            return True, "utf-8-sig"
        except UnicodeDecodeError:
            return False, None
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        try:
            data.decode("utf-16")
            return True, "utf-16"
        except UnicodeDecodeError:
            return False, None
    if b"\x00" in data:
        pattern = _looks_like_utf16(data)
        if pattern is None:
            return False, None
        try:
            data.decode(pattern)
            return True, pattern
        except UnicodeDecodeError:
            return False, None
    try:
        data.decode("utf-8")
        return True, "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return True, "cp1252"
    except UnicodeDecodeError:
        return False, None


def _inspect_zip(data: bytes) -> tuple[bool, tuple[str, ...], bool]:
    """Return ``(is_docx, names, readable)`` for a ZIP candidate."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = tuple(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False, (), False
    is_docx = "[Content_Types].xml" in names and "word/document.xml" in names
    return is_docx, names, True


def _stream_bodies(data: bytes) -> list[bytes]:
    bodies: list[bytes] = []
    total = 0
    for match in _STREAM_BODY.finditer(data):
        body = match.group(1)
        if len(body) > _MAX_STREAM_BYTES:
            body = body[:_MAX_STREAM_BYTES]
        bodies.append(body)
        total += len(body)
        if total >= _MAX_TOTAL_STREAM_BYTES:
            break
    return bodies


def pdf_looks_encrypted(data: bytes) -> bool:
    """True when the bytes declare an ``/Encrypt`` dictionary.

    A heuristic that only routes the document to manual review, so a false
    positive costs a reviewer a click and never costs an applicant a rejection.
    """
    return _ENCRYPT_NAME.search(data) is not None


def pdf_looks_scan_only(data: bytes) -> bool:
    """True when no content stream shows a text operator.

    Every stream is inspected: compressed streams are inflated with zlib, and a
    stream that cannot be inflated is searched as-is (an uncompressed content
    stream is readable directly). The verdict is only returned when at least one
    stream was actually inspected; a PDF we could read nothing from is left
    ``unknown`` rather than mislabelled scan-only.
    """
    if _TEXT_OPERATOR.search(data):
        return False
    inspected = False
    for body in _stream_bodies(data):
        inspected = True
        if _TEXT_OPERATOR.search(body):
            return False
        if any(token in body for token in _FLATE):
            try:
                import zlib

                inflated = zlib.decompress(body)
            except Exception:  # zlib.error and friends; all mean "not inflatable"
                continue
            if _TEXT_OPERATOR.search(inflated):
                return False
    return inspected


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def sniff_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    size_bytes: int | None = None,
) -> SniffResult:
    """Sniff a byte buffer.

    Callers that only hold a header window cannot get a trustworthy
    encrypted/scan-only verdict for a PDF; pass the whole document, or use
    :func:`sniff_path`.
    """
    if not data:
        result = SniffResult(media_type=MediaType.TXT, detail="empty", encoding=None)
        return _with_extension_verdict(result, filename, size_bytes)

    if data[:_PDF_HEADER_WINDOW].find(PDF_MAGIC) >= 0:
        encrypted = pdf_looks_encrypted(data)
        # An encrypted stream is opaque, so a "no text operator" reading would be
        # meaningless there: encrypted is the state, not scan-only.
        result = SniffResult(
            media_type=MediaType.PDF,
            detail="pdf",
            encrypted=encrypted,
            scan_only=(not encrypted) and pdf_looks_scan_only(data),
        )
        return _with_extension_verdict(result, filename, size_bytes)

    if data.startswith(OLE_MAGIC):
        result = SniffResult(
            media_type=MediaType.UNSUPPORTED,
            detail="ole_cfb",
            reason_code=Code.UNSUPPORTED_FORMAT,
        )
        return _with_extension_verdict(result, filename, size_bytes)

    if data.startswith(ZIP_MAGICS):
        is_docx, names, readable = _inspect_zip(data)
        if is_docx:
            result = SniffResult(
                media_type=MediaType.DOCX,
                detail="docx",
                container_entries=names[:64],
            )
            return _with_extension_verdict(result, filename, size_bytes)
        result = SniffResult(
            media_type=MediaType.UNSUPPORTED,
            detail="zip_archive" if readable else "zip_unreadable",
            reason_code=Code.UNSUPPORTED_FORMAT,
            container_entries=names[:64],
        )
        return _with_extension_verdict(result, filename, size_bytes)

    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:6].lower().startswith(RTF_MAGIC.lower()):
        result = SniffResult(
            media_type=MediaType.UNSUPPORTED,
            detail="rtf",
            reason_code=Code.UNSUPPORTED_FORMAT,
        )
        return _with_extension_verdict(result, filename, size_bytes)

    if _HTML_HINT.search(data[:4096]):
        result = SniffResult(
            media_type=MediaType.UNSUPPORTED,
            detail="html",
            reason_code=Code.UNSUPPORTED_FORMAT,
        )
        return _with_extension_verdict(result, filename, size_bytes)

    is_text, encoding = _decode_as_text(data)
    if is_text:
        result = SniffResult(media_type=MediaType.TXT, detail="text", encoding=encoding)
        return _with_extension_verdict(result, filename, size_bytes)

    result = SniffResult(
        media_type=MediaType.UNSUPPORTED,
        detail="binary",
        reason_code=Code.UNSUPPORTED_FORMAT,
    )
    return _with_extension_verdict(result, filename, size_bytes)


def sniff_path(
    path: str | Path,
    *,
    filename: str | None = None,
    max_zip_read_bytes: int = _MAX_ZIP_READ_BYTES,
) -> SniffResult:
    """Sniff a file on disk.

    Reads a header window plus a trailer window, and the whole file only when the
    header says it is a ZIP (a DOCX can only be confirmed from the archive
    directory, which lives at the end). The reported ``extension`` follows
    ``filename`` when supplied, and otherwise the path's own name.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        result = SniffResult(
            media_type=MediaType.UNKNOWN,
            detail="missing",
            reason_code=Code.FILE_MISSING,
        )
        return _with_extension_verdict(result, filename or p.name, None)

    with open(p, "rb") as handle:
        head = handle.read(_HEAD_BYTES)
        tail = b""
        if size > len(head):
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read(_TAIL_BYTES)

    name = filename if filename is not None else p.name

    if head.startswith(ZIP_MAGICS):
        if size <= max_zip_read_bytes:
            with open(p, "rb") as handle:
                whole = handle.read()
            return sniff_bytes(whole, filename=name, size_bytes=size)
        result = SniffResult(
            media_type=MediaType.UNSUPPORTED,
            detail="zip_unreadable",
            reason_code=Code.UNSUPPORTED_FORMAT,
        )
        return _with_extension_verdict(result, name, size)

    if head[:_PDF_HEADER_WINDOW].find(PDF_MAGIC) >= 0:
        merged = head + (b"\n" + tail if tail else b"")
        encrypted = pdf_looks_encrypted(merged)
        result = SniffResult(
            media_type=MediaType.PDF,
            detail="pdf",
            encrypted=encrypted,
            scan_only=(not encrypted) and pdf_looks_scan_only(merged),
        )
        return _with_extension_verdict(result, name, size)

    return sniff_bytes(head, filename=name, size_bytes=size)
