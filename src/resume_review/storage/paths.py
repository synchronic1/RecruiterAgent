"""Root binding, path containment, and reparse-point defences.

Authority: PRD sections 4 and 13.2.

Every managed move must stay inside the registered root and on the same supported
volume. This module is the single place that decides whether a candidate path is
allowed, so that "resolve, then validate containment, then touch the file" is
written once and audited once.

Threat model handled here:

* Traversal via ``..``, absolute paths, drive-relative paths, and UNC paths.
* Escape through a symlink or NTFS junction anywhere in the parent chain.
* Escape through a symlink as the final component (a "move" that would follow the
  link and write outside the root).
* Windows path-spelling tricks: ``\\?\\`` device prefixes, alternate data streams
  (``file.txt:stream``), trailing dots and spaces that the Win32 layer strips,
  and reserved device names (``CON``, ``NUL``, ``COM1`` ...).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..errors import InvalidInput, ResumeReviewError

__all__ = [
    "PathEscape",
    "SymlinkEscape",
    "normalize_rel_path",
    "join_rel",
    "to_absolute",
    "to_relative",
    "assert_within_root",
    "assert_no_reparse_traversal",
    "is_reparse_point",
    "extended_path",
    "safe_final_component",
    "containment_report",
]

_WINDOWS = os.name == "nt"

#: Win32 reserved device names. A file literally named ``NUL.txt`` is not writable.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNC_RE = re.compile(r"^(\\\\|//)")


class PathEscape(ResumeReviewError):
    code = "PATH_ESCAPE"
    http_status = 422


class SymlinkEscape(ResumeReviewError):
    code = "SYMLINK_REJECTED"
    http_status = 422


# ---------------------------------------------------------------------------
# Relative path handling
# ---------------------------------------------------------------------------
def normalize_rel_path(rel: str) -> str:
    """Normalize a root-relative path to canonical forward-slash form.

    Raises :class:`PathEscape` for anything that is not strictly relative and
    strictly descended from the root. This function is deliberately strict: it is
    better to reject a legitimate but unusual filename than to accept an escape.
    """
    if not isinstance(rel, str) or rel == "":
        raise PathEscape("An empty path is not a valid workspace path.")

    # Reject embedded NUL and C0/C1 control characters outright. They have no
    # legitimate use in a filename and several of them are used in truncation and
    # display-spoofing attacks.
    if "\x00" in rel or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in rel):
        raise PathEscape("The path contains a control character.", detail={"reason": "control_char"})

    candidate = rel.replace("\\", "/")

    if _DRIVE_RE.match(candidate):
        raise PathEscape("An absolute or drive-relative path is not allowed.", detail={"reason": "drive"})
    if candidate.startswith("/"):
        raise PathEscape("An absolute path is not allowed.", detail={"reason": "absolute"})
    if candidate.startswith("//") or _UNC_RE.match(candidate):
        raise PathEscape("A UNC path is not allowed.", detail={"reason": "unc"})
    if candidate.startswith("\\\\?\\") or candidate.lower().startswith("//?/"):
        raise PathEscape("A device-namespace path is not allowed.", detail={"reason": "device"})

    parts: list[str] = []
    for raw in candidate.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise PathEscape("Parent-directory traversal is not allowed.", detail={"reason": "traversal"})
        if raw != raw.rstrip(" ."):
            # Win32 silently strips trailing dots and spaces, so "evil." and "evil"
            # name the same file. Refuse rather than guess which was meant.
            raise PathEscape(
                "A path component ends with a dot or space, which Windows would silently rewrite.",
                detail={"reason": "trailing_dot_space"},
            )
        if ":" in raw:
            # Alternate data stream syntax on NTFS: "report.pdf:hidden".
            raise PathEscape("A path component contains a colon.", detail={"reason": "stream"})
        stem = raw.split(".")[0].upper()
        if stem in _WIN_RESERVED:
            raise PathEscape(
                "The path uses a reserved device name.",
                detail={"reason": "reserved_device_name"},
            )
        parts.append(raw)

    if not parts:
        raise PathEscape("The path resolves to the root itself.", detail={"reason": "root"})

    return "/".join(parts)


def join_rel(*parts: str) -> str:
    """Join already-normalized relative fragments into one relative path."""
    cleaned = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/".join(cleaned)


def safe_final_component(name: str) -> str:
    """Reduce an arbitrary observed filename to a safe single path component.

    Used for destinations inside ``Rejected/`` and ``Trash/`` where we must
    preserve the original filename but cannot trust it as a path.
    """
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\\", "_").replace("/", "_").replace(":", "_")
    if "\x00" in name or any(ord(c) < 32 for c in name):
        name = "".join(c if ord(c) >= 32 else "_" for c in name if c != "\x00")
    name = name.strip().rstrip(" .")
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = f"_{name}"
    if not name:
        name = "unnamed"
    return name[:200]


# ---------------------------------------------------------------------------
# Absolute <-> relative
# ---------------------------------------------------------------------------
def extended_path(path: str | os.PathLike[str]) -> str:
    """Return a Windows extended-length path (``\\\\?\\``) for API calls.

    Only used at the boundary where we hand a path to a Win32 API. Python's own
    ``os`` functions already handle long paths on modern Windows, but the ctypes
    calls to ``MoveFileExW`` and friends need the prefix to be safe.
    """
    p = os.path.abspath(os.fspath(path))
    if not _WINDOWS:
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def to_absolute(root: str | os.PathLike[str], rel: str) -> Path:
    """Join a validated relative path onto the root, without resolving links.

    The result is lexical only. Callers that are about to touch the filesystem
    must additionally call :func:`assert_no_reparse_traversal`.
    """
    norm = normalize_rel_path(rel)
    root_path = Path(root)
    return root_path.joinpath(*norm.split("/"))


def to_relative(root: str | os.PathLike[str], absolute: str | os.PathLike[str]) -> str:
    """Express an absolute path as a root-relative path, or raise ``PathEscape``."""
    root_resolved = Path(root).resolve(strict=False)
    try:
        rel = Path(absolute).resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise PathEscape(
            "The path is outside the registered workspace root.",
            detail={"reason": "outside_root"},
        ) from exc
    return rel.as_posix()


def assert_within_root(root: str | os.PathLike[str], absolute: str | os.PathLike[str]) -> None:
    """Raise unless ``absolute`` is lexically and physically inside ``root``."""
    root_path = Path(root)
    root_lexical = Path(os.path.abspath(os.fspath(root_path)))
    target_lexical = Path(os.path.abspath(os.fspath(absolute)))

    try:
        target_lexical.relative_to(root_lexical)
    except ValueError as exc:
        raise PathEscape(
            "The path is outside the registered workspace root.",
            detail={"reason": "outside_root"},
        ) from exc

    # A physical check as well: if the root itself is reachable through a link the
    # lexical test can pass while the real target is elsewhere.
    if os.path.exists(root_path):
        root_real = Path(os.path.realpath(root_path))
        here = target_lexical
        probe = here if os.path.exists(here) else here.parent
        while not os.path.exists(probe) and probe != probe.parent:
            probe = probe.parent
        if os.path.exists(probe):
            probe_real = Path(os.path.realpath(probe))
            try:
                probe_real.relative_to(root_real)
            except ValueError as exc:
                raise SymlinkEscape(
                    "The path escapes the workspace root through a link.",
                    detail={"reason": "realpath_escape"},
                ) from exc


# ---------------------------------------------------------------------------
# Reparse points / symlinks
# ---------------------------------------------------------------------------
def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """True if the path itself is a symlink, junction, or other reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    if _WINDOWS:
        attrs = getattr(st, "st_file_attributes", 0)
        # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        return bool(attrs & 0x400)
    return False


