"""Release integrity: build, verify, deploy, and re-verify the application bundle.

Authority: PRD section 4 ("A checksum in the same writable folder as an executable
is not sufficient protection against replacement. Bootstrap verifies deployed code
against its trusted installed release before launching it.") and AT-04.

The trust model has two halves and both are required:

* ``build_manifest``/``verify_manifest`` compute and check a per-file SHA-256 plus a
  single ``bundle_hash`` over the sorted ``path\\0sha256\\n`` lines.
* ``deploy_bundle`` records that manifest in the *protected host registry* before
  copying anything, so the deployed copy is checked against a record the folder's
  writers cannot reach.

A modified deployed executable or template is not a warning to be logged: it blocks
execution and requires a controlled repair.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import __version__ as APP_VERSION
from .. import SCHEMA_VERSION
from ..errors import ResumeReviewError
from ..storage import ensure_directory, sha256_file
from ..util import now_iso
from . import registry as registry_module
from . import workspace

__all__ = [
    "MANIFEST_VERSION",
    "ManifestVerdict",
    "DeployResult",
    "build_manifest",
    "compute_bundle_hash",
    "verify_manifest",
    "deploy_bundle",
    "verify_deployed",
    "bundle_file_paths",
]

MANIFEST_VERSION = "1.0"

#: The deployed copy of the manifest itself. It is generated at deploy time and so
#: is never part of the source bundle it describes; verification skips it.
SELF_MANIFEST_REL = "manifest.json"

#: Directories that are build artefacts rather than reviewed release content.
_EXCLUDED_DIRS = frozenset({"__pycache__", ".git", ".svn", ".hg", ".mypy_cache", ".pytest_cache"})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")

#: Extensions treated as executable content inside a bundle.
_EXECUTABLE_SUFFIXES = (".py", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".js")


class ManifestError(ResumeReviewError):
    """A manifest is malformed, missing, or does not match the deployed bundle."""

    code = "MANIFEST_MISMATCH"
    http_status = 409


@dataclass
class ManifestVerdict:
    """Outcome of comparing a directory against an expected manifest.

    ``ok`` is the only field callers should branch on. The three lists name every
    file that is missing, extra, or modified so an operator can repair precisely
    rather than reinstalling blindly.
    """

    ok: bool
    actual_bundle_hash: str = ""
    expected_bundle_hash: str | None = None
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add_issue(self, code: str, reason: str, **detail: Any) -> None:
        self.issues.append({"code": code, "reason": reason, **detail})
        self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "actual_bundle_hash": self.actual_bundle_hash,
            "expected_bundle_hash": self.expected_bundle_hash,
            "missing": list(self.missing),
            "extra": list(self.extra),
            "modified": list(self.modified),
            "issues": list(self.issues),
        }

    def summary(self) -> str:
        if self.ok:
            return "The deployed bundle matches its trusted manifest."
        return (
            f"{len(self.missing)} missing, {len(self.extra)} extra, "
            f"{len(self.modified)} modified file(s); a controlled repair is required."
        )


@dataclass
class DeployResult:
    deployed_path: Path
    file_count: int
    bundle_hash: str
    manifest_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployed_path": str(self.deployed_path),
            "file_count": self.file_count,
            "bundle_hash": self.bundle_hash,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
        }


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def bundle_file_paths(bundle_dir: str | os.PathLike[str]) -> list[Path]:
    """Every reviewable file in ``bundle_dir``, sorted by bundle-relative path.

    Sorted order is not cosmetic: ``bundle_hash`` is defined over the sorted path
    list, so an unstable walk order would produce a different hash for an identical
    bundle. Build artefacts and VCS control directories are excluded because they
    are not part of the reviewed release.
    """
    root = Path(bundle_dir)
    if not root.is_dir():
        raise ManifestError(
            "The approved application bundle is missing from this installation.",
            code="MANIFEST_MISSING",
            detail={"reason": "bundle_dir_absent"},
        )
    found: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for name in filenames:
            if name.endswith(_EXCLUDED_SUFFIXES):
                continue
            found.append(Path(current) / name)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def compute_bundle_hash(files: Iterable[dict[str, Any]]) -> str:
    """sha256 over sorted ``"path\\0sha256\\n"`` lines (see manifest schema).

    The separator is a NUL rather than a space so a path containing whitespace
    cannot be made to alias a different path/hash pair.
    """
    from ..models import sha256_hex

    lines = sorted(f"{entry['path']}\0{entry['sha256']}\n" for entry in files)
    return sha256_hex("".join(lines))


def _mode_for(rel: str) -> str:
    if rel.startswith("assets/") or rel.startswith("templates/"):
        return "asset"
    if rel.lower().endswith(_EXECUTABLE_SUFFIXES):
        return "executable"
    return "data"


def build_manifest(
    bundle_dir: str | os.PathLike[str],
    *,
    app_version: str = APP_VERSION,
    schema_version: int = SCHEMA_VERSION,
    python_requires: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Produce a manifest dict matching ``schemas/manifest.schema.json``."""
    root = Path(bundle_dir)
    files: list[dict[str, Any]] = []
    for path in bundle_file_paths(root):
        rel = path.relative_to(root).as_posix()
        files.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "mode": _mode_for(rel),
            }
        )
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "app_version": app_version,
        "schema_version": schema_version,
        "created_at": created_at or now_iso(),
        "files": files,
        "bundle_hash": compute_bundle_hash(files),
    }
    if python_requires:
        manifest["python_requires"] = python_requires
    _validate_manifest_shape(manifest)
    return manifest


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    """Structural check of a manifest we are about to trust.

    ``jsonschema`` is a test-time dependency, not a runtime one, so the checks that
    matter for safety are written out here: a missing hash must never be treated as
    a match.
    """
    if not isinstance(manifest, dict):
        raise ManifestError(
            "The trusted manifest is not an object.", code="MANIFEST_UNTRUSTED"
        )
    for key in ("manifest_version", "app_version", "schema_version", "files", "bundle_hash"):
        if key not in manifest:
            raise ManifestError(
                "The trusted manifest is incomplete.",
                code="MANIFEST_UNTRUSTED",
                detail={"missing_key": key},
            )
    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise ManifestError(
            "The trusted manifest uses an unsupported version.",
            code="MANIFEST_UNTRUSTED",
            detail={"manifest_version": str(manifest["manifest_version"])},
        )
    if not isinstance(manifest["files"], list):
        raise ManifestError("The trusted manifest files list is malformed.", code="MANIFEST_UNTRUSTED")
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("sha256"):
            raise ManifestError(
                "A trusted manifest entry is malformed.",
                code="MANIFEST_UNTRUSTED",
                detail={"reason": "entry_shape"},
            )


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------
def verify_manifest(
    bundle_dir: str | os.PathLike[str],
    expected: dict[str, Any],
    *,
    exclude: Iterable[str] = (),
) -> ManifestVerdict:
    """Compare a directory against ``expected``, then verify the bundle hash.

    Reports every file that is missing, extra, or modified instead of stopping at
    the first difference, because a controlled repair needs the complete list.

    ``exclude`` names bundle-relative files that are not part of the described set;
    deployment uses it for the manifest that is written alongside the bundle.
    """
    _validate_manifest_shape(expected)
    root = Path(bundle_dir)
    excluded = set(exclude)
    verdict = ManifestVerdict(
        ok=True,
        expected_bundle_hash=str(expected.get("bundle_hash", "")),
    )

    expected_files = {str(e["path"]): e for e in expected["files"]}
    actual_files: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for path in bundle_file_paths(root):
            rel = path.relative_to(root).as_posix()
            if rel in excluded:
                continue
            actual_files[rel] = {"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size}
    else:
        # A missing directory is not "everything modified"; it is everything missing.
        verdict.add_issue("MANIFEST_MISSING", "not_deployed")

    for rel, entry in sorted(expected_files.items()):
        actual = actual_files.get(rel)
        if actual is None:
            verdict.missing.append(rel)
            continue
        if actual["sha256"] != entry["sha256"]:
            verdict.modified.append(rel)

    for rel in sorted(actual_files):
        if rel not in expected_files:
            verdict.extra.append(rel)

    verdict.actual_bundle_hash = compute_bundle_hash(list(actual_files.values()))

    # Recompute what the expected bundle hash should be. If the manifest's own hash
    # field does not match its file list, the manifest was edited and is untrusted.
    recomputed_expected = compute_bundle_hash(list(expected_files.values()))
    if recomputed_expected != verdict.expected_bundle_hash:
        verdict.add_issue("MANIFEST_UNTRUSTED", "bundle_hash_does_not_cover_file_list")

    if verdict.missing or verdict.extra or verdict.modified:
        verdict.ok = False

    return verdict


def verify_deployed(
    root: str | os.PathLike[str],
    registry: registry_module.HostRegistry,
    instance_id: str | None = None,
) -> ManifestVerdict:
    """Check ``.review/app/`` against the trusted manifest held in the registry.

    This is the call setup makes before launching anything. It returns a verdict
    rather than raising so the caller can decide between blocking and reporting a
    repair requirement; ``ok`` is false when the bundle is not deployed at all.
    """
    resolved_id = instance_id
    if resolved_id is None:
        entry = registry.find_by_root(root)
        resolved_id = entry.instance_id if entry else None

    app = workspace.app_dir(root)
    if not app.is_dir():
        verdict = ManifestVerdict(ok=False, expected_bundle_hash=None)
        verdict.add_issue("MANIFEST_MISSING", "bundle_not_deployed")
        return verdict
    if resolved_id is None:
        verdict = ManifestVerdict(ok=False, expected_bundle_hash=None)
        verdict.add_issue("MANIFEST_MISSING", "instance_not_registered")
        return verdict

    trusted = registry.get_trusted_manifest(resolved_id)
    if trusted is None:
        verdict = ManifestVerdict(ok=False, expected_bundle_hash=None)
        verdict.add_issue("MANIFEST_MISSING", "no_trusted_manifest")
        return verdict

    return verify_manifest(app, trusted, exclude=(SELF_MANIFEST_REL,))


# ---------------------------------------------------------------------------
# Deploying
# ---------------------------------------------------------------------------
def deploy_bundle(
    bundle_dir: str | os.PathLike[str],
    root: str | os.PathLike[str],
    *,
    registry: registry_module.HostRegistry,
    instance_id: str,
    app_version: str = APP_VERSION,
    schema_version: int = SCHEMA_VERSION,
) -> DeployResult:
    """Copy the reviewed bundle into ``.review/app/`` and verify what landed.

    Order is the point: the manifest is recorded in the host registry *first*, so
    the freshly copied files are checked against a trusted record rather than
    against themselves. Writing each file through a temp-and-rename keeps a reader
    from ever seeing a half-copied executable.
    """
    source = Path(bundle_dir)
    manifest = build_manifest(source, app_version=app_version, schema_version=schema_version)
    registry.set_trusted_manifest(instance_id, manifest, canonical_root=root)

    target_root = workspace.app_dir(root)
    ensure_directory(target_root)

    destination = target_root / "manifest.json"
    for path in bundle_file_paths(source):
        rel = path.relative_to(source).as_posix()
        out = target_root.joinpath(*rel.split("/"))
        ensure_directory(out.parent)
        _copy_file_atomic(path, out)

    _write_json_atomic(destination, manifest)

    verdict = verify_manifest(target_root, manifest, exclude=(SELF_MANIFEST_REL,))
    if not verdict.ok:
        raise ManifestError(
            "The deployed application bundle could not be verified after copying. "
            "Execution is blocked until an operator performs a controlled repair.",
            code="MANIFEST_MISMATCH",
            detail=verdict.to_dict(),
        )

    return DeployResult(
        deployed_path=target_root,
        file_count=len(manifest["files"]),
        bundle_hash=manifest["bundle_hash"],
        manifest_path=destination,
    )


def _copy_file_atomic(source: Path, destination: Path) -> None:
    data = source.read_bytes()
    _write_bytes_atomic(destination, data)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to its own file via a sibling temp file and a rename.

    ``O_BINARY`` is mandatory on Windows: the CRT defaults ``os.open`` to text mode,
    which would translate every ``\\n`` in a copied executable or template into
    ``\\r\\n`` and make the deployed bytes differ from the reviewed ones. That would
    read as tampering on the very next verification.
    """
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
