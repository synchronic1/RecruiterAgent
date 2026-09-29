"""Enumerate candidate submissions under a registered root.

Authority: PRD sections 4, 6.1 and 6.2, and AT-06 ("Complete census. Scan the
400-document fixture; every expected submission is visible, including unsupported
or failed items.").

Every entry is either a candidate file or an explicitly skipped path with a
reason, so the census can account for every input: ``files + skipped`` explains
the whole directory tree. A skipped entry is never silently dropped, because a
submission that vanishes without a reason is exactly the failure AT-06 exists to
catch.

Two rules encoded here:

* **mtime is not a submission date.** ``submitted_at`` is always ``None`` unless
  something else established it. Filesystem metadata says when a copy finished,
  not when a person applied (PRD section 6.2).
* **Never follow a link.** Symlinks and NTFS junctions are excluded outright,
  and their targets are never descended into, so a link cannot pull the census
  outside the root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..models import (
    DEFAULT_LIMITS,
    DISCOVERY_EXCLUDED_DIRS,
    DISCOVERY_EXCLUDED_PREFIXES,
    DISCOVERY_EXCLUDED_SUFFIXES,
    REPORT_FILENAME,
    ResourceLimits,
)
from ..security.untrusted import is_instruction_file
from ..storage import file_identity, is_reparse_point
from ..util import now_iso

__all__ = [
    "SKIP_EXCLUDED_DIR",
    "SKIP_REPARSE_POINT",
    "SKIP_RESERVED_REPORT",
    "SKIP_TEMPORARY_FILE",
    "SKIP_UNREADABLE",
    "SKIP_NOT_A_FILE",
    "DiscoveredFile",
    "SkippedEntry",
    "DiscoveryCensus",
    "iter_discovery",
    "discover",
]

SKIP_EXCLUDED_DIR = "excluded_directory"
SKIP_REPARSE_POINT = "reparse_point"
SKIP_RESERVED_REPORT = "reserved_report"
SKIP_TEMPORARY_FILE = "temporary_file"
SKIP_UNREADABLE = "unreadable"
SKIP_NOT_A_FILE = "not_a_file"


@dataclass(frozen=True)
class DiscoveredFile:
    """One candidate submission, identified by where it is *now*.

    The document ID is assigned later by the pipeline; discovery never derives
    identity from a name or a path (PRD section 4).
    """

    rel_path: str
    absolute_path: Path
    original_filename: str
    size_bytes: int
    fs_identity: str | None
    mtime_ns: int | None
    extension: str
    is_instruction_file: bool = False
    exceeds_max_bytes: bool = False
    #: Always None here. A file's mtime is not a submission date; only a source
    #: that actually records one (an intake manifest, an explicit reviewer entry)
    #: may set it. The field exists so the type states the rule, not to fill it.
    submitted_at: str | None = None
    discovered_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "rel_path": self.rel_path,
            "original_filename": self.original_filename,
            "size_bytes": self.size_bytes,
            "fs_identity": self.fs_identity,
            "extension": self.extension,
            "is_instruction_file": self.is_instruction_file,
            "exceeds_max_bytes": self.exceeds_max_bytes,
            "submitted_at": self.submitted_at,
            "discovered_at": self.discovered_at,
        }


@dataclass(frozen=True)
class SkippedEntry:
    """A path the census saw and deliberately did not consider a submission."""

    rel_path: str
    reason: str
    detail: str = ""
    is_dir: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "rel_path": self.rel_path,
            "reason": self.reason,
            "detail": self.detail,
            "is_dir": self.is_dir,
        }


@dataclass
class DiscoveryCensus:
    """The materialised result of one pass over the root."""

    root: Path
    files: list[DiscoveredFile] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)
    scanned_at: str = ""

    @property
    def total_entries(self) -> int:
        return len(self.files) + len(self.skipped)

    def skipped_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.skipped:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "root_rel": ".",
            "file_count": len(self.files),
            "skipped_count": len(self.skipped),
            "skipped_reasons": self.skipped_reasons(),
            "scanned_at": self.scanned_at,
        }


def _is_temporary_name(name: str) -> bool:
    lowered = name.lower()
    if any(name.startswith(prefix) for prefix in DISCOVERY_EXCLUDED_PREFIXES):
        return True
    if any(lowered.startswith(prefix) for prefix in DISCOVERY_EXCLUDED_PREFIXES):
        return True
    return any(lowered.endswith(suffix) for suffix in DISCOVERY_EXCLUDED_SUFFIXES)


def _extension_of(name: str) -> str:
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].strip().lower()


def _sorted_entries(entries: list[os.DirEntry[str]]) -> list[os.DirEntry[str]]:
    """Deterministic traversal order, independent of the filesystem's own order."""
    return sorted(entries, key=lambda e: (e.name.casefold(), e.name))


