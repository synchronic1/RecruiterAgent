# OpenClaw Folder-Based Resume Review
## Product requirements and implementation handoff

**Version:** 1.0  
**Date:** September 29, 2026  
**Product owner:** Peter  
**Audience:** Coding agents, application engineers, and the recruiting operator  
**Status:** Build specification. The application, helper, and OpenClaw skill described here have not been implemented or tested by this document.  
**Design baseline:** The agreed conversation and the earlier version 0.1 PRD. This edition expands the architecture, contracts, failure handling, release gates, and agent handoff.

> **Product in one sentence:** Point OpenClaw at a folder of resumes; its reusable skill provisions a folder-specific review page, local helper, and database, then helps a human reviewer understand submissions and safely organize only the files the reviewer approves.

---

## 1. Product intent and agreed boundaries

The recruiter already has a job requisition and a folder containing potentially 400 resumes. They need a fast way to understand those documents, sort and filter the list, record decisions, ask follow-up questions, and organize the originals. They do not need to adopt another applicant tracking system.

The folder is the application instance and the unit of portability. Every job folder has its own report, database, criteria, summaries, human decisions, tasks, conversation records, and file-action history. An installed OpenClaw skill deploys the same tested application bundle into each instance. It does not invent a different application implementation on every run.

### 1.1 Requirements carried forward

- Use **OpenClaw specifically** for agent orchestration and the configured model route.
- Operate locally at minimum and support a shared-drive deployment with one storage-host-local helper owner.
- Generate a template-based HTML companion page per folder; refreshing shows the latest committed results.
- Present hundreds of resumes in a compact, sortable list with useful summaries and review tasks.
- Provide mutually exclusive row decisions and separate bulk-selection checkboxes.
- Let human-approved decisions drive safe file organization, including a Rejected subfolder and recoverable Trash.
- Preserve notes, decisions, tasks, and history through rescans, restarts, and template regeneration.
- Retain folder-scoped agent chat for explanations and further filtering. A refreshable report does not remove the original chat requirement.

### 1.2 Terminology

A **submission** is one registered primary resume file in the initial release. A **document ID** identifies that submission across renames and managed moves. Multiple files from one person are not automatically merged into a single candidate identity. The interface should say “submissions” where a count could otherwise imply a verified number of distinct people.

**Keep** means retain for further review, not hire. **Reject** is a human review disposition, not an email notification. **Hold** means defer judgment. **Move to Trash** is a separate, recoverable file-management action, not a hiring assessment.

**Local application** means the helper and authoritative application database run on the storage host. **Local inference** is a separate model-route restriction. Neither implies the other.

### 1.3 Explicit non-goals

No ATS replacement, email intake connector, candidate outreach, scheduling, background checks, social-profile enrichment, autonomous hiring decision, opaque suitability score, payroll integration, public applicant portal, new model-serving engine, multi-tenant SaaS, or automatic permanent deletion. No mandatory cloud database, vector database, Redis, message broker, or frontend development server.

Images of people, demographic inference, personality scoring, “culture fit,” and inferred sensitive traits must not influence screening. Human decisions remain authoritative.

## 2. Outcomes, scope, and release definition

### 2.1 Successful operator experience

The operator can start with an existing folder, provision the workspace, inspect partial summaries while processing continues, review all submissions, record Keep/Reject/Hold, preview an organization plan, apply it once, refresh the page, and verify exactly which files moved. Another folder remains entirely unaffected.

The page remains useful when inference is unavailable: existing summaries, sorting, manual review, notes, and approved file actions continue to work. If the helper is unavailable, the generated HTML remains a clearly labeled, read-only snapshot.

### 2.2 Priority levels

| Level | Delivery scope | Completion meaning |
| --- | --- | --- |
| P0 | Local setup, database, HTML, human decisions, document extraction, OpenClaw summaries, safe file actions, recovery | Usable local pilot with all safety gates |
| P1 | Folder-bound chat, shared-host authentication, concurrent-review conflicts, complete portability and operating documentation | Full agreed version 1 scope |
| P2 | Optional OCR, additional formats, advanced review collaboration, optional automatic refresh | Later additions; never a substitute for P0/P1 |

The first local demonstration is not the full finished product. Chat and supported shared-drive operation must be delivered before claiming completion of the entire requested scope.

### 2.3 Functional requirement register

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-01 | Idempotent setup of one independent instance per selected folder | P0 |
| FR-02 | Versioned helper, template, schema migrations, and asset integrity checks | P0 |
| FR-03 | Incremental discovery and processing of PDF, DOCX, and TXT | P0 |
| FR-04 | Evidence-backed summaries and explicit unknown/manual-review states | P0 |
| FR-05 | Compact sortable table, stable filters, document links, detail panel | P0 |
| FR-06 | Row decisions, bulk selections, notes, and persistent review tasks | P0 |
| FR-07 | Separate save, plan, approval, apply, and restore operations | P0 |
| FR-08 | No-clobber moves, recoverable Trash, journal, and crash reconciliation | P0 |
| FR-09 | Refreshable connected page and self-contained read-only snapshot | P0 |
| FR-10 | OpenClaw adapter, restricted analysis route, private-mode enforcement | P0 |
| FR-11 | Folder-specific chat and validated natural-language filter proposals | P1 |
| FR-12 | Authenticated shared-host review with optimistic concurrency control | P1 |
| FR-13 | Backup, restore, controlled relocation, and migration verification | P0/P1 |
| FR-14 | Auditability, data minimization, access control, retention configuration | P0/P1 |

## 3. System architecture and responsibilities

### 3.1 Component boundaries

**Reusable OpenClaw skill.** Contains instructions, a bootstrap entry point, reviewed helper artifacts, HTML/CSS/JavaScript templates, extraction adapters, schemas, migrations, and fixtures. Its setup workflow deploys and registers an instance. Its ongoing workflow requests scans, summaries, reports, and action plans through the helper.

**Folder-local application.** Holds the authoritative recruiting state. The report is a generated view of that state, never the source of truth. No browser local-storage database is authoritative.

**Local helper.** Serves the application, authenticates requests, performs all database writes, owns the durable work queue, validates analysis, generates snapshots, and executes approved filesystem operations. This is deterministic application code, not an LLM choosing how to move files.

**OpenClaw analysis adapter.** Sends bounded evidence and approved criteria to a restricted OpenClaw agent route. Receives structured results and explanations. It does not pass browser-supplied agent targets, arbitrary tools, filesystem commands, or model overrides through to the Gateway.

**Browser.** Displays state, supports deterministic sorting/filtering, collects reviewer decisions, and obtains explicit approval of concrete plans. It never receives the Gateway credential or direct database access.

### 3.2 Why there is no second model runtime

OpenClaw supplies the agent loop, tool integration, and prompt assembly; the underlying configured model supplies inference. Its documented runtime also maintains workspace/session state outside ordinary project files. Reuse that existing route rather than starting a new loaded model per job folder. [S1]

“Everything stays in the folder” applies to authoritative application records and portable reports. Host dependencies, service registration, OS credentials, OpenClaw runtime records, and any explicitly approved provider processing are exceptions that setup must disclose. Local-only processing must block remote fallback, including summarization, chat, OCR, and any future embeddings.

### 3.3 Proposed reference implementation

Use Python 3.12 or later, FastAPI with a pinned ASGI server, the standard SQLite interface, and plain HTML/CSS/JavaScript. Use a pinned PDF text parser and a DOCX parser; `pypdf` and `python-docx` are proposed adapters, subject to dependency and security review. The application must be packaged so normal setup does not require building native dependencies or downloading a fresh runtime for every folder.

Use one helper process per instance for the initial implementation. Multiple instances may share an approved host runtime installation, but not their databases or review state. A future host supervisor is optional; do not make it an MVP dependency.