def assert_no_reparse_traversal(
    root: str | os.PathLike[str],
    rel: str,
    *,
    allow_final_absent: bool = True,
) -> Path:
    """Validate that no component of ``rel`` is a reparse point, then return the path.

    ``allow_final_absent`` permits the final component to not exist yet, which is
    the normal case for a move *destination*. Parent components must always exist
    and must never be links.
    """
    norm = normalize_rel_path(rel)
    root_path = Path(root)
    parts = norm.split("/")

    # The root itself must not be a reparse point, otherwise every containment
    # guarantee below is meaningless.
    if is_reparse_point(root_path):
        raise SymlinkEscape(
            "The workspace root is a link or reparse point, which is not supported.",
            detail={"reason": "root_is_link"},
        )

    current = root_path
    for index, part in enumerate(parts):
        current = current / part
        final = index == len(parts) - 1
        if is_reparse_point(current):
            raise SymlinkEscape(
                "A path component is a link or reparse point.",
                detail={"reason": "component_is_link", "depth": index},
            )
        if not final and not os.path.isdir(current):
            if os.path.exists(current):
                raise PathEscape(
                    "A path component exists but is not a directory.",
                    detail={"reason": "not_a_directory", "depth": index},
                )

    if not allow_final_absent and not os.path.exists(current):
        raise ResumeReviewError(
            "The expected path does not exist.",
            code="FILE_MISSING",
            http_status=404,
            detail={"reason": "absent"},
        )

    return current


def containment_report(root: str | os.PathLike[str], rel: str) -> dict[str, Any]:
    """Non-raising diagnostic used by ``repair --dry-run`` and tests."""
    report: dict[str, Any] = {"rel": rel, "ok": False}
    try:
        norm = normalize_rel_path(rel)
    except PathEscape as exc:
        report["reason"] = exc.code
        report["message"] = exc.message
        return report
    try:
        path = assert_no_reparse_traversal(root, norm)
        assert_within_root(root, path)
    except ResumeReviewError as exc:
        report["reason"] = exc.code
        report["message"] = exc.message
        return report
    report.update({"ok": True, "normalized": norm, "lexical": str(to_absolute(root, norm))})
    return report
