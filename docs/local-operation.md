# Local operation

Authority: PRD section 5.3 (application command contract), section 15.1 (local
mode), section 10 (state model and action semantics), section 13 (safe file
actions, approvals, recovery), and section 11.3 (SQLite configuration). This is
the day-to-day runbook for one instance on one machine, after `setup` has
succeeded. See `docs/installation.md` for provisioning.

Every command is implemented in `src/resume_review/cli.py`. All commands take
`--instance <id>` and resolve the workspace root from the host registry, never
from the caller: a command cannot be pointed at an arbitrary folder.

## 1. The five-state model, and why three of them are separate

A document carries three independent dimensions, not one (`src/resume_review/models.py`):

| Dimension | Values | Meaning |
| --- | --- | --- |
| Reviewer decision | `unreviewed`, `keep`, `reject`, `hold` | What a person decided |
| Pending intent | `move_rejected`, and the keep/hold counterparts | What a person intends to do to the file |
| Actual location | `active`, `rejected`, `trash` | Where the file is right now |

They are separate because a decision is not a file move. Marking a document
Reject does not move it, must not move it, and only an exact, human-approved plan
executes a move. This is why `apply-actions` exists as its own step and why
there is no `approve` subcommand.

Every mutation bumps `instances.state_revision` and writes an `audit_events` row
in the same transaction (`db/connection.py`, `Database.write`). Concurrent edits
use optimistic version checks: a second reviewer acting on a stale revision gets
a conflict, never a silent last-writer-wins.

## 2. Status

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli status --instance <id>
```

`status` prints the JSON envelope by default. It reports `state_revision`,
`storage_mode`, `app_version`, `schema_version`, the document/decision/task
counts, and `lock_backend`. Verified output shape in `docs/installation.md`
section 8.

## 3. Scan: deterministic discovery and extraction

Scanning is explicit and incremental. There is no file watcher; nothing happens
until you ask for it.

Verified:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli scan --instance <id>
```

Observed output (exit code 0):

```text
scan: ok (OK) instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

`scan` runs the deterministic pipeline and performs **no inference**. It is given
a model client that raises if a model call is ever attempted
(`_disabled_adapter` in `cli.py`). Extraction covers `.pdf`, `.docx`, and `.txt`
(`src/resume_review/ingest/`). Parsing happens without executing macros, embedded
scripts, links, or active content.

Discovery excludes `.review`, `Rejected`, `Trash`, temporary files, symlinks,
junctions, and unselected nested folders (`ingest/discover.py`; `DISCOVERY_EXCLUDED_DIRS`
in `models.py`).

`scan` can warn without failing. Two warnings matter operationally:

- Some documents await approved criteria. They are extracted and visible for
  manual review but were not assessed. No job-match assessment is fabricated.
- No approved analysis route is configured. Matched documents are left for manual
  review.

A registered document keeps its identity across a managed move. After a scan, the
state revision increases and the counts in `status` reflect the new census.

## 4. Serving the page: start, or render a snapshot

Two ways to look at an instance.

**Connected page.** Verified that it serves and that it enforces authentication:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli start --instance <id> --port 8799
```

Observed, in the foreground:

```text
INFO:     Started server process [59300]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8799 (Press CTRL+C to quit)
```

An unauthenticated request to the business endpoint returned HTTP 401. That is
the expected result: the route prefix is `/api/v1/instances/{instance_id}`
(`api/app.py`, `API_PREFIX`) and every business endpoint authenticates, authorizes
the instance, authorizes the operation, and then resolves document ids.

`start` binds `127.0.0.1` only. The host is hardcoded, not a flag. A command-line
run prints no pairing URL; session issuance is the helper's own concern
(`auth/pairing.py`, `auth/sessions.py`). **The operator-facing launch/pairing
flow that prints a single-use loopback URL is NOT IMPLEMENTED as a CLI command in
this build.** `PairingManager.create` exists and works, and `cli.py` `start` does
not call it.

**Read-only snapshot.** Verified:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli render --instance <id>
```

Observed output (exit code 0):

```text
render: ok (OK) instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