Use a single in-process scheduler with durable SQLite jobs. Parsing occurs outside the event loop in constrained worker processes. Model calls run without holding database transactions. No distributed execution is required for 400 submissions.

The coding agent must pin exact dependency versions and publish the tested compatibility matrix. This document does not claim a particular library or OpenClaw release is installed on the user's machine.

## 4. Folder layout, ownership, and portability

```text
Job - Operations Manager/
    candidate-001.pdf
    candidate-002.docx
    review.html
    Rejected/
        <document-id>/
            original-filename.pdf
    Trash/
        <batch-id>/<document-id>/
            original-filename.docx
    .review/
        instance.json
        job.json
        review.db
        app/
            manifest.json
            helper-entrypoint
            templates/
            assets/
        extracted/
            <document-id>/<source-revision>.json
        exports/
            snapshot.json
        journals/
            <batch-id>.json
        backups/
        migrations/
```

The database is authoritative. `instance.json`, `job.json`, snapshots, and journal exports are application-generated manifests or projections. Editing them externally does not mutate authoritative state. Any supported import requires schema validation, revision checking, and audit.

Paths stored in records are relative to the registered root. A protected host registry maps instance IDs to canonical roots and service addresses. Instance identity is not a hash of an absolute path and is not derived from a candidate name.

The host registry also stores or references the trusted release manifest and secrets. A checksum in the same writable folder as an executable is not sufficient protection against replacement. Bootstrap verifies deployed code against its trusted installed release before launching it.

Initial discovery excludes `.review`, `Rejected`, `Trash`, temporary files, symlinks, junctions, and unselected nested folders. Registered documents remain tracked after managed moves. Directory names are reserved only after ownership/collision checks; setup must not take over a pre-existing unrelated folder or overwrite an unrelated `review.html`.

A **relocation** preserves the instance ID and history. Stop writes, create a consistent backup, copy or move the workspace, rebind the root on the new host, verify hashes, and start one owner. A **fork** creates a new instance ID, resets active plans and credentials, and requires explicit choice about importing decisions and notes. Never run two live copies with the same instance ID.

## 5. Skill packaging, setup, and lifecycle

### 5.1 Actual OpenClaw packaging contract

OpenClaw skills use a directory with `SKILL.md`, YAML frontmatter, and Markdown instructions. The documented minimum frontmatter includes `name` and `description`; `{baseDir}` addresses bundled files. This enables packaging the workflow and helper, but is not a built-in resume-review feature. [S2]

The installed skill should be named `resume-review`. Use the deployment conventions supported by the actual installed OpenClaw release. Do not silently alter unrelated skills, agent memory, model configuration, or global tool policies.

### 5.2 Setup sequence

1. Resolve the explicitly selected root and reject traversal, untrusted links, and unsupported storage topology.
2. Check read/write access, available space, reserved-name conflicts, instance identity, and existing helper ownership.
3. Verify the approved helper release, runtime, document parsers, OpenClaw connectivity, and model/privacy policy.
4. Initialize an empty database or back up and migrate an existing one. Never reset it because a template changed.
5. Register the job description and human-approved criteria. Without criteria, allow neutral summaries and manual review, but no fabricated job-match assessment.
6. Deploy the versioned helper entry point and report assets. Reuse approved host dependencies.
7. Start or reconnect to the sole healthy helper. Obtain an OS-backed ownership lock, not just a PID file.
8. Provision the local review session and any restricted analysis route with the required administrator authorization.
9. Return the instance ID, connected page address, snapshot path, storage mode, model route, health, and next action.
10. Offer an explicit scan/summarize operation. Large inference work must show estimated scope and available budget before initiation.

Respect OpenClaw execution policy and approvals. A skill's text does not grant shell or administrator permission. Do not broaden an allowlist to an unrestricted interpreter as a setup shortcut. [S3]

### 5.3 Application command contract

These are **new product commands to implement**, not existing OpenClaw commands:

```text
resume-review setup --folder <root> --job <job-description-file>
resume-review start --instance <id>
resume-review status --instance <id> --json
resume-review scan --instance <id>
resume-review summarize --instance <id> --changed-only
resume-review render --instance <id>
resume-review plan-actions --instance <id> --request <json-file>
resume-review apply-actions --instance <id> --batch <approved-batch-id>
resume-review backup --instance <id>
resume-review repair --instance <id> --dry-run
resume-review stop --instance <id>
```

A CLI command cannot manufacture human approval. `apply-actions` succeeds only for a still-valid approval already recorded by the review interface. The analysis agent must not receive even that executor credential. Restore is requested through the same plan/approve/apply workflow.

Machine-readable output uses `ok`, `code`, `instance_id`, `data`, `warnings`, and `request_id`. Define stable exit codes for success, invalid input, permission failure, conflict, dependency failure, and unsupported storage. Do not encode candidate names or credentials in error telemetry.

On repeat setup, preserve IDs, summaries, notes, reviewer decisions, completed tasks, and journals. On upgrade, make a verified backup before migration; reject unsupported downgrades rather than corrupting newer data.

## 6. Discovery and document processing

### 6.1 Supported initial inputs

Text-bearing PDF, DOCX, and TXT. Sniff the actual file type rather than trusting only the extension. Legacy DOC, archives, application emails, password-protected files, corrupt files, and scan-only PDFs receive explicit unsupported or manual-review states. They remain visible and are never automatically rejected.

Proposed defaults: 25 MiB maximum source file, 50 pages, and 200,000 extracted characters per submission. These are adjustable resource limits, not silent truncation rules. Exceeding a limit creates a review task and a visible reason. Any later increase must respect parser and model limits.

OCR is an optional, explicitly enabled local fallback. Do not upload scans to a cloud OCR provider or infer from photographs without authorization.

### 6.2 Stable discovery

Record original filename, current relative path, byte size, content hash, filesystem identity where available, and ingestion time. Do not call a file's modification time the application submission date. Unknown submitted dates remain unknown.

Wait for a file to become stable before parsing: compare size/metadata across a configurable interval, copy to a controlled temporary read snapshot, hash the bytes being parsed, and confirm the source did not change. A file still being copied stays pending rather than producing a misleading partial summary.

Same bytes at two paths create two submissions plus a duplicate-content flag. Do not automatically delete, merge people, or transfer a decision between duplicates. Path changes alone should preserve identity when reconciliation can establish it reliably; ambiguous external moves require human reconciliation.

### 6.3 Extraction and analysis pipeline

```text
Discover -> stabilize -> register revision -> extract source spans
         -> validate extraction -> enqueue bounded analysis
         -> validate result -> commit profile/evidence/tasks
         -> publish snapshot
```

Source spans carry stable IDs, page numbers for PDFs, paragraph/table locators for DOCX, and line ranges for TXT. Extract DOCX table content as well as paragraphs. Do not invent page numbers for formats where pagination is not reliably defined.

A document's processing state is independent of its review decision. On parser failure, retain any previous valid profile as visibly stale and create a manual-review task. Failures must not make rows disappear.

Process incrementally. Cache extraction by content hash and parser version; cache assessments by document revision, approved criteria version, prompt version, model route/version where known, and analysis schema version. Same-document retries must not create duplicate profiles or tasks.

### 6.4 Queue, retries, and cancellation

Default to one model request in flight per instance, with a configurable host-wide cap. Reserve capacity for interactive chat rather than launching 400 simultaneous requests. Use durable jobs, leases, retry counts, and cancellation tokens.

Retry transient failures at most three total attempts with bounded backoff. Permit at most one structured-output repair attempt within that budget. Permanent parser errors and repeated invalid model output go to manual review. Stop/cancel lets an already-running operation finish or time out, but does not discard committed results.

