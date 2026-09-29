# Contract: report payload and browser API surface

**Status:** frozen for the first implementation phase.
**Authority:** PRD sections 8.1, 8.2, 8.3, 8.5, 9.3, 12.

This document is the seam between `resume_review/reporting/` (which produces the
payload), `web/assets/report.js` (which consumes it) and `resume_review/api/`
(which serves the connected-mode equivalents).

Changing anything here changes three modules. Do not change it without updating
all three and the tests that assert the shape.

---

## 1. Two delivery modes

| Mode | How it is opened | State source | Edits |
| --- | --- | --- | --- |
| `connected` | Authenticated URL served by the helper | `/api/v1/instances/{id}/...` | Enabled per role |
| `snapshot` | Double-click `review.html` (`file:` origin) | The `SNAPSHOT` object embedded in the document | Disabled |

A `file:` document has an opaque origin, so a snapshot must never fetch a
neighbouring JSON file. Everything the snapshot needs is embedded. The browser
must not attempt any network call in snapshot mode — not even a `HEAD`.

The embedded payload is delivered as JSON with `<`, `>`, `&`, `U+2028` and
`U+2029` escaped (see `security/untrusted.py::escape_json_for_html`) so applicant
text cannot close the script element.

---

## 2. Snapshot payload

```jsonc
{
  "schema_version": "1.0",
  "mode": "snapshot",
  "generated_at": "2026-09-29T12:00:00+00:00",
  "instance": {
    "instance_id": "inst_...",
    "job_title": "Operations Manager",
    "app_version": "0.1.0",
    "schema_version": 1,
    "state_revision": 412,
    "last_analysis_at": "2026-09-29T11:58:00+00:00",   // null when none
    "storage_mode": "local",
    "criteria_version": 3,
    "criteria": [
      // Only approved criteria appear here. A proposal never reaches the report.
      {
        "criterion_id": "cr_01",
        "version": 3,
        "definition": "Coordinate subcontractors on commercial sites",
        "label": "required",              // "required" | "preferred" | null
        "rationale": "..."
      }
    ]
  },
  "counts": {
    // Whole-instance totals. Always present, even when a filter is active.
    "total": 400,
    "filtered": 400,
    "processed": 388,
    "unreviewed": 350,
    "keep": 20,
    "reject": 25,
    "hold": 5,
    "manual_review": 12,
    "pending_action": 8,
    "needs_recheck": 1,
    "open_tasks": 31
  },
  "documents": [
    {
      "document_id": "doc_...",
      "display_name": null,              // null means unknown, not empty string
      "original_filename": "candidate-001.pdf",
      "current_rel_path": "candidate-001.pdf",
      "media_type": "pdf",               // pdf | docx | txt | unsupported | unknown
      "size_bytes": 182004,
      "ingested_at": "2026-09-29T10:04:11+00:00",
      "submitted_at": null,              // never a file mtime; null means unknown
      "processing_state": "ready",       // see ProcessingState in models.py
      "processing_detail": null,
      "location": "active",              // active | rejected | trash | missing | conflict
      "location_version": 1,
      "review_state": "unreviewed",      // unreviewed | keep | reject | hold
      "decision_revision": 0,
      "decision_needs_recheck": false,
      "recheck_reason": null,
      "disposition_frozen": false,
      "pending_intent": "none",          // see PendingIntent in models.py
      "intent_revision": 0,
      "duplicate_content": false,
      "duplicate_of": null,
      "open_task_count": 1,
      "task_warning": false,             // true when any open task is 'attention' severity
      "summary_text": "Reports commercial renovation coordination experience.",
      "summary_stale": false,
      "criteria": [
        {
          "criterion_id": "cr_01",
          "result": "supported",         // supported | not_found | unclear | needs_manual_review | null
          "explanation": "The resume describes subcontractor coordination.",
          "evidence_ids": ["ev_001"]
        }
      ],
      "evidence": [
        {
          "id": "ev_001",
          "criterion_id": "cr_01",
          "span_id": "page_1_block_4",
          "quote": "Coordinated subcontractors on commercial renovations.",
          "locator": { "page": 1 },
          "validation": "verified"       // verified | quote_missing | span_missing | unchecked
        }
      ],
      "tasks": [
        {
          "id": "task_...",
          "title": "Verify certification",
          "origin": "agent",             // human | agent | system
          "state": "open",
          "severity": "normal",
          "criterion_id": "cr_02",
          "detail": "..."
        }
      ],
      "notes": [
        { "id": "note_...", "body": "...", "author": "reviewer@host", "updated_at": "..." }
      ],
      "decision_history": [
        { "disposition": "hold", "actor": "reviewer@host", "at": "...", "decision_revision": 1 }
      ],
      "file_actions": [
        {
          "batch_id": "batch_...",
          "kind": "move_rejected",
          "state": "committed",
          "destination": "Rejected/doc_.../candidate-001.pdf",
          "at": "...",
          "error_code": null
        }
      ],
      "document_link": "./candidate-001.pdf",   // relative, safely encoded
      "warnings": [
        { "code": "SCAN_ONLY_DOCUMENT", "message": "No extractable text was found." }
      ]
    }
  ]
}
```

