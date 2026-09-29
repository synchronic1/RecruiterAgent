# Installation and first provisioning

Authority: PRD section 5.2 (setup sequence), section 5.3 (application command
contract), and section 4 (folder layout, ownership, portability). This runbook
takes one machine from "no instance" to "a provisioned job folder with a current
database, a deployed report bundle, and a host-registry entry".

Every command below is the product CLI declared in `pyproject.toml`
(`resume-review = "resume_review.cli:main"`), implemented in
`src/resume_review/cli.py`. A CLI command cannot manufacture human approval, and
nothing here does.

## 1. Prerequisites

| Requirement | Value | Source |
| --- | --- | --- |
| Operating system | Windows 11 (the tested local target) | PRD 15.1 |
| Python | 3.14 interpreter in a repository-local virtual environment | `.venv/Scripts/python.exe` in this repository |
| Workspace volume | A local fixed or local removable volume, not a mapped drive or UNC path | `src/resume_review/storage/topology.py` |
| Free space at the root | At least 64 MiB | `MIN_FREE_BYTES` in `src/resume_review/bootstrap/setup.py` |
| Job description | A UTF-8 text file | `_cmd_setup` in `src/resume_review/cli.py` requires it |

The storage rule is not advisory. `storage/topology.py` classifies the target
volume and `assert_supported_for_database` refuses a network filesystem with
`NETWORK_FILESYSTEM_DATABASE`, which the CLI maps to exit code 6. The reason is
that the live SQLite database must stay on storage local to the machine hosting
the folder; SQLite documents that its locking and, in WAL mode, its shared-memory
index do not work correctly on a network filesystem. `db/connection.py` only
requests `PRAGMA journal_mode = WAL` when the path is on a local fixed or local
removable volume, and falls back to the rollback journal otherwise.

Install the runtime dependencies into the virtual environment before the first
command. This step was **not run by the author of this runbook** and is therefore
unverified:

```powershell
cd C:\Users\NM2\Documents\RecruiterAgent
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

## 2. Confirm the CLI is the one you think it is

Verified. Observed on this repository:

```powershell
cd C:\Users\NM2\Documents\RecruiterAgent
.\.venv\Scripts\python.exe -m resume_review.cli --help
```

Observed output (exit code 0):

```text
usage: resume-review [-h] [--version] COMMAND ...

Folder-local, evidence-backed resume review. Each resume folder is one instance with its own database, report, and action history.

positional arguments:
  COMMAND
    setup          Provision or re-provision an instance.
    start          Serve the connected review page for an instance.
    status         Report instance status (JSON by default).
    scan           Run the deterministic discovery and extraction pass.
    summarize      Run the configured analysis route.
    render         Render the review.html snapshot.
    plan-actions   Build an action plan from a request file.
    apply-actions  Apply an already-approved action batch.
    backup         Take a verified database backup.
    repair         Reconcile interrupted file operations.
    stop           Report or release the instance helper lock.

options:
  -h, --help       show this help message and exit
  --version        show program's version number and exit

exit codes:
  0  success
  1  unexpected internal failure
  2  invalid input, not found, or a refused downgrade
  3  permission failure
  4  conflict (stale plan, missing or expired approval, ...)
  5  dependency failure (no HTTP API package, no analysis route, ...)
  6  unsupported storage topology
```

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli --version
```

Observed output (exit code 0):

```text
resume-review 0.1.0
```

The same entry point is installed as `.venv\Scripts\resume-review.exe`
(`[project.scripts]` in `pyproject.toml`). Where this runbook writes
`.\.venv\Scripts\python.exe -m resume_review.cli`, `resume-review.exe` is
equivalent.

## 3. Write the job description

`setup` reads the job description as UTF-8 text and derives the job title from
its first non-empty line, sanitised, never invented (`_derive_title` in
`bootstrap/setup.py`). Observed:

```powershell
New-Item -ItemType Directory C:\Jobs\_inputs | Out-Null
Set-Content -Encoding utf8 C:\Jobs\_inputs\job.txt "Operations Manager`n`nOwn the warehouse operations team."
```

Approved criteria are a separate, later step. Without criteria the instance still
records documents and supports manual review, but nothing is assessed; the scan
reports `awaiting_criteria` as a warning rather than fabricating a match
(`bootstrap/setup.py` step 5; `cli.py` `_cmd_scan`).

## 4. Provision the instance

