# Backup and recovery

- Status: Operator runbook
- Date: 2026-09-29
- Applies to: `resume-review` 0.1.0
- Authority: `docs/PRD.md` sections 11.2, 11.3, 13.1-13.3, 16.2;
  `docs/adr/0001-foundation-decisions.md`; `docs/AGENT_BUILD_HANDOFF.md`

This runbook covers the storage rules the instance database depends on, how to
take a verified database backup, what a complete workspace backup actually has to
contain, how to restore, and how to recover from a move that was interrupted
part-way.

The recovery story rests on three facts in the implementation:

- The move primitive fails rather than overwrites, and there is no fallback that
  degrades into a copy (`storage/no_clobber.py`).
- Every intended move is recorded in the database before the filesystem is
  touched, and in a durable journal beside it (`actions/journal.py`,
  `actions/executor.py`).
- Recovery classifies an operation from what is actually on disk and never
  deletes anything (`actions/recovery.py`).

---

## 1. Where the database may live

An instance database is `<root>/.review/review.db` (`bootstrap/workspace.py`,
`DB_FILENAME = "review.db"`).

| Topology | Verdict | Code |
| --- | --- | --- |
| `LOCAL_FIXED` | Supported. WAL is used. | - |
| `LOCAL_REMOVABLE` | Supported. WAL is used. | - |
| Network (SMB, NFS, UNC, mapped drive) | Refused outright. | `NETWORK_FILESYSTEM_DATABASE` |
| Unknown / unclassifiable | Refused unless the operator confirms explicitly. | `UNSUPPORTED_STORAGE_TOPOLOGY` |
| Read-only | Refused. | `READ_ONLY_LOCATION` |

The refusal comes from `storage/topology.py`
(`probe_topology`, `assert_supported_for_database`). It is checked before any file
is created, so a refused setup leaves the target directory untouched. A
mapped-drive live database is therefore rejected by setup, not merely discouraged.

The `NETWORK_FILESYSTEM_DATABASE` refusal was reproduced directly, against a
nonexistent UNC path. That is the strongest form of the check available without a
mounted share, because `probe_topology` tests the leading double backslash
lexically, before any filesystem call, so the path does not have to exist in order
to be refused. What remains unexercised is the mounted-share case and the
unknown-volume confirmation path. See 9.3.

Write-ahead logging is enabled **only** on `LOCAL_FIXED` and `LOCAL_REMOVABLE`
(`db/connection.py`, `open_connection`). On anything else the connection falls
back to `PRAGMA journal_mode = DELETE`. The reason is in ADR 0001: WAL's
shared-memory `-shm` file depends on byte-range locking that a network
implementation does not guarantee, so WAL on a share trades a detectable slowdown
for possible silent corruption.

Every connection also sets `PRAGMA foreign_keys=ON`, `synchronous=FULL`, and a
bounded `busy_timeout` (7500 ms), configured through `db.connection.DbConfig`.

Practical consequences for a backup plan:

- A "live" workspace cannot be hosted on a mapped drive or a share, so the backup
  problem is a same-host problem.
- Do not move a workspace onto a share later and keep it live there. Copying a
  workspace to a share as a *backup destination* is a different thing and is
  allowed, as long as it is not reopened as a live database from that path.
- The refusal is about the database, not about every file. `.review/backups/`
  copies and `review.html` may be copied elsewhere.

---

## 2. What a complete backup contains

`docs/PRD.md` section 11.3 is explicit: use the SQLite backup API or an equivalent
verified snapshot mechanism, never an arbitrary copy of an active database file,
and note that **a database backup alone does not back up the original resumes** -
a workspace backup must coordinate both.

The application can produce only the database half. The `backup` command and
`POST /backup` do not copy applicant files. A restorable workspace backup is
therefore an operator procedure with these parts:

| Part | Why it is required |
| --- | --- |
| `.review/review.db` | Decisions, notes, evidence, file_operations, audit history. Without it, the human state is gone. |
| `.review/journals/` | The durable intent record for any batch that was in flight. Needed only if the copy is taken while a move is in progress, but it is cheap to include. |
| `.review/backups/` | Previously verified copies. Optional to include, but a backup taken from a backup is not a backup strategy. |
| `.review/extracted/` | Extracted text. Can be regenerated from the originals, so it is the most expendable part - but regenerating changes nothing about the originals and costs time. |
| The applicant files in the root | The originals. A database without them is a decision log with no documents. |
| `Rejected/` and `Trash/` | These hold applicant files that a human approved moving. **A copy that omits them has silently lost the files that were moved.** |
| `review.html` | Rendered snapshot. Regenerable; include only if the retention policy wants the point-in-time rendering. |

Copy the whole job folder as a unit, and include the hidden `.review/`
directory. The most common way to produce an unusable backup is to copy the
visible files and forget `.review/`.

The account store and the host registry are outside the workspace and are
deliberately not part of a workspace backup: `docs/PRD.md` section 16.2 requires
tokens and login secrets to stay outside shared folders, generated HTML, and
portable backups, and `auth/store.py` enforces those path rules.

Do not copy a live database file with Explorer, `cp`, or `robocopy`. A plain copy
of a database that is mid-write is a torn read and is not a backup
(`api/backup.py` says so in its own words). Copy the whole folder only while the
helper is stopped, or copy the database with the `backup` command described next.

---

## 3. Taking a verified database backup

### 3.1 Command line

```
resume-review backup --instance <instance_id> [--json]
```

It uses SQLite's online backup API to write a consistent snapshot to
`<root>/.review/backups/review-<stamp>-<suffix>.db`, reopens the copy, and runs
`PRAGMA integrity_check`. A copy that fails the check is unlinked and reported as
a conflict; the command never reports a backup it has not verified
(`api/backup.py`, `take_backup`).

The destination is derived from the live database file's own directory, so the
backup always lands on the same local volume as the database it copies. There is
no option to choose a destination, deliberately: a caller-chosen destination would
be a way to write a database copy somewhere unsupported or somewhere the caller
cannot be trusted to secure.

### 3.2 HTTP

```
POST /api/v1/instances/{instance_id}/backup
```

Administrator role only, with an `Idempotency-Key` header. The request body is
strict: extra fields are forbidden, so a caller cannot smuggle in a destination
(`api/backup.py`, `BackupRequest`). The operation is synchronous; the response is
the verified result, not a job id.

### 3.3 What to do with the result

1. Record the reported backup file name and byte size.
2. Copy `.review/backups/review-<stamp>-<suffix>.db` off the workspace if the
   backup plan requires an off-folder copy. This is a plain file copy of a file
   that nobody is writing to, which is safe.
3. Prune old backups yourself. **NOT IMPLEMENTED:** no retention or pruning of
   `.review/backups/` exists in the code. Each backup is a full second copy of the
   review state, including model summaries and evidence quotes, so backups are
   themselves personal data and belong in the retention policy.

### 3.4 Before a migration

`bootstrap/setup.py` (`_backup_database`) takes a pre-migration backup through the
migration `backup_hook` when migrations are applied to an already-populated
database, and enforces a minimum free-space floor of 64 MiB before it does. This
means a workspace upgraded into a new build keeps its pre-upgrade database
automatically. Migrations are forward-only and checksummed
(`db/migrations.py`); opening a database written by a newer build is refused with
`DOWNGRADE_REFUSED` (exit code 2).

---

## 4. The journal, and what is authoritative

An action batch writes a durable journal at
`.review/journals/<batch-id>.json` (`actions/journal.py`,
`bootstrap/workspace.py`). Steps are recorded in order:

```
planned -> intent_recorded -> file_moved -> committed
```