Before accepting a result, compare its input revision and criteria version with current state. Superseded results may be retained for history but must not become the current assessment.

## 7. Evidence-backed analysis and criteria

### 7.1 Two-stage analysis

Separate **neutral factual extraction** from **job-specific criterion assessment**. Changing a requisition should reuse valid extracted evidence rather than reparsing every original. The approved criteria, not a resume's contents, define what to examine.

A summary contains a short role/experience overview, relevant explicit skills, material qualification evidence, uncertainty, and suggested verification tasks. Store source locators for every material factual claim. Applicant statements are not independently verified facts about employment history or credential validity.

Use these criterion results only:

| Result | Meaning |
| --- | --- |
| `supported` | Relevant applicant-reported evidence was located |
| `not_found` | The processed document did not establish the criterion |
| `unclear` | Relevant text exists but cannot safely establish the criterion |
| `needs_manual_review` | Processing limits or quality prevent assessment |

`not_found` is not “unqualified.” No result directly sets Keep or Reject.

### 7.2 Evidence validation limits

The helper can verify schema, permitted criterion IDs, source revision, locator existence, and whether a quoted excerpt occurs in the identified source span. Those checks do **not** prove that an interpretation is correct. Human evaluation and visible evidence are still required for semantic accuracy.

If a field is unsupported, omit it or mark it uncertain. Do not add exact years of experience from overlapping dates, assume full-time work, infer age from graduation dates, or declare a license active without evidence. Any calculated duration must state the as-of date, relevant role intervals, handling of overlap, and uncertainty bounds.

### 7.3 Analysis result example

Illustrative application payload; IDs and content below are synthetic:

```json
{
  "schema_version": "1.0",
  "instance_id": "inst_demo",
  "document_id": "doc_demo_001",
  "source_revision": 2,
  "criteria_version": 3,
  "summary": {
    "text": "Reports commercial renovation coordination experience.",
    "evidence_ids": ["ev_001"]
  },
  "criteria": [
    {
      "criterion_id": "cr_01",
      "result": "supported",
      "explanation": "The resume describes subcontractor coordination.",
      "evidence_ids": ["ev_001"]
    }
  ],
  "evidence": [
    {
      "id": "ev_001",
      "span_id": "page_1_block_4",
      "locator": {"page": 1},
      "quote": "Coordinated subcontractors on commercial renovations."
    }
  ],
  "suggested_tasks": [],
  "warnings": []
}
```

The helper attaches trusted run metadata itself: actual request ID, parser/prompt/model configuration, start/end times, token usage when available, and validation outcome. Do not trust the model to report which route actually processed the data.

### 7.4 Approved criteria and change management

Each criterion has an ID, plain-language definition, job-related rationale, evidence rule, optional required/preferred label, creator, approval timestamp, and version. The operator reviews proposed criteria before activation. Required/preferred labels do not authorize automatic rejection or hide unknowns.

A criteria change makes dependent assessments stale. A changed document makes its summary stale and flags an existing decision for reconsideration. Neither event automatically overwrites the human decision. A saved filter referring to a removed criterion becomes visibly invalid until revised.

## 8. Review page and interaction requirements

### 8.1 Page structure

Use one compact application with a header, status/count strip, toolbar, main table, document-detail drawer, optional chat panel, and an action-review drawer. Do not turn the landing page into a marketing site or a large ATS dashboard.

The header shows job title, instance identity, connected/snapshot mode, last analysis time, snapshot/state revision, and helper/agent health. The count strip separates whole-instance totals from the current filtered set. Show processed, unreviewed, kept, rejected, held, manual-review, and pending-action counts with unambiguous labels.

The toolbar provides search, explicit filter chips, location view, processing state, reviewer state, sort, scan, summarize changed items, refresh, and review pending actions. Refresh must not trigger inference or approve anything.

### 8.2 Table specification

| Column | Behavior |
| --- | --- |
| Selection | Checkbox for bulk scope only; not a hiring decision |
| Submission | Display name when available, filename fallback, stable document link |
| Summary | One or two lines; expand in detail drawer |
| Relevant evidence | A few approved criterion indicators; no aggregate fit score |
| Review tasks | Open task count and processing warnings |
| Decision | Mutually exclusive Unreviewed / Keep / Reject / Hold controls |
| File action | Pending move/restore/trash state and execution outcome |
| File access | Open original and open detail; no arbitrary path input |

Default sorting is ingestion order with document ID as a stable tie-breaker. Support filename/name, ingestion date, processing status, reviewer disposition, task count, and explicitly approved evidence fields. Names can be used to locate and alphabetize records, not as a suitability feature. Unknown values are visibly distinct from zero and sort consistently.

Use pagination of 50 rows by default with a visible total and explicit page navigation. Rendering all 400 rows is acceptable if accessibility and performance targets pass. Virtualization is optional, not an excuse for losing keyboard access or selections.

### 8.3 Decisions and save behavior

A radio group or segmented control represents one decision per submission. A separate button/menu offers Move to Trash. Do not use independent Keep and Reject checkboxes that can both be active.

Decision changes save through the helper with the expected decision revision. Show Saving, Saved, Conflict, or Failed. A failed request remains visibly unresolved; do not present it as committed. Changing a decision creates an audit event but does not move a file.

While a Trash request is pending or the file is in Trash, freeze disposition controls until the request is canceled or the file is restored. Preserve the pre-Trash decision and location in history.

Bulk actions must distinguish **select this page** from **select all matching results**. The latter resolves to an explicit, immutable ID/revision set at confirmation time. Adding a resume or changing a filter afterward must not silently extend that set. Show the selected count, hidden-selected count, and a clear deselect action.

For database-only bulk decision changes, validate the complete set first and commit all changes in one transaction or return a conflict without applying any. Filesystem batches have separate partial-completion semantics.

### 8.4 Detail drawer and review tasks

Display the original document link, extracted text, short summary, criterion-by-criterion evidence, source location, processing limitations, human notes, decision history, and task checklist. The reviewer can correct a factual extraction through a recorded override without editing the original resume.

Tasks include verify a certification, clarify date overlap, inspect an unreadable document, reconsider a decision after a source update, and reconcile a failed move. Distinguish agent-suggested tasks from human-created tasks. Store task state separately from profiles. Regeneration must not overwrite notes or automatically reopen completed tasks; new material evidence may create a linked new task.

Use a deduplication key based on task type, document, criterion, and relevant source revision. Closing a task records who closed it and why. A “needs attention” task is not a negative assessment of the applicant.

### 8.5 Connected page versus direct-file snapshot

**Connected mode:** the helper serves the page over an authenticated local or protected network address. State comes from the helper API; edits, chat, and approvals are enabled according to role.

**Snapshot mode:** double-clicking `review.html` shows embedded data and bundled presentation logic without needing a neighboring JSON fetch. Sorting and filtering work in memory; persistent edits, chat, scan, and file actions are disabled. Display the exact snapshot timestamp and a clear Open connected review link containing no credential.

Modern browsers commonly give `file:` documents opaque origins, so fetching neighboring files is not a reliable portable design. A self-contained snapshot avoids relying on that behavior. [S10]

Escape all embedded values, including script-closing sequences. Do not interpolate applicant content as HTML or JavaScript. Publish via a temporary file and a tested replacement operation; when replacement is temporarily blocked, keep the old valid report and expose a stale-snapshot warning. Connected mode must show current committed database state even if snapshot publishing needs a retry.

A snapshot contains sensitive recruiting information. Its visibility follows filesystem permissions, not the helper's login screen. Local document links are relative and safely encoded; when a browser blocks opening a file, show the path and instructions rather than claiming the document has vanished.

## 9. Folder-scoped chat and filtering

