# ADR 0001: Foundation decisions

- Status: Accepted
- Date: 2026-09-29
- Scope: instance database, filesystem move semantics, state model, extraction
  caching, instance ownership, and release integrity

This record captures the decisions that everything else in `resume-review` is
built on, the alternatives that were rejected, and the places where the
implementation deliberately departs from a literal reading of the PRD. Later
work should treat these as settled unless it supersedes this record explicitly.

---

## 1. SQLite is the instance store, and WAL only on local storage

**Context.** Each job folder owns its state and must survive the folder being
copied, moved between directories, or left alone for months. A server-side
database would make the folder useless without the server; a document store
would be unreviewable by hand during recovery.

**Decision.** One SQLite database at `<root>/.review/review.db` (recorded here
originally as `resume-review.db`; the name as built has always been
`review.db`, from `DB_FILENAME` in `bootstrap/workspace.py`), opened
through `resume_review.db.connection.open_connection`. Write-ahead logging is
enabled **only** when the resolved storage topology is `LOCAL_FIXED` or
`LOCAL_REMOVABLE`. The connection always sets `PRAGMA foreign_keys=ON`,
`synchronous=FULL`, and a bounded `busy_timeout`.

**Why.** SQLite is a single file that travels with the folder, is inspectable
with ordinary tools, and gives real transactions. WAL is the right durability
mode on a local disk, but its shared-memory (`-shm`) file is unsafe on network
shares, where byte-range lock behaviour is not guaranteed by the remote
implementation. Enabling WAL on a network path would trade a rare, detectable
slowdown for a possible silent corruption.

**Consequence.** A network or unclassifiable path cannot be used as a database
location at all: `assert_supported_for_database` refuses it with
`NETWORK_FILESYSTEM_DATABASE` (exit 6) or, for `UNKNOWN`, with
`UNSUPPORTED_STORAGE_TOPOLOGY` unless the operator confirms explicitly. The
refusal is checked before any file is created, so a refused setup leaves the
target directory untouched.

---

## 2. No-clobber moves use a kernel primitive, with no copy-and-delete fallback

**Context.** PRD 13.2 requires a move that "never overwrite[s] a destination"
and is "a tested no-clobber move primitive for each supported host OS."

**Decision.** `resume_review.storage.no_clobber.atomic_no_clobber_move` is the
only sanctioned way to move a managed applicant file. It drives a kernel
operation that *fails* when the destination exists:

| Host | Primitive |
| --- | --- |
| Windows | `MoveFileExW` without `MOVEFILE_REPLACE_EXISTING` |
| Linux | `renameat2(..., RENAME_NOREPLACE)` |
| macOS | `renamex_np(..., RENAME_EXCL)` |
| Other POSIX | `link(2)` then `unlink(2)` (atomic; `link` fails `EEXIST`) |

**Why not check-then-rename.** A userspace existence check followed by a rename
has a time-of-check/time-of-use window. Another actor — a second helper, a
sync client, the reviewer in Explorer — can create the destination in that
window, and the rename then destroys it. Only the kernel can make "fail if the
destination exists" atomic with the move itself.

**Why no copy-then-delete fallback.** A cross-volume request is refused
outright. Degrading a rename into copy-then-unlink would silently drop the
atomicity that the whole recovery story depends on: an interrupted copy leaves
a plausible-looking but incomplete file, and the source is then removed. A
refusal is recoverable; a half-move presented as completed is not.

**Consequence.** Nothing in this codebase may call `os.replace`,
`shutil.move`, or a shell string to move a managed file. `AGENTS.md` states the
rule, and the review of every later module should check it.

---

## 3. Five state dimensions stay independent

**Context.** PRD section 10 names five concepts that recur in every decision
about a document, and warns that collapsing them produces a UI that lies.

**Decision.** `resume_review.models` keeps five separate enums, stored in
separate columns:

| Dimension | Enum | Representative values |
| --- | --- | --- |
| Processing | `ProcessingState` | discovered, extracting, analyzing, ready, manual_review, error, stale |
| Review decision | `ReviewState` | unreviewed, keep, reject, hold |
| Actual location | `Location` | active, rejected, trash, missing, conflict |
| Pending intent | `PendingIntent` | none, move_rejected, restore_active, move_trash, restore_previous |
| Action execution | `ExecutionState` | planned, approved, applying, completed, partial, blocked, canceled |

**Why.** A row may legitimately be *reject* + *active* + *move_rejected pending*:
the reviewer decided, the file has not moved yet, and a move is queued. That is
three independent truths. A single `status` column forces a choice among them,
and the first thing to break is the rule that the UI must not show a completed
rejection-folder move before the move happened.