def iter_discovery(
    root: str | os.PathLike[str],
    *,
    limits: ResourceLimits | None = None,
) -> Iterator[DiscoveredFile | SkippedEntry]:
    """Yield every candidate file and every skip reason under ``root``.

    Incremental on purpose: the pipeline can register a submission while the rest
    of the tree is still being walked, and a caller that only wants the first N
    entries does not pay for the rest.
    """
    effective = limits or DEFAULT_LIMITS
    root_path = Path(root)
    discovered_at = now_iso()

    if not root_path.is_dir():
        yield SkippedEntry(rel_path=".", reason=SKIP_UNREADABLE, detail="root_not_a_directory", is_dir=True)
        return

    stack: list[tuple[Path, str]] = [(root_path, "")]

    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            rel = prefix or "."
            yield SkippedEntry(rel_path=rel, reason=SKIP_UNREADABLE, detail="directory_unreadable", is_dir=True)
            continue

        subdirectories: list[tuple[Path, str]] = []

        for entry in _sorted_entries(entries):
            rel_path = f"{prefix}/{entry.name}" if prefix else entry.name

            if entry.is_symlink() or is_reparse_point(entry.path):
                # Never follow: a link can point anywhere, including outside the
                # root, and its target must not enter the census.
                yield SkippedEntry(
                    rel_path=rel_path,
                    reason=SKIP_REPARSE_POINT,
                    detail="link_not_followed",
                    is_dir=entry.is_dir(follow_symlinks=False),
                )
                continue

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                yield SkippedEntry(rel_path=rel_path, reason=SKIP_UNREADABLE, detail="stat_failed")
                continue

            if is_dir:
                if entry.name in DISCOVERY_EXCLUDED_DIRS:
                    yield SkippedEntry(
                        rel_path=rel_path,
                        reason=SKIP_EXCLUDED_DIR,
                        detail=entry.name,
                        is_dir=True,
                    )
                    continue
                subdirectories.append((Path(entry.path), rel_path))
                continue

            if entry.name == REPORT_FILENAME:
                # The generated report is a projection of the database, never a
                # submission, wherever it appears.
                yield SkippedEntry(rel_path=rel_path, reason=SKIP_RESERVED_REPORT)
                continue

            if _is_temporary_name(entry.name):
                yield SkippedEntry(rel_path=rel_path, reason=SKIP_TEMPORARY_FILE, detail=entry.name)
                continue

            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                yield SkippedEntry(rel_path=rel_path, reason=SKIP_UNREADABLE, detail="stat_failed")
                continue

            import stat as stat_module

            if not stat_module.S_ISREG(st.st_mode):
                yield SkippedEntry(rel_path=rel_path, reason=SKIP_NOT_A_FILE)
                continue

            identity = file_identity(entry.path)
            size = int(st.st_size)
            yield DiscoveredFile(
                rel_path=rel_path,
                absolute_path=Path(entry.path),
                original_filename=entry.name,
                size_bytes=size,
                fs_identity=identity.digest_hint() if identity is not None else None,
                mtime_ns=int(getattr(st, "st_mtime_ns", 0)),
                extension=_extension_of(entry.name),
                is_instruction_file=is_instruction_file(entry.name),
                exceeds_max_bytes=size > effective.max_source_bytes,
                discovered_at=discovered_at,
            )

        # Depth-first, but push in reverse so the deterministic name order is
        # preserved by the LIFO stack.
        for item in reversed(subdirectories):
            stack.append(item)


def discover(
    root: str | os.PathLike[str],
    *,
    limits: ResourceLimits | None = None,
) -> DiscoveryCensus:
    """Materialise :func:`iter_discovery` into a census."""
    root_path = Path(root)
    census = DiscoveryCensus(root=root_path, scanned_at=now_iso())
    for entry in iter_discovery(root_path, limits=limits):
        if isinstance(entry, DiscoveredFile):
            census.files.append(entry)
        else:
            census.skipped.append(entry)
    return census