### 9.1 Retained interaction

Examples: “Show submissions that explicitly mention commercial construction.” “Within these results, show subcontractor coordination.” “Explain the evidence for this criterion.” “Which documents still need manual review?”

Chat must be inside the companion application for the full release. A native OpenClaw conversation may also invoke the skill, but that is not a substitute for the requested page-bound experience.

### 9.2 Query pipeline

The helper binds each request to the authenticated reviewer, instance ID, current criteria version, explicit scope, current filter, and selected document IDs. The model returns an explanation and a structured filter proposal. The browser shows the interpreted conditions and unknown-value treatment before applying them.

Compile a small allowlisted filter tree into parameterized queries. Never execute model-produced SQL, regular expressions without limits, JavaScript, shell commands, or filesystem paths. Chat may propose new job-related criteria, but the reviewer must approve them before analysis begins.

A filter must not silently hide unprocessed submissions. Default unknown handling is `include_with_warning`. The reviewer may explicitly choose to show only supported evidence, with a persistent count/link for omitted unknowns. The system must not translate “not established” into “does not have.”

### 9.3 Proposed filter contract

```json
{
  "schema_version": "1.0",
  "instance_id": "inst_demo",
  "criteria_version": 3,
  "expression": {
    "type": "and",
    "children": [
      {
        "type": "predicate",
        "field": "review_state",
        "op": "in",
        "value": ["unreviewed", "keep", "hold"]
      },
      {
        "type": "predicate",
        "field": "criterion:cr_01",
        "op": "is_supported"
      }
    ]
  },
  "unknown_policy": "include_with_warning",
  "sort": [{"field": "ingested_at", "direction": "asc"}]
}
```

Allow only registered fields and operators. Bound tree depth to three and predicates to twenty initially. Evidence predicates use true/false/unknown semantics; a missing or stale assessment evaluates unknown. Do not introduce a generic negation operator until its unknown behavior is explicitly implemented and tested.

Saved filters store their definition and criteria version. Applying or undoing a filter changes the view only. Chat requests such as “move the rejects” create an action plan, never an approval or an immediate move.

### 9.4 Context isolation

Use an opaque conversation ID per instance and reviewer; bind it server-side to the authenticated identity. Do not use candidate names or full folder paths as provider-visible session IDs. A new analysis document uses a distinct analysis-session binding, not the reviewer's freeform chat thread.

Retrieve only permitted evidence needed for the question. Do not stuff 400 full resumes into a prompt. For a new criterion, queue explicit per-document assessment and show incomplete coverage until it finishes. Do not answer an all-folder question from a partial context as though every submission was inspected.

Disable cross-job memory retrieval and writing for analysis. Store the application's conversation history in the instance database, while disclosing any separate OpenClaw session retention. A reviewer losing access to an instance must lose access to its chat and document endpoints as well.

## 10. State model and action semantics

Keep five concepts independent:

| Dimension | Representative values | Owner |
| --- | --- | --- |
| Processing | discovered, extracting, analyzing, ready, manual_review, error, stale | Helper pipeline |
| Review decision | unreviewed, keep, reject, hold | Human reviewer |
| Actual location | active, rejected, trash, missing, conflict | Verified filesystem reconciliation |
| Pending intent | none, move_rejected, restore_active, move_trash, restore_previous | Human request or unapproved agent proposal |
| Action execution | planned, approved, applying, completed, partial, blocked, canceled | Helper executor |

Also track `decision_needs_recheck` after material input changes. A row can legitimately be Reject + active + move pending. It must not display a successful rejection-folder move before that move actually happens.

### 10.1 File behavior after approval

| User intent | Deterministic behavior |
| --- | --- |
| Keep in active folder | No filesystem move |
| Keep while in Rejected | Propose return to recorded active path; move only after approval |
| Reject | Propose move into `Rejected/<document-id>/<original-filename>` |
| Hold or Unreviewed | Preserve current location; do not implicitly restore |
| Move to Trash | Propose move into `Trash/<batch-id>/<document-id>/<filename>` |
| Restore from Trash | Propose previous recorded path, preserving earlier decision |
| Missing source | Block the action and create a reconciliation task |

A restored rejected file may return to Rejected because that was its previous location. “Restore previous location” and “return to active folder” are separate intents. The UI must not collapse them into an ambiguous Undo button.

Permanent deletion and automatic Trash purging are excluded from version 1. Retention policies may produce review tasks, not silent destruction.

## 11. Database and persistence contracts

### 11.1 Logical tables

| Table | Essential fields and constraints |
| --- | --- |
| `instances` | UUID, schema/app versions, state_revision, created_at; one instance per database |
| `jobs` | Job text/reference, current criteria version, owner approval |
| `criteria` | ID, version, definition, rationale, evidence rule, approved_by/at |
| `documents` | UUID, relative paths, original name, current revision/hash, location_version, processing state |
| `document_revisions` | Immutable hash, size, parser metadata, source span reference, captured_at |
| `profiles` | Document/revision/criteria/prompt/schema/model binding, summary, validation, generated_at |
| `evidence` | Profile ID, claim/criterion ID, span locator, excerpt, result |
| `decisions` | Document ID, disposition, decision_revision, actor, timestamp, needs_recheck |
| `notes` | Document ID, body, author, note_revision, created/updated timestamps |
| `review_tasks` | Document/criterion/source binding, dedupe key, state, human resolution |
| `action_intents` | Document ID, requested action, intent_revision, requester, state |
| `action_batches` | ID, plan JSON/hash, approval actor/time, expiry, execution state |
| `file_operations` | Operation ID, batch, source/destination, expected identity/hash, durable step, error |
| `processing_jobs` | Job key, input versions, state, lease token/expiry, attempts, cancellation |
| `conversations` / `messages` | Opaque instance/reviewer binding, local history, adapter session reference |
| `audit_events` | Monotonic sequence, actor, event, affected IDs, prior/new refs, timestamp, outcome |
| `idempotency_records` | Actor/route/key scope, request hash, response or job reference |

Authentication secrets and Gateway tokens are not stored in the portable database. Store only non-secret actor references and role assignments required to explain history.

### 11.2 Required invariants

Use foreign keys, validated enum constraints, unique processing-job keys, stable document IDs, and transactions for related state changes. Index current path, processing state, review state, and criterion result. Stable pagination always includes a deterministic ID tie-breaker.

Separate decision revisions from note revisions so a note edit does not automatically invalidate an otherwise identical move plan. Source, location, decision, intent, and criteria revisions relevant to an action must be checked before execution.

A successful application mutation increments the state revision and emits an audit event in the same transaction. Analysis cannot write into human decision or approval tables. Storage paths are resolved by document IDs, never trusted from a model response.

### 11.3 SQLite configuration

Use SQLite only on supported storage-host-local filesystems. Local WAL may be used with a patched, tested SQLite build and deliberate durability/checkpoint settings. Record the actual SQLite version in diagnostics. Do not configure WAL on an SMB/NFS-mounted live database; SQLite explicitly documents that limitation. [S8]

Serialize writes through the helper and bound lock waits. Use the SQLite backup API or an equivalent verified snapshot mechanism, not an arbitrary copy of an active database file. A database backup alone does not back up the original resumes; workspace backup must coordinate both. [S9]

Audit history is append-only through the application API, but a filesystem administrator can still modify local data. Do not advertise cryptographic tamper-proofing without an independently protected integrity mechanism.

## 12. Helper API and permission contracts

All business endpoints are scoped under `/api/v1/instances/{instance_id}`. Authenticate first, authorize the instance and operation, then resolve document IDs. A hidden or unguessable UUID is not authorization. The API must return structured validation errors without leaking other instances.

### 12.1 Endpoint surface to implement

