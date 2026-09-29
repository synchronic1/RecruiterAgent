"""Layer-direction enforcement for :mod:`resume_review`.

AGENTS.md ("Rules for changes") states, verbatim::

    Layer direction is enforced. db and storage never import api. actions never
    imports openclaw_adapter. analysis never imports actions. Tests assert this
    (tests/unit/test_layering.py).

The package docstring in ``resume_review/__init__.py`` documents the same
intent: ``storage`` is the lowest layer (filesystem primitives, no-clobber
moves must stay dependency-free), ``db`` sits above it, and ``api`` is an entry
point that nothing below it may import.

This module does not grep for a handful of strings. It parses every ``.py`` file
under ``src/resume_review`` with :mod:`ast`, so it sees imports wherever they
appear: module level, inside functions, inside ``try``/``except``, inside
``if TYPE_CHECKING`` blocks, and relative imports resolved to absolute dotted
names. A forbidden edge is reported with the offending module, the offending
import target, and the source line.

The analyzer is itself exercised by negative-control tests below (a deliberate
violation must be flagged), so a broken analyzer cannot let these tests pass
vacuously. Cycle detection at module granularity is intentionally *not* asserted
here; AGENTS.md does not claim it, and a correct implementation is subtle.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SOURCE_ROOT / "resume_review"

# Number of modules under src/resume_review when this test was written. The
# walk below must not silently find zero files (an empty walk would make every
# assertion vacuous), so we pin a floor and fail loudly if it drops.
MINIMUM_MODULES = 54

# (importer package, package it must never import). Prefix match, so
# "resume_review.db.connection" counts as belonging to "resume_review.db" and
# "resume_review.api.routes" counts as belonging to "resume_review.api".
FORBIDDEN_EDGES: tuple[tuple[str, str], ...] = (
    # AGENTS.md, verbatim rules.
    ("resume_review.db", "resume_review.api"),
    ("resume_review.storage", "resume_review.api"),
    ("resume_review.actions", "resume_review.openclaw_adapter"),
    ("resume_review.analysis", "resume_review.actions"),
    # Implied by the architecture map: storage is the lowest layer, and its
    # no-clobber primitives are meant to depend on models/errors only.
    ("resume_review.storage", "resume_review.db"),
    ("resume_review.storage", "resume_review.actions"),
    # Implied by the architecture map: security is untrusted-content handling,
    # not an entry-point-facing layer.
    ("resume_review.security", "resume_review.api"),
    ("resume_review.security", "resume_review.actions"),
)


class ImportedName(NamedTuple):
    """One imported dotted target and the line it was written on."""

    target: str
    lineno: int


def module_name_for(path: Path) -> str:
    """Return the dotted module name for a file under ``src``."""
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """Resolve a relative import to an absolute dotted name.

    ``level`` is ``node.level`` from an ``ImportFrom`` (0 = absolute, in which
    case ``module`` is already absolute and must not be prefixed). For a regular
    module the caller passes its containing package; for a package's ``__init__``
    the package is the package itself.
    """
    if level == 0:
        return module or ""
    parts = package.split(".") if package else []
    if level:
        drop = level - 1
        if drop:
            parts = parts[: len(parts) - drop] if drop <= len(parts) else []
    if module:
        parts = parts + module.split(".")
    return ".".join(part for part in parts if part)


def collect_imports(
    source: str, module_name: str, *, is_package: bool = False
) -> list[ImportedName]:
    """Parse ``source`` and return every imported target it names.

    Handles ``import a.b.c``, ``from a.b import c, d`` (recording both
    ``a.b`` and ``a.b.c``/``a.b.d`` so ``from resume_review import api`` is
    caught), ``from . import x`` and ``from ..pkg import y`` relative forms, and
    star imports. Imports nested anywhere in the tree are visited.
    """
    package = module_name if is_package else module_name.rpartition(".")[0]
    tree = ast.parse(source, filename=module_name or "<memory>")
    found: list[ImportedName] = []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                found.append(ImportedName(alias.name, node.lineno))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = _resolve_relative(package, node.level, node.module)
            if base:
                found.append(ImportedName(base, node.lineno))
            for alias in node.names:
                if alias.name == "*":
                    continue
                member = ".".join(part for part in (base, alias.name) if part)
                if member:
                    found.append(ImportedName(member, node.lineno))

    Visitor().visit(tree)
    return found


def discovered_modules() -> dict[str, Path]:
    """Map every module under the package to its source path."""
    modules: dict[str, Path] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        modules[module_name_for(path)] = path
    return modules


@lru_cache(maxsize=None)
def build_import_graph() -> dict[str, tuple[ImportedName, ...]]:
    """Parse the whole package once and return module -> imported targets."""
    graph: dict[str, tuple[ImportedName, ...]] = {}
    for name, path in discovered_modules().items():
        source = path.read_text(encoding="utf-8")
        imports = collect_imports(source, name, is_package=path.name == "__init__.py")
        graph[name] = tuple(imports)
    return graph


def _under(name: str, root: str) -> bool:
    """Whether ``name`` is ``root`` or a dotted descendant of it."""
    return name == root or name.startswith(root + ".")


@pytest.mark.parametrize("importer_root, forbidden_root", FORBIDDEN_EDGES)
def test_forbidden_import_edges(importer_root: str, forbidden_root: str) -> None:
    """No module under ``importer_root`` may import ``forbidden_root``."""
    violations = [
        f"{module} (line {item.lineno}) imports {item.target}"
        for module, imported in build_import_graph().items()
        if _under(module, importer_root)
        for item in imported
        if _under(item.target, forbidden_root)
    ]
    assert not violations, (
        f"layer violation: {importer_root}.* must not import {forbidden_root}.* -- "
        + "; ".join(sorted(violations))
    )


def test_walk_finds_the_package() -> None:
    """The static walk must not be silently empty."""
    modules = discovered_modules()
    assert len(modules) >= MINIMUM_MODULES, (
        f"expected at least {MINIMUM_MODULES} modules under {PACKAGE_ROOT}, "
        f"found {len(modules)}: {sorted(modules)}"
    )
    # The layers the rules talk about must actually be present in the walk.
    for package in ("resume_review.db", "resume_review.storage", "resume_review.actions",
                    "resume_review.analysis", "resume_review.security"):
        assert any(_under(name, package) for name in modules), f"no modules found for {package}"


def test_graph_sees_nested_and_relative_imports() -> None:
    """The analyzer must see nested imports and resolve relative ones.

    A function-local relative import is the case a naive top-level grep misses.
    """
    source = "def _f():\n    from ..api import routes\n"
    imported = collect_imports(source, "resume_review.db.repository")
    targets = {item.target for item in imported}
    assert "resume_review.api" in targets
    assert "resume_review.api.routes" in targets

    # ``from resume_review import api`` names the subpackage only via a member
    # alias; it must still be recorded as ``resume_review.api``.
    imported = collect_imports("from resume_review import api\n", "resume_review.util")
    assert any(item.target == "resume_review.api" for item in imported)

    # Nested inside a try/except and a TYPE_CHECKING block.
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from resume_review.actions import executor\n"
        "try:\n"
        "    from resume_review.actions import planner\n"
        "except ImportError:\n"
        "    pass\n"
    )
    targets = {item.target for item in collect_imports(source, "resume_review.analysis.chat")}
    assert "resume_review.actions" in targets
    assert "resume_review.actions.executor" in targets
    assert "resume_review.actions.planner" in targets


@pytest.mark.parametrize("importer_root, forbidden_root", FORBIDDEN_EDGES)
def test_detector_flags_a_deliberate_violation(
    importer_root: str, forbidden_root: str
) -> None:
    """Negative control: the analyzer must flag a planted forbidden import.

    Without this, a checker that returned nothing would make every rule above
    pass forever. Each rule is proven to be able to fail.
    """
    module = f"{importer_root}._probe"
    source = f"from {forbidden_root} import thing\n"
    imported = collect_imports(source, module)
    assert any(_under(item.target, forbidden_root) for item in imported), (
        f"detector failed to flag '{source.strip()}' in {module}"
    )
