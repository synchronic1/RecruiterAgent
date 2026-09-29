"""Folder layout, path constants, and collision-safe adoption checks.

Authority: PRD section 4 ("Folder layout, ownership, and portability") and
acceptance test AT-02.

The folder *is* the instance. Provisioning it is therefore the one operation that
must never guess: taking over a folder that already belongs to something else, or
overwriting an unrelated ``review.html``, destroys data that was never ours.

Two rules follow from that and are implemented here:

* ``ensure_layout`` creates exactly the reserved names the PRD lists and nothing
  else, so a folder we provision is recognisable and no stray name is claimed.
* Adoption requires positive proof of ownership -- ``.review/instance.json`` with
  our app marker and the expected instance id. Absence of proof is a conflict, not
  permission.

``.review/instance.json`` and ``.review/job.json`` are application-generated
manifests. Editing them externally does not change authoritative state, which
lives in the database (PRD section 4).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import ResumeReviewError
from ..models import (
    REPORT_FILENAME,
    REJECTED_DIR,
    REVIEW_DIR,
    REVIEW_SUBDIRS,
    TRASH_DIR,
)
from ..storage import ensure_directory

__all__ = [
    "APP_MARKER",
    "INSTANCE_MANIFEST_VERSION",
    "MANIFEST_MARKER_KEY",
    "CollisionError",
    "CollisionReport",
    "instance_manifest_path",
    "job_manifest_path",
    "db_path",
    "report_path",
    "review_dir",
    "rejected_dir",
    "trash_dir",
    "app_dir",
    "extracted_dir",
    "journal_path",
    "backups_dir",
    "exports_dir",
    "locks_dir",
    "owner_lock_path",
    "tmp_dir",
    "read_instance_manifest",
    "recognised_instance_id",
    "is_recognisably_ours",
    "assert_no_collision",
    "ensure_layout",
]

#: Value of the ``app`` key inside ``instance.json``. A foreign folder that happens
#: to contain a file called ``instance.json`` must not be mistaken for ours.
APP_MARKER = "resume-review"
#: Bumped when the shape of ``instance.json``/``job.json`` changes.
INSTANCE_MANIFEST_VERSION = "1.0"

#: Key holding the marker inside the generated manifests.
MANIFEST_MARKER_KEY = "app"

INSTANCE_FILENAME = "instance.json"
JOB_FILENAME = "job.json"
DB_FILENAME = "review.db"


class CollisionError(ResumeReviewError):
    """Refusal to adopt a folder that is not recognisably this application's."""

    code = "SETUP_COLLISION"
    http_status = 409


class CollisionReport:
    """Names the conflicting reserved entries so the operator can see why.

    Only reserved names and a reason code appear here: the message is safe to log
    and never contains an absolute path or applicant data.
    """

    def __init__(self, conflicts: list[dict[str, str]] | None = None) -> None:
        self.conflicts: list[dict[str, str]] = list(conflicts or [])

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def add(self, name: str, reason: str) -> None:
        self.conflicts.append({"name": name, "reason": reason})

    @property
    def names(self) -> list[str]:
        return [c["name"] for c in self.conflicts]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "conflicts": list(self.conflicts)}

    def raise_if_any(self) -> None:
        if self.conflicts:
            raise CollisionError(
                "This folder already contains entries this application did not create, "
                "so it will not be adopted. Move the folder's contents aside or choose "
                "an empty folder.",
                detail={"conflicts": list(self.conflicts)},
            )


# ---------------------------------------------------------------------------
# Path constants (PRD section 4)
# ---------------------------------------------------------------------------
def review_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / REVIEW_DIR


def rejected_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / REJECTED_DIR


def trash_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / TRASH_DIR