| Method and suffix | Purpose | Allowed principal |
| --- | --- | --- |
| GET `/status` | Health, versions, counts, queue, snapshot status | Viewer, reviewer, administrator |
| GET `/documents` | Paginated/filterable list | Viewer, reviewer |
| GET `/documents/{id}` | Detail and evidence | Viewer, reviewer |
| GET `/documents/{id}/original` | Scoped document stream/download | Viewer, reviewer |
| PATCH `/documents/{id}/decision` | Human disposition with expected revision | Reviewer |
| POST `/decisions/bulk` | Explicit-set atomic review-state change | Reviewer |
| POST/PATCH `/documents/{id}/notes` | Human notes with revisions | Reviewer |
| POST/PATCH `/tasks` | Human task creation/completion | Reviewer |
| POST `/criteria/proposals` | Propose definitions only | Reviewer, scoped agent |
| POST `/criteria/{version}/activate` | Approve criteria version | Reviewer |
| POST `/scan` | Queue deterministic scan | Reviewer, scoped orchestrator |
| POST `/analysis/jobs` | Queue bounded analysis | Reviewer, scoped orchestrator |
| POST `/analysis/results` | Validate a leased result for one job | Bound worker identity only |
| PUT `/documents/{id}/action-intent` | Save/cancel pending intent with expected revision | Reviewer |
| POST `/actions/plan` | Build concrete immutable plan | Reviewer, scoped orchestrator |
| POST `/actions/{batch}/approve` | Record plan-bound human authorization | Reviewer; never model/worker |
| POST `/actions/{batch}/apply` | Start an already approved batch | Reviewer, scoped executor |
| POST `/actions/{batch}/cancel` | Cancel unstarted work | Reviewer |
| POST `/actions/{batch}/restore-plan` | Create a new inverse plan | Reviewer |
| POST `/chat` | Queue or stream folder-scoped conversation | Reviewer |
| GET `/jobs/{id}` | Poll progress/result | Authorized requester |
| POST `/backup` | Create consistent workspace backup | Administrator |

The application must not expose generic SQL, shell, upload-and-execute, arbitrary path read, or proxy-any-Gateway-request endpoints. The frontend's OpenClaw interaction always passes through a narrow helper adapter.

### 12.2 Mutation rules

For versioned changes require the appropriate `expected_revision`. For retryable POST operations require `Idempotency-Key`, scoped to principal, instance, and route. Reusing a key with a different payload returns a conflict; repeating the same request returns the original result or job ID without repeating side effects.

A standard response includes request ID, instance ID, committed state revision, data, and warnings. Use 401/403 for authentication/authorization failure, 409 for stale state or conflicting idempotency reuse, 422 for invalid application data, and 503 for unavailable dependencies. Long work returns 202 with a durable job ID. These are proposed API choices, not existing OpenClaw endpoints.

Example decision write:

```json
{
  "document_id": "doc_demo_001",
  "decision": "reject",
  "expected_revision": 7
}
```

The helper obtains the reviewer identity from the authenticated session, not an `actor` field supplied by the caller. Saving this payload changes only the review record; it does not authorize a file move.

### 12.3 Action plan payload

The helper generates and canonicalizes the plan; it does not accept a model-supplied plan as authoritative:

```json
{
  "schema_version": "1.0",
  "instance_id": "inst_demo",
  "batch_id": "batch_demo_001",
  "criteria_version": 3,
  "operations": [
    {
      "operation_id": "op_demo_001",
      "document_id": "doc_demo_001",
      "kind": "move_rejected",
      "source": "candidate-001.pdf",
      "destination": "Rejected/doc_demo_001/candidate-001.pdf",
      "source_revision": 2,
      "expected_sha256": "<64-character-content-hash>",
      "decision_revision": 8,
      "intent_revision": 1,
      "location_version": 1
    }
  ],
  "plan_hash": "<hash-of-canonical-plan-excluding-this-field>"
}
```

The approval record separately captures the authenticated reviewer, plan hash, approval time, and expiry. The example placeholders are documentation, not runnable request values.

## 13. Safe file actions, approvals, and recovery

### 13.1 Plan -> approve -> apply

First save decisions. Then resolve the exact selected documents into a concrete plan. Show counts, source locations, destination locations, skipped/no-op items, warnings, and any Trash actions. The reviewer confirms that exact plan. Sorting, filtering, a chat message, or the fact that a file is marked Reject is not equivalent to approval.

Use a proposed 15-minute approval lifetime before execution begins. A changed source, decision, intent, location, criteria version, destination, or root binding invalidates the relevant authorization. Documents flagged `decision_needs_recheck` require the reviewer to reconfirm against the current source or record an explicit override reason before approval. Revalidate the whole plan before the first operation and each remaining operation immediately before execution. Notes or unrelated new submissions do not alter the approved set.

If the initial validation fails, move nothing. If a conflict appears during execution, stop the remaining work, record partial completion, and require a revised plan for the remainder. Completed operations are not secretly rolled back.

### 13.2 Filesystem safety

All managed moves stay within the registered root and the same supported filesystem/volume. Do not silently fall back from a failed rename to copy-and-delete. Cross-volume organization is out of scope.

Implement a tested no-clobber move primitive for each supported host OS. A check-then-overwriting-rename sequence is not enough. Never overwrite a destination, replace an unrelated file, follow a symlink/junction escape, or concatenate shell commands. Revalidate path containment and file identity at the actual operation boundary.

Record operation intent durably before touching files. Verify the source hash/revision and destination ownership. After the move, verify the destination, commit its location and operation state, and publish the updated snapshot. Keep transactions short; database updates and filesystem operations are not one atomic transaction.

### 13.3 Recovery contract

For each operation retain states such as `planned`, `intent_recorded`, `file_moved`, `committed`, and `needs_reconciliation`. After a crash, inspect the journal plus actual source/destination identity and content.

| Observed condition | Recovery behavior |
| --- | --- |
| Source exists as expected; destination absent | Safe candidate to resume after approval/expiry policy checks |
| Source absent; expected operation-owned destination verified | Reconcile the location and commit the already-performed move |
| Both exist | Stop for reconciliation; never delete one merely because hashes match |
| Neither exists | Mark missing and request human investigation |
| Content, path ownership, or identity differs | Block and preserve evidence of the conflict |

Destination identity includes its operation/document-owned namespace and recorded metadata, not just a matching content hash. A copied file from another actor must not be mistaken for proof of a completed move.

Replaying an apply request must not repeat completed operations. Prefer idempotent state transitions and explicit reconciliation over an untestable claim of “exactly once” filesystem execution.

Cancel stops work not yet started; it cannot undo a completed move. Restore/undo produces a new plan and audit trail. If the old path is occupied or content changed externally, show the conflict and request new approval instead of overwriting.

## 14. OpenClaw integration and restricted execution

### 14.1 Verified interface, proposed adapter

OpenClaw documents an optional, disabled-by-default Chat Completions endpoint at `/v1/chat/completions`. It runs a Gateway agent turn. The `model` field can target an agent, and a stable `user` value can maintain a conversation. Shared-secret authentication carries operator-level access rather than a narrowly scoped browser-user credential. [S4]

The application should implement an `OpenClawAdapter` around that verified surface, with compatibility tests for the installed release. Keep the Gateway private and its credential in protected host configuration. Allowlist the application agent target server-side; do not forward arbitrary browser headers, model IDs, tool definitions, or endpoint paths.

A configured OpenClaw endpoint does not require an OpenAI-hosted model. The adapter must honor the operator's approved provider/local-model policy.

### 14.2 Mandatory separation of privileges

Use a trusted setup/orchestration context for installation and a separate restricted analysis context for resume content. The analysis context must have no shell, write/edit, browser-control, messaging, credential-reading, unrestricted file-reading, or cross-session access. It receives only the current job's bounded data and returns structured output.

