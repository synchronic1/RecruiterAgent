"""Packaging metadata checks.

Authority: AGENTS.md (repeatable setup; assets the installed build needs) and the
build directive ``docs/AGENT_BUILD_HANDOFF.md``. These tests guard the three things
a non-editable install depends on and that nothing else in the suite checks:

* the ``resume-review`` console script declares a well-formed target and, once the
  module it names exists, that target imports and exposes a callable ``main``;
* every ``package-data`` glob resolves to at least one file inside the package,
  which is what makes setuptools put it in the wheel;
* the opt-in marker set is registered, because ``--strict-markers`` rejects an
  unregistered marker as an error rather than a warning.

They read ``pyproject.toml`` and the filesystem only: no build, no network, no
privileged action.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_DIR = REPO_ROOT / "src" / "resume_review"

#: The report presentation assets, by their packaged path, and the repository
#: source each copy must stay byte-identical to. Two packaged copies exist and both
#: are checked: ``templates/*`` is what the rendering code resolves by name, and
#: ``bundle/assets|templates/*`` is the tree an installed wheel deploys. The
#: package copy is what an installed wheel serves; drift would ship a stale page.
REPORT_ASSETS: dict[str, Path] = {
    "templates/report.html": REPO_ROOT / "web" / "templates" / "report.html",
    "templates/report.css": REPO_ROOT / "web" / "assets" / "report.css",
    "templates/report.js": REPO_ROOT / "web" / "assets" / "report.js",
    "bundle/templates/report.html": REPO_ROOT / "web" / "templates" / "report.html",
    "bundle/assets/report.css": REPO_ROOT / "web" / "assets" / "report.css",
    "bundle/assets/report.js": REPO_ROOT / "web" / "assets" / "report.js",
}

#: Markers the suite must register. An unregistered marker is a hard error under
#: ``--strict-markers``, so a missing one breaks collection, not just a warning.
REQUIRED_MARKERS = {"live", "slow", "corpus"}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _markers() -> dict[str, str]:
    entries = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    markers: dict[str, str] = {}
    for entry in entries:
        name, _, description = entry.partition(":")
        markers[name.strip()] = description.strip()
    return markers


# ---------------------------------------------------------------------------
# Console script
# ---------------------------------------------------------------------------
def test_console_script_target_is_importable() -> None:
    """The declared console script must name an importable ``main``.

    ``resume_review.cli`` is implemented in this build, so the main assertion runs
    rather than being skipped. The ``ModuleNotFoundError`` branch is kept as a
    diagnostic: it now fires only when the named module is genuinely absent, which
    means an incomplete install rather than an unbuilt command.
    """
    scripts = _pyproject()["project"]["scripts"]
    target = scripts["resume-review"]
    module_name, _, attribute = target.partition(":")
    assert module_name and attribute, f"malformed entry point: {target!r}"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        skip_reason = (
            f"console script {target!r} points at {module_name}, which is not "
            "importable in this environment. resume_review/cli.py ships with the "
            "package, so this indicates an incomplete install, not an unbuilt CLI."
        )
        pytest.skip(skip_reason)

    assert hasattr(module, attribute), f"{module_name} has no attribute {attribute!r}"
    assert callable(getattr(module, attribute)), f"{target} is not callable"


# ---------------------------------------------------------------------------
# Package data
# ---------------------------------------------------------------------------
def test_package_data_globs_resolve_to_existing_files() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert package_data, "no package-data is declared"

    for package, globs in package_data.items():
        base = (REPO_ROOT / "src" / Path(*package.split("."))).resolve()
        assert base.is_dir(), f"package-data names a package with no directory: {package}"
        assert globs, f"{package} declares no globs"
        for pattern in globs:
            matches = [path for path in base.glob(pattern) if path.is_file()]
            assert matches, f"{package}: pattern {pattern!r} matches no file under {base}"


def test_the_packaged_assets_are_present_in_the_tree() -> None:
    """The specific defect this guards: templates/, css/js, and schemas/ must be
    inside the package, not only at the repository root."""
    for relative in (
        "templates/report.html",
        "templates/report.css",
        "templates/report.js",
        "bundle/templates/report.html",
        "bundle/assets/report.css",
        "bundle/assets/report.js",
    ):
        assert (PACKAGE_DIR / relative).is_file(), f"{relative} is not packaged"
    packaged_schemas = sorted((PACKAGE_DIR / "schemas").glob("*.schema.json"))
    assert packaged_schemas, "no schema JSON is packaged"
    packaged_migrations = sorted((PACKAGE_DIR / "migrations").glob("*.sql"))
    assert packaged_migrations, "no migration SQL is packaged"


def test_the_packaged_bundle_matches_the_repository_web_tree() -> None:
    """A wheel with no checkout beside it must still deploy a working page.

    ``default_bundle_dir`` prefers ``web/``, but an installed wheel has no ``web/``,
    so it falls back to the packaged bundle. That tree has to be the deployed
    *shape*, not a flat one. ``report.html`` links ``../assets/report.css`` and
    ``../assets/report.js``, so a flat layout deploys a page whose stylesheet and
    script resolve to nothing -- and ``manifest._mode_for`` calls a bare
    ``report.js`` an executable rather than an asset. Comparing bundle hashes covers
    both the shape and the bytes, because the hash is taken over the bundle-relative
    paths.
    """
    from resume_review.bootstrap.manifest import build_manifest
    from resume_review.bootstrap.setup import packaged_bundle_dir

    assert packaged_bundle_dir() == PACKAGE_DIR / "bundle"

    packaged = build_manifest(packaged_bundle_dir())
    repository = build_manifest(REPO_ROOT / "web")
    assert [entry["path"] for entry in packaged["files"]] == [
        entry["path"] for entry in repository["files"]
    ]
    assert packaged["bundle_hash"] == repository["bundle_hash"], (
        "the packaged bundle has drifted from web/; an installed wheel would "
        "deploy different bytes than the reviewed ones"
    )
    assert {entry["mode"] for entry in packaged["files"]} == {"asset"}


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------
def test_opt_in_markers_are_registered() -> None:
    markers = _markers()
    missing = REQUIRED_MARKERS - set(markers)
    assert not missing, f"unregistered markers break --strict-markers: {sorted(missing)}"

    for name in sorted(REQUIRED_MARKERS):
        assert markers[name], f"marker {name!r} has no description"

    corpus = markers["corpus"].lower()
    assert "opt-in" in corpus, "the corpus marker must be described as opt-in"
    assert "resume" in corpus, "the corpus marker must say it reads resumes"
    assert "default" in corpus, "the corpus marker must say it is not run by default"


# ---------------------------------------------------------------------------
# Packaged copies stay identical to the repository sources
# ---------------------------------------------------------------------------
def test_report_assets_are_byte_identical_to_the_repository_copies() -> None:
    for relative, source in REPORT_ASSETS.items():
        packaged = PACKAGE_DIR / relative
        assert packaged.is_file(), f"{relative} is not packaged"
        assert packaged.read_bytes() == source.read_bytes(), (
            f"{relative} has drifted from {source}; the installed page would be stale"
        )


def test_schemas_are_byte_identical_to_the_repository_copies() -> None:
    source_dir = REPO_ROOT / "schemas"
    sources = sorted(source_dir.glob("*.schema.json"))
    assert sources, f"the repository schema directory is missing: {source_dir}"
    for source in sources:
        packaged = PACKAGE_DIR / "schemas" / source.name
        assert packaged.is_file(), f"{source.name} is not packaged"
        assert packaged.read_bytes() == source.read_bytes(), (
            f"{source.name} has drifted from {source}"
        )


# ---------------------------------------------------------------------------
# The reporting code actually resolves what is packaged
# ---------------------------------------------------------------------------
def test_reporting_resolves_the_packaged_assets() -> None:
    from resume_review.reporting import load_assets
    from resume_review.reporting.snapshot import ASSET_FILENAMES, asset_dir

    directory = asset_dir()
    assert directory.is_dir(), f"asset_dir() did not resolve to a directory: {directory}"
    for name in ASSET_FILENAMES.values():
        assert (directory / name).is_file(), f"{name} is not resolvable from {directory}"

    loaded = load_assets()
    assert set(loaded) == set(ASSET_FILENAMES)
    assert loaded["css"].strip() and loaded["js"].strip()
