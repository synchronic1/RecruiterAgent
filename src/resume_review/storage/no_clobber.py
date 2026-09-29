"""Atomic same-volume no-clobber move primitives.

Authority: PRD section 13.2.

    "Implement a tested no-clobber move primitive for each supported host OS. A
     check-then-overwriting-rename sequence is not enough. Never overwrite a
     destination, replace an unrelated file, follow a symlink/junction escape, or
     concatenate shell commands."

Why this module exists
----------------------
``os.replace`` and ``shutil.move`` both overwrite. ``os.rename`` is atomic but
silently replaces on POSIX. A "does the destination exist? then rename" sequence
has a time-of-check/time-of-use window in which another actor can create the
destination, and the rename will then destroy it.

So every supported platform is driven through a kernel primitive that *fails*
when the destination exists, rather than a userspace check:

===========  ============================================================
Windows      ``MoveFileExW`` without ``MOVEFILE_REPLACE_EXISTING``; the API
             returns ``ERROR_ALREADY_EXISTS`` as part of the rename itself.
Linux        ``renameat2(..., RENAME_NOREPLACE)``.
macOS        ``renamex_np(..., RENAME_EXCL)``.
POSIX        ``link(2)`` then ``unlink(2)``. ``link`` fails with ``EEXIST`` if
fallback     the destination exists and is atomic. Only used when
             ``renameat2``/``renamex_np`` are unavailable.
===========  ============================================================

There is deliberately **no copy-and-delete fallback**. A cross-volume request is
refused outright; silently degrading a rename into copy-then-unlink would lose the
atomicity the whole recovery design rests on.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import ResumeReviewError
from .paths import extended_path, is_reparse_point

__all__ = [
    "MoveOutcome",
    "MoveResult",
    "FileIdentity",
    "file_identity",
    "same_volume",
    "atomic_no_clobber_move",
    "same_inode",
    "no_clobber_backend_name",
    "NoClobberUnsupported",
]

_WINDOWS = os.name == "nt"
_MACOS = sys.platform == "darwin"
_LINUX = sys.platform.startswith("linux")

# Windows MoveFileEx flags
_MOVEFILE_WRITE_THROUGH = 0x00000008
# Do NOT add MOVEFILE_REPLACE_EXISTING (0x1) or MOVEFILE_COPY_ALLOWED (0x2):
# the first destroys the no-clobber guarantee, the second silently enables
# cross-volume copy+delete semantics.

# Linux RENAME_NOREPLACE
_RENAME_NOREPLACE = 1

# macOS RENAME_EXCL
_RENAME_EXCL = 0x00000004

#: Linux syscall numbers for renameat2 when the libc wrapper is missing.
_RENAMEAT2_SYSCALL = {
    "x86_64": 316,
    "aarch64": 276,
    "arm": 382,
    "i386": 353,
    "ppc64le": 357,
    "s390x": 347,
    "riscv64": 276,
}
_AT_FDCWD = -100


class NoClobberUnsupported(ResumeReviewError):
    """No safe no-clobber primitive is available on this host."""

    code = "NO_CLOBBER_UNSUPPORTED"
    http_status = 409


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class MoveOutcome:
    """Outcome of one attempted move. Callers switch on this, never on errno."""

    MOVED = "moved"
    SOURCE_MISSING = "source_missing"
    DESTINATION_EXISTS = "destination_exists"
    CROSS_VOLUME = "cross_volume"
    SOURCE_CHANGED = "source_changed"
    SOURCE_IS_REPARSE = "source_is_reparse"
    DESTINATION_IS_REPARSE = "destination_is_reparse"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class FileIdentity:
    """Physical identity of a file, as observed through the filesystem.

    ``volume`` and ``inode`` come from the OS (``st_dev``/``st_ino``, which Python
    populates with the volume serial number and file index on Windows). They are
    used to distinguish "the file we recorded" from "a different file that happens
    to sit at the same path". They are a diagnostic, never an authorization input.
    """

    volume: int
    inode: int
    size: int
    mtime_ns: int

    def digest_hint(self) -> str:
        return f"{self.volume}:{self.inode}:{self.size}:{self.mtime_ns}"


@dataclass(frozen=True)
class MoveResult:
    outcome: str
    detail: str = ""
    errno_value: int | None = None
    source_identity: FileIdentity | None = None
    destination_identity: FileIdentity | None = None

    @property
    def moved(self) -> bool:
        return self.outcome == MoveOutcome.MOVED

    @property
    def destination_blocks(self) -> bool:
        return self.outcome == MoveOutcome.DESTINATION_EXISTS

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "errno": self.errno_value,
        }


# ---------------------------------------------------------------------------
# Identity and volume
# ---------------------------------------------------------------------------
def file_identity(path: str | os.PathLike[str]) -> FileIdentity | None:
    """Best-effort physical identity. Returns ``None`` when the path is absent."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return FileIdentity(
        volume=int(getattr(st, "st_dev", 0)),
        inode=int(getattr(st, "st_ino", 0)),
        size=int(st.st_size),
        mtime_ns=int(getattr(st, "st_mtime_ns", 0)),
    )