Do not set an applicant-controlled folder as OpenClaw's instruction workspace. A resume folder might contain a malicious `AGENTS.md`, `SKILL.md`, or similar file; these are data or ignored files, never instructions to load. Keep agent instructions in a trusted, separately managed workspace.

Actual tool policy, sandbox/OS permissions, and the helper's authorization checks enforce this separation. Instructions inside `SKILL.md` are not a security boundary. OpenClaw documents configurable per-agent tool permissions; verify that the chosen restricted route truly lacks dangerous capabilities. [S6]

The model does not receive the worker-result submission credential, approval credential, or executor credential. The helper dispatches the call and commits a validated result using its own bound job identity. A response that imitates an API command remains data.

The application must fail closed for inference when it cannot verify an adequately restricted route. Manual review remains available. Do not fix failed integration by attaching the dashboard to an unrestricted personal agent.

### 14.3 Trust boundary and compatibility gate

OpenClaw's documented security model assumes a trusted operator/team boundary, not mutually adversarial tenants on one Gateway. This application targets one organization/team. Separate organizations or mixed-trust tenants require separate security boundaries, not just different folder IDs. [S5]

Before live use, verify skill loading, authorized helper invocation, endpoint activation, authentication, agent targeting, session separation, tool denial, timeout behavior, JSON handling, provider route, and failure reporting. Record the tested OpenClaw version. Mock results do not count as passing this gate.

## 15. Local and shared-drive deployment

### 15.1 Local mode

The folder, helper, and SQLite database are on the same host's supported local filesystem. Bind to loopback by default. Use an authenticated launch/pairing mechanism tied to the operating user, then issue an application session. No administrative privilege should be needed after initial approved installation.

The folder-local helper entry point reconnects to the correct instance, rather than starting a new writer on every browser refresh. A stopped helper can be restarted without re-running inference. The direct-file report remains read-only until connected mode is opened.

Target Windows 11 for the local helper/reviewer workflow and a supported Linux host for shared deployment; add macOS support when tested. These are implementation targets, not assertions about OpenClaw's native host support. Where OpenClaw uses WSL or another host, verify the bridge and keep the database owned by the actual storage host. Do not treat a network or compatibility-layer mount as tested native local storage by assumption.

### 15.2 Shared folder with storage-host-local helper

Run the helper on the machine physically hosting the job folder and use that machine's local filesystem path. Reviewers connect to the helper via authenticated HTTPS on an approved private network or protected tunnel. They do not open the live SQLite database through a mapped drive.

SQLite describes network-filesystem synchronization and locking risks and a storage-local proxy approach. This is why the database stays beside the resumes on the storage host while remote clients use the application API. [S7]

One writer on a different machine does not remove the underlying network-filesystem risk. Setup must detect or explicitly verify storage topology. Unknown topology is not automatically accepted.

If the NAS cannot run the helper, full writable, folder-contained mode is unsupported on that NAS for version 1. Report that plainly. Do not silently move authoritative recruiting state to another computer or a cloud database. A later architecture exception requires explicit approval; read-only snapshots can still be viewed.

### 15.3 Shared access controls

Use distinct authenticated reviewer identities with Viewer, Reviewer, and Administrator roles. A display name typed into the page is not authentication. The shared helper may use a tested host-local account store or an existing identity-aware proxy; document and test the selected mechanism before release.

Host administrators retain filesystem power. Reviewers should use the UI for mutations, while application code, database files, secrets, and managed destinations are protected by ACLs. An inbound drop area may be writable for submission intake, but that must not grant applicants access to the report or `.review` directory.

Only one helper owner is allowed per instance. Validate ownership with an OS-backed lock, instance identity, and host registry. Do not steal ownership based only on a stale timestamp. Another host cannot safely take over until the previous owner is stopped/fenced and the database is consistently transferred.

Concurrent edits use optimistic version checks. A second reviewer editing an old decision gets a conflict showing the current value and actor; the server never silently applies last-writer-wins.

## 16. Security, privacy, and hiring controls

### 16.1 Untrusted content and network boundaries

Parse resumes in resource-constrained workers without executing macros, embedded scripts, links, or active content. Treat job descriptions and imported criteria as untrusted text until reviewed. Prompt-injection strings must not change policies, reveal other documents, authorize operations, or trigger external requests.

Serve original files through scoped handlers. Default to safe download; an embedded PDF preview must use a hardened sandbox/isolation strategy. Never expose `.review` through a generic static directory mount. Escape filenames, notes, summaries, chat output, and CSV fields; neutralize spreadsheet formula injection in any later CSV export.

Enforce Host and Origin checks, CSRF protection for session-authenticated mutations, strict CORS, conservative content security policy, clickjacking protections, request limits, and session expiry. Use instance-specific session binding; do not assume cookies are isolated by TCP port. Deny unapproved external asset requests and analytics.

### 16.2 Data flow and privacy

Before first analysis, show where resume text is processed and which route is approved. In local-only mode, loss of the local model must stop analysis rather than trigger a remote fallback. Verify route behavior through configuration and network tests, not a model's statement that it is local.

Avoid sending contact details when they are not needed for qualification analysis. Keep operational logs to IDs, counts, timings, and error codes where possible. Reports, extracted text, chats, backups, and OpenClaw transcripts can all contain recruiting data and require controlled retention.

Keep tokens and login secrets outside shared folders, generated HTML, and portable backups. Do not store live credentials in browser local storage. Disable cross-job/global memory of applicant information for the analysis route. Backups must be protected at least as strongly as the active folder.

### 16.3 Human decision boundaries

The application may summarize explicit professional evidence and help apply approved, job-related filters. It must not infer or filter on protected traits, use names/photos as demographic proxies, score health, religion, family status, presumed personality, or infer age from dates. No automatic rejection or aggregate suitability ranking.

EEOC guidance addresses discriminatory hiring practices and job-related selection requirements; disability-related AI guidance also highlights accessibility and assessment risks. Human review is an application control, not a blanket exemption from employment obligations. [S11] [S12]

Before production use, the organization must approve criteria, review accommodations/accessibility, define access and retention, and assess applicable automated-employment-tool rules. For example, NYC describes bias-audit and notice obligations for covered AEDTs; applicability depends on the tool and use. This PRD does not certify compliance or assume that calling a tool “assistive” removes those obligations. [S13]

The initial release has no purge scheduler. Retention settings create administrative review tasks. A future permanent purge must cover originals, extracts, reports, chats, backups, and applicable runtime/provider records through a separately designed process.

## 17. Performance and quality targets

All numbers below are proposed acceptance targets, not measured results or model-speed promises.

### 17.1 Reference test conditions

Benchmark on a documented four-core or better host with 16 GB RAM and local SSD storage, using a supported browser. Record OS, CPU, memory, storage, Python/SQLite/OpenClaw versions, network topology, document mix, and model route. Exclude model weights and inference service memory from helper-only memory reporting.

Use a synthetic fixture of 400 submissions: 240 PDFs, 120 DOCX files, and 40 TXT files. Include labeled duplicates, scan-only PDFs, encrypted/corrupt files, unusual names, injection text, long documents, overlaps in work dates, and missing qualifications within that total. Generate separate adversarial path and permissions fixtures.

| Metric | Target |
| --- | --- |
| Cached connected page usable, 400 rows | p95 within 2 seconds locally |
| Sort/filter over loaded list | p95 within 250 milliseconds |
| Single decision save | p95 within 500 milliseconds locally, excluding deliberate lock conflicts |
| Row completeness | All 400 submissions accounted for, including failures |
| No-op rescan | Zero new model calls for unchanged inputs/configuration |
| Original-file integrity | Zero unapproved moves or byte changes in the test suite |
| Setup rerun | Zero lost decisions, duplicate rows, or duplicate writers |

