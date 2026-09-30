"""Render the self-contained snapshot document.

Authority: PRD section 8.5 ("Connected page versus direct-file snapshot") and the
frozen seam in ``docs/contracts/report-payload.md`` (section 1, "Two delivery
modes").

A snapshot is opened from a ``file:`` URL, which modern browsers give an opaque
origin. So the document must be genuinely self-contained: the payload is embedded,
the stylesheet and script are inlined, and the document makes no external request of
any kind — no stylesheet, script, image, font, analytics beacon, or ``HEAD`` probe.

Two independent escapes protect the embedding (PRD section 8.5: "Escape all embedded
values, including script-closing sequences"):

* ``escape_json_for_html`` neutralises ``<``, ``>``, ``&``, ``U+2028`` and ``U+2029``
  in the embedded JSON, so applicant text cannot close the ``<script>`` element.
* ``escape_html`` escapes every value interpolated into markup or an attribute.

The document also carries the application shell (the element ids the browser client
addresses). ``web/assets/report.js`` is written to *read* that shell by id rather
than build it, so the shell lives here, beside the payload it presents. The
presentation assets are read at render time from the packaged
``resume_review/templates`` directory and inlined; if they are absent the render
fails with a clear error rather than emitting a document with no behaviour.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from ..errors import Code, ResumeReviewError
from ..models import REPORT_FILENAME, canonical_json
from ..security.untrusted import escape_html, escape_json_for_html
from .publish import PublishResult, publish_report

__all__ = [
    "ASSET_FILENAMES",
    "CSP_POLICY",
    "PAYLOAD_ELEMENT_ID",
    "asset_dir",
    "load_assets",
    "render_snapshot",
    "write_snapshot",
]

#: Inlined presentation assets, keyed by the role they play.
ASSET_FILENAMES: dict[str, str] = {"css": "report.css", "js": "report.js"}

#: The id of the JSON slot the browser client reads. ``report.js`` accepts
#: ``rr-snapshot`` first and ``SNAPSHOT`` as a fallback; this is the primary.
PAYLOAD_ELEMENT_ID = "rr-snapshot"

#: A content policy that forbids network access outright. ``default-src 'none'``
#: blocks every fetch class; the two ``'unsafe-inline'`` allowances are required
#: because the stylesheet and script are inlined by design.
CSP_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "media-src 'none'; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "img-src data:; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'"
)


def asset_dir() -> Path:
    """The presentation-asset directory shipped with this installation.

    The assets are packaged under ``resume_review/templates`` so a non-editable
    install carries them, and are located through :mod:`importlib.resources` rather
    than by walking up from this file - a path like ``parents[3]`` reaches the
    source tree in a checkout but not the installed package. A checkout that has
    not had package data installed still resolves the repository ``web/assets``
    directory it was developed against.
    """
    try:
        packaged = Path(str(resources.files("resume_review").joinpath("templates")))
        if (packaged / ASSET_FILENAMES["css"]).is_file():
            return packaged
    except (ModuleNotFoundError, AttributeError, TypeError):
        # A namespace or otherwise unusual loader; fall through to the checkout.
        pass
    return Path(__file__).resolve().parents[3] / "web" / "assets"


def load_assets(directory: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Read ``report.css`` and ``report.js`` for inlining.

    Assets are read at render time so the packaged files are the single source of
    truth. A missing asset is a defective installation, not an applicant problem, so
    it is reported as a manifest failure with the missing names in ``detail``.
    """
    root = Path(directory) if directory is not None else asset_dir()
    loaded: dict[str, str] = {}
    missing: list[str] = []
    for key, name in ASSET_FILENAMES.items():
        path = root / name
        if not path.is_file():
            missing.append(name)
            continue
        loaded[key] = path.read_text(encoding="utf-8")
    if missing:
        raise ResumeReviewError(
            "The report presentation assets are missing from this installation.",
            code=Code.MANIFEST_MISSING,
            detail={"missing": sorted(missing)},
        )
    return loaded