`render` writes `review.html` at the folder root through a publish step that
keeps the previous report if the new one cannot be published
(`reporting/snapshot.py`). The command exits 0 only when the new report was
actually published; a failed publish exits 1 and says the previous report was
kept. That 1 is the generic `EXIT_UNEXPECTED`, not a category code:
`SNAPSHOT_PUBLISH_FAILED` has no entry in `_EXIT_MAP` (`errors.py`), so
`exit_code_for` falls through to 1 for it. See the note under `backup` below. Treat the snapshot as read-only: a direct-file report has no server behind
it, so decisions saved in a plain opened file are not authoritative until the
connected page is opened and the change is saved there.

`start` requires the HTTP API package. If it is absent, the command exits 5 with
`ROUTE_UNAVAILABLE` and tells you to use the snapshot. In this repository the API
package is present (`src/resume_review/api/`), so `start` works.

## 5. Summarize: the only step that uses inference

`summarize` needs an approved, restricted analysis route. Without one it refuses
rather than degrading silently. Verified, no route configured:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli summarize --instance <id> --changed-only
```

Observed output (exit code 5):

```text
summarize: error ROUTE_UNAVAILABLE: No approved analysis route is configured, so no summaries can be produced. Configure the RESUME_REVIEW_LIVE_* route variables and try again. Manual review and the deterministic scan do not need a route.
```

The route is configured from six environment variables (`LIVE_ENV` in `cli.py`),
which are the same names the live integration suite uses:

```text
RESUME_REVIEW_LIVE_BASE_URL
RESUME_REVIEW_LIVE_AGENT_ID
RESUME_REVIEW_LIVE_SECRET_FILE
RESUME_REVIEW_LIVE_ROUTE
RESUME_REVIEW_LIVE_ATTESTATION
RESUME_REVIEW_LIVE_PROVIDER_RECORD
```

All six must be set. The secret is passed by **file path**, never inline, and is
read per call; it is never logged, never placed in an exception message, and
never rendered. The route attestation is a JSON object whose fields are the
`RouteAttestation` dataclass fields; unknown keys are ignored.

`--changed-only` queues only documents whose bytes changed since their last
assessment; unchanged documents reuse their committed profile. `--limit N` bounds
one drain.

Behaviour on a configured restricted route (`openclaw_adapter/`, `analysis/`):

- A response that contains a tool call is refused with `ROUTE_POLICY_VIOLATION`.
  A route that returns a tool call is not the restricted context the policy
  believed it was, so the adapter fails closed.
- Model output is data, validated against a versioned JSON schema before it
  reaches the database. Storage paths are resolved from document ids, never taken
  from a model response.
- If the route is local-only and the transport fails, the adapter returns
  `LOCAL_ONLY_FALLBACK_BLOCKED` and does **not** retry elsewhere. A lost local
  route is exactly the moment a remote fallback would be tempting, so it is
  refused.
- Never point the analysis route at an applicant folder. The analysis agent must
  not receive the executor credential, and the resume folder must not be the
  agent's workspace (an applicant-authored `AGENTS.md` in a workspace would be
  injected into the system prompt as operating instructions).

Whether a route is live-tested is a per-deployment fact. In this repository the
live gate is `tests/integration/test_openclaw_live.py`; an unconfigured run skips
with a stated reason. **No live analysis route was exercised while writing this
runbook.**

## 6. Plan, approve, apply

This is the only path that moves a file, and it has three distinct steps.

**Step 1 — plan.** The request file names document ids and, optionally, per-document
intents. Verified:

```json
{
  "document_ids": ["doc_1dfa1d9a5aa7416599ccb296c3aa8d42"],
  "intents": {"doc_1dfa1d9a5aa7416599ccb296c3aa8d42": "move_rejected"}
}
```

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli plan-actions --instance <id> --request request.json
```

Observed output (exit code 0):