### Rules the renderer must obey

* `null` means **unknown** and renders as a distinct marker (an em dash with a
  tooltip), never as `0`, `""`, or the word "none".
* `not_found` renders as "not established in this document", never as
  "does not have" and never as a negative.
* No aggregate suitability score, rank, or ordering by inferred quality appears
  anywhere in the payload or the UI.
* Names are used to locate and alphabetize records only. They are never a
  suitability feature, and never a sort the UI offers as "best match".

---

## 3. Connected-mode API used by the browser

All paths are relative to `/api/v1/instances/{instance_id}`. Responses use the
envelope in `schemas/api_envelope.schema.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/status` | Header/health/counts |
| `GET` | `/documents?page=&page_size=&sort=&direction=&filter=` | Paged table |
| `GET` | `/documents/{id}` | Detail drawer |
| `GET` | `/documents/{id}/original` | Scoped original stream |
| `PATCH` | `/documents/{id}/decision` | Single disposition write |
| `POST` | `/decisions/bulk` | Atomic explicit-set disposition write |
| `POST` | `/documents/{id}/notes` | Create note |
| `PATCH` | `/notes/{note_id}` | Edit note |
| `POST` | `/tasks` | Create task |
| `PATCH` | `/tasks/{task_id}` | Close/reopen task |
| `PUT` | `/documents/{id}/action-intent` | Save or cancel pending intent |
| `POST` | `/actions/plan` | Build an immutable plan from an explicit ID set |
| `GET` | `/actions/{batch}` | Plan detail and state |
| `POST` | `/actions/{batch}/approve` | Record human approval |
| `POST` | `/actions/{batch}/apply` | Execute an approved batch |
| `POST` | `/actions/{batch}/cancel` | Cancel unstarted work |
| `POST` | `/actions/{batch}/restore-plan` | Create the inverse plan |
| `POST` | `/chat` | Folder-scoped conversation |
| `GET` | `/jobs/{id}` | Poll a durable job |
| `POST` | `/scan` | Queue a scan |
| `POST` | `/analysis/jobs` | Queue bounded analysis |
| `POST` | `/backup` | Consistent workspace backup |

### Mutation rules the client must follow

* Every versioned write sends `expected_revision` and handles `409` by showing
  the current value and actor — never by silently retrying with the new revision.
* Every retryable `POST` sends a stable `Idempotency-Key`.
* The client never sends `actor`. Identity comes from the session.
* The client never receives, stores, or transmits the Gateway credential.

---

## 4. Table columns

Fixed order. Column headers are buttons that set `sort`/`direction`; each is
keyboard reachable and reports `aria-sort`.

| # | Key | Contents |
| --- | --- | --- |
| 1 | `select` | Bulk-scope checkbox only. Not a decision. |
| 2 | `submission` | Display name, else filename. Link opens the detail drawer. |
| 3 | `summary` | One or two lines; full text in the drawer. |
| 4 | `evidence` | One indicator per approved criterion. No aggregate score. |
| 5 | `tasks` | Open count, plus a warning marker when severity is `attention`. |
| 6 | `decision` | Mutually exclusive Unreviewed / Keep / Reject / Hold. |
| 7 | `action` | Pending intent, plus the outcome of the last executed operation. |
| 8 | `access` | Open original, open detail. No free-text path input. |

Default sort is `ingested_at asc` with `document_id asc` as a stable
tie-breaker. Supported sort keys: `ingested_at`, `original_filename`,
`display_name`, `processing_state`, `review_state`, `open_task_count`,
`current_rel_path`, `document_id`.

### Bulk selection semantics

Two distinct actions, never merged:

* **Select this page** — the rows currently rendered.
* **Select all matching results** — resolves to an explicit immutable set of
  `{document_id, decision_revision}` pairs at confirmation time. Adding a
  submission or changing a filter afterwards must not extend that set.

The toolbar always shows: selected count, hidden-selected count (rows selected
while not matching the current filter), and a deselect action.

---

## 5. Decision save feedback

Exactly one of these states is shown per row at all times:

`Saved` (with the revision), `Saving…`, `Conflict` (current value and actor shown),
`Failed` (with a retry action).

A failed request is never rendered as committed. The row keeps its previous
value and the failure stays visible until resolved.

While `disposition_frozen` is true (a Trash request is pending, or the file is in
Trash) the decision controls are disabled and the reason is shown.

---

## 6. Chat panel

The panel is present in connected mode only. It shows, for every assistant turn:

1. The plain-language explanation.
2. The interpreted filter conditions, rendered as text, before they are applied.
3. The unknown-value treatment, with the count of rows it affects.
4. A link showing how many rows were omitted by the filter.

Applying or undoing a filter changes the view only. It never changes a decision
and never moves a file. A request such as "move the rejects" produces an action
plan that still requires explicit human approval.
