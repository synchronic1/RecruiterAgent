"""Checks for the browser client assets and their integrity manifest.

Authority: PRD sections 8.1-8.5 (review page, snapshot versus connected mode) and
AT-04 (asset integrity), AT-34 (injection containment), AT-39 (no state conveyed
by colour alone). The frozen seam is docs/contracts/report-payload.md.

No browser is available in this environment. These tests prove what can be proven
statically - that the module parses, that its pure logic behaves, that the
snapshot path contains no network call, that no payload value can reach a raw
markup sink, and that the committed manifest matches the committed bytes. They do
not prove that the page renders correctly in a browser.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION
from resume_review.bootstrap.manifest import build_manifest, bundle_file_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"
TEMPLATE = WEB / "templates" / "report.html"
CSS = WEB / "assets" / "report.css"
JS = WEB / "assets" / "report.js"
HARNESS = REPO_ROOT / "tests" / "browser" / "check_report_js.mjs"
MANIFEST_SCHEMA = REPO_ROOT / "schemas" / "manifest.schema.json"

#: Bundle-relative paths, as the deployed .review/app/ layout names them.
EXPECTED_BUNDLE_PATHS = ["assets/report.css", "assets/report.js", "templates/report.html"]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the report client's pure logic")
    return node


@pytest.fixture(scope="module")
def js_source() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css_source() -> str:
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html_source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest() -> dict:
    """The release manifest for ``web/``, built from the bytes on disk.

    Derived rather than read from a committed file. A checked-in copy used to live
    at ``web/assets/manifest.json``, and it was wrong the way any such copy is
    eventually wrong: it recorded ``schema_version: 1`` while the builder emitted
    ``2``, and -- because it sat inside the directory it described -- it appeared in
    its own file list, so ``build_manifest`` hashed four files where the committed
    copy listed three. Neither discrepancy could fail a test that compared the copy
    against itself. Building here means these assertions test the builder's output,
    so a drifting bundle fails instead of a stale specimen passing.
    """
    return build_manifest(WEB)


# ---------------------------------------------------------------------------
# The module parses and its pure logic behaves
# ---------------------------------------------------------------------------
def test_report_js_parses_under_node() -> None:
    result = subprocess.run(
        [_node(), "--check", str(JS)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"node --check failed:\n{result.stdout}\n{result.stderr}"


def test_pure_logic_harness_passes() -> None:
    result = subprocess.run(
        [_node(), str(HARNESS)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    assert "0 failed" in result.stdout, result.stdout
    assert re.search(r"\b\d+ passed, 0 failed", result.stdout), result.stdout


def test_harness_covers_the_required_behaviours() -> None:
    """The harness must actually assert the behaviours the task names, not merely
    exit zero. Each name below is a check that must be present."""
    source = HARNESS.read_text(encoding="utf-8")
    for name in (
        "COLUMNS match the contract order",
        "SORT_KEYS match the contract allowlist",
        "escaped, not treated as markup",
        "unknown",
        "select all matching results resolves to an immutable set",
        "a failed request is never rendered as committed",
        "the snapshot path makes no network call",
    ):
        assert name in source, f"harness is missing a check for: {name}"


# ---------------------------------------------------------------------------
# Snapshot mode performs no network request (contract section 1)
# ---------------------------------------------------------------------------
def _function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_snapshot_boot_makes_no_network_call(js_source: str) -> None:
    body = _function_body(js_source, "function snapshotBoot(", "async function connectedBoot(")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "apiRequest(", "EventSource", "WebSocket"):
        assert forbidden not in body, f"snapshotBoot must not reference {forbidden}"


def test_every_request_goes_through_the_guarded_entry_point(js_source: str) -> None:
    assert js_source.count("await DOM.window.fetch(") == 1, "exactly one fetch call site is allowed"
    body = _function_body(js_source, "async function apiRequest(", "\nasync function writeDecision")
    assert "modeAllowsNetwork(DOM.state.mode)" in body, "apiRequest must refuse to run outside connected mode"


def test_a_file_origin_resolves_to_snapshot(js_source: str) -> None:
    # An opaque file: origin can never be treated as a connected page, even when
    # the payload is missing.
    assert "isFileOrigin" in js_source
    assert "function isFileOrigin(href)" in js_source


def test_connected_mode_uses_the_contract_endpoints(js_source: str) -> None:
    for suffix in ('"/status"', '"/chat"', '"/actions/plan"', '"/scan"', '"/analysis/jobs"'):
        assert suffix in js_source, f"the client must use {suffix}"
    assert "documentsQuery(" in js_source
    assert "/decision" in js_source


def test_versioned_writes_send_expected_revision(js_source: str) -> None:
    assert "expected_revision" in js_source
    assert '"Idempotency-Key"' in js_source
    assert "newIdempotencyKey" in js_source


def test_the_client_never_builds_an_actor_field(js_source: str) -> None:
    """Identity comes from the session. An actor field is never sent."""
    assert not re.search(r'\bactor\s*:', js_source), "the client must not build an actor field"


# ---------------------------------------------------------------------------
# Injection containment (PRD 16.1, AT-34)
# ---------------------------------------------------------------------------
def test_no_raw_markup_sink_can_receive_payload_data(js_source: str) -> None:
    occurrences = list(re.finditer(r"innerHTML", js_source))
    assert len(occurrences) == 1, f"expected exactly one innerHTML token, saw {len(occurrences)}"
    line_start = js_source.rfind("\n", 0, occurrences[0].start()) + 1
    line_end = js_source.find("\n", occurrences[0].end())
    line = js_source[line_start:line_end]
    assert "buildHeaderHtml()" in line, f"the only innerHTML must be the static header: {line.strip()}"
    for sink in ("outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
        assert sink not in js_source, f"{sink} must not appear in the report client"


def test_the_explicit_escaping_helper_matches_the_python_one() -> None:
    from resume_review.security.untrusted import escape_html

    hostile = '<img src=x onerror=alert(1)>'
    python_escaped = escape_html(hostile)
    source = JS.read_text(encoding="utf-8")
    # Every mapping the Python helper applies must exist in the JS helper, so a
    # value escaped on one side of the seam is escaped the same way on the other.
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;", "&#x2F;", "&#x60;", "&#x3D;"):
        assert entity in source, f"escapeHtml is missing {entity}"
    assert "&#x2F;script" not in python_escaped


def test_applicant_text_is_written_with_text_content(js_source: str) -> None:
    assert "textContent" in js_source
    # The renderers that write applicant-sourced strings must use textContent.
    for renderer in ("function renderSubmissionCell(", "function renderSummaryCell(", "function renderAccessCell("):
        body = _function_body(js_source, renderer, "\nfunction ")
        assert "innerHTML" not in body, f"{renderer} must not build markup"


def test_document_links_are_restricted_to_relative_references(js_source: str) -> None:
    assert "function safeRelativeLink(" in js_source
    sanitizer = _function_body(js_source, "export function safeRelativeLink(", "\n/* ==========")
    assert "encodeURIComponent" in sanitizer, "the accepted link must be encoded"
    assert "return null;" in sanitizer, "an unsafe link must be refused, not repaired"
    # The access column must push the payload link through that filter rather
    # than handing it straight to href.
    access = _function_body(js_source, "function renderAccessCell(", "\nfunction renderRows(")
    assert "safeRelativeLink(row.document_link)" in access


# ---------------------------------------------------------------------------
# Presentation requirements (PRD 8.1, 8.3; AT-39)
# ---------------------------------------------------------------------------
def test_shell_is_an_application_with_every_required_region(html_source: str) -> None:
    for region in (
        'id="rr-job-title"',        # header
        'id="rr-counts"',           # status and count strip
        'class="rr-toolbar"',       # toolbar
        'id="rr-table"',            # main table
        'id="rr-detail"',           # document-detail drawer
        'id="rr-chat"',             # optional chat panel
        'id="rr-actions"',          # action-review drawer
        'id="rr-pager"',            # explicit page navigation
    ):
        assert region in html_source, f"the shell is missing {region}"


def test_shell_has_no_inline_executable_script(html_source: str) -> None:
    """A conservative content security policy cannot allow inline script, so the
    only scripts are the module and the two inert JSON payload slots."""
    tags = re.findall(r"<script\b[^>]*>", html_source)
    assert tags, "the shell must load the module"
    for tag in tags:
        assert 'type="application/json"' in tag or 'type="module"' in tag, tag
        if 'type="application/json"' not in tag:
            assert "src=" in tag, f"an executable script must have a src: {tag}"


def test_shell_offers_a_graceful_message_when_the_module_is_blocked(html_source: str) -> None:
    # A file: document has an opaque origin, so a browser may refuse the module.
    # The shell must say so rather than showing an empty page.
    assert 'id="rr-module-warning"' in html_source
    assert "file:" in html_source
    assert "<noscript>" in html_source


def test_css_defines_its_palette_as_root_custom_properties(css_source: str) -> None:
    assert ":root {" in css_source
    assert "--rr-surface:" in css_source
    assert "--rr-ink:" in css_source
    # Dark mode is a selected set under both the OS setting and the page toggle.
    assert "prefers-color-scheme: dark" in css_source
    assert ':root:not([data-theme="light"])' in css_source
    assert ':root[data-theme="dark"]' in css_source


def test_css_loads_no_external_asset(css_source: str) -> None:
    assert "@import" not in css_source
    assert "url(" not in css_source
    assert "@font-face" not in css_source


def test_css_reflows_the_table_at_phone_width(css_source: str) -> None:
    assert "max-width: 760px" in css_source
    assert "attr(data-label)" in css_source
    assert "overflow-x: visible" in css_source


def test_css_never_conveys_state_by_colour_alone(css_source: str) -> None:
    # Each evidence state draws its own shape as well as its own colour.
    for status in ("good", "neutral", "warning", "serious", "unknown"):
        assert f'.rr-ind[data-status="{status}"]' in css_source, status
    shapes = (
        '.rr-ind[data-status="good"]::before { background: currentColor; }',
        '.rr-ind[data-status="neutral"]::before { border-style: dashed; }',
        '.rr-ind[data-status="warning"]::before {',
        '.rr-ind[data-status="serious"]::before { transform: rotate(45deg);',
        '.rr-ind[data-status="unknown"]::before { border-radius: 50%; }',
    )
    for shape in shapes:
        assert shape in css_source, shape
    # Focus must remain visible for keyboard-only operation.
    assert ":focus-visible" in css_source


def test_css_has_no_non_ascii_glyph_dependency(css_source: str) -> None:
    assert css_source.isascii(), "the stylesheet must not depend on non-ASCII glyphs"


def test_assets_contain_no_emoji(js_source: str, css_source: str, html_source: str) -> None:
    """No emoji anywhere (AGENTS.md rule for changes). The assets are plain
    ASCII, so this asserts the stronger, checkable property."""
    for name, text in (("report.js", js_source), ("report.css", css_source), ("report.html", html_source)):
        assert text.isascii(), f"{name} must be plain ASCII"


# ---------------------------------------------------------------------------
# Integrity manifest (AT-04, schemas/manifest.schema.json)
# ---------------------------------------------------------------------------
def test_manifest_conforms_to_the_frozen_schema(manifest: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(instance=manifest, schema=schema)


def test_manifest_lists_every_bundled_asset(manifest: dict) -> None:
    paths = sorted(item["path"] for item in manifest["files"])
    assert paths == EXPECTED_BUNDLE_PATHS
    for item in manifest["files"]:
        assert item["mode"] == "asset"


def test_manifest_hashes_match_the_committed_bytes(manifest: dict) -> None:
    sources = {
        "templates/report.html": TEMPLATE,
        "assets/report.css": CSS,
        "assets/report.js": JS,
    }
    for item in manifest["files"]:
        data = sources[item["path"]].read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest(), f"stale hash for {item['path']}"
        assert item["size"] == len(data), f"stale size for {item['path']}"


def test_manifest_bundle_hash_follows_the_documented_rule(manifest: dict) -> None:
    digest = hashlib.sha256()
    for item in sorted(manifest["files"], key=lambda row: row["path"]):
        digest.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
    assert manifest["bundle_hash"] == digest.hexdigest()


def test_manifest_carries_the_app_metadata(manifest: dict) -> None:
    assert manifest["manifest_version"] == "1.0"
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["app_version"] == "0.1.0"
    assert manifest["created_at"]


def test_manifest_describes_exactly_the_reviewed_assets(manifest: dict) -> None:
    """The bundle holds the three reviewed sources, and every one is an asset.

    A manifest cannot vouch for its own bytes (PRD section 4) -- the protected host
    registry does that -- so it must not live inside the bundle it describes.
    ``deploy_bundle`` writes its generated copy at the bundle *root* and excludes
    that one path by name, so anything else left in the tree is walked, hashed, and
    reported as an extra file on the next verification.

    The mode check is not decoration. ``_mode_for`` labels a path an asset when it
    starts with ``assets/`` or ``templates/`` and an executable when it ends in
    ``.js``; the nested layout here means ``report.js`` is an asset, while the flat
    packaged fallback in ``default_bundle_dir`` would mislabel it as executable.
    """
    assert [item["path"] for item in manifest["files"]] == EXPECTED_BUNDLE_PATHS
    assert {item["mode"] for item in manifest["files"]} == {"asset"}


def test_the_bundle_directory_holds_only_the_documented_paths() -> None:
    """The file list above is only correct while the directory agrees with it."""
    on_disk = sorted(path.relative_to(WEB).as_posix() for path in bundle_file_paths(WEB))
    assert on_disk == EXPECTED_BUNDLE_PATHS
