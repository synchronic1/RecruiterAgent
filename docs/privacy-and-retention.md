# Privacy and retention

- Status: Operator runbook
- Date: 2026-09-29
- Applies to: `resume-review` 0.1.0
- Authority: `docs/PRD.md` sections 11, 14.2, 16.2, 16.3; `docs/AGENT_BUILD_HANDOFF.md`

This runbook states what personal data a review workspace holds, where each piece
lives on disk, who can reach it, what the system deliberately refuses to infer,
and which retention decisions belong to the operator rather than to the code.

It describes the implementation in `src/resume_review/` at the revision named
above. Where something is not implemented it is labeled **NOT IMPLEMENTED**.

Nothing here is legal advice. Retention periods, lawful basis, and who is allowed
to see a candidate's data are the operator's decisions.

---

## 1. Data inventory

One job folder is one instance. Its state is split across three kinds of places:
the applicant files themselves, the instance database, and the working
subdirectories under `.review/`.

| Location | What it holds | Source |
| --- | --- | --- |
| `<root>/` (the job folder) | The applicant submissions, exactly where they were placed. The file is the record of truth and is never rewritten in place. | `bootstrap/workspace.py` |
| `.review/review.db` | The instance database (SQLite). Details in section 1.1. | `bootstrap/workspace.py` (`DB_FILENAME = "review.db"`) |
| `.review/extracted/<document-id>/<revision>.json` | Extracted document text, one file per document revision. `document_revisions.span_ref` points into it. | `bootstrap/workspace.py` (`extracted_dir`) |
| `.review/backups/review-<stamp>-<suffix>.db` | Verified database backups. | `api/backup.py`, `bootstrap/workspace.py` (`backups_dir`) |
| `.review/journals/<batch-id>.json` | The durable move journal for one action batch: document ids, relative paths, planned steps. | `actions/journal.py` |
| `.review/exports/` | Export output. | `bootstrap/workspace.py` (`exports_dir`) |
| `.review/locks/owner.lock` | The single-writer lock. | `bootstrap/workspace.py` (`owner_lock_path`) |
| `.review/instance.json` and `.review/job.json` | The instance and job manifests that describe the workspace. | `bootstrap/setup.py` |
| `Rejected/<document-id>/<filename>` | Submissions a human approved moving to the rejected area. | `actions/planner.py` |
| `Trash/<batch-id>/<document-id>/<filename>` | Submissions a human approved moving to recoverable Trash. | `actions/planner.py` |
| `<root>/review.html` | The rendered review snapshot. Self-contained; it embeds applicant content. | `reporting/snapshot.py` |
| Reviewer account store | Passwords (scrypt hashes) for local accounts. Host-local, outside the workspace and outside `.review/`. | `auth/store.py` |
| Host registry | `instance_id -> canonical root path` for workspaces provisioned on this host. Contains paths, never applicant content. Located under `%LOCALAPPDATA%\ResumeReview\` and redirectable with `RESUME_REVIEW_REGISTRY_DIR`. | `bootstrap/registry.py` |
| Sessions | In memory only, never written to disk. | `auth/sessions.py` |

### 1.1 What the instance database holds

The schema is `src/resume_review/migrations/0001_initial.sql` (extended by
`0002_file_operation_source_identity.sql`). The tables that carry personal data
or personal judgement are:

| Table | Content relevant to privacy |
| --- | --- |
| `jobs` | `description_text` and `description_sha256`: the job requisition, which may be company-internal. |
| `criteria` | The review criteria derived from the requisition. |
| `documents` | `original_filename`, `display_name`, `current_rel_path`, `first_seen_rel_path`, `media_type`, `size_bytes`, `content_sha256`, `fs_identity`, `location`, `decision_needs_recheck`, `duplicate_content`. This is metadata about a named person's file. |
| `profiles` | `summary_text`, `model_route`, `validation_state`, `is_fixture`, `is_current`, `stale`. The model-derived summary of a candidate. |
| `evidence` | `quote`, `locator_json`, `validation`. Quotations taken from the applicant's document, each with a locator back to it. |
| `decisions` | The reviewer's decision per document. Human-authored. |
| `notes` | Free text written by reviewers. Not validated, not sanitized beyond display escaping. |
| `review_tasks` | Follow-up work items, including limit-exceeded and manual-review flags. |
| `action_intents`, `action_batches` | Proposed and approved moves: `plan_json`, `plan_hash`, `approval_actor`, `approval_time`, `approval_expires_at`, `execution_state`. |
| `file_operations` | The authoritative per-file move record: source, destination, identity, outcome. |
| `conversations`, `messages` | Page-bound review chat, if used. |
| `audit_events` | Append-only record of state mutations. |
| `idempotency_records`, `saved_filters`, `processing_jobs`, `extraction_cache` | Operational records. |

The schema file states, in its own words, that "Authentication secrets, Gateway
tokens and login credentials are NEVER stored here." That is the design rule: the
database holds review state, not credentials.

The volume of extracted personal text is bounded by `models.ResourceLimits`:
`max_source_bytes` 25 MiB, `max_pages` 50, `max_extracted_chars` 200000. Exceeding
a limit raises a review task with a visible reason; it never truncates silently
and never auto-rejects (`models.py`).

### 1.2 Applicant files are also a delivery vector

Every file inside a job folder is applicant-controlled data. A candidate can place
an `AGENTS.md`, `SKILL.md`, `CLAUDE.md`, or any other markdown file in the folder.
`security/untrusted.py` (`INSTRUCTION_FILENAMES`, `is_instruction_file`) treats
such a file as data to be summarized, never as an instruction to follow. Text that
reaches a model is wrapped in an explicit data envelope (`wrap_untrusted`), and
`scan_for_injection` produces an advisory flag only. Instruction files are
surfaced to the reviewer, not obeyed.

Discovery deliberately skips the reserved directories (`.review`, `Rejected`,
`Trash`), the reserved report name, reparse points and symbolic links, and
temporary-file patterns (`~$`, `.~lock.`, `~`, `.tmp`, `.crdownload`, `.part`,
`.partial`, `.swp`, `.bak`) - `ingest/discover.py` and `models.py`
(`DISCOVERY_EXCLUDED_*`).

---

## 2. What is deliberately not stored or inferred

These are design constraints from `AGENTS.md` and `docs/PRD.md`, and they are the
part of this document an operator can rely on when asked "what does the tool say
about this person".

| Constraint | Where it is enforced |
| --- | --- |
| No protected-trait inference (age, date of birth, race, sex, marital status, disability, and the rest). | `openclaw_adapter/prompts.py` instructs the model against it; tests assert the instruction text is present. |
| No aggregate suitability score, ranking, percentage, grade, or "best match" ordering. | `reporting/payload.py` states and the reporting tests assert that none is emitted; `tests/unit/test_reporting.py` checks the forbidden keys are absent. |
| No automatic rejection. Reject is a human decision, recorded by a human. | `actions/planner.py` never moves anything; `actions/executor.py` requires a recorded approval before any move. |
| A file is never moved because a model suggested Reject. | Only an exact, human-approved plan executes, and only through `storage.no_clobber.atomic_no_clobber_move`. |
| `mtime` is never treated as a submission date. | `ingest/discover.py` sets `submitted_at = None` unless a source actually recorded one. |
| No applicant messaging. | **NOT IMPLEMENTED** - no messaging path exists in the codebase. |
| No ATS integration or status push. | **NOT IMPLEMENTED** - no ATS client exists in the codebase. |
| No permanent deletion, no automatic purging. | `actions/recovery.py` states "There is no deletion path." |
| No purge scheduler. | **NOT IMPLEMENTED** - `docs/PRD.md` section 16.3 states the initial release has no purge scheduler. |
| No retention setting that creates administrative review tasks. | **NOT IMPLEMENTED** - no retention configuration exists in the code; a retention follow-up must be created by hand as a review task. |

Model output is data. It is validated against a versioned schema before it reaches
the database, and any claim it makes must carry an evidence quote with a locator
(`analysis/pipeline.py`, `analysis/validate.py`). Storage paths are always resolved
from document ids and never taken from a model response.

---

## 3. Who can reach the data

Three layers decide access, in this order of reality:

1. **The host filesystem.** Anyone with read access to the job folder has the
   applicant files, the database, the extracted text, the backups, and the
   rendered report. This is the real boundary. No application control changes it.
   Folder ACLs, disk encryption, and who can log into the storage host are the
   operator's responsibility.
2. **Storage topology.** The database is refused on network and unclassifiable
   locations, so it cannot be reached by pointing a mapped drive at a share. See
   `docs/backup-and-recovery.md`.
3. **Application authentication and authorization.** Required whenever the
   connected review page is served.

### 3.1 Application access control

| Control | Behavior | Source |
| --- | --- | --- |
| Accounts | Host-local account store with scrypt password hashing (`N=2**14`, `r=8`, `p=1`, `dklen=32`), minimum password length 8. The store may not live in `.review/`, inside the workspace root, or on a network filesystem. | `auth/store.py` (`assert_host_local_secret_path`) |
| Roles | `Viewer`, `Reviewer`, `Administrator`. Mutations are role-gated; a failure is `ROLE_INSUFFICIENT`. | `models.py`, `auth/guards.py` (`require_role`) |
| Sessions | Cookie bound to one instance id; 12 hours absolute and 30 minutes idle by default; held in memory only, so a restart signs everyone out. | `auth/sessions.py` |
| Origin and Host checks | Every request is checked before the route handler; failures are `ORIGIN_REJECTED` and `HOST_REJECTED`. The `null` origin is never treated as same-origin. | `auth/guards.py` |
| CSRF | Required for session-authenticated mutations; failure is `CSRF_FAILED`. | `auth/guards.py`, `auth/csrf.py` |
| Rate limiting | Connection and chat endpoints are rate limited; failure is `RATE_LIMITED`. | `auth/guards.py` |
| Backup | Administrator-only, synchronous, and it accepts no caller-chosen destination. | `api/backup.py` |
| Error envelopes and logs | Credential-shaped strings and literal sensitive values are redacted before an error body is produced. Error text never contains a candidate name or a filesystem path. | `api/envelope.py`, `auth/guards.py` |

A `Viewer` can read the review state but cannot record a decision, because a
decision is a mutation. There is no anonymous read path: an unauthenticated
request is rejected before any business logic runs.

---

## 4. Three separate state dimensions

`docs/adr/0001-foundation-decisions.md` fixes five independent enums in
`models.py`, stored in separate columns. Three of them matter for every
privacy-and-retention statement, because collapsing them produces a statement
that is false:

| Dimension | Enum | Column it is stored in | Meaning |
| --- | --- | --- | --- |
| Review decision | `ReviewState` (`unreviewed`, `keep`, `reject`, `hold`) | `decisions.disposition` | What a human decided about the candidate. Human-authored. |
| Pending intent | `PendingIntent` (`none`, `move_rejected`, `restore_active`, `move_trash`, `restore_previous`) | `action_intents.intent` with `action_intents.state` | What has been requested but may not have happened. |
| Actual location | `Location` (`active`, `rejected`, `trash`, `missing`, `conflict`) | `documents.location` (with `documents.location_version`) | Where a verified filesystem reconciliation last saw the file. Owned by reconciliation, never by the decision. |

The column names above were read directly from the database created by a scratch
`setup` (`PRAGMA table_info`); the enum members and the separation rule are from
`models.py` and ADR 0001. A document also carries `decision_needs_recheck` and
`recheck_reason` as their own columns.

A row may legitimately be `reject` + `move_rejected` pending + `active`. All three
are true at once. The consequences:

- A "reject" decision does **not** mean the file left the job folder. It may still
  be sitting in place with a move queued.
- A file that is `active` may have no decision yet, even if a model produced a
  summary.
- `decision_needs_recheck` is a separate flag, not a hidden sixth decision value.
  A changed document means the earlier decision may no longer be about the same
  bytes, and the system says so rather than silently carrying the decision over.

For retention this means: the retention clock cannot be read off a decision, and
the bytes at a path are not necessarily the bytes the decision was about.
`content_sha256` and `first_seen_rel_path` are kept for exactly that reason.

---

## 5. Retention: what the operator must decide

The application imposes no retention period. It provides no deletion operation.
Everything below is a decision the operator makes and then executes by hand.

### 5.1 There is no in-app deletion

- **NOT IMPLEMENTED:** no delete endpoint, no delete CLI command, no purge job,
  no scheduled cleanup, no "empty Trash" action. `actions/recovery.py` states
  plainly that there is no deletion path, and `AGENTS.md` requires recoverable
  Trash only.
- The only file destination the application chooses is `Rejected/` or `Trash/`,
  and only after an exact human approval.
- Nothing is moved because a model suggested Reject. A model suggestion is data.
- `audit_events` is append-only through the API, so the record of what happened
  is not editable from inside the application.

### 5.2 Deleting a workspace is a manual filesystem operation

When the operator decides a job folder's retention period has ended, deletion
happens outside the application and must account for everything in section 1:

1. Stop the helper (`resume-review stop --instance <id>`) so the writer lock is
   released and no move is in flight.
2. Confirm no batch is mid-flight: run `resume-review repair --instance <id>
   --dry-run` and confirm it reports no operation needing reconciliation. Deleting
   a folder with an uncommitted move destroys the evidence needed to reconcile it.
3. Delete the workspace, including `.review/` (database, backups, journals,
   extracted text, exports, manifests), `review.html`, and the applicant files.
   Note that `Rejected/` and `Trash/` still contain applicant files: a deletion
   that removes only the top-level folder and forgets `Trash/` has not deleted the
   data.
4. Remove the workspace's entry from the host registry, or leave it - **the
   registry is not updated automatically by folder deletion**, so a stale
   entry is possible. A stale entry holds a path, not applicant content.
5. The account store is outside the workspace and is shared across instances.
   Deleting a workspace does not delete reviewer accounts, and it does not delete
   a departed reviewer's credentials. Remove accounts separately.

Because deletion is out of band, deletions are deliberate and auditable only in
the sense that they leave no in-app trace beyond the absence of the workspace.

### 5.3 Decisions that belong to the operator

| Decision | Why it matters | Where the tool leaves it |
| --- | --- | --- |
| Retention period per job folder, and what triggers the clock (requisition close, hiring decision, fixed interval). | The tool enforces no period. | Operator policy. Record it as a `review_task` if you want it visible. |
| Whether `Rejected/` and `Trash/` contents are deleted on the same clock as the rest. | They hold applicant files. | Operator policy. |
| Whether `.review/backups/` copies are retained longer than the live database. | Each backup is a full second copy of the review state, including model summaries and evidence quotes. | Operator policy. |
| Whether `.review/extracted/` copies are deleted earlier than the database. | Extracted text is raw applicant content; the database holds metadata and quotes. | Operator policy. |
| Who holds ACL access to the storage host and the job folder. | This is the real access boundary (section 3). | Host administration. |
| Whether the configured analysis route receives applicant text. | Analysis sends document text to the configured Gateway route (`openclaw_adapter/`). | Operator decides whether the route is on-box or off-box. |
| Whether `review.html` or backups may be copied off the host. | Both embed applicant content in a single file that is easy to move and easy to forget. | Operator policy. |
| Whether shared-host-local deployment mode is used, and who else can reach that storage host. | `storage_mode` records `local` or `shared_host_local`. | Operator policy (`bootstrap/setup.py`). |

Keep the account store out of any portable backup: `docs/PRD.md` section 16.2
requires tokens and login secrets to stay outside shared folders, generated HTML,
and portable backups, and `auth/store.py` enforces the path rules that keep it
there.

---

## 6. Operator checklist

1. Decide and write down the retention period and the trigger for each job folder.
2. Confirm the workspace is on storage-host-local storage before trusting any
   privacy statement about it. `setup` prints the topology block it resolved
   (`kind`, `mount_point`, `supports_live_database`, `certain`), and `status`
   reports the resulting `storage_mode`. See `docs/backup-and-recovery.md`.
3. Confirm who can reach the host and the folder ACL, and treat that as the real
   boundary; the application controls are defence in depth on top of it.
4. Before any deletion, run `resume-review repair --instance <id> --dry-run` and
   confirm nothing needs reconciliation.
5. Remember the three dimensions when answering a data request: a decision, a
   pending intent, and a location are three different answers, and only
   reconciliation says where a file actually is.
6. Remember that extracted text, backups, journals, `Rejected/`, `Trash/`, and
   `review.html` are all copies of applicant data outside the database.

---

## 7. Verification status of this document

Every module named above was read at the revision stated in the header. The CLI
commands in this runbook were executed against a scratch workspace on
2026-09-29; the observed output is recorded in section 9 of
`docs/backup-and-recovery.md`.

Established by reading code rather than by executing it:

- The `NETWORK_FILESYSTEM_DATABASE` refusal was reproduced for a UNC path:
  `probe_topology` tests the leading `\\` lexically, before any filesystem call,
  so the path need not exist in order to be refused. A drive letter that does not
  exist on the host is refused with `UNSUPPORTED_STORAGE_TOPOLOGY` instead, which
  is the correct code for an unclassifiable volume. A mounted share was not
  available, so the `DRIVE_REMOTE` branch is read rather than executed. See
  section 9.3 of `docs/backup-and-recovery.md`, which also records how a shell can
  collapse `\\` and make a UNC path resolve to a local drive.
- The scrypt parameters, session lifetimes, role table, and redaction rules were
  read from `auth/` and `api/envelope.py`, not exercised.
- The claim that `mtime` is never a submission date, and the exclusion lists, were
  read from `ingest/discover.py` and `models.py`.
- The absence of applicant messaging, ATS integration, a purge scheduler, a
  retention setting, and any deletion path was established by searching the
  codebase and reading `actions/recovery.py`; it is an absence, so it cannot be
  proven by running something.