```text
plan-actions: ok (OK) instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

The plan is concrete and immutable. Verified operation shape for the request
above:

```text
batch_id= batch_36e0c2c1ad734a6c9b7d209f416b2c60
execution_state= planned
operation= {'operation_id': 'op_7fdfdff1967d404f8232525be3bbbed6',
            'document_id': 'doc_1dfa1d9a5aa7416599ccb296c3aa8d42',
            'kind': 'move_rejected',
            'source': 'candidate-001.txt',
            'destination': 'Rejected/doc_1dfa1d9a5aa7416599ccb296c3aa8d42/candidate-001.txt',
            'source_revision': 1,
            'expected_sha256': '39a68f40...',
            'expected_size': 110,
            'decision_revision': 0,
            'intent_revision': 0,
            'location_version': 0,
            'expected_previous_location': None,
            'origin_batch_id': None}
```

Note what the plan carries: the expected content hash and size, the source
revision, the decision and intent revisions, and the location version. The
destination is derived from the document id, not from the filename, and the
original filename is preserved inside it. A later change to any of those inputs
invalidates the plan; it cannot be applied against stale state.

The request file may not supply `actor`, `requested_by`, `approval`, or
`approved_by`. Those fields are refused with `INVALID_INPUT` because identity
comes from the authenticated session and approval from the review interface.
Planning writes durable per-operation rows immediately, before any file is
touched, so recovery can reconcile from them.

**Step 2 — approve, in the review interface only.** There is no approve command in
the CLI on purpose. Planning is a request; a request is not consent.

**Step 3 — apply.** Verified refusal without an approval:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli apply-actions --instance <id> --batch batch_36e0c2c1ad734a6c9b7d209f416b2c60
```

Observed output (exit code 4):

```text
apply-actions: error APPROVAL_REQUIRED: This batch has no recorded human approval. Sorting, filtering, or a document being marked Reject is not approval.
```

The source file was checked afterwards and was still present and unmoved. That is
the guarantee: a model suggestion, a Reject decision, or a filter cannot move a
file. `apply-actions` succeeds only for a still-valid approval already recorded
by the review interface, matched to the plan hash and not expired.

`apply-actions --dry-run` validates the same preconditions without touching the
filesystem.

Filesystem properties enforced by `actions/executor.py` and the
`storage/no_clobber.py` move primitive:

- Same-volume, no-clobber moves only. A destination that already exists is a
  conflict, never an overwrite. `os.replace`, `shutil.move`, and shell strings
  are not used for managed moves.
- Deletion is recoverable Trash only. No permanent deletion, no automatic
  purging.
- Every destination path is asserted to be inside the registered root and free of
  symlinks, junctions, and other reparse points on every component
  (`storage/paths.py`). Traversal, drive-relative paths, UNC paths, NTFS
  alternate data streams, trailing dot or space components, and reserved device
  names are all refused.

## 7. Recover from an interrupted apply

Verified dry run:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli repair --instance <id> --dry-run
```

Observed output (exit code 0):

```text
repair: ok (OK) instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

`repair` reconciles from the durable operation rows against what is actually on
disk. `--dry-run` writes nothing, to the journal or to location state. Some
outcomes require a human decision and are reported rather than guessed; the
command warns and marks `requires_human` instead of reconciling them
automatically. `--batch <id>` narrows the repair to one batch.

## 8. Backup and restore

Take a verified backup:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli backup --instance <id>
```

Verified, exit code 0. The command uses the SQLite online backup API and then
opens the copy and runs `PRAGMA integrity_check`. If the check does not return
`ok`, the file is discarded and the command fails with exit 1 rather than leaving
an unverified backup. Exit 1 is generic for the same reason as `render` above:
the failure is raised as `SNAPSHOT_PUBLISH_FAILED`, which is unmapped. The
property the command actually guarantees is the one that matters here -- no
unverified copy is left in `.review/backups/`. The artifact lands in `.review/backups/` with a timestamped
name; observed file: `review-20260929T190559-30cf7136.db`. Observed directory
contents after one `backup`:

```text
C:\Jobs\Operations-Manager\
    Rejected\
    Trash\
    candidate-001.txt
    review.html
    .review\backups\review-20260929T190559-30cf7136.db
```

Restore is **NOT IMPLEMENTED as a single command.** It is requested through the
same plan/approve/apply workflow: a restore is a planned action that a human
approves, and the helper executes it. Do not hand-copy `review.db` over a live
database and do not restore while an owner holds the instance.

A backup taken through `backup` or through the pre-migration hook is the same
kind of object. Backups contain recruiting data and the extracted text cache;
protect them at least as strongly as the active folder (PRD 16.2).

## 9. Stop

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli stop --instance <id>
```

