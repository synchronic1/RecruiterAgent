# Shared-host operation

Authority: PRD section 15.2 (shared folder with storage-host-local helper),
section 15.3 (shared access controls), section 4 (relocation and fork), section
11.3, and section 16. This runbook is about one machine physically hosting the
job folder and serving reviewers over a private network, and about what that
topology changes relative to `docs/local-operation.md`.

Read the headline first: **the application as built serves loopback HTTP only.
Serving remote reviewers over authenticated HTTPS is NOT IMPLEMENTED in this
build.** The deployment the PRD describes is the target architecture, and this
file records which parts of it the code enforces today, which parts it refuses to
violate, and which parts remain to be built. Do not read this file as an
installation guide for a working multi-reviewer server.

## 1. The one rule that shapes everything

**The live SQLite database stays on storage local to the machine that physically
hosts the job folder.**

Reviewers connect to that machine through the application API. They do not open
the live database through a mapped drive. The reason is not a preference: SQLite
documents that its locking and, in WAL mode, its shared-memory index are not
correct over a network filesystem, and the PRD is explicit that "one writer on a
different machine does not remove the underlying network-filesystem risk".

The code enforces this at provisioning time:

- `storage/topology.py` classifies the volume. A UNC path, a mapped network drive
  (`GetDriveTypeW` returns `DRIVE_REMOTE`), or any filesystem in `_NETWORK_FS`
  (`nfs`, `cifs`, `smb`, `smbfs`, `fuse.sshfs`, `9p`, `virtiofs`, and others)
  is classified `NETWORK`.
- `assert_supported_for_database` then raises `NETWORK_FILESYSTEM_DATABASE`,
  which `errors.py` maps to process exit code 6. The message names the limitation
  and states that read-only snapshots can still be viewed rather than silently
  relocating state.
- An unclassifiable volume is `UNKNOWN` and is refused with
  `UNSUPPORTED_STORAGE_TOPOLOGY` (also exit 6) unless the caller explicitly
  confirms it. Unknown topology is not automatically accepted.
- `db/connection.py` requests `PRAGMA journal_mode = WAL` only when the path is
  on a local fixed or local removable volume. On anything else it sets the
  rollback journal. This is a second, independent guard: even a database that
  somehow ends up on network storage will not be put into WAL mode.

If the device holding the folders cannot run the helper, then a fully writable,
folder-contained workspace is **unsupported on that device for version 1**.
Report that plainly. Do not move authoritative recruiting state to another
computer or a cloud database; that requires explicit approval as an architecture
exception. Read-only snapshots (`review.html`) can still be viewed from such a
device.

## 2. What this topology changes about ownership and locking

Only one helper owner is allowed per instance. The lock is a kernel lock, not a
file's mere presence:

| Platform | Primitive | File |
| --- | --- | --- |
| Windows | `msvcrt.locking` / `LockFile` on a one-byte range | `.review/locks/owner.lock` |
| POSIX | `fcntl.flock(LOCK_EX \| LOCK_NB)` | `.review/locks/owner.lock` |

Source: `bootstrap/ownership.py`. Both are released by the operating system when
the owning process dies, which is the property a PID file lacks.

Consequences for a shared host:

- **Ownership is never stolen.** `InstanceLock.acquire` offers a bounded timeout
  for a controlled restart and raises `INSTANCE_LOCKED_BY_OTHER_OWNER` otherwise.
  There is no timeout at which stealing happens. A stale-looking timestamp is
  never a reason to take a lock that is still held.
- **The lock is meaningful across processes on the same machine.** It is not a
  cross-host fencing mechanism. Another host cannot safely take over until the
  previous owner is stopped or fenced and the database is consistently
  transferred. Two live copies of one instance id must never run, even briefly.
- If a second host must run the helper, the previous owner stops first, the
  database is transferred consistently, and the registry is rebound on the new
  host. Anything else is a fork, not a move (section 5).

The registry that maps instance ids to canonical roots is per-host, at
`%LOCALAPPDATA%\ResumeReview\registry.json` (or `RESUME_REVIEW_REGISTRY_DIR`).
It is not a shared or replicated store. Moving the workspace to a different host
means rebinding the root there.

## 3. The database location is not a setting

`setup_instance` computes the database path as
`.review/review.db` under the registered root (`workspace.db_path`). There is no
option to place the live database elsewhere. The pre-migration backup, the CLI
`backup` command, and the journals all live under the same root.

This is deliberate: the folder is the instance. A configuration that separated
the database from the resumes would also separate the authoritative state from
the thing being reviewed, and would make the backup unit ambiguous.

## 4. Reviewer identities and roles

PRD 15.3 requires distinct authenticated reviewer identities with Viewer,
Reviewer, and Administrator roles, and states that a display name typed into the
page is not authentication.