def same_inode(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """True when two paths are two names for the same underlying file.

    Used by crash recovery to recognise the POSIX ``link``-then-``unlink``
    interruption window: both names existing but sharing one inode is *not* the
    ambiguous "both exist" case, it is a move whose source cleanup is pending.
    """
    ia = file_identity(a)
    ib = file_identity(b)
    if ia is None or ib is None:
        return False
    return ia.volume == ib.volume and ia.inode == ib.inode and ia.inode != 0


def _stat_dir(path: str | os.PathLike[str]) -> int | None:
    probe = path if os.path.isdir(path) else os.path.dirname(str(path))
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return int(os.stat(probe).st_dev)
    except OSError:
        return None


def same_volume(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> bool:
    """True when source and destination live on the same filesystem/volume.

    Both arguments may name files that do not exist yet; the containing directories
    are walked upward to the nearest existing ancestor.
    """
    a = _stat_dir(source)
    b = _stat_dir(destination)
    if a is None or b is None:
        return False
    return a == b


def sha256_file(path: str | os.PathLike[str], *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Platform primitives
# ---------------------------------------------------------------------------
def _win_move_no_replace(src: str, dst: str) -> tuple[str, int | None]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int

    ctypes.set_last_error(0)
    ok = move(extended_path(src), extended_path(dst), ctypes.c_uint32(_MOVEFILE_WRITE_THROUGH))
    if ok:
        return MoveOutcome.MOVED, None

    err = ctypes.get_last_error()
    if err == 183:  # ERROR_ALREADY_EXISTS
        return MoveOutcome.DESTINATION_EXISTS, err
    if err == 80:  # ERROR_FILE_EXISTS
        return MoveOutcome.DESTINATION_EXISTS, err
    if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
        return MoveOutcome.SOURCE_MISSING, err
    if err == 17:  # ERROR_NOT_SAME_DEVICE
        return MoveOutcome.CROSS_VOLUME, err
    if err == 5 and os.path.exists(dst):  # ERROR_ACCESS_DENIED onto an existing node
        return MoveOutcome.DESTINATION_EXISTS, err
    return MoveOutcome.FAILED, err


def _posix_renameat2_no_replace(src: str, dst: str) -> tuple[str, int | None]:
    libc = ctypes.CDLL(None, use_errno=True)

    fn = getattr(libc, "renameat2", None)
    if fn is not None:
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE)
        if rc == 0:
            return MoveOutcome.MOVED, None
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):
            return _map_errno(err), err

    # glibc without the wrapper: go through syscall(2) directly.
    number = _RENAMEAT2_SYSCALL.get(os.uname().machine)
    if number is not None:
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        rc = syscall(
            ctypes.c_long(number),
            ctypes.c_int(_AT_FDCWD),
            os.fsencode(src),
            ctypes.c_int(_AT_FDCWD),
            os.fsencode(dst),
            ctypes.c_uint(_RENAME_NOREPLACE),
        )
        if rc == 0:
            return MoveOutcome.MOVED, None
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):
            return _map_errno(err), err

    return MoveOutcome.UNSUPPORTED, None


