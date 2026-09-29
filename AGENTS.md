# AGENTS.md — resume-review

> **This file is a build-time instruction file for coding agents working on this repository.**
> It is **never** loaded as instructions by the application or by the analysis agent at runtime.
> The application treats any `AGENTS.md` / `SKILL.md` / `.md` file found inside a *job folder*
> as untrusted applicant-controlled data (PRD §14.2). See `src/resume_review/security/untrusted.py`.

Authority: `docs/PRD.md` (OpenClaw Resume Review PRD v1.0) and `docs/AGENT_BUILD_HANDOFF.md`.

## Non-negotiable constraints

These come from the handoff and are enforced by tests. Do not weaken them without an ADR in `docs/adr/`.

1. Local operation, per-folder ownership, repeatable setup, storage-host-local shared deployment.
2. Page-bound chat is a retained requirement; deterministic review must work with **no** inference.
3. Reviewer decision, pending intent, and actual location are three separate state dimensions.
4. Never move a file because a model suggested Reject. Only an exact, human-approved plan executes,
   and only through the helper.
5. Recoverable Trash only. No permanent deletion, no automatic purging, no applicant messaging, no ATS.
6. SQLite stays on storage-host-local storage. A mapped-drive live database is rejected by setup.
7. No Gateway credential in HTML. No unrestricted Gateway proxy. Analysis runs on a restricted route.
8. Human state and evidence survive rescans, restarts, template regeneration, and migrations.
9. No sensitive-trait inference, no opaque suitability score, no automatic rejection.
10. Fixtures, implemented features, live-tested integrations, and unfinished work are labeled distinctly.

## Architecture map

| Concern | Module |
| --- | --- |
| Folder provisioning, ownership, integrity | `src/resume_review/bootstrap/` |
| HTTP API, request/response contracts | `src/resume_review/api/` |
| Sessions, roles, CSRF, Origin/Host checks | `src/resume_review/auth/` |
| Connection, migrations, repositories, invariants | `src/resume_review/db/` |
| Discovery, stabilization, extraction adapters | `src/resume_review/ingest/` |
| Two-stage analysis + evidence validation | `src/resume_review/analysis/` |
| OpenClaw Chat Completions adapter | `src/resume_review/openclaw_adapter/` |
| Plan → approve → apply, journal, recovery | `src/resume_review/actions/` |
| No-clobber move primitives, path containment | `src/resume_review/storage/` |
| Self-contained + connected report rendering | `src/resume_review/reporting/` |
| Untrusted-content handling, injection containment | `src/resume_review/security/` |

## Rules for changes

- **Layer direction is enforced.** `db` and `storage` never import `api`. `actions` never imports
  `openclaw_adapter`. `analysis` never imports `actions`. Tests assert this
  (`tests/unit/test_layering.py`).
- **All business endpoints** live under `/api/v1/instances/{instance_id}`. Authenticate → authorize
  instance → authorize operation → resolve document IDs. Never trust a caller-supplied `actor`.
- **Every state mutation** bumps `instances.state_revision` and writes an `audit_events` row in the
  same transaction.
- **Model output is data.** Validate against a versioned JSON schema before it reaches the database.
  Storage paths are resolved from document IDs, never taken from a model response.
- **Filesystem operations** go through `storage.no_clobber` only. Never `os.replace`, never
  `shutil.move`, never a shell string. A check-then-rename sequence is a bug.
- Keep comments at the density of the surrounding code. No emoji in source or docs.

## Commands

```bash
python -m pytest                     # full deterministic suite
python -m pytest -m "not slow"       # fast subset
python -m pytest -m live             # requires a live OpenClaw route; excluded by default
python -m resume_review.cli --help   # product CLI (PRD §5.3)
```

## Honesty rules

Do not claim a component works because its interface, prompt, or mock exists. Every claim in
`docs/acceptance-report.md` cites a test ID and a recorded result. Unfinished controls stay labeled
`NOT IMPLEMENTED`.