The folder must be an **absolute** path. A relative path is refused before
anything is created. Verified:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli setup --folder relative\path --job C:\Jobs\_inputs\job.txt
```

Observed output (exit code 2):

```text
setup: error INVALID_INPUT: The workspace folder must be an absolute path.
```

Now the real run. Verified against an empty folder on a local fixed volume:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli setup --folder C:\Jobs\Operations-Manager --job C:\Jobs\_inputs\job.txt
```

Observed output (exit code 0):

```text
setup: ok (OK) instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

The instance id is opaque and generated by `util.new_id`. It is never a hash of
the absolute path and never derived from a candidate name, so moving the folder
later does not change who the instance is (`bootstrap/registry.py`). Record the id
printed here; every later command addresses the instance by it.

For machine-readable output add `--json`. The envelope keys are `ok`, `code`,
`instance_id`, `data`, `warnings`, `request_id`, and `state_revision`
(`_envelope` in `cli.py`).

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli setup --folder C:\Jobs\Operations-Manager --job C:\Jobs\_inputs\job.txt --json
```

## 5. What setup actually did

`setup_instance` in `src/resume_review/bootstrap/setup.py` runs the PRD 5.2
sequence in a fixed order. The order is load-bearing:

1. **Resolve and validate the root.** Absolute path required. A symlink, junction,
   or other reparse point as the root is refused (`SymlinkEscape`,
   `root_is_link`).
2. **Classify storage topology.** A network filesystem is refused. An
   unclassifiable volume is refused with `UNSUPPORTED_STORAGE_TOPOLOGY` unless the
   caller explicitly confirms it; unknown topology is not automatically accepted.
3. **Check access, free space, and reserved-name collisions.** An existing folder
   that is not recognisably this application's is refused rather than adopted.
4. **Acquire the OS-backed single-writer lock** at `.review/locks/owner.lock`
   before touching the database (`bootstrap/ownership.py`).
5. **Verify release integrity**, then initialise or migrate the database. On an
   upgrade the pre-migration backup runs first and must pass `PRAGMA
   integrity_check` or migration is not attempted (`db/migrations.py`).
6. **Deploy the versioned report bundle** to `.review/app/`, and record its
   trusted manifest in the host registry.
7. **Register the instance** and write `.review/instance.json` and
   `.review/job.json`.

Verified layout after the run above:

```text
C:\Jobs\Operations-Manager\
    Rejected\
    Trash\
    .review\
        app\
            assets\
            manifest.json
            templates\
        backups\
        exports\
        extracted\
        instance.json
        job.json
        journals\
        locks\
            owner.lock
        migrations\
        review.db
        tmp\
```

This directory set is created by `ensure_layout` in
`src/resume_review/bootstrap/workspace.py`, which creates exactly the reserved
names the PRD lists and nothing else. `Rejected/` and `Trash/` are the only
applicant-visible directories this application claims.

`review.html` appears at the folder root only after a `render`; it is not created
by `setup`.

## 6. Where the host registry lives

The host registry is **outside** the job folder, under the operating user's
private application data:

```text
%LOCALAPPDATA%\ResumeReview\registry.json
```

Source: `default_registry_dir()` in `src/resume_review/bootstrap/registry.py`.
The placement is the point: the job folder is writable by anyone who can drop a
resume into it, so a trusted manifest stored beside the deployed bundle would be
rewritable by the same actor. The registry holds the trusted release manifest per
instance, so a tampered `.review/app/` can be detected by comparison against
something the folder's writers cannot reach.

An operator who needs a different private location sets
`RESUME_REVIEW_REGISTRY_DIR` to a **directory**. The verification runs in this
runbook set it to a temporary directory so no state was left in the real per-user
registry; the commands above are otherwise identical to a production run.

## 7. Re-running setup is safe and is how you upgrade

`setup` is idempotent. It preserves identifiers, summaries, notes, reviewer
decisions, completed tasks, and the audit trail, and it releases the lock on the
way out so a normal restart succeeds. The return value carries before/after counts
so the claim is provable rather than asserted.