Measure ingestion, parsing, inference, snapshot generation, and UI latency separately. Display completed/remaining/error counts and token/cost usage when the route provides it. Do not promise that all 400 resumes will finish in a fixed time independent of hardware and model.

### 17.2 Analysis quality gate

Create a human-reviewed gold set of at least 100 synthetic or appropriately authorized submissions. Label explicit facts, source spans, criterion evidence, and unknowns. Target at least 95% precision and 90% recall on the predefined explicit-fact fields; report denominators and failure categories, not one opaque quality number.

Require valid source references for every material claim in the evaluation set and zero fabricated evidence excerpts. A failing critical claim blocks release even when an aggregate metric passes. Compare paired fixtures that differ only in irrelevant identity cues; qualification extraction and filtering should not change because of those cues.

Evaluate long documents, different layouts, overlapping dates, and unreadable inputs separately. Do not use the model as the sole judge of its own evidence correctness. Real hiring use requires human oversight beyond passing synthetic tests.

## 18. Acceptance test catalog

Each test must have a reproducible fixture, observable assertions, and saved pass/fail output. Mark external integrations as mocked or live. The build cannot be called complete with untested safety gates.

### Setup and state

**AT-01 — Repeat setup.** Run setup twice on a populated instance; IDs, human state, versions, and history survive and only one helper owns it.

**AT-02 — Safe collision.** Place unrelated `review.html`, `.review`, and Rejected directories in a folder; setup refuses destructive adoption and reports the conflict.

**AT-03 — Two independent jobs.** Provision two folders with identical filenames; no data, filters, conversations, or approvals cross between them.

**AT-04 — Asset integrity.** Modify a deployed executable/template; protected manifest verification blocks unapproved execution or reports a controlled repair requirement.

**AT-05 — Upgrade preservation.** Upgrade a previous schema after backup; decisions, notes, completed tasks, IDs, and history remain intact. Unsupported downgrade is refused.

### Processing and evidence

**AT-06 — Complete census.** Scan the 400-document fixture; every expected submission is visible, including unsupported or failed items.

**AT-07 — Incremental cache.** Add one new document, then rescan twice; only the new document is analyzed once unless a relevant version changes.

**AT-08 — Copy in progress.** Grow a file during discovery; parsing waits for stable bytes and never commits a partial profile as final.

**AT-09 — Duplicate handling.** Two paths with identical bytes remain separate submissions with duplicate flags and independent decisions.

**AT-10 — Unknown is not failure.** Missing credentials, scanned input, encrypted input, and parser errors become unknown/manual-review, never automatic Reject.

**AT-11 — Evidence validation.** Reject nonexistent source spans, fabricated quotations, invalid criteria, and wrong input revisions. Test semantic correctness separately.

**AT-12 — Date uncertainty.** Overlapping roles and partial dates do not become fabricated exact experience totals or inferred ages.

**AT-13 — Human-state preservation.** Regenerate all profiles and change criteria; notes/decisions/completed tasks remain, while dependent assessments are correctly marked stale.

**AT-14 — Job restart.** Interrupt extraction and analysis; restart safely reclaims expired work without accepting superseded results or duplicating tasks.

### Review and chat

**AT-15 — Table controls.** Sort/filter/paginate 400 records; totals, stable ordering, keyboard access, and selected IDs remain correct.

**AT-16 — Save versus move.** Save Reject, refresh, sort, and chat; no original moves until the separate approval workflow succeeds.

**AT-17 — Bulk scope.** Select a page and all matching results in separate tests; only the confirmed immutable set is affected, even when new files arrive.

**AT-18 — Conflict display.** Two reviewers edit the same decision revision; one succeeds and the stale write receives a visible conflict without overwrite.

**AT-19 — Snapshot mode.** Stop the helper and open `review.html` directly; summaries and in-memory filters work, timestamps are visible, persistent edits/chat are disabled.

**AT-20 — Snapshot interruption.** Interrupt publishing or temporarily lock the report; retain a valid prior snapshot and visibly report staleness.

**AT-21 — Chat interpretation.** Natural-language filters produce validated criteria with visible unknown treatment; filter undo restores the previous view without changing decisions.

**AT-22 — Chat isolation.** Simultaneous reviewers/jobs never share unintended session context; an all-folder answer discloses incomplete coverage.

### File safety and recovery

**AT-23 — Correct approval.** An authenticated reviewer approves a concrete plan; only those operations become executable. Model text cannot create approval.

**AT-24 — Replay protection.** Replay decision, plan, and apply requests using the same idempotency key; no duplicate side effect occurs. Changed payload/key reuse conflicts.

**AT-25 — Stale authorization.** Change source bytes, decision, location, intent, criteria, or root after planning; the stale action is rejected.

**AT-26 — Destination collision.** Create a destination after planning; no overwrite occurs, remaining work stops, and a revised plan is required.

**AT-27 — Escape and volume boundaries.** Traversal, symlinks, junctions, malicious filenames, and cross-volume destinations cannot escape or trigger copy-and-delete fallback.

**AT-28 — Crash points.** Inject crashes before intent persistence, after intent, after move, and before database commit; recovery never loses a file or falsely reports success.

**AT-29 — Partial execution.** Fail the third operation in a batch; completed operations are recorded, remaining operations stop, and the UI accurately reports partial completion.

**AT-30 — Trash and restore.** Trash a kept and a rejected submission; restore returns each to its recorded location and earlier decision. Conflicts block rather than overwrite.

**AT-31 — Ambiguous recovery.** Both source and destination exist, or neither exists; recovery requires reconciliation and never silently deletes a duplicate.

### Security and deployment

**AT-32 — Host topology.** Local-disk and storage-host-local shared modes pass; a remote-mounted live SQLite database is rejected by setup.

**AT-33 — Authentication.** Unauthenticated, wrong-role, wrong-instance, forged-Origin, and CSRF attempts fail. Shared identity comes from authentication, not request text.

**AT-34 — Injection containment.** Resume instructions, HTML payloads, rogue `AGENTS.md`, and model-generated commands cannot execute code, read secrets, or mutate other records.

**AT-35 — Restricted OpenClaw.** Live adapter tests prove dangerous tools are denied and browser requests cannot choose arbitrary agents, credentials, tools, or model overrides.

**AT-36 — Private-mode failure.** Make the approved local model unavailable; processing stops without external fallback or candidate-data egress.

**AT-37 — Owner locking.** A duplicate helper or second host cannot acquire the same live instance; normal restart after controlled shutdown succeeds.

**AT-38 — Backup and relocation.** Restore a coordinated database/original-file backup onto a supported host; IDs, evidence, human state, and document links remain consistent.

**AT-39 — Fairness/accessibility checks.** Irrelevant identity-cue changes do not change qualification results; keyboard-only users can inspect, decide, and approve without relying on color alone.

**AT-40 — Release evidence.** Produce benchmark results, gold-set evaluation, live integration evidence, dependency lockfiles, and an explicit known-limitations list. Unimplemented controls remain labeled incomplete.

## 19. Coding-agent implementation sequence

### Milestone 0 — Contracts and test fixtures

Create the repository, requirement/test mapping, data model, JSON schemas, storage adapter interface, API contract, synthetic fixture generator, and dependency lock strategy. Write invariants and failing safety tests before file-mutation code. Establish supported local storage and the single-writer design.

**Gate:** Schemas and state transitions are coherent; fixtures reproduce the main failure cases without real applicant data.

### Milestone 1 — Local deterministic vertical slice