def _macos_renamex_np_no_replace(src: str, dst: str) -> tuple[str, int | None]:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renamex_np", None)
    if fn is None:
        return MoveOutcome.UNSUPPORTED, None
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    ctypes.set_errno(0)
    rc = fn(os.fsencode(src), os.fsencode(dst), ctypes.c_uint(_RENAME_EXCL))
    if rc == 0:
        return MoveOutcome.MOVED, None
    return _map_errno(ctypes.get_errno()), ctypes.get_errno()


def _link_unlink_no_replace(src: str, dst: str) -> tuple[str, int | None]:
    """Portable POSIX fallback: ``link`` is atomic and refuses an existing target.

    If the process dies between ``link`` and ``unlink`` both names exist and share
    one inode. Recovery classifies that with :func:`same_inode` as a move whose
    source cleanup is pending, not as an ambiguous duplicate.
    """
    try:
        os.link(src, dst)
    except OSError as exc:
        return _map_errno(exc.errno or 0), exc.errno
    try:
        os.unlink(src)
    except OSError as exc:  # pragma: no cover - destination is already correct
        return MoveOutcome.FAILED, exc.errno
    return MoveOutcome.MOVED, None


def _map_errno(err: int) -> str:
    if err in (errno.EEXIST,):
        return MoveOutcome.DESTINATION_EXISTS
    if err in (errno.ENOENT,):
        return MoveOutcome.SOURCE_MISSING
    if err in (errno.EXDEV,):
        return MoveOutcome.CROSS_VOLUME
    if err in (errno.ENOTDIR, errno.EISDIR):
        return MoveOutcome.DESTINATION_EXISTS
    return MoveOutcome.FAILED


_BACKEND_NAME = (
    "MoveFileExW(no-replace)"
    if _WINDOWS
    else "renamex_np(RENAME_EXCL)"
    if _MACOS
    else "renameat2(RENAME_NOREPLACE)"
    if _LINUX
    else "link+unlink"
)


def no_clobber_backend_name() -> str:
    """Name of the primitive this host will use. Recorded in diagnostics."""
    return _BACKEND_NAME


def _platform_move(src: str, dst: str) -> tuple[str, int | None]:
    if _WINDOWS:
        return _win_move_no_replace(src, dst)
    if _MACOS:
        outcome, err = _macos_renamex_np_no_replace(src, dst)
        if outcome != MoveOutcome.UNSUPPORTED:
            return outcome, err
        return _link_unlink_no_replace(src, dst)
    if _LINUX:
        outcome, err = _posix_renameat2_no_replace(src, dst)
        if outcome != MoveOutcome.UNSUPPORTED:
            return outcome, err
        return _link_unlink_no_replace(src, dst)
    return _link_unlink_no_replace(src, dst)