Verified on a populated instance (one document, one open task):

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli setup --folder C:\Jobs\Operations-Manager --job C:\Jobs\_inputs\job.txt --json
```

Observed (exit code 0):

```text
ok True created False preserved True
counts_before {'documents': 1, 'decisions_set': 0, 'notes': 0, 'tasks_open': 1, 'tasks_closed': 0, 'profiles': 0, 'intents': 0, 'audit_events': 9}
counts_after  {'documents': 1, 'decisions_set': 0, 'notes': 0, 'tasks_open': 1, 'tasks_closed': 0, 'profiles': 0, 'intents': 0, 'audit_events': 9}
```

`preserved` is `True` only when every counted dimension and the state revision
are unchanged.

Two refusals worth knowing, both verified:

- A folder that already holds an unrelated `review.html`, or a reserved directory
  this application did not create, is refused. Observed output (exit code 4):

  ```text
  setup: error SETUP_COLLISION: This folder already contains entries this application did not create, so it will not be adopted. Move the folder's contents aside or choose an empty folder.
  ```

  The check is deliberately conservative: any reserved name present without an
  ownership manifest is a conflict, because an empty `Rejected\` made by hand
  cannot be told from one that already holds moved files
  (`assert_no_collision` in `bootstrap/workspace.py`).

- A database written by a newer build is refused with `DOWNGRADE_REFUSED`. The
  database is not touched. Downgrades are refused rather than risk writing
  older-shaped rows into a newer schema (`assert_downgrade_allowed` in
  `db/migrations.py`).

## 8. First status check

Verified:

```powershell
.\.venv\Scripts\python.exe -m resume_review.cli status --instance inst_413fbfca4a3840e5afd93d61ba1ae306
```

Observed output (exit code 0; `status` prints JSON by default):

```json
{
  "ok": true,
  "code": "OK",
  "instance_id": "inst_413fbfca4a3840e5afd93d61ba1ae306",
  "data": {
    "instance_id": "inst_413fbfca4a3840e5afd93d61ba1ae306",
    "root_label": "jobfolder",
    "state_revision": 0,
    "storage_mode": "local",
    "app_version": "0.1.0",
    "schema_version": 2,
    "counts": {
      "total": 0, "processed": 0, "unreviewed": 0, "keep": 0, "reject": 0,
      "hold": 0, "manual_review": 0, "pending_action": 0, "needs_recheck": 0,
      "open_tasks": 0
    },
    "manifest_schema_version": 2,
    "lock_backend": "msvcrt.LockFile(no-steal)"
  },
  "warnings": [],
  "request_id": "req_119795456454af14aa887bc4f37135b763b",
  "state_revision": 0
}
```

`lock_backend` is `msvcrt.LockFile(no-steal)` on Windows and
`fcntl.flock(LOCK_EX|LOCK_NB)` elsewhere (`lock_backend_name` in
`bootstrap/ownership.py`). An unknown instance id exits 2 with
`INSTANCE_NOT_FOUND`; verified.

## 9. After setup

Setup stops at "the instance exists, its database is current, its bundle is
deployed, and its manifests are written". It starts no helper process. Continue
with:

1. `scan` to register submissions (`docs/local-operation.md` section 3).
2. `start` to serve the connected review page, or `render` for the read-only
   snapshot (`docs/local-operation.md` section 4).

`setup` reports `service_address` as `null`; no service address is derived at
provisioning time.

## 10. Not implemented in this build

These are stated plainly because the interface for them exists in the code but no
operator-facing command does:

- **Creating reviewer accounts from the CLI is NOT IMPLEMENTED.** `AccountStore`
  in `src/resume_review/auth/store.py` implements a host-local, scrypt-hashed
  account store with Viewer, Reviewer, and Administrator roles, and
  `create_user` works, but no CLI subcommand or documented endpoint in this build
  calls it. Provisioning reviewer identities is therefore an open item before
  shared use.
- **Purging or retention scheduling is NOT IMPLEMENTED.** There is no purge
  scheduler; retention settings are intended to create administrative review
  tasks, and no permanent-deletion path exists.
- **A permanent-delete path is NOT IMPLEMENTED, deliberately.** Deletion is
  recoverable Trash only. There is no automatic purging.
- **ATS integration is NOT IMPLEMENTED**, and no applicant messaging exists.

## 11. What could not be verified here

| Claim | Status |
| --- | --- |
| That `setup` refuses a mapped network drive with exit 6 | Not verified end to end. No network drive was available. The refusal path is `storage/topology.py` (`_probe_windows`, `_DRIVE_REMOTE` to `NETWORK`) and `assert_supported_for_database`, which raises `NETWORK_FILESYSTEM_DATABASE`; `errors.py` maps that code to exit 6. |
| That dependency installation succeeds from `requirements.lock` | Not run. |
| That a schema migration on a populated older database preserves data | Not verified. No older database was available to upgrade. `apply_migrations` and its backup hook were read, not executed against a pending migration. |
| Anything about the shared-host topology | Out of scope for this file; see `docs/shared-host-operation.md`. |