**Consequence.** No module may derive one dimension from another. In
particular, `Location` is owned by filesystem reconciliation, never by the
decision the reviewer made, and `decision_needs_recheck` is a separate flag
rather than a sixth value smuggled into `ReviewState`.

---

## 4. Extraction is cached by content hash and parser version

**Context.** PRD 12 ("Process incrementally. Cache extraction by content hash
and parser version ... Same-document retries must not create duplicate profiles
or tasks.").

**Decision.** The `extraction_cache` table is keyed by content hash plus parser
version (see `migrations/0001_initial.sql`). A document revision that hashes to
bytes already parsed by the same parser version reuses the cached extraction.
Assessment caching, when added, is keyed by document revision plus criteria
version, prompt version, model route/version where known, and analysis schema
version.

**Why content hash, not path or mtime.** Recruiters rename files, duplicate
them, and re-download them into different folders. Byte content is the only
stable identity for "the same resume". Modification time is explicitly not an
application submission date (PRD, ingestion section), and a path changes under
ordinary use.

**Why the parser version is in the key.** A parser upgrade that fixes an
encoding bug must not be masked by a cache entry produced by the old parser.
Hashing the input alone would do exactly that.

**Consequence.** Cache lookups are content-addressed and never keyed on a
candidate name or a folder path, so no cache key is personally identifying.

---

## 5. Ownership is an OS-backed lock, not a PID file

**Context.** PRD 15.3: "Validate ownership with an OS-backed lock ... Do not
steal ownership based only on a stale timestamp."

**Decision.** `resume_review.bootstrap.ownership.InstanceLock` holds a kernel
lock on one byte of `<root>/.review/locks/owner.lock`:

- Windows: `msvcrt.locking(fd, LK_NBLCK, 1)`.
- POSIX: `fcntl.flock(fd, LOCK_EX | LOCK_NB)`.

A second live owner fails with `INSTANCE_LOCKED_BY_OTHER_OWNER` (409). The file
also carries human-readable diagnostics (pid, host, boot time, app version,
instance id, acquired-at), but **no ownership decision is ever made from
them**.

**Why not a PID file.** A PID file's owner can vanish without cleaning up — a
crash, a `SIGKILL`, a power loss, a container restart. The file then claims a
dead owner forever, and the usual workaround ("if the pid is gone, take it")
races with PID reuse and cannot see a holder on another machine. A kernel lock
is released by the operating system when the process dies, which is exactly the
property that is needed, and it costs nothing to keep.

**Why the lock is not advisory-only here.** Both primitives are exclusive
whether or not the peer cooperates, so a helper that ignores the protocol still
cannot make a second writer's lock succeed.

**Consequence.** A normal restart (process stopped, lock released by the OS,
then started again) succeeds — proven by `test_second_owner_cannot_acquire_and_can_after_release`.
Ownership acquisition is a distinct step from setup: setup takes the lock, and
refuses to run while another owner holds it.

---

## 6. The trusted release manifest lives outside the job folder

**Context.** PRD section 4: "A checksum in the same writable folder as an
executable is not sufficient protection against replacement."

**Decision.** `resume_review.bootstrap.registry.HostRegistry` stores the
per-instance record and the trusted release manifest on the operating user's
private application-data path — `%LOCALAPPDATA%/ResumeReview` on Windows,
`$XDG_DATA_HOME/resume-review` or `~/.local/share/resume-review` elsewhere —
overridable with `RESUME_REVIEW_REGISTRY_DIR` for tests and operators. Each
entry records the canonical root, service address, storage mode, app version,
schema version, trusted bundle hash, `created_at`, and `last_seen`.

**Why.** Anyone who can drop a resume into the folder can write the folder.
A manifest stored beside the deployed bundle would be rewritable by the same
actor, which is precisely the attacker the check exists to catch. Keeping the
trusted copy on a path those writers cannot reach makes tampering detectable.

**Why instance identity is opaque.** The id comes from
`resume_review.util.new_id("instance")`. It is never a hash of an absolute
path and never derived from an applicant name: a folder can be moved or renamed
without changing who the instance is, and an id must not leak a person's name.

**Consequence.** A corrupt registry is *refused* (`MANIFEST_UNTRUSTED`) rather
than silently recreated, because recreating it would discard the trusted
manifests — the one thing an attacker able to write that file would want.
Registry read-modify-write is serialized by an OS-backed lock of its own and
committed by writing a temp file and renaming it into place.

---

## 7. The trusted manifest is recorded before the bundle is copied

**Context.** AT-04 requires that bootstrap verifies deployed code against its
trusted installed release before launching it.

**Decision.** `deploy_bundle` calls `registry.set_trusted_manifest(...)`
**first**, then copies the reviewed bundle into `.review/app/`, then verifies
what landed against that recorded manifest. `verify_deployed` is what setup
calls before launching anything, and a mismatch raises `MANIFEST_MISMATCH`
(exit 3).

**Why this order.** If the manifest were recorded after the copy, the copy
would be checked against itself and any pre-existing tampering would be blessed
as the new baseline. Recording first means the comparison has an independent
reference.

**Consequence.** A tampered deployed template blocks setup and is left in place
for an operator to inspect — the tool does not "repair" by overwriting evidence.
`bundle_hash` is sha256 over the sorted `path\0sha256\n` lines; the NUL
separator stops a path containing spaces from aliasing another path/hash pair.

---

## 8. Bootstrap writes bypass `state_revision` and the audit log

**Context.** `AGENTS.md` requires every state mutation to bump
`instances.state_revision` and write an `audit_events` row in the same
transaction.

**Decision.** Provisioning writes (creating the job row, applying migrations,
writing generated manifests) run in a bare transaction that neither bumps
`state_revision` nor writes an audit row.

**Why.** Those writes create the instance rather than mutate reviewed state, and
setup is idempotent: running it again must not look like a state change. If
every repeat setup bumped the revision, clients holding a revision would be
invalidated for no reason, and the audit log would fill with entries that
record nothing a reviewer did. The rule in `AGENTS.md` governs mutations made
through the API; provisioning is the act that establishes the instance the rule
is about.

**Consequence.** `test_setup_twice_preserves_state_and_leaves_one_owner` proves
a seeded revision of 7 survives a second setup, and that the seed rows are
counted identically before and after.

---

## 9. Departures, platform notes, and their justification

These are the points where the implementation does something the PRD does not
spell out, or where a platform forced a shape the design would not otherwise
have chosen. Each is a deliberate exception, recorded so a later reader does
not "fix" it back.

### 9.1 `os.replace` is used for the application's own records

`AGENTS.md` forbids `os.replace` for managed files. The host registry, the
lock file's diagnostics, the generated manifests, and the deployed bundle files
are *not* managed applicant files: they are the application's own records, and
the correct update for a record is "replace it wholesale, atomically". A temp
file plus `os.replace` gives a reader either the old document or the new one,
never a mixture. Managed applicant files still go through
`storage.no_clobber.atomic_no_clobber_move` without exception.

### 9.2 `O_BINARY` on every Windows `os.open`

The Windows CRT opens files in text mode by default, so `os.write` translates
every `\n` into `\r\n`. This was observed in practice: a copied `helper.py`
landed as `print(1)\r\r\n` because the source already contained `\r\n` and the
CRT added another `\r`. The effect on a deployed bundle is that the copied
bytes differ from the reviewed bytes and the very next verification reports
tampering; the effect on the lock diagnostics is that the fixed-size padded
block is no longer a fixed size, so a later shorter write leaves a stale tail.
Every `os.open` that writes in this codebase therefore passes
`getattr(os, "O_BINARY", 0)`.

### 9.3 The lock byte is byte 0; diagnostics start at byte 1

Windows region locks are **mandatory**: while a process holds a byte range, no
other handle can read those bytes. An attempt to place the lock at an offset
and keep a human-readable payload in bytes 0..offset silently made the whole
prefix unreadable, and `msvcrt.locking` at a non-zero offset locked from byte 0
through the end of the requested range anyway. The layout was therefore fixed
as: lock byte 0 exactly, write diagnostics from byte 1 onward as a
space-padded 4095-byte block, and read them back from byte 1. This keeps the
diagnostics visible to a second process while the lock is held — the whole
point of writing them.

### 9.4 Verification reports the complete difference, not the first

`verify_manifest` collects every missing, extra, and modified path, and also
recomputes the expected bundle hash from the manifest's own file list. A
manifest whose `bundle_hash` field does not cover its `files` list is reported
as `MANIFEST_UNTRUSTED`. A controlled repair needs the full list, and a
manifest that has been edited is not a baseline at all.

### 9.5 Collision detection uses an application marker

A folder is recognised as ours by `<root>/.review/instance.json` containing
`{"app": "resume-review", "instance_id": ...}`. Anything else at a reserved
name — an unrelated `review.html`, a foreign `.review`, `Rejected`, or `Trash` —
is a collision (`SETUP_COLLISION`, exit 4), and nothing is overwritten or
adopted. A request to provision a folder already bound to a different instance
id is also a collision. Collision messages name conflict entries by relative
name only: no absolute path and no candidate name appears in an error.

### 9.6 The bootstrap bundle source is injectable

`setup_instance` accepts `bundle_dir` (defaulting to the repository `web/`
directory, or the packaged templates when installed). Deploying from a
directory that other processes edit concurrently would make verification
non-deterministic, so the parameter exists both to keep tests reproducible and
to let an installer point at a verified staging directory.