Build setup, database migrations, helper ownership, list/detail APIs, HTML table, save-state feedback, decisions, notes/tasks, and snapshot generation. Use synthetic precomputed profiles; visibly mark them as fixtures.

**Gate:** AT-01 through AT-05 and applicable review/snapshot tests pass. The operator can review 400 rows without any model connection.

### Milestone 2 — Safe organization

Implement plans, human approval, no-clobber storage adapters, journaled moves, Trash, restore, cancellation, idempotency, and crash reconciliation. This is the most important reliability milestone.

**Gate:** AT-23 through AT-31 pass under injected faults. No testing on real recruiting folders until these gates pass.

### Milestone 3 — OpenClaw skill and analysis

Package the skill, implement extraction and job leasing, connect the restricted OpenClaw adapter, validate evidence, enforce route/privacy policy, and preserve human state through resummaries.

**Gate:** Live restricted-integration tests, incremental-processing tests, and the evidence-quality gate pass. A mock adapter alone is insufficient.

### Milestone 4 — Original chat experience and shared deployment

Implement page-bound chat, validated filters, explicit incomplete-coverage reporting, shared-host identity, role checks, reviewer conflicts, and supported network deployment documentation.

**Gate:** The requested end-to-end shared and conversational workflow works without direct network access to the SQLite file.

### Milestone 5 — Operational release

Finish backups, restore/relocate/fork flows, migration checks, OS packaging, accessibility, security tests, benchmarks, and operator documentation. Run all acceptance tests from a clean install and a populated upgrade.

**Gate:** Complete a demonstration of setup -> summarize -> review -> chat/filter -> plan -> approve -> apply -> refresh -> restore. Publish remaining limitations, tested versions, and evidence for every release claim.

## 20. Expected repository and deliverables

```text
resume-review/
    README.md
    AGENTS.md
    pyproject.toml
    <dependency-lockfile>
    skill/
        SKILL.md
        scripts/
        references/
    src/resume_review/
        bootstrap/
        api/
        auth/
        db/
        ingest/
        analysis/
        openclaw_adapter/
        actions/
        storage/
        reporting/
    web/
        templates/
        assets/
    schemas/
    migrations/
    tests/
        unit/
        integration/
        security/
        recovery/
        browser/
        fixtures/
    docs/
        installation.md
        local-operation.md
        shared-host-operation.md
        privacy-and-retention.md
        backup-and-recovery.md
        compatibility.md
        acceptance-report.md
```

Deliver the reusable skill, bootstrap/package artifacts, helper, report template/assets, migrations, extraction and OpenClaw adapters, schemas, test fixtures, acceptance tests, packaging instructions, operational runbooks, and release evidence. Include an example synthetic job folder. Do not include real applicant records, credentials, or a runnable-looking mock presented as completed software.

## 21. Default decisions and remaining deployment inputs

The following defaults let implementation start without reopening agreed product decisions:

| Topic | Default |
| --- | --- |
| Instance granularity | One job folder, one database, one helper owner |
| Authoritative storage | Folder-local on the storage host |
| UI technology | Versioned plain HTML/CSS/JavaScript |
| Primary decision controls | Unreviewed / Keep / Reject / Hold |
| Deletion | Recoverable Trash only |
| File movement | Exact-plan human approval; same-volume no-clobber moves |
| Scanning | Explicit incremental scan; file watching optional |
| Chat | Required for full release; never bypasses approval |
| Model selection | Existing approved OpenClaw route; no silent fallback |
| Ranking | No aggregate suitability score or automatic rejection |
| Unknown values | Include with warning by default |
| External services | None required beyond an explicitly approved model route |
| Unsupported shared storage | Explain limitation; do not relocate state silently |

Actual root paths, installed OpenClaw version, provider route, chosen host, reviewer identities, legal jurisdiction, and organizational retention policy are deployment inputs. The agent must collect them during setup or release qualification. They are not excuses to delay the deterministic local build or replace the agreed architecture.

## 22. Definition of done and operating checklist

**Before processing real data:** the administrator has approved the model/privacy route, criteria, access rules, supported topology, and retention approach; the restricted analysis route is verified; safe-action and recovery tests pass; the report's filesystem visibility is understood.

**For each job:** setup registers one owner, scanning accounts for every input, incomplete processing remains visible, human review state persists, and actions remain pending until an exact plan is approved.

**Before each apply:** verify selected IDs, source/destination paths, review decisions, missing/unknown items, Trash count, and current approval. Stop on stale state or conflicts.

**At release:** all applicable acceptance tests pass with saved evidence; full P0/P1 scope is delivered; live OpenClaw behavior is verified; supported hosts and deployment limitations are documented; no uncontrolled executable, Gateway secret, or applicant data is included in the release bundle.

**Agent handoff directive:** Build the deterministic folder application and safe action engine first. Integrate OpenClaw as a bounded analysis/orchestration layer, not as an unrestricted filesystem actor. Preserve the human's authority and the folder's portability. Never claim a component works solely because its interface, prompt, or mock exists.

## 23. Research notes and primary references

Checked September 29, 2026. The sources below support external technical constraints and legal-context statements. Architecture, defaults, performance targets, and acceptance tests are proposed product requirements. Reverify version-sensitive integration behavior against the installed release during implementation.

**[S1] OpenClaw — Agent runtime.** Agent loop, workspace, tool wiring, and runtime/session boundaries.  
`https://docs.openclaw.ai/concepts/agent`

**[S2] OpenClaw — Skills.** `SKILL.md`, frontmatter, bundled resources, and `{baseDir}` convention.  
`https://docs.openclaw.ai/tools/skills`

**[S3] OpenClaw — Exec tool.** Execution policies, approval controls, and restrictions.  
`https://docs.openclaw.ai/tools/exec`

**[S4] OpenClaw — OpenAI chat completions.** Optional HTTP surface, agent targeting, session continuity, and operator-access warning. This is a compatibility interface, not a requirement to use an OpenAI-hosted model.  
`https://docs.openclaw.ai/gateway/openai-http-api`

**[S5] OpenClaw — Security.** Trusted-operator/team boundary and deployment guidance.  
`https://docs.openclaw.ai/gateway/security`

**[S6] OpenClaw — Tool and agent permissions.** Per-agent tool restrictions and sandbox considerations.  
`https://docs.openclaw.ai/gateway/security/tool-permissions`

**[S7] SQLite — SQLite Over a Network, Caveats and Considerations.** Network filesystem reliability risks and storage-local service alternative.  
`https://www.sqlite.org/useovernet.html`

**[S8] SQLite — Write-Ahead Logging.** WAL's same-host/network-filesystem limitations.  
`https://www.sqlite.org/wal.html`

**[S9] SQLite — Online Backup API.** Consistent database snapshot mechanics.  
`https://www.sqlite.org/backup.html`

**[S10] MDN — Same-origin policy.** Browser origin treatment of local files and cross-origin protections.  
`https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy`

**[S11] U.S. EEOC — Prohibited Employment Policies/Practices.** Hiring discrimination and selection-practice context.  
`https://www.eeoc.gov/prohibited-employment-policiespractices`

**[S12] U.S. EEOC — Artificial Intelligence and the ADA.** Disability-related guidance for AI-assisted assessment.  
`https://www.eeoc.gov/eeoc-disability-related-resources/artificial-intelligence-and-ada`

**[S13] NYC Department of Consumer and Worker Protection — Automated Employment Decision Tools.** Example of jurisdiction-specific audit and notice requirements; not an applicability determination for this product.  
`https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page`

**Design provenance:** Peter's agreed OpenClaw resume-folder workflow, September 29, 2026, and `OpenClaw_Resume_Review_PRD.md`, version 0.1. This expanded version preserves the original folder-scoped chat, local/shared operation, human review, repeatable setup, and recoverable file-management constraints.