with side states `needs_reconciliation`, `failed`, and `skipped`. Each write is
durable: the journal is replaced through a temporary file with an `fsync` and a
directory `fsync`, so a crash cannot leave a half-written journal
(`_durable_replace`).

The database is authoritative. `file_operations` is the record of what was
intended and what happened; the journal is a recovery aid that tells `repair` what
to look at when the process died between the two. `reconcile_with_database`
resolves any disagreement in the database's favour, and `load_journal` reports a
corrupt journal rather than guessing.

The ordering matters. Intent is recorded in the journal, then in the database,
**before** the file is touched (`actions/executor.py`, `_record_intent`). That is
what makes an interrupted move recoverable: there is always a record that a move
was supposed to happen, even if the move itself did not complete.

---

## 5. Recovering an interrupted move

### 5.1 Recovery contract

`docs/PRD.md` section 13.3 defines the contract, and `actions/recovery.py`
implements it. Recovery never deletes and never guesses:

| What is on disk | Verdict |
| --- | --- |
| Source present, destination absent | Resume: the move did not happen. |
| Source absent, destination present and verified | Commit: the move happened, finish recording it. |
| Both present | Stop for reconciliation. Never delete either one. |
| Neither present | Mark the operation and the document `missing`. |
| Identity or ownership differs | Block, and preserve the evidence. |

The last two rows are the important ones for an operator. The tool will not
resolve an ambiguous state by deleting a file, and it will not assume two files
with the same name are the same file. Identity is carried as a filesystem
identity and a content digest (`storage/no_clobber.py`, `FileIdentity`,
`digest_hint`), and the second migration
(`0002_file_operation_source_identity.sql`) added per-operation source identity
precisely so a recovered move is checked against the file it was planned for.

`ACTIVE_OPERATION_STATES` - `planned`, `intent_recorded`, `file_moved`,
`needs_reconciliation`, `failed` - are the states that make an operation eligible
for recovery.

### 5.2 Command line

```
resume-review repair --instance <instance_id> [--batch <batch_id>] [--dry-run] [--json]
```

- `--dry-run` classifies and reports without changing anything. **Always start
  here.**
- `--batch` restricts the pass to one batch; without it, `repair` considers the
  batches with active operations.
- Omit `--dry-run` to perform the actions the classification allows. The
  classification is computed first and the code reconciles rather than moves when
  the disk already agrees with the intended outcome (`actions/executor.py`,
  `_execute_one`).

The CLI returns stable exit codes (section 8). A `repair` that stops for
reconciliation is expected to be non-zero: it is reporting that a human must look,
not that the tool failed.

### 5.3 A refusal is a valid outcome

Two things are refused on purpose, and both are recoverable:

- **Cross-volume move.** The no-clobber primitive refuses it, and there is
  deliberately no copy-and-delete fallback. A copy-then-unlink would silently drop
  the atomicity the recovery story depends on: an interrupted copy leaves a
  plausible-looking but incomplete file, and the source would then be removed.
- **Missing or expired approval.** Applying a batch requires a recorded approval
  bound to the plan hash, with a 900-second lifetime (`APPROVAL_LIFETIME_SECONDS`
  in `actions/executor.py`; `models.ResourceLimits.approval_lifetime_seconds`).
  The errors are `APPROVAL_REQUIRED` and `APPROVAL_EXPIRED`.

### 5.4 Restoring a file that was moved

A restore is not an undo of a decision. `actions/restore.py` distinguishes
`restore_active` from `restore_previous` and never collapses them into one
ambiguous "Undo", because "put it back where it came from" and "put it back in the
active folder" are different requests. A restore is itself planned, approved, and
applied as a batch, so it goes through the same approval gate and the same
no-clobber move as the original action. The moved file's earlier location is
remembered in `file_operations` and `first_seen_rel_path`.

### 5.5 There is no deletion path

`actions/recovery.py` states it directly: there is no deletion path. Recovery can
resume, commit, block, or mark missing. It cannot delete a file, cannot empty
Trash, and cannot purge a row. Recoverable Trash only.

