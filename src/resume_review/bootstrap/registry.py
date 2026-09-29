"""The protected host registry.

Authority: PRD section 4 ("The host registry also stores or references the trusted
release manifest and secrets. A checksum in the same writable folder as an
executable is not sufficient protection against replacement.").

The registry lives **outside** the job folder, under the operating user's private
application-data directory. That placement is the whole point: the folder is
writable by anyone who can drop a resume into it, so a manifest stored next to the
deployed bundle would be rewritable by the same actor. Keeping the trusted copy on
the host's private path means a tampered ``.review/app/`` can be detected by
comparing it against something the folder's writers cannot reach.

What lives here:

* ``instance_id -> {canonical root, service address, storage mode, app version,
  schema version, trusted bundle hash, created_at, last_seen}``
* the trusted release manifest itself, one per instance

Instance identity is an opaque identifier from :func:`resume_review.util.new_id`.
It is never a hash of an absolute path and never derived from an applicant name,
so moving a folder does not change who the instance is.

Writes are atomic (temp file plus rename in the same directory) and serialized by an
OS-backed lock, so two concurrent setup runs cannot interleave a read-modify-write
and lose one another's registration. The registry file is the application's own
record rather than a managed applicant artifact, which is why replacing it wholesale
is correct here and the no-clobber move primitive is not used for it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .. import __version__ as APP_VERSION
from .. import SCHEMA_VERSION
from ..errors import ResumeReviewError
from ..util import new_id, now_iso

__all__ = [
    "REGISTRY_VERSION",
    "RegistryEntry",
    "HostRegistry",
    "default_registry_dir",
    "default_registry_path",
]

REGISTRY_VERSION = "1.0"
REGISTRY_FILENAME = "registry.json"
REGISTRY_LOCK_FILENAME = "registry.lock"

#: Environment override used by tests and by an operator who needs a non-default
#: private location. It names a *directory*, not the file.
ENV_REGISTRY_DIR = "RESUME_REVIEW_REGISTRY_DIR"


def default_registry_dir() -> Path:
    """The protected per-user directory, per platform convention.

    Windows: ``%LOCALAPPDATA%/ResumeReview``. Elsewhere: ``$XDG_DATA_HOME`` or
    ``~/.local/share``, plus ``resume-review``.
    """
    override = os.environ.get(ENV_REGISTRY_DIR)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return Path(base) / "ResumeReview"
    xdg = os.environ.get("XDG_DATA_HOME")
    base_path = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base_path / "resume-review"


def default_registry_path() -> Path:
    return default_registry_dir() / REGISTRY_FILENAME


@dataclass
class RegistryEntry:
    """One registered instance, as stored on the host's private path."""

    instance_id: str
    canonical_root: str
    service_address: str | None
    storage_mode: str
    app_version: str
    schema_version: int
    trusted_bundle_hash: str | None
    created_at: str
    last_seen: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _RegistryLock:
    """A small OS-backed exclusive lock guarding registry read-modify-write.

    Reimplemented here rather than imported from :mod:`ownership` on purpose: the
    instance lock belongs to a job folder and carries instance diagnostics, whereas
    this lock guards a host file and must work before any instance exists. Both are
    nonetheless real kernel locks, released by the OS on process death.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "_RegistryLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        _lock_fd(self._fd)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                _unlock_fd(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - unlocked already
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - unlocked already
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically within ``path``'s directory.

    ``os.replace`` of a temp file onto the target is the standard single-file
    atomic update: a reader sees either the old document or the new one, never a
    half-written mixture. The temp name is randomised so two writers cannot collide
    on it even before the lock is taken.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{new_id('tmp')}.tmp"
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    # O_BINARY: the CRT would otherwise translate newlines to CRLF on Windows and
    # the byte count in the registry document would not match what was hashed.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(str(tmp), str(path))
    except BaseException:  # pragma: no cover - cleanup on a failed rename
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


class HostRegistry:
    """Read/write access to the protected registry document.

    The whole document is small (one entry per job folder), so it is loaded and
    written as a unit under a lock. There is no partial-update path that could
    leave two instances' records inconsistent with each other.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_registry_path()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock_path(self) -> Path:
        return self._path.parent / REGISTRY_LOCK_FILENAME

    # -- document ----------------------------------------------------------
    def exists(self) -> bool:
        return self._path.is_file()

    def _empty(self) -> dict[str, Any]:
        return {
            "registry_version": REGISTRY_VERSION,
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "instances": {},
        }

    def load(self) -> dict[str, Any]:
        """Read the registry, tolerating absence but not corruption.

        A corrupt registry is refused rather than silently recreated: recreating it
        would discard the trusted manifests, which is exactly what an attacker who
        can write the file would want.
        """
        if not self._path.is_file():
            return self._empty()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ResumeReviewError(
                "The host registry could not be read. It has not been modified.",
                code="MANIFEST_UNTRUSTED",
                http_status=409,
                detail={"reason": "registry_unreadable"},
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("instances"), dict):
            raise ResumeReviewError(
                "The host registry is not in a recognised format. It has not been modified.",
                code="MANIFEST_UNTRUSTED",
                http_status=409,
                detail={"reason": "registry_shape"},
            )
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["registry_version"] = REGISTRY_VERSION
        data["updated_at"] = now_iso()
        _atomic_write_json(self._path, data)

    # -- entries -----------------------------------------------------------
    def get(self, instance_id: str) -> RegistryEntry | None:
        raw = self.load()["instances"].get(instance_id)
        if not isinstance(raw, dict):
            return None
        return self._to_entry(raw)

    def list_entries(self) -> list[RegistryEntry]:
        instances = self.load()["instances"]
        return [self._to_entry(raw) for raw in instances.values() if isinstance(raw, dict)]

    def find_by_root(self, canonical_root: str | os.PathLike[str]) -> RegistryEntry | None:
        """Locate an instance by its canonical root.

        Used to detect that a folder is already bound to a different instance id.
        The comparison normalises case on Windows, where ``C:\\Jobs`` and
        ``c:\\jobs`` are the same directory.
        """
        wanted = _normalise_root(canonical_root)
        for entry in self.list_entries():
            if _normalise_root(entry.canonical_root) == wanted:
                return entry
        return None

    @staticmethod
    def _to_entry(raw: dict[str, Any]) -> RegistryEntry:
        return RegistryEntry(
            instance_id=str(raw.get("instance_id", "")),
            canonical_root=str(raw.get("canonical_root", "")),
            service_address=raw.get("service_address"),
            storage_mode=str(raw.get("storage_mode", "local")),
            app_version=str(raw.get("app_version", "")),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            trusted_bundle_hash=raw.get("trusted_bundle_hash"),
            created_at=str(raw.get("created_at", "")),
            last_seen=str(raw.get("last_seen", "")),
        )

    # -- mutations ---------------------------------------------------------
    def register(
        self,
        instance_id: str | None,
        *,
        canonical_root: str | os.PathLike[str],
        service_address: str | None = None,
        storage_mode: str = "local",
        app_version: str = APP_VERSION,
        schema_version: int = SCHEMA_VERSION,
        trusted_bundle_hash: str | None = None,
    ) -> RegistryEntry:
        """Create or refresh the entry for one instance, atomically.

        If ``instance_id`` is ``None`` a new opaque id is minted. Re-registering an
        existing id preserves its ``created_at`` so the registry reflects when the
        instance was first provisioned, not when setup last ran.
        """
        with _RegistryLock(self.lock_path):
            data = self.load()
            instances = data["instances"]
            resolved_id = instance_id or new_id("instance")
            existing = instances.get(resolved_id)
            if isinstance(existing, dict):
                created_at = str(existing.get("created_at") or now_iso())
                if trusted_bundle_hash is None:
                    trusted_bundle_hash = existing.get("trusted_bundle_hash")
            else:
                created_at = now_iso()
            entry = RegistryEntry(
                instance_id=resolved_id,
                canonical_root=str(Path(canonical_root).resolve(strict=False)),
                service_address=service_address,
                storage_mode=storage_mode,
                app_version=app_version,
                schema_version=schema_version,
                trusted_bundle_hash=trusted_bundle_hash,
                created_at=created_at,
                last_seen=now_iso(),
            )
            record = entry.to_dict()
            if isinstance(existing, dict) and "trusted_manifest" in existing:
                record["trusted_manifest"] = existing["trusted_manifest"]
            instances[resolved_id] = record
            self._save(data)
            return entry

    def touch(self, instance_id: str) -> bool:
        with _RegistryLock(self.lock_path):
            data = self.load()
            raw = data["instances"].get(instance_id)
            if not isinstance(raw, dict):
                return False
            raw["last_seen"] = now_iso()
            raw["app_version"] = APP_VERSION
            raw["schema_version"] = SCHEMA_VERSION
            self._save(data)
            return True

    def remove(self, instance_id: str) -> bool:
        with _RegistryLock(self.lock_path):
            data = self.load()
            if instance_id not in data["instances"]:
                return False
            del data["instances"][instance_id]
            self._save(data)
            return True

    # -- trusted manifest --------------------------------------------------
    def set_trusted_manifest(
        self,
        instance_id: str,
        manifest: dict[str, Any],
        *,
        canonical_root: str | os.PathLike[str] | None = None,
    ) -> None:
        """Record the trusted release manifest for an instance.

        Called *before* the bundle is copied into the folder, so the deployed copy
        is verified against a record that already exists on the protected path.
        """
        with _RegistryLock(self.lock_path):
            data = self.load()
            instances = data["instances"]
            raw = instances.get(instance_id)
            if not isinstance(raw, dict):
                if canonical_root is None:
                    raise ResumeReviewError(
                        "The instance is not registered, so a trusted manifest cannot be recorded.",
                        code="INSTANCE_NOT_FOUND",
                        http_status=404,
                        detail={"reason": "unregistered"},
                    )
                raw = {
                    "instance_id": instance_id,
                    "canonical_root": str(Path(canonical_root).resolve(strict=False)),
                    "service_address": None,
                    "storage_mode": "local",
                    "app_version": APP_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "trusted_bundle_hash": None,
                    "created_at": now_iso(),
                    "last_seen": now_iso(),
                }
                instances[instance_id] = raw
            raw["trusted_manifest"] = manifest
            raw["trusted_bundle_hash"] = manifest.get("bundle_hash")
            raw["last_seen"] = now_iso()
            self._save(data)

    def get_trusted_manifest(self, instance_id: str) -> dict[str, Any] | None:
        raw = self.load()["instances"].get(instance_id)
        if not isinstance(raw, dict):
            return None
        manifest = raw.get("trusted_manifest")
        return manifest if isinstance(manifest, dict) else None

    def clear_trusted_manifest(self, instance_id: str) -> bool:
        with _RegistryLock(self.lock_path):
            data = self.load()
            raw = data["instances"].get(instance_id)
            if not isinstance(raw, dict) or "trusted_manifest" not in raw:
                return False
            del raw["trusted_manifest"]
            raw["trusted_bundle_hash"] = None
            self._save(data)
            return True

    # -- context helper ----------------------------------------------------
    def session(self) -> Iterator["HostRegistry"]:  # pragma: no cover - convenience
        yield self


def _normalise_root(root: str | os.PathLike[str]) -> str:
    resolved = str(Path(root).resolve(strict=False))
    if sys.platform == "win32":
        return resolved.rstrip("\\/").lower()
    return resolved.rstrip("/")