What exists in the code:

- `auth/store.py` implements `AccountStore`, a JSON-backed host-local account
  store. Passwords are hashed with `hashlib.scrypt` (N=2^14, r=8, p=1, dklen=32)
  and the parameters are recorded inside each verifier string, so a future cost
  increase re-hashes on the next successful sign-in. Nothing logs or returns a
  plaintext password; the verifier is excluded from the dataclass `repr`.
- `assert_host_local_secret_path` mechanically enforces where the account file
  may live. It refuses, in order: any path containing a `.review` component, any
  path inside the portable job folder, and any path on a network filesystem. It
  raises rather than warning, because exporting sign-in secrets with the job
  folder is exactly the failure this prevents.
- The store writes atomically and sets the file mode `0o600` and its directory
  `0o700`. On Windows those POSIX bits are best-effort; use ACLs.
- Roles are `Viewer`, `Reviewer`, and `Administrator` (`models.py`,
  `Role`). The display name is a label only; it never becomes the actor
  reference.
- `auth/sessions.py` issues opaque, in-memory sessions. There is no JWT and no
  persistence: when the helper stops, every session is gone. Each session is
  bound to exactly one instance id, and the cookie name is derived from the
  instance id (`rr_sess_<sha256(instance_id)[:16]>`), because cookies are not
  isolated by TCP port. Two expiries are enforced: absolute (12 hours) and idle
  (30 minutes), both on every resolve.
- `auth/pairing.py` implements single-use, 120-second pairing tokens bound to the
  operating-system user that launched the helper. The token is removed before
  validation, so a rejected attempt cannot be retried and a successful one cannot
  be replayed.

What is missing: **creating and managing accounts from the CLI is NOT
IMPLEMENTED.** `AccountStore.create_user` and `set_role` work, but no CLI
subcommand and no documented endpoint in this build calls them. Until that is
wired, an operator has no supported, tested way to provision reviewer identities
on the storage host. Treat this as a blocker for shared use, not a detail.

Alternatives the PRD permits, such as an existing identity-aware proxy in front
of the helper, are **NOT IMPLEMENTED** and untested here.

## 5. Relocation and fork

These are different operations with different outcomes (`bootstrap/` and PRD 4).

**Relocation** preserves the instance id and its history:

1. Stop writes. The owner releases the lock; confirm with
   `resume_review.cli stop --instance <id>`.
2. Take a consistent backup. `resume_review.cli backup --instance <id>` produces
   a verified copy in `.review/backups/` (integrity checked; a failed check
   discards the file and fails the command).
3. Copy or move the whole workspace.
4. Rebind the root on the new host. `setup` on the moved folder adopts the
   folder's own instance identity from `.review/instance.json` rather than minting
   a second one, and re-registers the canonical root on the new host.
5. Verify hashes and start exactly one owner.

Never run two live copies with the same instance id. Because instance identity is
an opaque id and not a hash of the absolute path, moving the folder does not
change who the instance is.

**Fork** creates a new instance id, resets active plans and credentials, and
requires an explicit choice about importing decisions and notes. A fork is not
achieved by copying the folder and running both copies.

Do not hand-edit `instance.json` or `job.json`. They are application-generated
manifests; editing them externally does not mutate authoritative state, which
lives in the database.

## 6. Access control around the folder

Host administrators retain filesystem power. Application code, database files,
secrets, and managed destinations should be protected by ACLs so that:

- Reviewers use the UI for mutations. The server never silently applies
  last-writer-wins; a second reviewer editing an old decision gets a conflict
  showing the current value and actor.
- An inbound drop area may be writable for submission intake, but that must not
  grant applicants access to the report or to `.review`. Never expose `.review`
  through a generic static directory mount.
- The account store file is readable only by the service account.
- Original files are served through scoped handlers with safe download by
  default. Filenames, notes, summaries, chat output, and CSV fields are escaped;
  spreadsheet formula injection is neutralised in any later CSV export.
- Host and Origin checks, CSRF protection for session-authenticated mutations,
  strict CORS, a conservative content security policy, clickjacking protections,
  request limits, and session expiry are all enforced (`auth/guards.py`,
  `auth/csrf.py`). Unapproved external asset requests and analytics are denied.

`setup` refuses to adopt a folder it does not recognise, and will not overwrite
an unrelated `review.html`. That protects against a shared folder being claimed
by mistake, not against a hostile local user with write access.

## 7. Why authenticated HTTPS is the missing piece

PRD 15.2 says reviewers "connect to the helper via authenticated HTTPS on an
approved private network or protected tunnel".

The code does not do this yet, and the gap is structural rather than a missing
flag:

- `cli.py` `_cmd_start` hardcodes `host = "127.0.0.1"`. There is no bind-address
  or TLS option.
