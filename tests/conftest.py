"""Shared pytest fixtures.

The default test suite is synthetic-only. A separate, strictly opt-in suite under
``tests/corpus/`` reads the real-resume corpus kept (gitignored) under ``resume/``;
it runs only when ``RESUME_REVIEW_REAL_CORPUS`` is set, and its gate lives in
``tests/corpus/conftest.py`` (see ``docs/corpus.md``). Real applicant records,
credentials, and Gateway tokens must never appear in this repository or in any
committed fixture, golden file, snapshot, or report (PRD section 20).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _can_create_under(path: Path) -> bool:
    """Whether directories could be created under ``path``.

    Deliberately does **not** create anything: a directory left with a broken
    ACL can make ``CreateDirectory`` block indefinitely rather than fail, so
    probing by mkdir risks hanging the whole suite. Listing and a write-access
    check catch the same faults without that risk.
    """
    try:
        if path.exists():
            with os.scandir(path) as entries:
                next(iter(entries), None)
            return os.access(path, os.W_OK)
        parent = path.parent
        return parent.is_dir() and os.access(parent, os.W_OK)
    except OSError:
        return False


def _ensure_usable_temp_root() -> None:
    """Fall back to a repository-local temp root when the system one is unusable.

    pytest keeps its per-session directories under ``<tmp>/pytest-of-<user>/``
    and lists that directory on every run. If a previous run left it with an ACL
    this account cannot read -- a real scenario on Windows when concurrent
    sandboxed runs collide -- every test errors at fixture setup, which reads as
    a failing suite rather than an environment fault. Redirecting keeps the
    failure honest: the tests still run, on a root we have proven we can use.
    """
    system_root = Path(tempfile.gettempdir())
    # Rebuild exactly the directory name pytest uses, without its private API.
    try:
        import getpass

        user = getpass.getuser()
    except Exception:  # pragma: no cover - getpass only fails without a login name
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    pytest_root = system_root / f"pytest-of-{user}"

    if _can_create_under(pytest_root):
        return

    local_root = REPO_ROOT / ".pytest-tmp"
    if not _can_create_under(local_root):
        return  # Nothing we can do; let pytest report the real error.
    # pytest assumes its temp root already exists; it only creates its own
    # numbered children underneath it.
    local_root.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(local_root)
    sys.stderr.write(
        f"tests/conftest.py: {str(pytest_root)!r} is not usable "
        f"(stale ACL or permissions); using {str(local_root)!r} instead.\n"
    )


_ensure_usable_temp_root()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Iterator[Path]:
    """A synthetic job folder with the reserved subdirectories present."""
    root = tmp_path / "Job - Operations Manager"
    root.mkdir(parents=True)
    yield root
    # Nothing to clean: tmp_path is managed by pytest.


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Snapshot and restore the environment so a test cannot leak settings."""
    saved = dict(os.environ)
    try:
        yield None
    finally:
        os.environ.clear()
        os.environ.update(saved)