def _asset(assets: Mapping[str, str], key: str) -> str:
    try:
        return str(assets[key])
    except (KeyError, TypeError) as exc:
        raise ResumeReviewError(
            "A required report presentation asset was not supplied.",
            code=Code.MANIFEST_MISSING,
            detail={"missing": [ASSET_FILENAMES[key]]},
        ) from exc


def _guard_inline(text: str, *, tag: str, name: str) -> str:
    """Refuse to inline a file whose text could close its own container.

    A literal ``</script`` (or ``</style``) inside inlined content would terminate
    the element early and turn the remainder into markup. Neither installed asset is
    expected to contain one; if a future edit introduces it, this fails loudly rather
    than emitting a broken or unsafe document.
    """
    if f"</{tag}" in text.lower():
        raise ResumeReviewError(
            "A report asset contains a sequence that cannot be inlined safely.",
            code=Code.MANIFEST_MISSING,
            detail={"asset": name, "reason": "container_close_sequence"},
        )
    return text


def _connected_anchor(connected_url: str | None) -> str:
    """"Open connected review" link, carrying no credential and no network scheme.

    The helper passes its own bound address when it has one. With no address known —
    the offline double-click case — the link is rendered as unavailable and pointed
    at the document itself, never at a guessed host or a token.
    """
    if connected_url:
        return (
            f'<a class="connected-link" href="{escape_html(connected_url)}">'
            "Open connected review</a>"
        )
    return (
        '<a class="connected-link is-unavailable" href="#" aria-disabled="true" '
        'title="Start the helper to open the connected review.">'
        "Open connected review</a>"
    )


#: The count strip mirrors COUNT_FIELDS in report.js, plus the scoped and
#: page-level figures the contract requires to stay distinct from the totals.
_COUNT_ROWS: tuple[tuple[str, str], ...] = (
    ("total", "Total"),
    ("processed", "Processed"),
    ("unreviewed", "Unreviewed"),
    ("keep", "Keep"),
    ("reject", "Reject"),
    ("hold", "Hold"),
    ("manual_review", "Manual review"),
    ("pending_action", "Pending action"),
    ("needs_recheck", "Decision needs recheck"),
    ("open_tasks", "Open review tasks"),
    ("filtered", "Filtered"),
    ("page", "On this page"),
    ("omitted", "Hidden by filters"),
    ("selected", "Selected"),
)

_SORT_KEYS: tuple[tuple[str, str], ...] = (
    ("ingested_at", "Ingested"),
    ("original_filename", "Original filename"),
    ("display_name", "Display name"),
    ("processing_state", "Processing state"),
    ("review_state", "Reviewer state"),
    ("open_task_count", "Open task count"),
    ("current_rel_path", "Current path"),
    ("document_id", "Document ID"),
)

_LOCATION_OPTIONS: tuple[str, ...] = ("active", "rejected", "trash", "missing", "conflict")
_PROCESSING_OPTIONS: tuple[str, ...] = (
    "discovered",
    "extracting",
    "analyzing",
    "ready",
    "manual_review",
    "error",
    "stale",
)
_REVIEW_OPTIONS: tuple[str, ...] = ("unreviewed", "keep", "reject", "hold")
_TASK_OPTIONS: tuple[str, ...] = ("attention", "open", "none")


def _options(values: tuple[str, ...], *, any_label: str = "Any") -> str:
    rendered = [f'<option value="any">{escape_html(any_label)}</option>']
    for value in values:
        rendered.append(f'<option value="{escape_html(value)}">{escape_html(value)}</option>')
    return "".join(rendered)


def _count_strip() -> str:
    cells = []
    for key, label in _COUNT_ROWS:
        cells.append(
            f'<span class="rr-count"><span class="rr-count-label">{escape_html(label)}</span>'
            f' <span class="rr-count-value" id="rr-count-{escape_html(key.replace("_", "-"))}"></span></span>'
        )
    overview = '<div class="rr-overview" aria-label="Candidate overview">' + "".join(
        f'<p><span>{label}</span><strong id="rr-overview-{key}"></strong></p>'
        for key, label in (("total", "All candidates"), ("unreviewed", "To review"),
                           ("keep", "Keep / advance"), ("reject", "Reject"), ("hold", "On hold"))
    ) + "</div>"
    return (overview + '<details class="rr-disclosure rr-count-details"><summary>Detailed counts</summary>'
            '<section class="rr-counts" aria-label="Counts">' + "".join(cells) + "</section></details>")


