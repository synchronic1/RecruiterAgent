# Workspace layout

One job folder is one instance.

```text
Job - Operations Manager/
    candidate-001.pdf              <- applicant files, managed here
    candidate-002.docx
    review.html                    <- generated report (snapshot mode)
    Rejected/
        <document-id>/
            original-filename.pdf  <- moved here only after approval
    Trash/
        <batch-id>/<document-id>/
            original-filename.docx
    .review/                       <- application state; not a submission folder
        instance.json              <- generated manifest (not authoritative)
        job.json                   <- generated manifest (not authoritative)
        review.db                  <- AUTHORITATIVE state
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
        locks/
            owner.lock             <- OS-backed single-writer lock
        tmp/
```

## What is authoritative

`review.db` is the source of truth. `instance.json`, `job.json`, snapshots, and
journal exports are **application-generated projections**. Editing one externally
does not mutate authoritative state. Any supported import requires schema
validation, revision checking, and an audit record.

No browser local-storage database is authoritative.

## Paths

Paths stored in records are relative to the registered root, with forward slashes.
Absolute paths live only in the protected host registry — never in the database,
never in a report, never in a model request.

Instance identity is not a hash of an absolute path and is not derived from a
candidate name.

## Discovery exclusions

Initial discovery skips `.review`, `Rejected`, `Trash`, temporary files
(`~$*`, `*.tmp`, `*.crdownload`, `*.part`, `*.swp`), symlinks and junctions, and
unselected nested folders. Registered documents remain tracked after managed moves.

`.review/` is never served by a generic static directory mount.

## Reserved names

`Rejected`, `Trash`, `.review`, and `review.html` are reserved only **after**
ownership and collision checks. Setup must not take over a pre-existing unrelated
folder or overwrite an unrelated `review.html` — it reports a collision instead.

## Portability

**Relocation** preserves the instance ID and history: stop writes, take a
consistent backup, copy or move the workspace, rebind the root on the new host,
verify hashes, and start one owner.

**Fork** creates a new instance ID, resets active plans and credentials, and
requires an explicit choice about importing decisions and notes.

Never run two live copies with the same instance ID.
