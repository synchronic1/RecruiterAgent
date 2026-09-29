# ADR 0002: The per-operation source identity that proves a completed move

- Status: Accepted
- Date: 2026-09-29
- Scope: crash reconciliation (PRD 13.3, row 2), the `file_operations` journal,
  planning, and the destination-ownership proof
- Depends on: ADR 0001 (foundation decisions), specifically decision 2 (kernel
  no-clobber moves) and decision 3 (independent state dimensions)

This record closes a partial implementation of PRD 13.3 row 2. It explains why a
per-operation source identity is required, why the filesystem makes that identity
sufficient, and what recovery does when the identity is unavailable.

---

## 1. Context

PRD 13.3 specifies crash recovery for an interrupted file operation. Its second
row reads:

> Source absent; expected operation-owned destination verified -> Reconcile the
> location and commit the already-performed move.

and its fifth row, on which row 2 depends:

> Content, path ownership, or identity differs -> Block and preserve evidence of
> the conflict.

The accompanying rule is explicit: destination identity is "its
operation/document-owned namespace and recorded metadata, not just a matching
content hash. A copied file from another actor must not be mistaken for proof of
a completed move."

The migration `0001_initial.sql` gives `file_operations` the source path, the
`source_revision`, the expected size, and the expected content hash, but nothing
that identifies the *physical file* the plan was built from. `documents.fs_identity`
records the document's identity, but it is the document's **current** identity and
is not bound to any particular operation or `source_revision`. As a result,
recovery could only corroborate a row-2 destination using the document's moving
identity, and when that was absent it reported `IDENTITY_UNVERIFIED` rather than
commit. The row was therefore only partly implemented: safe, but unable to commit
a genuinely completed move without leaning on state that is not tied to the
operation.

## 2. Decision

`file_operations` gains one nullable column, `source_identity`, added by
`migrations/0002_file_operation_source_identity.sql`. It holds the same
`volume:inode:size:mtime_ns` string produced by
`storage.no_clobber.FileIdentity.digest_hint`.

It is written once, at plan time, from the file still sitting at the operation's
recorded source path: the planner captures it with
`storage.no_clobber.file_identity` while it builds each `PlannedOperation`, and
`Repository.create_file_operations` writes it into the durable journal row. The
planner stages the value on the repository through
`Repository.record_planned_source_identity`; planning stays read-only, and no
batch, operation, or audit row is written until the plan is persisted.

Recovery's row-2 path (`actions/recovery.py`) **prefers** this operation-bound
identity for the destination-ownership proof, falling back to
`documents.fs_identity` only when the operation carries none (a journal row
written before this migration). The evidence records which value was used and
where it came from, so a reviewer sees exactly what was relied on.

## 3. Why a rename makes the identity sufficient

A same-volume rename preserves the file's physical identity. On Windows,
`MoveFileExW` moves the directory entry and the file index
(`st_ino` as Python reports it) is unchanged; on POSIX, `rename(2)` and the
`link`/`unlink` fallback preserve `st_ino`. ADR 0001 decision 2 already requires
every managed move to be exactly this kind of kernel rename and forbids a
copy-and-delete fallback.

Two consequences follow directly:

1. The identity captured at plan time is exactly the identity the destination
   must present after a genuine move. Requiring a match is not an extra heuristic;
   it is the definition of the operation having happened.
2. A file **copied** to the recorded destination -- by any other actor, for any
   reason, with byte-identical content -- has a new identity and therefore
   **cannot** satisfy the check. This is precisely the "copied file mistaken for a
   completed move" case the PRD names.

This is strictly stronger than a size comparison. Size was already removed as an
ownership signal because a copy shares it; content hash is no better. The inode is
the one property a copy cannot reproduce and a rename cannot change.

## 4. Fail-safe behaviour when the identity is unavailable

The identity is corroborating evidence, never an authorization input, and its
absence is never treated as agreement.

- If either side reports no usable file index (inode `0`, as some Windows
  configurations and filesystems do), `_identity_agrees` returns `None`, and the
  destination is reported as `IDENTITY_UNVERIFIED`: the move is **not** committed
  on size or content hash alone.
- If the operation has no recorded identity (a journal row written before
  migration 0002) and the document has none either, the result is the same:
  `IDENTITY_UNVERIFIED`, no commit, and the evidence lists what could and could
  not be established.
- The fallback to `documents.fs_identity` never *substitutes* for a recorded
  operation identity: when the operation carries a value, that value is the one
  compared, even when its inode is unusable. The comparison is against the
  operation's own record, not whatever the document looks like now.

## 5. Consequences

- A genuine same-volume move whose source has disappeared is now reconcilable to
  `committed` on the strength of an identity bound to the operation, not to the
  document's current state.
- A copy placed at the recorded destination produces `IDENTITY_OR_CONTENT_DIFFERS`
  (or `IDENTITY_UNVERIFIED`), blocks, and preserves every file for inspection.
  Recovery still never deletes either file (ADR 0001 decision 3; PRD 13.3 row 3).
- `FileOperationRecord` predates this column and is unchanged. Recovery resolves
  the identity through `Repository.get_file_operation_source_identity`, keeping
  the frozen model and its row mapper untouched.
- The identity is captured while the source is at its recorded path, before any
  move. A plan whose source file cannot be read records no identity and degrades
  to the fail-safe path rather than inventing one.
- Migration 0002 is additive: it changes no existing column and restructures no
  table from 0001.