def _app_shell() -> list[str]:
    """The element shell the browser client addresses by id.

    Every id here is one ``report.js`` looks up. The client fills these nodes with
    textContent; it never builds the surrounding structure, so a missing id would
    silently drop a region of the page.
    """
    return [
        '<section class="rr-toolbar" aria-label="Toolbar">',
        '<label class="rr-field">Search <input id="rr-search" type="search" autocomplete="off"></label>',
        '<details class="rr-disclosure"><summary id="rr-filter-summary">Filters and tools</summary>',
        f'<label class="rr-field">Location <select id="rr-filter-location">{_options(_LOCATION_OPTIONS)}</select></label>',
        f'<label class="rr-field">Processing <select id="rr-filter-processing">{_options(_PROCESSING_OPTIONS)}</select></label>',
        f'<label class="rr-field">Reviewer state <select id="rr-filter-review">{_options(_REVIEW_OPTIONS)}</select></label>',
        f'<label class="rr-field">Review tasks <select id="rr-filter-tasks">{_options(_TASK_OPTIONS)}</select></label>',
        '<label class="rr-field">Sort <select id="rr-sort">'
        + "".join(f'<option value="{escape_html(k)}">{escape_html(label)}</option>' for k, label in _SORT_KEYS)
        + "</select></label>",
        '<label class="rr-field">Direction <select id="rr-sort-direction">'
        '<option value="asc">Ascending</option><option value="desc">Descending</option></select></label>',
        '<button type="button" id="rr-clear-filters">Clear filters</button>',
        '<p id="rr-ops-snapshot" class="rr-hint">Read-only snapshot: edits, chat, scan, and file actions are disabled.</p>',
        '<span id="rr-ops-connected" class="rr-ops" hidden>',
        '<button type="button" id="rr-btn-refresh">Refresh</button>',
        '<button type="button" id="rr-btn-scan">Scan</button>',
        '<button type="button" id="rr-btn-summarize">Summarize changed items</button>',
        "</span>",
        '<span id="rr-ops-role" class="rr-hint"></span>',
        "</details>",
        "</section>",
        '<section class="rr-selection" aria-label="Bulk selection">',
        '<span id="rr-sel-count">0 selected</span>',
        '<span id="rr-sel-hidden">0 hidden by the current filter</span>',
        '<span id="rr-sel-note"></span>',
        '<button type="button" id="rr-btn-select-page">Select this page</button>',
        '<button type="button" id="rr-btn-select-matching">Select all matching results</button>',
        '<button type="button" id="rr-btn-deselect">Deselect</button>',
        "</section>",
        '<ul id="rr-filter-chips" class="rr-chips" aria-label="Active filter conditions"></ul>',
        '<section class="rr-bulk-bar" id="rr-bulk-bar" aria-label="Selected candidate actions">'
        '<div class="rr-bulk-summary"><strong id="rr-bulk-count">No candidates selected</strong>'
        '<span id="rr-bulk-scope">Select candidates using the checkboxes below.</span></div>'
        '<div class="rr-bulk-buttons" role="group" aria-label="Set review decision">'
        '<button type="button" data-bulk-decision="keep" disabled>Keep / advance</button>'
        '<button type="button" data-bulk-decision="reject" disabled>Reject</button>'
        '<button type="button" data-bulk-decision="hold" disabled>Hold</button>'
        '<button type="button" data-bulk-decision="unreviewed" disabled>Reset review</button></div>'
        '<p id="rr-bulk-status" role="status" aria-live="polite">'
        'Read-only snapshot. Open the connected review page to save decisions.</p></section>',
        '<table class="rr-table" id="rr-table">',
        '<caption class="rr-visually-hidden">Submissions in this review. Default order is ingestion order.</caption>',
        '<thead><tr id="rr-head-row"></tr></thead>',
        '<tbody id="rr-rows"></tbody>',
        "</table>",
        '<p id="rr-empty" class="rr-empty" hidden></p>',
        '<p class="rr-pager">',
        '<button type="button" id="rr-btn-prev">Previous</button>',
        '<span id="rr-page-info"></span>',
        '<span id="rr-page-number"></span>',
        '<label class="rr-field">Rows per page <select id="rr-page-size">'
        '<option value="50">50</option><option value="100">100</option><option value="200">200</option>'
        "</select></label>",
        '<button type="button" id="rr-btn-next">Next</button>',
        "</p>",
        '<aside id="rr-detail" class="rr-drawer" aria-label="Document detail" hidden>',
        '<button type="button" id="rr-detail-close">Close detail</button>',
        '<h2 id="rr-detail-title"></h2>',
        '<div id="rr-detail-body"></div>',
        "</aside>",
        '<aside id="rr-chat" class="rr-drawer" aria-label="Folder chat" hidden>',
        '<button type="button" id="rr-chat-close">Close chat</button>',
        '<h2>Folder chat</h2>',
        '<ul id="rr-chat-log"></ul>',
        '<form id="rr-chat-form">',
        '<label class="rr-field">Ask about this folder <input id="rr-chat-input" type="text" autocomplete="off"></label>',
        '<button type="submit" id="rr-chat-send">Send</button>',
        "</form>",
        '<button type="button" id="rr-chat-clear">Clear displayed messages</button>',
        "</aside>",
        '<aside id="rr-actions" class="rr-drawer" aria-label="Action review" hidden>',
        '<button type="button" id="rr-actions-close">Close action review</button>',
        '<h2>Action review</h2>',
        '<div id="rr-action-body"></div>',
        "</aside>",
        '<footer class="rr-footer">',
        '<span id="rr-footer-source"></span>',
        '<button type="button" id="rr-btn-actions">Review actions</button>',
        '<button type="button" id="rr-btn-chat" aria-expanded="false" aria-controls="rr-chat">Chat</button>',
        '<label class="rr-field">Theme <select id="rr-theme">'
        '<option value="auto">Auto</option><option value="light">Light</option><option value="dark">Dark</option>'
        "</select></label>",
        "</footer>",
    ]