def app_dir(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / "app"


def db_path(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / DB_FILENAME


def report_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / REPORT_FILENAME


def extracted_dir(root: str | os.PathLike[str], document_id: str) -> Path:
    """Extraction cache directory for one document: ``.review/extracted/<id>/``.

    The document id is a validated opaque identifier from :mod:`resume_review.util`,
    never a filename taken from the applicant's document.
    """
    return review_dir(root) / "extracted" / _safe_id(document_id)


def journal_path(root: str | os.PathLike[str], batch_id: str) -> Path:
    return review_dir(root) / "journals" / f"{_safe_id(batch_id)}.json"


def backups_dir(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / "backups"


def exports_dir(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / "exports"


def locks_dir(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / "locks"


def owner_lock_path(root: str | os.PathLike[str]) -> Path:
    """The single-writer lock file. Held through an OS lock, not a PID file."""
    return locks_dir(root) / "owner.lock"


def tmp_dir(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / "tmp"


def instance_manifest_path(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / INSTANCE_FILENAME


def job_manifest_path(root: str | os.PathLike[str]) -> Path:
    return review_dir(root) / JOB_FILENAME


def _safe_id(value: str) -> str:
    """Reduce an identifier to a single safe path component.

    Identifiers are generated by the application, but they arrive from a database
    row or a caller, so they are re-validated rather than trusted to be clean.
    """
    cleaned = str(value).replace("\\", "_").replace("/", "_").replace(":", "_").strip()
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    if not cleaned or cleaned in (".", ".."):
        raise ResumeReviewError(
            "An identifier was not usable as a path component.",
            code="INVALID_INPUT",
            http_status=422,
        )
    return cleaned[:128]


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
def read_instance_manifest(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read ``.review/instance.json``, returning ``None`` when absent or unreadable.

    A malformed manifest is not ours; the caller treats ``None`` as "no proof of
    ownership" rather than repairing it in place.
    """
    path = instance_manifest_path(root)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get(MANIFEST_MARKER_KEY) != APP_MARKER:
        return None
    return data


def recognised_instance_id(root: str | os.PathLike[str]) -> str | None:
    """The instance id this folder proves it belongs to, or ``None``.

    Both halves are required: the application marker and a non-empty instance id.
    An empty id means the manifest was hand-edited into something we cannot trust.
    """
    data = read_instance_manifest(root)
    if data is None:
        return None
    instance_id = data.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    return instance_id


def is_recognisably_ours(root: str | os.PathLike[str], *, instance_id: str | None = None) -> bool:
    """True when the folder carries our manifest and, if asked, the expected id."""
    found = recognised_instance_id(root)
    if found is None:
        return False
    if instance_id is not None and found != instance_id:
        return False
    return True


# ---------------------------------------------------------------------------
# Collision safety (AT-02)
# ---------------------------------------------------------------------------
def assert_no_collision(
    root: str | os.PathLike[str],
    *,
    requested_instance_id: str | None = None,
) -> CollisionReport:
    """Refuse destructive adoption; return an empty report when safe to proceed.

    The check is deliberately conservative: any of the reserved names present
    without our ownership manifest is a conflict, because we cannot tell an empty
    ``Rejected/`` an operator made by hand from one that already holds moved files.

    When a manifest is present but carries a *different* instance id than the one
    requested, that is also a conflict: binding two ids to one folder would make
    the folder's identity ambiguous.
    """
    root_path = Path(root)
    report = CollisionReport()

    ours = recognised_instance_id(root_path)

    if root_path.exists() and not root_path.is_dir():
        raise ResumeReviewError(
            "The selected path exists but is not a folder.",
            code="INVALID_INPUT",
            http_status=422,
            detail={"reason": "not_a_directory"},
        )

    review = review_dir(root_path)
    if review.exists() and not review.is_dir():
        report.add(REVIEW_DIR, "not_a_directory")
    elif review.exists() and ours is None:
        # A .review directory we did not create. Never merge with it.
        report.add(REVIEW_DIR, "unrecognised")

    if ours is not None and requested_instance_id is not None and ours != requested_instance_id:
        report.add(REVIEW_DIR, "instance_id_mismatch")

    for name, path in ((REJECTED_DIR, rejected_dir(root_path)), (TRASH_DIR, trash_dir(root_path))):
        if not path.exists():
            continue
        if not path.is_dir():
            report.add(name, "not_a_directory")
        elif ours is None:
            report.add(name, "unrecognised")

    report_file = report_path(root_path)
    if report_file.exists():
        if not report_file.is_file():
            report.add(REPORT_FILENAME, "not_a_regular_file")
        elif ours is None:
            # An unrelated review.html must never be overwritten (PRD section 4).
            report.add(REPORT_FILENAME, "unrecognised")

    return report


# ---------------------------------------------------------------------------
# Layout provisioning
# ---------------------------------------------------------------------------
def ensure_layout(root: str | os.PathLike[str]) -> list[Path]:
    """Create the PRD section 4 layout, and nothing beyond it.

    Returns the directories created, so setup can report what it claimed. The
    caller must have already run :func:`assert_no_collision`; this function
    creates missing directories only and never removes or replaces anything.
    """
    root_path = Path(root)
    created: list[Path] = []
    wanted: list[Path] = [root_path, review_dir(root_path)]
    wanted.extend(review_dir(root_path) / Path(sub) for sub in REVIEW_SUBDIRS)
    wanted.append(rejected_dir(root_path))
    wanted.append(trash_dir(root_path))

    for directory in wanted:
        existed = directory.exists()
        ensure_directory(directory)
        if not existed:
            created.append(directory)
    return created