---

## 6. Restoring a workspace from backup

Restoring is a manual procedure, because it replaces the file that holds all human
state. **NOT IMPLEMENTED:** there is no restore command and no restore endpoint.

1. Stop the helper so the writer lock is released and nothing is mid-move:

   ```
   resume-review stop --instance <instance_id>
   ```

2. Put the workspace back on storage-host-local storage. Never restore a live
   database onto a mapped drive or a share; setup will refuse it, and reopening it
   there is exactly the corruption risk section 1 describes.
3. Replace `.review/review.db` with the verified backup file.
4. **Remove any `-wal` and `-shm` files left beside the old database before
   opening the restored one.** They belong to the database that was replaced, and
   pairing a stale `-wal` with a restored file is how a restore quietly produces a
   mixed state. A verified backup from the backup API is a complete database, not
   a journal member.
5. Verify the restored file before trusting it:

   ```
   sqlite3 .review/review.db "PRAGMA integrity_check;"
   ```

   expect `ok`.
6. Restore the applicant files with the database. If the backup set was taken
   while a move was in flight, the database and the visible file locations may
   disagree; that is what the next step is for.
7. Report status, then reconcile:

   ```
   resume-review status --instance <instance_id>
   resume-review repair --instance <instance_id> --dry-run
   ```

   `status` reports `storage_mode`, the schema version, the state revision, and
   the document counts. It does not report the journal mode or the file size; to
   confirm how the restored file is opened, run `PRAGMA integrity_check` (step 5)
   and `PRAGMA journal_mode` against it with `sqlite3`, or re-run `setup` on the
   folder, whose output includes the resolved topology and storage mode.
8. Read the `--dry-run` classification before running `repair` for real. If it
   reports a reconciliation stop, a human decides; the tool will not delete either
   copy to break the tie.

If the backup is older than the workspace, the difference is exactly the human
state recorded since: decisions, notes, evidence, and any completed moves. There
is no merge. You are choosing the older state on purpose.

---

## 7. Human state and evidence survive

The guarantee the operator depends on is in `AGENTS.md` and is enforced in the
schema and the write path:

| Event | What survives, and why |
| --- | --- |
| Rescan of the same folder | Decisions, notes, and evidence are keyed to document identity (`content_sha256`, `first_seen_rel_path`), not to a path. A rescan that finds the same bytes finds the same document. |
| Helper restart | Everything human is in SQLite, not in memory. Sessions are the only in-memory state and they are meant to end. |
| Template regeneration | Report templates regenerate `review.html`; they do not touch the database. |
| Migration | Forward-only and checksummed, with a pre-migration backup. |
| Interrupted move | The journal plus `file_operations` let `repair` reconstruct the outcome without deleting anything. |
| Decision on bytes that later change | `decision_needs_recheck` is set as a separate flag; the earlier decision is not silently reattributed to new bytes. |

Every state mutation bumps `instances.state_revision` and writes an `audit_events`
row **in the same transaction** (`db/connection.py`, `Database.write`, using
`BEGIN IMMEDIATE`). If the mutation fails, the failure is itself audited with an
error outcome. This is why a restored database can be trusted to describe a
consistent point in time, and why the audit trail does not have gaps around a
crash.

---

## 8. Exit codes

From the CLI (`cli.py`, and the `--help` footer):

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Unexpected internal failure. |
| 2 | Invalid input, not found, or a refused downgrade. |
| 3 | Permission failure. |
| 4 | Conflict: stale plan, plan hash mismatch, missing or expired approval. |
| 5 | Dependency failure: no HTTP API package, no analysis route. |
| 6 | Unsupported storage topology. |

