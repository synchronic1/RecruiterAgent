# Command reference

Every command is implemented by this repository's CLI. None of them are existing
OpenClaw commands.

## Envelope

Machine-readable output (add `--json` where offered) uses
`schemas/api_envelope.schema.json`:

```json
{
  "ok": true,
  "code": "OK",
  "instance_id": "inst_...",
  "request_id": "req_...",
  "state_revision": 412,
  "data": {},
  "warnings": [{ "code": "SCAN_ONLY_DOCUMENT", "message": "..." }]
}
```

A failure returns `ok: false` with `error.code`, a display-safe `error.message`,
and an `error.retryable` flag. Error messages never contain a candidate name, an
absolute path, or a credential.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Unexpected internal failure |
| 2 | Invalid input |
| 3 | Permission failure (authentication, authorization, integrity) |
| 4 | Conflict (stale revision, expired approval, destination occupied, idempotency reuse) |
| 5 | Dependency failure (route unavailable, adapter timeout, extraction failure) |
| 6 | Unsupported storage |

## Commands

### `setup --folder <root> --job <job-description-file>`

Provision or re-provision one instance. Idempotent: a repeat run preserves every
identifier, every human decision, every note, every completed task, and every
journal entry, and leaves exactly one owner.

Refuses, rather than working around:

* a network-share root (`NETWORK_FILESYSTEM_DATABASE`);
* a topology it cannot classify (`UNSUPPORTED_STORAGE_TOPOLOGY`) unless the
  operator explicitly confirms;
* an unrelated `review.html`, `.review/`, `Rejected/`, or `Trash/`
  (`SETUP_COLLISION`);
* a workspace owned by another live helper
  (`INSTANCE_LOCKED_BY_OTHER_OWNER`);
* a deployed bundle whose bytes differ from the trusted manifest
  (`MANIFEST_MISMATCH`).

Returns: instance ID, connected page address, snapshot path, storage mode, model
route, health, and a suggested next action.

### `start --instance <id>`

Start, or reconnect to, the sole healthy helper for an instance. Takes the
OS-backed ownership lock. A folder-local entry point reconnects to the correct
instance rather than starting a new writer on every browser refresh.

### `status --instance <id> --json`

Health, versions, counts, queue depth, snapshot status, ownership, and — when
available — token and cost usage for the approved route. Safe to call at any time;
it never triggers inference.

### `scan --instance <id>`

Deterministic discovery. Finds new, changed, and missing documents, waits for each
file to become stable before hashing it, and registers a new revision only when
the bytes actually changed. Creates no model calls.

### `summarize --instance <id> --changed-only`

Queue bounded analysis for documents that are new or whose revision, criteria
version, prompt version, schema version, or model route changed. A no-op rescan
produces zero model calls. Before starting, report the estimated scope and the
available budget and wait for agreement.

### `render --instance <id>`

Regenerate `review.html` from committed state. Never triggers inference, never
approves anything, and never mutates applicant records. If publishing the report
is blocked, the previous valid report is kept and the result carries a
stale-snapshot warning.

### `plan-actions --instance <id> --request <json-file>`

Build a concrete, immutable plan from an explicit set of document IDs. The plan
lists every operation with its source, destination, expected content hash, and the
revisions it was built from, plus skipped items and warnings, and carries a
`plan_hash`. Building a plan moves nothing and is not approval.

### `apply-actions --instance <id> --batch <approved-batch-id>`

Execute a batch that already carries a valid human approval for its exact
`plan_hash`. Revalidates the whole plan before the first operation and each
remaining operation immediately before it runs.

* A stale source, decision, intent, location, criteria version, destination, or
  root binding invalidates the authorization.
* If initial validation fails, nothing moves.
* If a conflict appears during execution, remaining work stops, partial completion
  is recorded, and a revised plan is required for the remainder. Completed
  operations are never secretly rolled back.
* Replaying the same apply request does not repeat completed operations.

### `backup --instance <id>`

Create a consistent backup that coordinates the database with the original files.
Uses the SQLite backup API or an equivalent verified snapshot mechanism — never an
arbitrary copy of a live database file.

### `repair --instance <id> --dry-run`

Inspect the journal plus the actual source and destination identity and content,
and report what reconciliation would do. Read the result to the user before
proposing a real repair.

| Observed condition | Reported recovery |
| --- | --- |
| Source present as expected, destination absent | resumable after the approval and expiry checks |
| Source absent, operation-owned destination verified | the move already happened; commit it |
| Both present | stop for reconciliation; never delete one merely because hashes match |
| Neither present | mark missing and request human investigation |
| Content, ownership, or identity differs | block and preserve the evidence |

### `stop --instance <id>`

Stop the helper and release the ownership lock. An operation already running is
allowed to finish or time out; committed results are never discarded.

## HTTP surface

Business endpoints are scoped under `/api/v1/instances/{instance_id}`. The helper
authenticates, then authorizes the instance, then authorizes the operation, and
only then resolves document IDs. A UUID is not authorization.

The application exposes **no** generic SQL, shell, upload-and-execute, arbitrary
path read, or proxy-any-Gateway-request endpoint. The browser's OpenClaw
interaction always passes through a narrow helper adapter.
