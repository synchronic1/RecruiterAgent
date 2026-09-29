# Migration files

The canonical, packaged migrations live at **`src/resume_review/migrations/`**.

They are shipped inside the Python distribution so that a deployed instance can
apply them from `.review/migrations/` without depending on this repository
layout. This directory exists only to point at them; it deliberately contains no
copies, because two copies of a migration is how schema drift starts.

```text
src/resume_review/migrations/
    0001_initial.sql
    0002_file_operation_source_identity.sql
```

Applied migrations are recorded in the `schema_migrations` table together with a
checksum of the file that was applied. `resume_review.db.migrations` refuses to
continue if a previously applied migration's bytes have changed, and refuses to
open a database written by a newer build (see `assert_downgrade_allowed`).
