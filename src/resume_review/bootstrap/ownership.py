"""OS-backed single-writer ownership of one instance (PRD 15.3, AT-37).

    "Only one helper owner is allowed per instance. Validate ownership with an
     OS-backed lock, instance identity, and host registry. Do not steal ownership
     based only on a stale timestamp."

A PID file cannot satisfy this. The process that wrote it can vanish without
cleaning up -- a crash, a ``SIGKILL``, a power loss -- and the file then claims a
dead owner forever. Worse, the usual workaround ("if the pid is gone, steal it")
races with pid reuse and cannot see a lock held from another machine at all.

So ownership is decided by a kernel lock:

===========  ============================================================
Windows      ``msvcrt.locking`` / ``LockFile`` on a one-byte range of
             ``.review/locks/owner.lock``.
POSIX        ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the same file.
===========  ============================================================

Both are released by the operating system when the owning process dies, which is
exactly the property a PID file lacks. The file also carries human-readable
diagnostics (pid, host, boot time, app version, instance id), but those are written
only for a person to read; no decision is ever made from them. A stale-looking
timestamp is never a reason to take a lock that is still held.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__ as APP_VERSION
from ..errors import ResumeReviewError
from ..util import now_iso

__all__ = [
    "InstanceLock",
    "InstanceLockedError",
    "lock_backend_name",
    "is_locked",
    "read_holder",
]

_WINDOWS = os.name == "nt"

#: Poll interval while waiting for a bounded ``timeout`` acquire.
_POLL_SECONDS = 0.05

#: Windows region locks are *mandatory*: another handle cannot read a locked byte.
#: So the locked byte is byte 0 only, and the human-readable diagnostics are written
#: from byte 1 onward, where a reader can still see them while the lock is held.
_LOCK_BYTE = 0
_DIAGNOSTIC_OFFSET = 1
_DIAGNOSTIC_SIZE = 4095


class InstanceLockedError(ResumeReviewError):
    """A live owner already holds this instance. Ownership is never stolen."""

    code = "INSTANCE_LOCKED_BY_OTHER_OWNER"
    http_status = 409


def lock_backend_name() -> str:
    """Name of the primitive this host uses. Recorded in setup diagnostics."""
    return "msvcrt.LockFile(no-steal)" if _WINDOWS else "fcntl.flock(LOCK_EX|LOCK_NB)"


def _try_lock(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock. Returns False when held."""
    if _WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if _WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - already unlocked by close
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - already unlocked by close
        pass


def _ensure_lock_byte(fd: int) -> None:
    """Guarantee the byte the lock protects exists before locking it."""
    try:
        size = os.fstat(fd).st_size
    except OSError:  # pragma: no cover - defensive
        size = 0
    if size < 1:
        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        os.write(fd, b"\x00")
        os.fsync(fd)


def _boot_time_iso() -> str:
    """Approximate host boot time.

    ``time.monotonic()`` counts from boot on both Windows (``GetTickCount64``) and
    Linux (``CLOCK_MONOTONIC``), so subtracting it from wall-clock time yields the
    boot instant. This is diagnostics only; it never feeds an ownership decision.
    """
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(time.time() - time.monotonic(), tz=timezone.utc).isoformat()
    except Exception:  # pragma: no cover - defensive
        return ""


@dataclass
class HolderInfo:
    pid: int | None
    host: str | None
    booted_at: str | None
    app_version: str | None
    instance_id: str | None
    acquired_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "host": self.host,
            "booted_at": self.booted_at,
            "app_version": self.app_version,
            "instance_id": self.instance_id,
            "acquired_at": self.acquired_at,
        }


