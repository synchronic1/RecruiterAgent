"""Wait for stable bytes before parsing, then hash exactly what is parsed.

Authority: PRD section 6.2 and AT-08.

    "Wait for a file to become stable before parsing: compare size/metadata across
     a configurable interval, copy to a controlled temporary read snapshot, hash the
     bytes being parsed, and confirm the source did not change. A file still being
     copied stays pending rather than producing a misleading partial summary."

Why a snapshot rather than parsing in place
-------------------------------------------
A file can be replaced between "is it stable?" and "parse it", and it can change
while it is being parsed. Either case would attach a summary to bytes that no
longer exist, which is worse than a pending row: it is a wrong one. So the bytes
that get hashed are the bytes in a private snapshot the parser reads, and the
source is re-observed after the copy to prove it did not move underneath us.

A refused stabilization is not an error state. The caller keeps the document at
``discovered`` / PENDING and tries again later; nothing is committed and no
partial profile is produced.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_module
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..models import DEFAULT_LIMITS, ResourceLimits
from ..storage import FileIdentity, file_identity, is_reparse_point

__all__ = [
    "SourceObservation",
    "StabilityVerdict",
    "StableSnapshot",
    "Stabilizer",
    "REASON_STABLE",
    "REASON_STILL_GROWING",
    "REASON_SOURCE_CHANGED",
    "REASON_SOURCE_MISSING",
    "REASON_NOT_REGULAR_FILE",
    "REASON_REPARSE_POINT",
    "REASON_UNREADABLE",
]

REASON_STABLE = "stable"
REASON_STILL_GROWING = "still_growing"
REASON_SOURCE_CHANGED = "source_changed_during_copy"
REASON_SOURCE_MISSING = "source_missing"
REASON_NOT_REGULAR_FILE = "not_regular_file"
REASON_REPARSE_POINT = "reparse_point"
REASON_UNREADABLE = "unreadable"

_COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class SourceObservation:
    """One (size, mtime_ns) reading of the source file.

    ``mtime_ns`` is used only to detect that the file is still changing. It is
    never a submission date (PRD section 6.2); see ``discover``.
    """

    exists: bool
    size: int | None
    mtime_ns: int | None
    is_regular_file: bool = True
    is_reparse_point: bool = False

    def same_as(self, other: "SourceObservation") -> bool:
        return (
            self.exists == other.exists
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
        )


@dataclass(frozen=True)
class StabilityVerdict:
    """The stabilization decision for one file."""

    stable: bool
    reason: str
    observations: int = 0
    identity: FileIdentity | None = None

    @property
    def pending(self) -> bool:
        """True when the caller should leave the document pending and retry."""
        return not self.stable


@dataclass
class StableSnapshot:
    """A private copy of stable bytes, plus the hash of exactly those bytes.

    ``cleanup`` removes the snapshot directory. The caller owns the lifetime: the
    extraction worker deletes it as soon as spans are persisted, including on the
    failure paths.
    """

    verdict: StabilityVerdict
    sha256: str | None = None
    size_bytes: int | None = None
    snapshot_path: Path | None = None
    snapshot_dir: Path | None = None
    source_path: Path | None = None
    exceeds_max_bytes: bool = False
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        return self.verdict.stable

    def cleanup(self) -> None:
        if self.snapshot_dir is not None:
            shutil.rmtree(self.snapshot_dir, ignore_errors=True)
            self.snapshot_dir = None


class Stabilizer:
    """Observe a file until it stops changing, then copy and hash it.

    ``sleep`` and ``progress_callback`` are injectable so tests can drive the
    "file grows while we watch" and "file changed during the copy" paths
    deterministically instead of racing a background writer. Neither hook may
    modify the source in production code.
    """

    def __init__(
        self,
        *,
        limits: ResourceLimits | None = None,
        interval_seconds: float | None = None,
        required_observations: int | None = None,
        snapshot_root: str | os.PathLike[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        self.limits = limits or DEFAULT_LIMITS
        self.interval_seconds = (
            self.limits.stabilize_interval_seconds if interval_seconds is None else interval_seconds
        )
        self.required_observations = (
            self.limits.stabilize_required_observations
            if required_observations is None
            else required_observations
        )
        self.snapshot_root = Path(snapshot_root) if snapshot_root is not None else None
        self._sleep = sleep
        self._progress_callback = progress_callback

    # -- observation -------------------------------------------------------
    def observe(self, path: str | os.PathLike[str]) -> SourceObservation:
        p = Path(path)
        if is_reparse_point(p):
            # A link could be repointed between observation and copy, which would
            # make every containment guarantee downstream meaningless.
            return SourceObservation(exists=True, size=None, mtime_ns=None, is_reparse_point=True)
        try:
            st = os.stat(p, follow_symlinks=False)
        except FileNotFoundError:
            return SourceObservation(exists=False, size=None, mtime_ns=None)
        except OSError:
            return SourceObservation(exists=True, size=None, mtime_ns=None, is_regular_file=False)
        regular = stat_module.S_ISREG(st.st_mode)
        return SourceObservation(
            exists=True,
            size=int(st.st_size),
            mtime_ns=int(getattr(st, "st_mtime_ns", 0)),
            is_regular_file=regular,
        )

    # -- waiting -----------------------------------------------------------
    def wait_for_stable(
        self,
        path: str | os.PathLike[str],
        *,
        max_wait_seconds: float = 30.0,
    ) -> StabilityVerdict:
        """Wait until ``required_observations`` consecutive readings agree.

        Returns a non-stable verdict rather than looping forever when the file
        keeps changing, because a submission being actively written is a normal
        event, not an error: it stays pending and the next scan retries.
        """
        required = max(1, int(self.required_observations))
        deadline = time.monotonic() + max(0.0, max_wait_seconds)
        observations = 0
        previous: SourceObservation | None = None
        stable_count = 0

        while True:
            current = self.observe(path)
            observations += 1

            if not current.exists:
                return StabilityVerdict(
                    stable=False, reason=REASON_SOURCE_MISSING, observations=observations
                )
            if current.is_reparse_point:
                return StabilityVerdict(
                    stable=False, reason=REASON_REPARSE_POINT, observations=observations
                )
            if not current.is_regular_file or current.size is None:
                return StabilityVerdict(
                    stable=False, reason=REASON_NOT_REGULAR_FILE, observations=observations
                )

            if previous is not None and previous.same_as(current):
                stable_count += 1
            else:
                stable_count = 1

            if stable_count >= required:
                return StabilityVerdict(
                    stable=True,
                    reason=REASON_STABLE,
                    observations=observations,
                    identity=file_identity(path),
                )

            previous = current
            if time.monotonic() >= deadline:
                return StabilityVerdict(
                    stable=False, reason=REASON_STILL_GROWING, observations=observations
                )
            if self.interval_seconds > 0:
                self._sleep(self.interval_seconds)

    # -- snapshot ----------------------------------------------------------
    def _make_snapshot_dir(self) -> Path:
        if self.snapshot_root is not None:
            self.snapshot_root.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="snap-", dir=str(self.snapshot_root)))
        return Path(tempfile.mkdtemp(prefix="resume-review-snap-"))

    def snapshot(
        self,
        path: str | os.PathLike[str],
        *,
        expected: SourceObservation | None = None,
    ) -> StableSnapshot:
        """Copy the bytes, hash them, and prove the source did not change.

        The copy is written to a private directory, hashed while it is written,
        and the source is re-observed afterwards. A source that changed during the
        copy invalidates the snapshot entirely: the file stays pending and the
        partial copy is deleted rather than summarised.
        """
        source = Path(path)
        before = expected if expected is not None else self.observe(source)

        if not before.exists:
            return StableSnapshot(
                verdict=StabilityVerdict(False, REASON_SOURCE_MISSING), source_path=source
            )
        if before.is_reparse_point:
            return StableSnapshot(
                verdict=StabilityVerdict(False, REASON_REPARSE_POINT), source_path=source
            )
        if not before.is_regular_file or before.size is None:
            return StableSnapshot(
                verdict=StabilityVerdict(False, REASON_NOT_REGULAR_FILE), source_path=source
            )

        snapshot_dir = self._make_snapshot_dir()
        snapshot_path = snapshot_dir / "source.bin"
        copied = 0
        try:
            hasher = hashlib.sha256()
            with open(source, "rb") as reader, open(snapshot_path, "wb") as writer:
                while True:
                    block = reader.read(_COPY_CHUNK)
                    if not block:
                        break
                    writer.write(block)
                    hasher.update(block)
                    copied += len(block)
                    if self._progress_callback is not None:
                        self._progress_callback(copied)
        except OSError:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return StableSnapshot(
                verdict=StabilityVerdict(False, REASON_UNREADABLE),
                source_path=source,
                detail=REASON_UNREADABLE,
            )

        after = self.observe(source)
        if not after.same_as(before) or copied != before.size:
            # The source moved under us. Discard rather than summarise bytes that
            # no longer correspond to any file the reviewer can open.
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return StableSnapshot(
                verdict=StabilityVerdict(
                    False, REASON_SOURCE_CHANGED, identity=file_identity(source)
                ),
                source_path=source,
                size_bytes=copied,
                detail=REASON_SOURCE_CHANGED,
            )

        exceeds = before.size > self.limits.max_source_bytes
        warnings: list[str] = []
        if exceeds:
            warnings.append("FILE_TOO_LARGE")

        return StableSnapshot(
            verdict=StabilityVerdict(
                stable=True,
                reason=REASON_STABLE,
                identity=file_identity(source),
            ),
            sha256=hasher.hexdigest(),
            size_bytes=copied,
            snapshot_path=snapshot_path,
            snapshot_dir=snapshot_dir,
            source_path=source,
            exceeds_max_bytes=exceeds,
            warnings=warnings,
        )

    def stabilize(
        self,
        path: str | os.PathLike[str],
        *,
        max_wait_seconds: float = 30.0,
    ) -> StableSnapshot:
        """Wait for stability, then snapshot. The single call the pipeline uses."""
        verdict = self.wait_for_stable(path, max_wait_seconds=max_wait_seconds)
        if not verdict.stable:
            observation = self.observe(path)
            return StableSnapshot(
                verdict=verdict,
                source_path=Path(path),
                size_bytes=observation.size,
                detail=verdict.reason,
            )
        return self.snapshot(path, expected=self.observe(path))