For a cron or scheduled backup, exit 0 is the only success. When a copy fails its
integrity check it is discarded and the command fails with exit 1, not a category
code: the failure is raised as `SNAPSHOT_PUBLISH_FAILED`, which has no entry in
`_EXIT_MAP` (`errors.py`), so `exit_code_for` falls through to the generic
`EXIT_UNEXPECTED`. Write the scheduler against "0 is success, any non-zero means
no new backup was taken"; do not test for a specific non-zero value. The previous
backup is untouched either way.

That specific failure was not reproduced. Exit 4 was reproduced, but for a
different conflict: a batch with no recorded approval (9.2).

---

## 9. Verification record

Commands were executed on 2026-09-29 on Windows 11 with the repository virtual
environment interpreter (`.venv/Scripts/python.exe`), against a scratch workspace
created for the purpose under `%TEMP%\rr-verify`, with
`RESUME_REVIEW_REGISTRY_DIR` redirected so the host registry was not modified.
Output is quoted as observed, with long JSON trimmed to the fields that matter.
Anything not in this section was established by reading the source and is not
claimed as executed.

### 9.1 Interpreter and CLI

```
$ ./.venv/Scripts/python.exe --version
Python 3.14.0

$ ./.venv/Scripts/python.exe -m resume_review.cli --version
resume-review 0.1.0
```

`--help` lists the subcommands `setup`, `start`, `status`, `scan`, `summarize`,
`render`, `plan-actions`, `apply-actions`, `backup`, `repair`, `stop`, and prints
the exit-code table reproduced in section 8.

### 9.2 End-to-end scratch run

`setup` on a two-document folder, exit 0. The topology block it returned:

```
"db_path": "...\\rr-verify\\ws\\.review\\review.db",
"report_path": "...\\rr-verify\\ws\\review.html",
"storage_mode": "local",
"topology": { "kind": "local_fixed", "detail": "A local fixed volume.",
              "mount_point": "C:\\", "filesystem_type": "win32:3",
              "writable": true, "supports_live_database": true, "certain": true },
"app_version": "0.1.0", "schema_version": 2,
"lock_backend": "msvcrt.LockFile(no-steal)",
"counts_before": { "documents": 0, ... }, "counts_after": { "documents": 0, ... },
"preserved": true, "state_revision_before": 0, "state_revision_after": 0
```

`status`, exit 0: `storage_mode: local`, `state_revision: 0`, `schema_version: 2`,
and counts all zero.

`scan`, exit 0:

```
"scan": { "discovered": 2, "created": 2, "revisions_added": 2, "extractions": 2,
          "cache_hits": 0, "parser_failures": 0, "analyses_enqueued": 0,
          "awaiting_criteria": 2 }
"counts": { "total": 2, "processed": 2, "unreviewed": 2, "manual_review": 2,
            "open_tasks": 2 }
```

with the warning `"Some documents await approved criteria; they are extracted and
visible for manual review but were not assessed."` No analysis ran, because no
criteria were approved - which is the deterministic-only path.

`render`, exit 0: `{"report_file": "review.html", "published": true,
"byte_size": 141270}`.

`plan-actions` with `{"document_ids": ["doc_x"], "intent": "move_rejected"}`,
exit 0. It returned a plan with `"operations": []` and
`"skipped": [{"document_id": "doc_x", "reason": "document_not_found"}]`,
`"execution_state": "planned"`, `"requested_by": "local-owner"`, and the
`next_action` text: `"Review this exact plan in the review interface and record an
approval, then run apply-actions with the batch id. Planning is not approval."`

`apply-actions` against that real, planned batch, with no approval recorded,
exit 4:

```
"ok": false, "code": "APPROVAL_REQUIRED",
"message": "This batch has no recorded human approval. Sorting, filtering, or a
            document being marked Reject is not approval."
```

The same refusal was returned with `--dry-run`. Immediately before and after that
attempt, the two applicant files were still in the folder root
(`candidate-a.txt`, `candidate-b.txt`) and `Rejected/` and `Trash/` were both
empty. The refusal moved nothing.

`apply-actions --batch <nonexistent>`, exit 2, code `NOT_FOUND`.