def render_snapshot(
    payload: Mapping[str, Any],
    assets: Mapping[str, str],
    *,
    connected_url: str | None = None,
    title: str | None = None,
) -> str:
    """Render the payload and inlined assets into one HTML document.

    The embedded payload's ``mode`` is forced to ``"snapshot"`` so the client
    disables edits, chat, scan, and file actions even if a caller passed a payload
    built for connected mode.
    """
    if not isinstance(payload, Mapping):
        raise ResumeReviewError(
            "A snapshot payload mapping is required to render the report.",
            code=Code.VALIDATION_FAILED,
        )

    css = _guard_inline(_asset(assets, "css"), tag="style", name=ASSET_FILENAMES["css"])
    js = _guard_inline(_asset(assets, "js"), tag="script", name=ASSET_FILENAMES["js"])

    embedded = dict(payload)
    embedded["mode"] = "snapshot"
    # canonical_json sorts keys so the document is byte-stable for identical state,
    # which keeps a republish with no change from producing a diff.
    embedded_json = escape_json_for_html(canonical_json(embedded))

    instance = payload.get("instance")
    instance = instance if isinstance(instance, Mapping) else {}
    counts = payload.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}

    job_title = instance.get("job_title")
    document_title = title or (job_title if isinstance(job_title, str) and job_title else "Resume review")
    generated_at = payload.get("generated_at")
    state_revision = instance.get("state_revision")

    title_text = escape_html(document_title)
    generated_text = escape_html(generated_at if generated_at is not None else "unknown")
    revision_text = escape_html(state_revision if state_revision is not None else "unknown")
    filtered_text = escape_html(counts.get("filtered") if counts.get("filtered") is not None else "unknown")

    lines = [
        "<!doctype html>",
        '<html lang="en" data-mode="snapshot">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        # Network access is forbidden at the document level, not merely avoided by
        # convention; a compromised or future asset cannot phone home.
        f'<meta http-equiv="Content-Security-Policy" content="{CSP_POLICY}">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>RecruiterAgent | {title_text} - review snapshot</title>",
        "<style>",
        css,
        "</style>",
        "</head>",
        "<body>",
        '<p id="rr-module-warning" class="rr-alert" hidden></p>',
        '<div class="rr-shell">',
        '<header class="app-header">',
        '<div class="rr-brand" aria-label="RecruiterAgent, OpenClaw skill">'
        '<span class="rr-brand-mark" aria-hidden="true">RA</span>'
        '<div><p class="rr-brand-name">Recruiter<span>Agent</span></p>'
        '<p class="rr-brand-caption">OpenClaw skill / Evidence-led review</p></div></div>',
        f'<h1 id="rr-job-title" class="job-title">{title_text}</h1>',
        '<p class="snapshot-strip">',
        '<span id="rr-mode-badge" class="mode-badge" data-mode="snapshot">Snapshot (read only)</span>',
        '<span>Instance <span id="rr-instance-id"></span></span>',
        f'<span class="snapshot-time">Snapshot taken <time datetime="{generated_text}">{generated_text}</time></span>',
        f'<span class="state-revision">State revision <span id="rr-state-revision" class="revision-value">{revision_text}</span></span>',
        '<span>Last analysis <span id="rr-last-analysis"></span></span>',
        "</p>",
        '<p id="rr-mode-detail" class="rr-hint"></p>',
        '<p id="rr-snapshot-banner" class="rr-hint" hidden></p>',
        '<p id="rr-health" class="rr-hint"></p>',
        '<p id="rr-alert" class="rr-alert" role="alert" hidden></p>',
        '<p id="rr-status" class="rr-status" role="status" aria-live="polite"></p>',
        '<p id="rr-connection-status" class="rr-status" role="status" hidden></p>',
        '<p class="connected-note">',
        _connected_anchor(connected_url),
        " Edits, chat, scan, and file actions are disabled in a saved snapshot.",
        "</p>",
        "</header>",
        '<nav class="rr-workspace-tabs" role="tablist" aria-label="Review workspace">'
        '<button type="button" id="rr-tab-candidates" role="tab" aria-selected="true" '
        'aria-controls="rr-panel-candidates" data-workspace-tab="candidates">Candidates</button>'
        '<button type="button" id="rr-tab-requisition" role="tab" aria-selected="false" '
        'aria-controls="rr-panel-requisition" tabindex="-1" data-workspace-tab="requisition">Requisition</button>'
        '<button type="button" id="rr-tab-feedback" role="tab" aria-selected="false" '
        'aria-controls="rr-panel-feedback" tabindex="-1" data-workspace-tab="feedback">OpenClaw feedback</button></nav>',
        '<section class="rr-requisition" id="rr-panel-requisition" role="tabpanel" '
        'aria-labelledby="rr-tab-requisition" hidden><p class="rr-eyebrow">Role reference</p>'
        '<h2>Original requisition</h2><span class="rr-badge" id="rr-requisition-badge">Not loaded</span>'
        '<p id="rr-requisition-summary">Open the connected review page to view or save this folder\'s requisition reference.</p>'
        '<details><summary>Add or edit requisition (connected review only)</summary>'
        '<form id="rr-requisition-form" class="rr-requisition-form">'
        '<label for="rr-requisition-title">Role / requisition title</label>'
        '<input id="rr-requisition-title" maxlength="200" required disabled>'
        '<label for="rr-requisition-file">Import a text file</label>'
        '<input id="rr-requisition-file" type="file" accept=".txt,.md" disabled>'
        '<label for="rr-requisition-text">Requisition text / job requirements</label>'
        '<textarea id="rr-requisition-text" rows="5" maxlength="20000" required disabled></textarea>'
        '<label for="rr-requisition-source">Original source link (optional)</label>'
        '<input id="rr-requisition-source" type="url" maxlength="2048" disabled>'
        '<div class="rr-requisition-actions">'
        '<button type="submit" id="rr-requisition-save" disabled>Save reference</button>'
        '<button type="button" id="rr-requisition-reload" disabled>Discard edits / reload saved</button>'
        '<p id="rr-requisition-status" role="status" aria-live="polite">Read-only snapshot.</p>'
        '</div></form></details></section>',
        '<div id="rr-criteria-editor" hidden><ul id="rr-criteria-draft-list"></ul>'
        '<form id="rr-criteria-draft-form"><textarea id="rr-criteria-input" disabled></textarea>'
        '<button id="rr-criteria-propose" disabled>Save criteria draft</button>'
        '<button id="rr-criteria-approve" disabled>Approve displayed draft</button>'
        '<button id="rr-criteria-reload" disabled>Reload criteria</button></form>'
        '<p id="rr-criteria-status">Criteria editing is unavailable in a saved snapshot.</p></div>',
        '<section class="rr-feedback" id="rr-panel-feedback" role="tabpanel" aria-labelledby="rr-tab-feedback" hidden>'
        '<div class="rr-feedback-intro"><p class="rr-eyebrow">Folder workspace</p>'
        '<h2 id="rr-feedback-heading">Feedback for OpenClaw</h2>'
        '<p>Ask a question, correct an interpretation, or suggest what to review across this job folder.</p></div>'
        '<form id="rr-feedback-form" class="rr-feedback-form">'
        '<label for="rr-feedback-input">Your feedback or instructions</label>'
        '<textarea id="rr-feedback-input" rows="3" maxlength="20000" required disabled '
        'aria-describedby="rr-feedback-status"></textarea>'
        '<div class="rr-feedback-actions">'
        '<button type="submit" id="rr-feedback-send" class="rr-primary" disabled>Send to OpenClaw</button>'
        '<p id="rr-feedback-status" class="rr-hint" role="status" aria-live="polite">'
        'Read-only snapshot. Open the connected review page to send feedback.</p>'
        '</div></form></section>',
        '<div id="rr-panel-candidates" role="tabpanel" aria-labelledby="rr-tab-candidates">',
        _count_strip(),
        '<main id="app" class="app" data-mode="snapshot" data-documents="' + filtered_text + '">',
        *_app_shell(),
        "<noscript>",
        "<p>This snapshot needs JavaScript for in-memory sorting and filtering. "
        "Its data is embedded in this file; no network access is required.</p>",
        "</noscript>",
        "</main>",
        "</div>",
        "</div>",
        # The payload is embedded once, as escaped JSON text, and parsed by the
        # bootstrap below. One copy, two consumers: the element and window.SNAPSHOT.
        f'<script id="{PAYLOAD_ELEMENT_ID}" type="application/json">{embedded_json}</script>',
        "<script>",
        f'window.SNAPSHOT = JSON.parse(document.getElementById("{PAYLOAD_ELEMENT_ID}").textContent);',
        "</script>",
        # report.js is an ES module (it exports ReportCore for the browser harness)
        # and boots itself on load, so it must be imported as a module.
        '<script type="module">',
        js,
        "</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(lines)


def write_snapshot(
    report_path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    assets: Mapping[str, str] | None = None,
    assets_dir: str | os.PathLike[str] | None = None,
    connected_url: str | None = None,
    title: str | None = None,
) -> PublishResult:
    """Render and atomically publish a snapshot to ``report_path``.

    ``assets`` may be supplied directly (tests, embedded callers); otherwise the
    installed ``web/assets`` files are read at render time. Publication follows the
    keep-the-previous-report policy in ``publish.py``.
    """
    if assets is None:
        assets = load_assets(assets_dir)
    html = render_snapshot(payload, assets, connected_url=connected_url, title=title)

    instance = payload.get("instance")
    instance = instance if isinstance(instance, Mapping) else {}
    state_revision = instance.get("state_revision")
    generated_at = payload.get("generated_at")
    return publish_report(
        report_path,
        html,
        state_revision=int(state_revision) if isinstance(state_revision, int) else None,
        generated_at=generated_at if isinstance(generated_at, str) else None,
    )


#: Re-exported so callers (CLI, tests) can name the conventional report filename
#: without importing ``models`` themselves.
REPORT_NAME = REPORT_FILENAME