# ---------------------------------------------------------------------------
# The public primitive
# ---------------------------------------------------------------------------
def atomic_no_clobber_move(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    create_parents: bool = False,
) -> MoveResult:
    """Move ``source`` to ``destination``, failing if the destination exists.

    Preconditions are revalidated here, at the operation boundary, rather than
    trusted from planning time. Order matters:

    1. Refuse a cross-volume request *before* touching anything, so we can never be
       tempted into a copy-and-delete fallback.
    2. Refuse a reparse point at either end.
    3. Verify the source content still matches what the plan recorded.
    4. Perform the kernel move.
    5. Re-verify the destination exists with the expected identity.

    There is no code path that overwrites, replaces, or deletes an unrelated file.
    """
    src = os.fspath(source)
    dst = os.fspath(destination)

    if not same_volume(src, dst):
        return MoveResult(
            outcome=MoveOutcome.CROSS_VOLUME,
            detail=(
                "Source and destination are on different volumes. Cross-volume "
                "organization is out of scope; the move was refused rather than "
                "degraded to copy-and-delete."
            ),
        )

    if is_reparse_point(src):
        return MoveResult(
            outcome=MoveOutcome.SOURCE_IS_REPARSE,
            detail="The source is a link or reparse point, which is never a managed file.",
        )

    try:
        src_stat = os.stat(src, follow_symlinks=False)
    except FileNotFoundError:
        return MoveResult(outcome=MoveOutcome.SOURCE_MISSING, detail="The source no longer exists.")
    except OSError as exc:
        return MoveResult(outcome=MoveOutcome.FAILED, detail="The source could not be read.", errno_value=exc.errno)

    if not os.path.isfile(src):
        return MoveResult(
            outcome=MoveOutcome.NOT_A_REGULAR_FILE,
            detail="Only regular files are managed; directories and devices are refused.",
        )

    if expected_size is not None and src_stat.st_size != expected_size:
        return MoveResult(
            outcome=MoveOutcome.SOURCE_CHANGED,
            detail="The source size changed since the plan was built.",
        )

    if expected_sha256:
        actual = sha256_file(src)
        if actual != expected_sha256:
            return MoveResult(
                outcome=MoveOutcome.SOURCE_CHANGED,
                detail="The source content changed since the plan was built.",
            )

    if os.path.lexists(dst):
        if is_reparse_point(dst):
            return MoveResult(
                outcome=MoveOutcome.DESTINATION_IS_REPARSE,
                detail="The destination is a link or reparse point; refusing to write through it.",
            )
        return MoveResult(
            outcome=MoveOutcome.DESTINATION_EXISTS,
            detail="The destination already exists.",
        )

    parent = os.path.dirname(dst)
    if create_parents:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return MoveResult(
                outcome=MoveOutcome.FAILED,
                detail="The destination directory could not be created.",
                errno_value=exc.errno,
            )
    elif not os.path.isdir(parent):
        return MoveResult(
            outcome=MoveOutcome.FAILED,
            detail="The destination directory does not exist.",
        )

    source_identity = file_identity(src)
    outcome, err = _platform_move(src, dst)

    if outcome != MoveOutcome.MOVED:
        return MoveResult(
            outcome=outcome,
            detail=_describe(outcome),
            errno_value=err,
            source_identity=source_identity,
        )

    # A rename preserves the inode; assert that rather than assume it.
    destination_identity = file_identity(dst)
    if destination_identity is None:
        return MoveResult(
            outcome=MoveOutcome.FAILED,
            detail="The move reported success but the destination is not readable.",
            source_identity=source_identity,
        )

    if source_identity is not None and not _same_physical(source_identity, destination_identity):
        # A rename that changes the inode is not a rename. Treat as ambiguous and
        # let recovery reconcile; never report a clean success we cannot justify.
        return MoveResult(
            outcome=MoveOutcome.FAILED,
            detail="The destination identity does not match the source; manual reconciliation is required.",
            source_identity=source_identity,
            destination_identity=destination_identity,
        )

    _sync_directory(os.path.dirname(dst))

    return MoveResult(
        outcome=MoveOutcome.MOVED,
        detail="Moved without clobbering.",
        source_identity=source_identity,
        destination_identity=destination_identity,
    )


def _same_physical(a: FileIdentity, b: FileIdentity) -> bool:
    if a.inode == 0 or b.inode == 0:
        # Some filesystems (and some Windows configurations) do not report a file
        # index. Fall back to size, which the caller has already checked.
        return a.size == b.size
    return a.volume == b.volume and a.inode == b.inode


def _describe(outcome: str) -> str:
    return {
        MoveOutcome.DESTINATION_EXISTS: "The destination already exists; nothing was overwritten.",
        MoveOutcome.SOURCE_MISSING: "The source no longer exists at the recorded path.",
        MoveOutcome.CROSS_VOLUME: "The destination is on a different volume.",
        MoveOutcome.SOURCE_CHANGED: "The source changed since the plan was built.",
        MoveOutcome.UNSUPPORTED: "This host provides no safe no-clobber move primitive.",
    }.get(outcome, "The move failed for an unreported reason.")


def _sync_directory(path: str) -> None:
    """Best-effort directory fsync so a rename survives a power loss.

    Silently skipped where the platform does not permit it; Windows has no
    directory fsync and MoveFileExW was already called with WRITE_THROUGH.
    """
    if _WINDOWS or not path:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ensure_directory(path: str | os.PathLike[str]) -> Path:
    """Create a directory tree, refusing to do so through a reparse point."""
    target = Path(os.path.abspath(os.fspath(path)))
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if is_reparse_point(probe):
        raise NoClobberUnsupported(
            "Refusing to create directories beneath a link or reparse point.",
            code="SYMLINK_REJECTED",
        )
    target.mkdir(parents=True, exist_ok=True)
    return target
