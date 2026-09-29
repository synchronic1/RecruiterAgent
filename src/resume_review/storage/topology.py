"""Storage topology detection.

Authority: PRD section 15 and acceptance test AT-32.

    "Use SQLite only on supported storage-host-local filesystems... Do not
     configure WAL on an SMB/NFS-mounted live database; SQLite explicitly
     documents that limitation."

    "Setup must detect or explicitly verify storage topology. Unknown topology is
     not automatically accepted."

This module answers one question honestly: *is this path on local storage we
support?* It returns ``UNKNOWN`` when it cannot tell, and callers must treat
``UNKNOWN`` as requiring explicit operator confirmation rather than as a pass.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "TopologyKind",
    "TopologyReport",
    "probe_topology",
    "is_network_path",
    "assert_supported_for_database",
]


class TopologyKind(str, Enum):
    LOCAL_FIXED = "local_fixed"
    LOCAL_REMOVABLE = "local_removable"
    NETWORK = "network"
    RAMDISK = "ramdisk"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TopologyReport:
    kind: TopologyKind
    detail: str
    mount_point: str | None = None
    filesystem_type: str | None = None
    writable: bool = False

    @property
    def supports_live_database(self) -> bool:
        """Only a local fixed or removable volume may hold the live database.

        A removable volume is permitted because a job folder legitimately lives on
        one; it is reported so the operator knows WAL durability is weaker there.
        """
        return self.kind in (TopologyKind.LOCAL_FIXED, TopologyKind.LOCAL_REMOVABLE)

    @property
    def is_certain(self) -> bool:
        return self.kind is not TopologyKind.UNKNOWN

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind.value),
            "detail": self.detail,
            "mount_point": self.mount_point,
            "filesystem_type": self.filesystem_type,
            "writable": self.writable,
            "supports_live_database": self.supports_live_database,
            "certain": self.is_certain,
        }


# Windows drive types
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6

# POSIX filesystem type magics we care about, for hosts without /proc.
_NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "afs",
    "fuse.sshfs", "fuse.smbnetfs", "fuse.gvfsd-fuse", "9p", "virtiofs",
    "davfs", "fuse.rclone", "glusterfs", "ceph", "lustre", "gpfs",
}
_RAM_FS = {"tmpfs", "ramfs", "devtmpfs"}


def is_network_path(path: str | os.PathLike[str]) -> bool:
    """True for a UNC path or a path with a network drive root."""
    p = os.fspath(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return True
    if os.name == "nt":
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if not drive:
            return True
        return _windows_drive_type(drive + "\\") == _DRIVE_REMOTE
    return False


def _windows_drive_type(root: str) -> int:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel32.GetDriveTypeW
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_uint
        return int(fn(root))
    except Exception:  # pragma: no cover - non-Windows or restricted host
        return 0


def probe_topology(path: str | os.PathLike[str]) -> TopologyReport:
    """Classify the storage backing ``path``.

    Walks up to the nearest existing ancestor so it can be called on a folder that
    has not been created yet, which is what setup needs.
    """
    target = Path(os.path.abspath(os.fspath(path)))
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    writable = os.access(probe, os.W_OK | os.X_OK)

    if os.name == "nt":
        return _probe_windows(probe, writable)
    return _probe_posix(probe, writable)


def _probe_windows(probe: Path, writable: bool) -> TopologyReport:
    text = str(probe)
    if text.startswith("\\\\") or text.startswith("//"):
        return TopologyReport(
            kind=TopologyKind.NETWORK,
            detail="The path is a UNC network share. A live SQLite database is not supported there.",
            filesystem_type="unc",
            writable=writable,
        )

    drive, _ = os.path.splitdrive(text)
    if not drive:
        return TopologyReport(
            kind=TopologyKind.UNKNOWN,
            detail="The volume for this path could not be determined.",
            writable=writable,
        )

    root = drive + "\\"
    dtype = _windows_drive_type(root)
    mapping = {
        _DRIVE_FIXED: (TopologyKind.LOCAL_FIXED, "A local fixed volume."),
        _DRIVE_REMOVABLE: (TopologyKind.LOCAL_REMOVABLE, "A local removable volume."),
        _DRIVE_REMOTE: (
            TopologyKind.NETWORK,
            "A mapped network drive. A live SQLite database is not supported there.",
        ),
        _DRIVE_RAMDISK: (TopologyKind.RAMDISK, "A RAM disk; data is lost on restart."),
        _DRIVE_CDROM: (TopologyKind.UNKNOWN, "An optical volume is not a supported workspace."),
    }
    if dtype in mapping:
        kind, detail = mapping[dtype]
        return TopologyReport(
            kind=kind, detail=detail, mount_point=root, filesystem_type=f"win32:{dtype}", writable=writable
        )
    return TopologyReport(
        kind=TopologyKind.UNKNOWN,
        detail="The volume type could not be classified.",
        mount_point=root,
        writable=writable,
    )


def _probe_posix(probe: Path, writable: bool) -> TopologyReport:
    mount_point, fstype = _posix_mount_info(probe)
    if fstype is None:
        return TopologyReport(
            kind=TopologyKind.UNKNOWN,
            detail="The filesystem type for this path could not be determined.",
            mount_point=mount_point,
            writable=writable,
        )

    normalized = fstype.lower()
    if normalized in _NETWORK_FS:
        return TopologyReport(
            kind=TopologyKind.NETWORK,
            detail=f"A network filesystem ({normalized}). A live SQLite database is not supported there.",
            mount_point=mount_point,
            filesystem_type=normalized,
            writable=writable,
        )
    if normalized in _RAM_FS:
        return TopologyReport(
            kind=TopologyKind.RAMDISK,
            detail=f"A memory filesystem ({normalized}); data is lost on restart.",
            mount_point=mount_point,
            filesystem_type=normalized,
            writable=writable,
        )
    return TopologyReport(
        kind=TopologyKind.LOCAL_FIXED,
        detail=f"A local filesystem ({normalized}).",
        mount_point=mount_point,
        filesystem_type=normalized,
        writable=writable,
    )


def _posix_mount_info(probe: Path) -> tuple[str | None, str | None]:
    """Return (mount point, filesystem type) for the longest mount covering ``probe``."""
    real = os.path.realpath(probe)
    best: tuple[str, str] | None = None

    for source in ("/proc/self/mountinfo", "/proc/mounts"):
        try:
            with open(source, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue

        if source.endswith("mountinfo"):
            for line in lines:
                fields = line.split()
                if len(fields) < 5:
                    continue
                mount_point = fields[4].replace("\\040", " ")
                # fstype follows the " - " separator.
                try:
                    sep = fields.index("-")
                except ValueError:
                    continue
                if sep + 1 >= len(fields):
                    continue
                fstype = fields[sep + 1]
                if real == mount_point or real.startswith(mount_point.rstrip("/") + "/"):
                    if best is None or len(mount_point) > len(best[0]):
                        best = (mount_point, fstype)
        else:
            for line in lines:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount_point = fields[1].replace("\\040", " ")
                fstype = fields[2]
                if real == mount_point or real.startswith(mount_point.rstrip("/") + "/"):
                    if best is None or len(mount_point) > len(best[0]):
                        best = (mount_point, fstype)

        if best is not None:
            return best

    if sys.platform == "darwin":
        return _darwin_mount_info(real)
    return None, None


def _darwin_mount_info(real: str) -> tuple[str | None, str | None]:  # pragma: no cover - macOS only
    try:
        with open("/proc/mounts", "r", encoding="utf-8"):
            pass
    except OSError:
        pass
    try:
        import subprocess

        out = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except Exception:
        return None, None
    best: tuple[str, str] | None = None
    for line in out.splitlines():
        # "server:/export on /Volumes/x (nfs, nodev, ...)"
        if " on " not in line or "(" not in line:
            continue
        mount_point = line.split(" on ", 1)[1].split(" (", 1)[0]
        fstype = line.split("(", 1)[1].split(",", 1)[0].strip()
        if real == mount_point or real.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype)
    return best if best else (None, None)


def assert_supported_for_database(path: str | os.PathLike[str], *, allow_unknown: bool = False) -> TopologyReport:
    """Raise unless ``path`` may hold a live SQLite database.

    ``allow_unknown`` exists only for tests and for ``repair --dry-run``. Setup
    itself never passes it: unknown topology is not automatically accepted.
    """
    from ..errors import ResumeReviewError

    report = probe_topology(path)

    if report.kind is TopologyKind.NETWORK:
        raise ResumeReviewError(
            "The workspace root is on a network filesystem. The live database must stay "
            "on storage local to the machine hosting the folder. If this device cannot run "
            "the helper, a fully writable folder-contained workspace is not supported here "
            "in version 1; read-only snapshots can still be viewed.",
            code="NETWORK_FILESYSTEM_DATABASE",
            http_status=409,
            detail=report.to_dict(),
        )

    if report.kind is TopologyKind.UNKNOWN and not allow_unknown:
        raise ResumeReviewError(
            "The storage backend for this folder could not be classified, so it cannot be "
            "accepted automatically. Confirm the topology explicitly to continue.",
            code="UNSUPPORTED_STORAGE_TOPOLOGY",
            http_status=409,
            detail=report.to_dict(),
        )

    if not report.writable:
        raise ResumeReviewError(
            "The workspace root is not writable.",
            code="READ_ONLY_LOCATION",
            http_status=409,
            detail=report.to_dict(),
        )

    return report