`backup`, exit 0:

```
"backup_file": "review-20260929T190817-4c9f1be7.db",
"byte_size": 348160, "verified": true, "integrity": "ok"
```

written to `.review/backups/`, matching the documented name pattern.

`repair --dry-run`, exit 0: `{"dry_run": true, "mutating": false, "applied": 0,
"diagnoses": [], "actions": []}` with the warning `"This was a dry run; no journal
or location state was written."`

`stop`, exit 0: `{"running": false}`.

Workspace tree after the run:

```
ws/
  candidate-a.txt  candidate-b.txt  review.html
  Rejected/  Trash/
  .review/  app/ backups/review-20260929T190817-4c9f1be7.db  exports/
            extracted/  instance.json  job.json  journals/  locks/owner.lock
            migrations/  review.db  tmp/
```

Direct database inspection of the resulting `review.db`:

```
journal_mode = wal
integrity_check = ok
instances row = (12, 'local', 2)          # state_revision, storage_mode, schema_version
documents rows = 2
file_operations rows = 0
action_batches = [('batch_219f80...', 'planned', None, '0e71daff...')]
audit_events rows = 12
document = ('doc_27a916a3...', 'candidate-a.txt', 'active', 'manual_review', 0)
document = ('doc_02bba072...', 'candidate-b.txt', 'active', 'manual_review', 0)
```

Two details worth noting. `journal_mode` was `wal`, as designed for a local fixed
volume. `foreign_keys` read `0` on that ad-hoc connection: the pragma is
per-connection and the application's own connections set it
(`db/connection.py`), so a check run from a bare `sqlite3` connection must set it
explicitly to mean anything.

### 9.3 Not verified by execution

- **The network refusal was reproduced for a UNC path; a mounted share was not
  available.** `assert_supported_for_database` refuses
  `\\fileserver\share\resume\ws` with `NETWORK_FILESYSTEM_DATABASE`, because
  `probe_topology` tests the leading `\\` lexically, before any filesystem call. A
  `Z:\mapped\resume\ws` naming a drive letter that does not exist is refused too,
  but with `UNSUPPORTED_STORAGE_TOPOLOGY`: an unclassifiable volume is UNKNOWN,
  not NETWORK, and that is the correct code for it. The `DRIVE_REMOTE` branch of
  `_probe_windows` is read, not executed.

  An earlier draft of this section reported the UNC path as *accepted*. That
  reading was wrong, and how it was wrong is worth recording. The path had been
  passed through a shell that collapsed `\\` to `\`, so Python received
  `\fileserver\share\resume\ws` and `os.path.abspath` resolved it against the
  current drive as `C:\fileserver\share\resume\ws` -- a local fixed volume, which
  is accepted correctly. Build the path with `chr(92)*2`, or confirm that
  `os.path.abspath` still returns a double backslash, before drawing any
  conclusion about this check. The same collapsing happens to an operator who
  types a UNC path into a shell that rewrites it, and there it is a genuine
  hazard: the workspace is provisioned on the local drive while the operator
  believes it is on the share, and nothing in the output contradicts them.
- **No restore from backup was performed.** Section 6 is a procedure derived from
  the code and the constraints, not a recorded restore.
- The `-wal`/`-shm` cleanup step in section 6 follows from `db/connection.py`
  choosing WAL on local storage; it was not exercised.
- The pre-migration backup hook and the 64 MiB free-space floor were read from
  `bootstrap/setup.py`, not triggered by an upgrade.
- `repair` without `--dry-run` was not run. Section 5.2 describes it from
  `actions/executor.py`; the executed command in this runbook's flow is the
  `--dry-run` form.
- The recovery classification table in section 5.1 was not reproduced for any of
  its five rows: the scratch run ended with `file_operations` empty, so there was
  no interrupted move to classify. The table is from `docs/PRD.md` section 13.3
  and `actions/recovery.py`.