- `cli.py` `_serve` calls `uvicorn.run(app, host=host, port=port)` with no
  certificate arguments.
- The pairing flow that mints a loopback launch URL (`auth/pairing.py`) is not
  invoked by `start`.

So the reachable modes today are: loopback HTTP on the storage host, plus
read-only `review.html` snapshots. A reviewer on another machine cannot connect
to a running helper. **Remote reviewer access is NOT IMPLEMENTED.**

Until it is, the honest shared-host pattern is: run the helper on the storage
host for local use, and treat the rendered snapshot as the read-only artifact
that may be copied to other machines. Snapshots contain recruiting data and
require controlled retention.

When remote access is built, the requirement is not optional: authenticated
HTTPS over an approved private network or protected tunnel, with the identity
mechanism documented and tested before release. Do not expose the helper to the
public internet; the analysis route's own gateway credential is a full
operator-scope credential, so an applicant-facing surface must never be able to
reach it.

## 8. Analysis on a shared host

- The resume-analysis context must not inherit the setup/orchestration
  capability. The setup context may request privileged installation steps
  through operator approval; the analysis context must not have them.
- The analysis agent's workspace must be a dedicated trusted folder, never an
  applicant folder. On the first turn of a session, OpenClaw injects the
  workspace's `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md`,
  and `MEMORY.md` into the system prompt. An applicant-controlled folder used as
  the workspace would inject applicant-authored text as operating instructions.
- Tool denial is enforced in the agent's configuration, not by prompt text. The
  required deny list and the operator attestation are recorded in
  `docs/compatibility.md`.
- Disable cross-job or global memory of applicant information for the analysis
  route.
- Keep tokens and login secrets outside shared folders, generated HTML, and
  portable backups. The Gateway credential must never appear in HTML, and no
  unrestricted Gateway proxy may be exposed. Analysis runs on a restricted route
  with no dangerous tools.

## 9. Constraints restated as requirements

| Requirement | Reason | Where enforced |
| --- | --- | --- |
| Live database on storage-host-local storage | SQLite locking and WAL shared-memory index are not correct on a network filesystem | `storage/topology.py`, `db/connection.py`, setup exit 6 |
| Mapped drive rejected for a live database | Same. A mapped drive is a network filesystem with a drive letter | `_probe_windows`, `DRIVE_REMOTE` |
| Per-folder ownership | One job folder is one instance with its own database and history | `bootstrap/workspace.py` |
| One owner per instance, OS-backed | A PID file or timestamp cannot prove liveness and cannot see a lock held from another machine | `bootstrap/ownership.py` |
| Recoverable Trash only | No permanent deletion, no automatic purging | `storage/no_clobber.py`, actions layer |
| No ATS integration, no applicant messaging | Out of scope for version 1 | product boundary |
| Credential never in HTML; restricted analysis route | The Gateway token is an owner/operator credential | `openclaw_adapter/`, `docs/compatibility.md` |
| No opaque suitability score, no automatic rejection | Human decision boundary | `analysis/`, review model |

## 10. Not implemented in this build

- **Remote reviewer access is NOT IMPLEMENTED.** `start` binds loopback HTTP
  only. No TLS, no bind-address option.
- **Reviewer account provisioning from the CLI is NOT IMPLEMENTED.**
- **Authenticated HTTPS over a private network or tunnel is NOT IMPLEMENTED.**
- **An identity-aware proxy integration is NOT IMPLEMENTED.**
- **An automated topology-confirmation flag for setup is NOT IMPLEMENTED at the
  CLI level.** `setup_instance` accepts `storage_confirm_unknown`, but the CLI
  exposes no flag for it, so an `UNKNOWN` volume cannot be confirmed from the
  command line.
- **A purge or retention scheduler is NOT IMPLEMENTED.**

## 11. What could not be verified here

| Claim | Status |
| --- | --- |
| That `setup` refuses a UNC path or a mapped network drive with exit 6 | Not verified end to end. No network share or mapped drive was available on the test host. The refusal path was read in `storage/topology.py` and `errors.py`; it was not executed. |
| That the two-machine topology works at all | Not verified. No remote reviewer ever connected, because remote access is not implemented. |
| That `AccountStore` provisions usable reviewer sign-in on the storage host | Not verified end to end. `create_user` was read, not run through a supported operator path (there is none). |
| That HTTPS with a private-network certificate is reachable | Not applicable; not implemented. |
| Behaviour on a Linux storage host | Not verified. No Linux host was available. `_probe_posix` reads `/proc/self/mountinfo` and `/proc/mounts`; the Windows path in this repository is the one exercised. |
| Relocation and fork procedures | Not verified. Read from `bootstrap/registry.py`, `bootstrap/workspace.py`, and the PRD; no relocation was performed. |