class InstanceLock:
    """The single-writer lock for one instance.

    Hold one for the lifetime of the helper process. ``acquire`` raises rather than
    waiting indefinitely by default; a helper that blocks forever on a lock is worse
    than one that reports the conflict.
    """

    def __init__(
        self,
        lock_path: str | os.PathLike[str],
        *,
        instance_id: str | None = None,
        app_version: str = APP_VERSION,
    ) -> None:
        self._path = Path(lock_path)
        self._instance_id = instance_id
        self._app_version = app_version
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        """True only when *this object* holds the lock."""
        return self._fd is not None

    # -- acquire / release -------------------------------------------------
    def acquire(self, *, timeout: float = 0.0) -> "InstanceLock":
        """Take the lock, or raise :class:`InstanceLockedError`.

        ``timeout`` seconds of bounded polling is offered for a controlled restart,
        where the previous owner is expected to close momentarily. Stealing is not
        offered at any timeout.
        """
        if self._fd is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_BINARY: without it the Windows CRT would translate the diagnostics'
        # newlines to CRLF, so the fixed-size padded block would no longer be a
        # fixed size and a later shorter write could leave a stale tail behind.
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        _ensure_lock_byte(fd)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if _try_lock(fd):
                break
            if time.monotonic() >= deadline:
                os.close(fd)
                raise InstanceLockedError(
                    "Another owner currently holds this workspace. Only one helper may "
                    "write to an instance at a time. Stop the other owner and try again.",
                    detail={"reason": "locked_by_other_owner", "holder": self._safe_holder()},
                )
            time.sleep(_POLL_SECONDS)

        self._fd = fd
        try:
            self._write_diagnostics()
        except OSError:  # pragma: no cover - diagnostics are best effort
            pass
        return self

    def release(self) -> None:
        """Release the lock and close the descriptor. Safe to call repeatedly."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- inspection --------------------------------------------------------
    def is_held(self) -> bool:
        """True when any live owner holds the lock, including this object.

        Probes with a second non-blocking lock. On both platforms a second handle
        to the same file contends with the first, so this cannot report a held lock
        as free.
        """
        if self._fd is not None:
            return True
        if not self._path.exists():
            return False
        try:
            probe = os.open(str(self._path), os.O_RDWR)
        except OSError:
            return False
        try:
            if _try_lock(probe):
                _unlock(probe)
                return False
            return True
        finally:
            os.close(probe)

    def holder_info(self) -> HolderInfo | None:
        """Best-effort diagnostics read from the lock file. Never authoritative."""
        return read_holder(self._path)

    def _safe_holder(self) -> dict[str, Any] | None:
        info = read_holder(self._path)
        return info.to_dict() if info else None

    def _write_diagnostics(self) -> None:
        assert self._fd is not None
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "booted_at": _boot_time_iso(),
            "app_version": self._app_version,
            "instance_id": self._instance_id,
            "acquired_at": now_iso(),
            "backend": lock_backend_name(),
        }
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        # A fixed-size, space-padded block so a later, shorter write fully replaces
        # an earlier one rather than leaving a tail that would break parsing.
        block = data.ljust(_DIAGNOSTIC_SIZE, b" ")[:_DIAGNOSTIC_SIZE]
        os.lseek(self._fd, _DIAGNOSTIC_OFFSET, os.SEEK_SET)
        os.write(self._fd, block)
        os.fsync(self._fd)


# ---------------------------------------------------------------------------
# Standalone helpers (no lock object required)
# ---------------------------------------------------------------------------
def read_holder(lock_path: str | os.PathLike[str]) -> HolderInfo | None:
    """Parse the diagnostics stored in a lock file, or ``None`` when unreadable.

    The writer holds the lock byte while writing, so the payload is read from byte 1
    onward, where it stays readable even under a Windows mandatory lock. A parse
    failure returns ``None`` rather than raising, because the content is advisory.
    """
    try:
        with open(lock_path, "rb") as handle:
            handle.seek(_DIAGNOSTIC_OFFSET)
            raw = (
                handle.read(_DIAGNOSTIC_SIZE)
                .decode("utf-8", errors="replace")
                .strip("\x00")
                .strip()
            )
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return HolderInfo(
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        host=data.get("host") if isinstance(data.get("host"), str) else None,
        booted_at=data.get("booted_at") if isinstance(data.get("booted_at"), str) else None,
        app_version=data.get("app_version") if isinstance(data.get("app_version"), str) else None,
        instance_id=data.get("instance_id") if isinstance(data.get("instance_id"), str) else None,
        acquired_at=data.get("acquired_at") if isinstance(data.get("acquired_at"), str) else None,
    )


def is_locked(lock_path: str | os.PathLike[str]) -> bool:
    """True when some live owner holds ``lock_path``."""
    probe = InstanceLock(lock_path)
    return probe.is_held()
