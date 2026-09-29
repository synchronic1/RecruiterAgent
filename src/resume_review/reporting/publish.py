"""Atomic publication of the generated report.

Authority: PRD section 8.5 ("Publish via a temporary file and a tested replacement
operation; when replacement is temporarily blocked, keep the old valid report and
expose a stale-snapshot warning") and AT-20 ("Snapshot interruption").

Why not ``storage.no_clobber``: managed applicant moves must never clobber and must
go through the journaled helper. The report is a *derived* artifact keyed by a
single fixed name, so an atomic replace is the correct primitive here; unlike a
submission there is nothing to preserve at the destination whose loss would be data
loss. The temporary file is created in the same directory so the rename is atomic on
one volume, and it is never named after applicant content.

Failure policy: a blocked or failed replacement raises nothing the caller must
catch to stay alive. It returns a result that says the previous report was kept and
that the published state is now stale, so the caller can surface the warning and
retry later without ever presenting an unwritten report as current.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..errors import Code
from ..models import Warning

__all__ = ["PublishResult", "publish_report"]


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one publication attempt.

    ``published`` is false exactly when the target was not replaced. ``stale`` is
    true when an older, still-valid report remains in place, which is the state the
    connected page must disclose until a retry succeeds.
    """

    path: str
    published: bool
    stale: bool
    state_revision: int | None
    generated_at: str | None
    warnings: list[Warning] = field(default_factory=list)
    byte_size: int = 0


def _temp_path(target: Path) -> Path:
    """A sibling temporary path derived from the target name plus a random suffix.

    Applicant content never appears in the temporary filename: it is built only from
    the fixed report name and a random token.
    """
    return target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"


def _reason_code(exc: OSError) -> str:
    """A non-secret reason label for a failed replacement.

    The Windows message text can contain an absolute path, so only a stable label
    is carried into the warning detail.
    """
    if isinstance(exc, PermissionError):
        return "locked_or_permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "directory_missing"
    if isinstance(exc, IsADirectoryError):
        return "target_is_directory"
    return exc.__class__.__name__.lower()


def publish_report(
    report_path: str | os.PathLike[str],
    content: str,
    *,
    state_revision: int | None = None,
    generated_at: str | None = None,
    previous_warnings: Sequence[Warning] | None = None,
) -> PublishResult:
    """Write ``content`` to ``report_path`` atomically.

    The sequence is: create a sibling temporary file, write and ``fsync`` it, then
    ``os.replace`` it onto the target. If any step fails the temporary file is
    removed, the previous report is left untouched, and the returned result reports
    the failure and whether the surviving report is stale.
    """
    target = Path(report_path)
    encoded = content.encode("utf-8")
    warnings: list[Warning] = list(previous_warnings or [])
    previous_exists = target.exists()
    tmp_path: Path | None = None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _temp_path(target)
        with open(tmp_path, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            # fsync the file, not just the buffer: a crash between write and rename
            # must not leave a truncated report that the rename then publishes.
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        return PublishResult(
            path=str(target),
            published=True,
            stale=False,
            state_revision=state_revision,
            generated_at=generated_at,
            warnings=warnings,
            byte_size=len(encoded),
        )
    except OSError as exc:
        reason = _reason_code(exc)
        warnings.append(
            Warning(
                code=Code.SNAPSHOT_PUBLISH_FAILED,
                message="The report could not be replaced; the previous report was kept.",
                detail={"reason": reason},
            )
        )
        if previous_exists:
            warnings.append(
                Warning(
                    code=Code.SNAPSHOT_STALE,
                    message=(
                        "The displayed report is a stale snapshot and does not include "
                        "the latest committed state."
                    ),
                    detail={"reason": reason},
                )
            )
        try:
            byte_size = target.stat().st_size if previous_exists else 0
        except OSError:  # pragma: no cover - target vanished mid-report
            byte_size = 0
        return PublishResult(
            path=str(target),
            published=False,
            stale=previous_exists,
            state_revision=state_revision,
            generated_at=generated_at,
            warnings=warnings,
            byte_size=byte_size,
        )
    finally:
        # A successful rename consumes the temporary file; every other path must
        # clean up so a failed publish never litters the workspace.
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    os.remove(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