Verified, exit code 0. `stop` reports and releases nothing; it inspects the lock.
If another process still holds `.review/locks/owner.lock`, it exits 3 with
`INSTANCE_LOCKED_BY_OTHER_OWNER` and names the holder (pid, host, app version,
instance id) from the advisory diagnostics block. It deliberately does **not**
terminate another process: stop the helper through the entry point that started
it.

Ownership is decided by a kernel lock, not by a PID file and not by a timestamp.
A stale-looking timestamp is never a reason to take a lock that is still held
(`bootstrap/ownership.py`). `setup` takes and releases the lock itself, so a
normal restart works (verified: repeat setup exited 0).

## 10. Exit codes and what to do

| Code | Meaning | Typical first action |
| --- | --- | --- |
| 0 | Success | none |
| 1 | Unexpected internal failure | read the `error.reason` in the JSON envelope; no raw traceback is printed |
| 2 | Invalid input, not found, refused downgrade | check the instance id, the request file, or the job file encoding |
| 3 | Permission failure | check ACLs on the folder and the registry path |
| 4 | Conflict: stale plan, missing or expired approval, collision, integrity failure | re-plan from current state; record approval through the interface |
| 5 | Dependency failure: no API package, no analysis route, failed publish | install the component or configure the route |
| 6 | Unsupported storage topology | move the workspace to local storage on the storage host |

Machine-readable output uses `ok`, `code`, `instance_id`, `data`, `warnings`,
`request_id`. Error telemetry never encodes candidate names or credentials.

## 11. Constraints that are requirements, not preferences

- **SQLite lives on storage-host-local storage.** `setup` rejects a live database
  on a network filesystem (exit 6). The reason is that SQLite's locking, and its
  WAL shared-memory index in particular, are not correct on a network
  filesystem. WAL is only requested when the path is on a local fixed or
  removable volume (`db/connection.py`).
- **One job folder, one database, one helper owner.** Ownership is an OS-backed
  lock at `.review/locks/owner.lock`.
- **Recoverable Trash only.** No permanent deletion, no automatic purging, no
  applicant messaging.
- **No ATS integration.**
- **The Gateway or provider credential must never appear in HTML,** and analysis
  runs on a restricted route. The adapter sends exactly four headers and has no
  parameter that could carry another one (`openclaw_adapter/`; see
  `docs/compatibility.md`).
- **No opaque suitability score, no automatic rejection, no sensitive-trait
  inference.** Human review is an application control.

## 12. Not implemented in this build

- **Launch/pairing URL printing from the CLI is NOT IMPLEMENTED.**
- **Reviewer account creation from the CLI is NOT IMPLEMENTED.** `AccountStore`
  (`auth/store.py`) has a working `create_user` and scrypt-hashed verifiers, but
  no CLI or documented endpoint calls it.
- **Restore as a single command is NOT IMPLEMENTED.**
- **A purge or retention scheduler is NOT IMPLEMENTED.** There is no permanent
  deletion path.
- **File watching is NOT IMPLEMENTED.** Scanning is explicitly invoked.
- **HTTPS serving is NOT IMPLEMENTED.** `start` binds loopback HTTP only.

## 13. What could not be verified here

| Claim | Status |
| --- | --- |
| That an approved batch actually moves a file and writes a journal | Not verified. No approval can be recorded from the CLI by design, so the successful apply path was not exercised. The refusal path was verified. |
| That a managed move is refused across volumes or onto an existing destination | Not verified end to end. Read in `storage/no_clobber.py` and the executor, not executed. |
| That `repair` reconciles a genuinely interrupted move | Only the dry run against a clean instance was verified. |
| That any analysis route produces a valid summary | Not verified. No live route was configured. |
| Session expiry values in a running helper | Not verified at runtime. Read from `auth/sessions.py`: absolute TTL 12 hours, idle TTL 30 minutes, cookie name `rr_sess_<sha256(instance_id)[:16]>`. |
