"""Opt-in gate for the real-resume-corpus suite.

The default ``pytest`` run is synthetic-only and must never read ``resume/``.
The tests under ``tests/corpus/`` read the real HuggingFace resume datasets the
repository keeps (gitignored) under ``resume/``. They are opt-in per run, and
this conftest is the single gate that enforces it.

Behaviour, exactly:

* ``RESUME_REVIEW_REAL_CORPUS`` unset (or not truthy) -- every test in this
  directory is collected and skipped with a visible reason. The corpus is never
  opened. The run reports skips, never a silent zero-collection, and never a
  failure.
* flag truthy but no dataset files found under ``resume/`` -- the tests here fail
  loudly, naming the fetch command ``python resume/_download.py``. Tests outside
  this directory are unaffected.
* flag truthy and the corpus is present -- the tests run, bounded by the sample
  sizes and time budget declared in ``test_corpus_smoke.py``.

The gate is a fixture declared in this conftest, not a session-wide
``pytest_collection_modifyitems`` hook: a hook defined in any conftest receives
every collected item for the run, which would have skipped the whole suite. A
conftest-scoped fixture applies only to the tests under this directory.

The gate deliberately does not live in ``tests/conftest.py``: that shared file
carries a hard-won Windows temp-root workaround owned by another concern, and
coupling the default suite to corpus machinery would disturb it.

Never write real corpus text into a committed fixture, golden file, snapshot, or
report. Tests read it at runtime only. See ``docs/corpus.md``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Environment flag that opts a run into the real corpus.
FLAG_ENV = "RESUME_REVIEW_REAL_CORPUS"

_TRUTHY = {"1", "true", "yes", "on"}

#: Repository root: tests/corpus/conftest.py -> tests/corpus -> tests -> repo.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The gitignored real-corpus tree (see .gitignore and docs/corpus.md).
CORPUS_ROOT = REPO_ROOT / "resume"

#: Known dataset directories under CORPUS_ROOT. A directory counts as "present"
#: only when it exists and holds at least one regular file, so the loose
#: README/_download.py at the corpus root cannot by itself satisfy the gate.
DATASET_DIRS: tuple[str, ...] = (
    "resume-atlas",
    "resume-real-pdfs-2480",
    "resume-raw-pdf-1940",
    "resume-jd-fit",
    "resume-mixed-real-synthetic",
    "resume-livecareer-strings",
    "resume-clean-2482",
    "popresume-text",
)

_SKIP_REASON = (
    f"real-corpus suite is opt-in: set {FLAG_ENV}=1 to run it against {CORPUS_ROOT} "
    "(see docs/corpus.md). The default suite is synthetic-only."
)


def flag_enabled() -> bool:
    """True when the opt-in flag is set to a recognised truthy value."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY


def corpus_ready() -> bool:
    """True when a known dataset directory exists and holds a regular file.

    The scan is bounded: it stops at the first regular file in each dataset
    directory, so it never walks the whole corpus tree.
    """
    if not CORPUS_ROOT.is_dir():
        return False
    for name in DATASET_DIRS:
        dataset = CORPUS_ROOT / name
        if not dataset.is_dir():
            continue
        try:
            for child in dataset.rglob("*"):
                if child.is_file():
                    return True
        except OSError:
            continue
    return False


def missing_message() -> str:
    """The loud operator message for flag-set-but-corpus-absent."""
    return (
        f"{FLAG_ENV} is set but no real corpus was found under {CORPUS_ROOT}. "
        "Fetch it first: python resume/_download.py  (then re-run; see docs/corpus.md)."
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``corpus`` marker.

    ``pyproject.toml`` is the authoritative marker registry; this is a safety net
    so ``pytest tests/corpus`` also works under an isolated config. Registering an
    already-registered marker is harmless.
    """
    config.addinivalue_line(
        "markers",
        "corpus: opt-in real-corpus suite that reads real people's resumes; never run by default",
    )


@pytest.fixture(autouse=True)
def _real_corpus_gate() -> None:
    """Skip this directory's tests when the flag is unset; fail loudly when the
    flag is set but the corpus is absent. Scoped to ``tests/corpus/`` only."""
    if not flag_enabled():
        pytest.skip(_SKIP_REASON)
    if not corpus_ready():
        pytest.fail(missing_message(), pytrace=False)


@pytest.fixture(scope="session")
def corpus_root() -> Path:
    """The real-corpus root, proven ready. Belt-and-braces re-check of the gate
    for any test that needs the path itself."""
    if not flag_enabled():
        pytest.skip(_SKIP_REASON)
    if not corpus_ready():
        pytest.fail(missing_message(), pytrace=False)
    return CORPUS_ROOT
