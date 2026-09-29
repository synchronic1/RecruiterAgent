"""Copy the reviewed report assets from ``web/`` into their two packaged trees.

``web/`` is the single source of truth for the report page, its stylesheet and its
client module. Two packaged copies are derived from it and must stay
byte-identical, because they are what an installed build deploys:

* ``src/resume_review/templates/*`` -- the flat set the reporting renderer
  resolves by name (``reporting/snapshot.py``, ``ASSET_FILENAMES``).
* ``src/resume_review/bundle/`` -- the nested tree a wheel deploys into
  ``.review/app/``. It keeps the ``assets/`` + ``templates/`` layout that
  ``report.html``'s relative links require; see ``bootstrap/setup.py``.

Why this exists: the copies are committed rather than generated, so every edit to
``web/`` has to be mirrored by hand. That has now drifted twice -- the suite goes
red on ``tests/unit/test_packaging.py`` and the installed page serves stale bytes
-- and the failure is easy to misread as a defect in the assets themselves rather
than a missing copy step. Run this after any change to ``web/``:

    ./.venv/Scripts/python.exe tools/sync_web_assets.py
    ./.venv/Scripts/python.exe -m pytest tests/unit/test_packaging.py

It is idempotent: with the trees already in step it copies nothing and reports no
changes. Exit status is 1 if a file could not be verified after copying.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "web"
PACKAGE = REPO_ROOT / "src" / "resume_review"

#: (source under web/, packaged destinations). Kept in step with REPORT_ASSETS in
#: tests/unit/test_packaging.py, which asserts the same equality from the other side.
COPIES: tuple[tuple[Path, tuple[Path, ...]], ...] = (
    (
        WEB / "templates" / "report.html",
        (PACKAGE / "templates" / "report.html", PACKAGE / "bundle" / "templates" / "report.html"),
    ),
    (
        WEB / "assets" / "report.css",
        (PACKAGE / "templates" / "report.css", PACKAGE / "bundle" / "assets" / "report.css"),
    ),
    (
        WEB / "assets" / "report.js",
        (PACKAGE / "templates" / "report.js", PACKAGE / "bundle" / "assets" / "report.js"),
    ),
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    changed = 0
    checked = 0
    for source, destinations in COPIES:
        if not source.is_file():
            print(f"FAIL missing source: {source}")
            return 1
        source_digest = _digest(source)
        for destination in destinations:
            checked += 1
            if destination.is_file() and _digest(destination) == source_digest:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            changed += 1
            print(f"copied {source.relative_to(REPO_ROOT)} -> {destination.relative_to(REPO_ROOT)}")

    # Re-verify rather than trusting the copy above: a partial write or a file held
    # open by another process is exactly what this script exists to catch.
    for source, destinations in COPIES:
        for destination in destinations:
            if _digest(destination) != _digest(source):
                print(f"FAIL did not verify: {destination}")
                return 1

    print(f"{checked} packaged files checked, {changed} updated, all verified identical to web/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
