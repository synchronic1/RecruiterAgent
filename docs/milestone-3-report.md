# Milestone 3 report: verified ground truth of the whole repository

Scope: an independent re-measurement of the entire repository after the
milestone-3 run, plus a truthful account of what the suite proves and what it
does not. Authority: `docs/PRD.md`, `docs/AGENT_BUILD_HANDOFF.md`, and the binding
constraints in `AGENTS.md`. This is the measurement of record; it reports what was
run and observed, and it fixes nothing.

**Headline: the suite is RED. Five consecutive full runs at the end of this
session all report `10 failed`, with the same ten test ids, and exit code 1. The
orchestrator's stated baseline of `680 passed, 11 skipped, 0 failed` does not
describe the tree this run produced.** The ten failures decompose into two
independent defects, both described in section 2. No source or test file was
edited by this work; the only file written is this report.

Note on the report path: a different agent in this run wrote an API-scoped report
to `docs/milestone-3-report.md` (mtime 11:21) while this measurement was in
progress. That path was assigned to this task, so this file supersedes it. Its
central API finding (the end-to-end HTTP flow over
`tests/integration/test_end_to_end_api.py`) was independently re-run here and
passes (`7 passed`, exit 0); the rest of its content is not reproduced, and no
claim below depends on it.

## Commands run and observed results

All commands were run from the repository root with the project interpreter
`.venv/Scripts/python.exe`. `pyproject.toml` sets `addopts="-q"`, so `-q` was
never passed twice. Environment: Python 3.14.0, pytest 8.4.2.

| Command | Observed result | Exit |
| --- | --- | --- |
| `.venv/Scripts/python.exe -m pytest` (run 1) | `10 failed, 1029 passed, 16 skipped, 1 xfailed, 300 warnings in 70.41s (0:01:10)` | 1 |
| `.venv/Scripts/python.exe -m pytest` (run 2) | `10 failed, 1029 passed, 16 skipped, 1 xfailed, 300 warnings in 72.61s (0:01:12)` | 1 |
| `.venv/Scripts/python.exe -m pytest` (run 3) | `10 failed, 1029 passed, 16 skipped, 1 xfailed, 300 warnings in 83.18s (0:01:23)` | 1 |
| `.venv/Scripts/python.exe -m pytest -rs` (skip reasons) | `12 failed, 1034 passed, 16 skipped, 1 xfailed, 333 warnings in 91.41s` | 1 |
| `.venv/Scripts/python.exe -m pytest -p no:cacheprovider` (run A, late) | `10 failed, 1038 passed, 16 skipped, 1 xfailed, 333 warnings in 74.85s (0:01:14)` | 1 |
| `.venv/Scripts/python.exe -m pytest -p no:cacheprovider` (run B, late) | `10 failed, 1038 passed, 16 skipped, 1 xfailed, 333 warnings in 79.96s (0:01:19)` | 1 |
| `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/integration/test_end_to_end_api.py` | `7 passed, 34 warnings in 2.45s` | 0 |

The exact summary line of the three initial runs is verbatim:

```
10 failed, 1029 passed, 16 skipped, 1 xfailed, 300 warnings in 70.41s (0:01:10)
```

### Determinism

Runs 1, 2 and 3 are byte-identical in counts and identical in the set of failing
test ids, so the suite is deterministic **within a fixed tree**. Runs A and B,
taken later in the session, are also identical to each other (`1038 passed`) but
differ from runs 1-3 by nine passing tests, and the `-rs` run in between shows
`12 failed` rather than `10`. That variation is **not** pytest nondeterminism. It
is concurrent mutation of the working tree by other agents in this run, which was
observed directly:

* `web/assets/report.css`, `report.js`, `manifest.json`,
  `src/resume_review/templates/report.{css,html,js}` and
  `web/templates/report.html` were rewritten at 11:19-11:25 while the suite was
  running. When the committed `web/assets/manifest.json` disagrees with the
  on-disk `assets/report.css`, the two report-asset integrity tests
  (`tests/unit/test_web_assets.py::test_manifest_hashes_match_the_committed_bytes`
  and
  `tests/unit/test_packaging.py::test_report_assets_are_byte_identical_to_the_repository_copies`)
  fail, which is what the `-rs` run's extra two failures were. Both pass in
  isolation once the manifest is regenerated (`35 passed in 0.19s` for
  `test_web_assets.py` + `test_packaging.py`), and both pass in runs A and B.
* One late run recorded `1 error` at setup of
  `tests/unit/test_cli.py::test_status_is_json_by_default` (`assert 3 == 0`,
  exit code `PERMISSION`) while `src/resume_review/templates/*` were being
  rewritten at 11:22:49. The same test passes in isolation and in runs A and B;
  the error was a mid-write race, not a defect.

The ten failing test ids, by contrast, are identical in every run and are the
stable ground truth of this report.

## 1. Verified counts

| Quantity | Observed (initial runs 1-3) | Observed (late runs A-B) |
| --- | --- | --- |
| passed | 1029 | 1038 |
| failed | 10 | 10 |
| skipped | 16 | 16 |
| xfailed | 1 | 1 |
| exit code | 1 | 1 |

The orchestrator's baseline was `680 passed, 11 skipped, 0 failed`. This run added
passing tests, skips and failures:

* **+5 skips**: the 4 opt-in real-corpus tests (`tests/corpus/test_corpus_smoke.py`)
  and 1 CLI guard skip (`tests/unit/test_cli.py:179`, see section 2) are new. The
  original 11 skips are the live OpenClaw gate.
* **+1 xfail**: `tests/unit/test_analysis_adversarial.py` contributes one expected
  failure that was not in the baseline.
* **+~349 passing tests** and **+10 failures**: the milestone-3 body of work
  (HTTP API, per-operation source identity, corpus opt-in, CLI, adversarial
  suites).

Both headline numbers are honest for the moment they were measured. The `passed`
count is a moving target because sibling agents were still adding tests during
this session; the failing set is not.

## 2. Every failure, with its exact assertion output

Ten tests fail, reproducibly, in every full run and in isolation. They split into
two independent defects.

### Defect A: migration 0002 was added without raising `SCHEMA_VERSION` (8 failures)

`src/resume_review/__init__.py` sets `SCHEMA_VERSION = 1`. The migration set also
contains `src/resume_review/migrations/0002_file_operation_source_identity.sql`,
which brings a database to version 2. `resume_review.db.migrations.assert_downgrade_allowed`
refuses a database whose version exceeds `SCHEMA_VERSION`, so every test that
reopens or re-migrates a database written by this build fails.

| Test id | Assertion output |
| --- | --- |
| `tests/integration/test_setup.py::test_instance_manifest_marker_and_ids_are_not_path_derived` | `assert 2 == 1` where `2 = int(marker["schema_version"])` (tests/integration/test_setup.py:146) |
| `tests/integration/test_setup.py::test_setup_twice_preserves_state_and_leaves_one_owner` | `MigrationError: This workspace was written by a newer version ... DOWNGRADE_REFUSED` (bootstrap/setup.py:224 -> db/migrations.py:153) |
| `tests/integration/test_setup.py::test_repeat_setup_does_not_rewrite_the_job_row_when_unchanged` | same `DOWNGRADE_REFUSED` |
| `tests/integration/test_setup.py::test_changed_job_description_updates_in_place` | same `DOWNGRADE_REFUSED` |
| `tests/integration/test_setup.py::test_redeploy_after_a_bundle_upgrade_is_verified` | same `DOWNGRADE_REFUSED` |
| `tests/integration/test_setup.py::test_setup_refuses_while_another_owner_holds_the_instance` | same `DOWNGRADE_REFUSED` |
| `tests/unit/test_repository.py::test_migration_applies_cleanly` | `assert 2 == 1` where `2 = current_version(...)` (tests/unit/test_repository.py:120) |
| `tests/unit/test_file_operation_identity.py::test_migration_0002_applies_and_is_idempotent` | `DOWNGRADE_REFUSED` on the idempotent second `apply_migrations(conn)` (tests/unit/test_file_operation_identity.py:369) |

One further test silently skips for exactly this reason rather than fail:
`tests/unit/test_cli.py::test_repeat_setup_preserves_human_state`, whose guard
`_frozen_schema_version_matches_migrations()` returns False and skips with
"frozen migration layer is inconsistent: an installed migration is newer than
SCHEMA_VERSION". That skip is itself evidence of the defect.

Required fix (not made here; the file is frozen for this task): raise
`SCHEMA_VERSION` in `src/resume_review/__init__.py` from `1` to `2` so the build
and its migration set agree. Reported in `blocked_on_frozen_file`.

### Defect B: operation-bound identity broke two pre-existing reconciliation tests (2 failures)

`tests/unit/test_executor.py` and `tests/unit/test_executor_adversarial.py` were
**not** modified in this run (mtimes 10:14 and 10:24, before the run boundary).
The run changed reconciliation to prefer the per-operation `source_identity`
recorded at plan time (`actions/planner.py` records it via
`Repository.record_planned_source_identity`; `actions/recovery.py` prefers it over
`documents.fs_identity`, per ADR 0002). Two pre-existing tests encode the old
behaviour and now fail.

| Test id | Assertion output |
| --- | --- |
| `tests/unit/test_executor.py::test_a_crash_after_the_move_is_reconciled_not_re_moved` | `assert False is True`; `ApplyOutcome(... ).ok` is False because "The destination content matches but its file identity does not match the recorded source; this is not established as proof of this operation's move." (tests/unit/test_executor.py:461) |
| `tests/unit/test_executor_adversarial.py::test_crash_reconciliation_without_identity_evidence_blocks_rather_than_guesses` | `AssertionError: assert 'completed' == 'blocked'` (tests/unit/test_executor_adversarial.py:610) |

Cause, from reading the two tests and the code they exercise:

* The first test simulates the interrupted move with `place_file(... DATA)` plus
  `unlink` — a **copy**, not a rename. A copy has a new inode, so the new,
  ADR-mandated identity proof correctly refuses to call it a completed move. The
  test's simulation is not a rename, which ADR 0001 requires of every managed
  move; under the new semantics the test's expectation (`ok is True`) is wrong.
* The second test simulates the move with `os.replace` — a real rename — and then
  asserts the outcome is `blocked` because it was written when no operation-bound
  identity was recorded. The planner now records one, so the rename's preserved
  identity matches and reconciliation commits; `'completed'` is the ADR-0002
  behaviour.

So Defect B is a genuine regression in the sense that the suite went from green to
red, but the two tests are stale relative to ADR 0002: either the tests must be
updated to simulate a rename and to expect a commit, or the implementation must
stop recording/ preferring the operation identity. That decision belongs to the
identity work's owner, not to this measurement.

## 3. Network isolation check

The only test module that can reach the network is
`tests/integration/test_openclaw_live.py`. It is the sole place a real HTTP client
is constructed against a configured endpoint
(`httpx.AsyncClient(...)` at line 491, `OpenClawAdapter(live.config())` at lines
510, 533, 557, 611, 633, 665, 678, 714), and the module is marked
`pytestmark = pytest.mark.live`. Without `RESUME_REVIEW_LIVE_*` environment
variables every item calls `pytest.skip(...)`; the `-rs` run shows all 11 of its
tests skipped with the message "no live OpenClaw route is configured". No live
route is configured in this environment.

Every other httpx use in the tests is offline:

* `tests/unit/test_openclaw_adapter.py` passes `httpx.MockTransport` to every
  `OpenClawAdapter` it builds (verified at every call site; the three bare
  `OpenClawAdapter(config)` calls at lines 271, 320 and 543 use a `config` whose
  `transport` is a recorder's MockTransport).
* `tests/unit/test_pipeline.py` uses a fake async adapter, not httpx.
* The API tests use `starlette.testclient.TestClient`, which drives the ASGI app
  in-process and opens no socket.
* `tests/security/test_auth.py` uses loopback URLs only as string literals.

The remaining 5 skips are 4 real-corpus tests (opt-in via
`RESUME_REVIEW_REAL_CORPUS=1`) and 1 CLI guard skip; none of them performs I/O in
the default configuration. **No test reached the network.**

## 4. Frozen-file and change-set check

There is no `.git` in this tree (`git status` -> "not a git repository"), so the
change set cannot be derived from version control. It was derived from file
modification times. The run boundary was taken as 2026-09-29 10:31:30, the mtime of
`docs/milestone-2-report.md`, whose own table records the orchestrator's `680
passed` baseline; files touched after that instant belong to this run.

Every source or test file touched after the boundary is listed in section 5. Two
findings are worth stating plainly:

1. **`src/resume_review/__init__.py` was modified in this run (10:49) but
   `SCHEMA_VERSION` was not raised**, even though migration 0002 was added in the
   same run. This single omission causes Defect A (8 tests) and the CLI guard skip.
2. **`actions/recovery.py` (11:08), `actions/planner.py` (10:45) and
   `db/repository.py` (10:45) changed move-reconciliation behaviour**, while the
   two pre-existing tests that depend on that behaviour
   (`test_executor.py`, `test_executor_adversarial.py`) were not touched. This
   causes Defect B (2 tests).

No source file outside those changed by this run appears in the list; nothing
under `.venv/`, `.pytest-tmp/` or `.pytest_cache/` is part of the product. Apart
from defects A and B, no surprise edits to pre-existing frozen behaviour were
detected. I cannot enumerate the sanctioned owned-path set for each agent (that
list is not in this session), so I can only report the complete touched set, not
diff it against an intended set.

## 5. Files created or changed in this run

Method: `find <tree> -newermt "2026-09-29 10:31:30"`, excluding `__pycache__` and
`*.egg-info`. "New" means the path did not exist before this run as far as mtime
alone can show; "changed" means it existed and was rewritten.

### Source, created or changed

| File | Time | Purpose (one line) |
| --- | --- | --- |
| `src/resume_review/__init__.py` | 10:49 | Package version module. **Changed but `SCHEMA_VERSION` left at 1** (Defect A). |
| `src/resume_review/migrations/0002_file_operation_source_identity.sql` | 10:42 | New migration adding `file_operations.source_identity`. |
| `src/resume_review/actions/planner.py` | 10:45 | Captures and stages per-operation source identity at plan time. |
| `src/resume_review/actions/recovery.py` | 11:08 | Prefers the operation-bound identity for row-2 reconciliation (ADR 0002). |
| `src/resume_review/db/repository.py` | 10:45 | `record_planned_source_identity` / `get_file_operation_source_identity`; writes the column. |
| `src/resume_review/api/__init__.py` | 10:43 | API package surface. |
| `src/resume_review/api/app.py` | 10:47 | FastAPI application factory and route registration. |
| `src/resume_review/api/deps.py` | 10:47 | Instance/actor dependency resolution. |
| `src/resume_review/api/envelope.py` | 10:43 | Response envelope construction. |
| `src/resume_review/api/errors.py` | 10:42 | Error-to-envelope mapping. |
| `src/resume_review/api/idempotency.py` | 10:42 | Idempotency-key handling for write endpoints. |
| `src/resume_review/api/backup.py` | 10:55 | Backup endpoint. |
| `src/resume_review/api/documents.py` | 10:55 | Document read/write endpoints. |
| `src/resume_review/api/jobs.py` | 10:55 | Job text endpoints. |
| `src/resume_review/api/scan.py` | 10:55 | Scan endpoint. |
| `src/resume_review/api/analysis.py` | 10:56 | Analysis/assessment endpoints. |
| `src/resume_review/api/chat.py` | 10:56 | Page-bound chat endpoint. |
| `src/resume_review/api/criteria.py` | 10:56 | Criteria endpoints. |
| `src/resume_review/api/review.py` | 10:56 | Review decision endpoints. |
| `src/resume_review/api/tasks.py` | 10:56 | Task endpoints. |
| `src/resume_review/api/actions.py` | 11:03 | Plan/approve/apply/restore endpoints. |
| `src/resume_review/cli.py` | 10:44 | Product CLI (`setup`, `scan`, `status`, JSON envelopes). |
| `src/resume_review/reporting/snapshot.py` | 11:21 | Report snapshot rendering. |
| `src/resume_review/schemas/*.json` (5 files) | 10:39 | Packaged schemas: action_plan, analysis_result, api_envelope, filter, manifest. |
| `src/resume_review/templates/report.{css,html,js}` | 11:25 | Packaged presentation assets. |

### Tests, created or changed

| File | Time | Tests collected | Purpose |
| --- | --- | --- | --- |
| `tests/conftest.py` | 10:43 | n/a | Shared fixtures. |
| `tests/unit/test_layering.py` | 10:39 | 18 | Enforces the AGENTS.md layer direction. |
| `tests/unit/test_packaging.py` | 10:43 | 7 | Packaged assets match repository copies. |
| `tests/unit/test_cli.py` | 10:46 | 26 | Product CLI behaviour (1 now skips; section 2). |
| `tests/unit/test_api_core.py` | 10:47 | 44 | Core HTTP routes: status, auth, origin, envelope. |
| `tests/unit/test_file_operation_identity.py` | 10:47 | 8 | **New.** Per-operation source identity and row-2 recovery. |
| `tests/unit/test_api_chat.py` | 10:56 | 22 | Chat endpoint guards and stubbed model. |
| `tests/unit/test_api_documents.py` | 10:57 | 38 | Document endpoints. |
| `tests/unit/test_api_actions.py` | 10:58 | 20 | Plan/approve/apply/restore endpoints. |
| `tests/unit/test_api_analysis.py` | 10:58 | 23 | Analysis endpoints. |
| `tests/unit/test_api_adversarial.py` | 11:08 | 94 | Adversarial API cases (smuggled fields, origin, replay). |
| `tests/unit/test_cli_layering_adversarial.py` | 11:09 | 34 | **New.** CLI/entry-point layering adversarial cases. |
| `tests/unit/test_identity_adversarial.py` | 11:10 | 27 | **New.** Identity/reconciliation adversarial cases. |
| `tests/unit/test_feedback_contract.py` | 11:22 | 2 | **New.** Feedback contract (added late, by a sibling). |
| `tests/integration/test_end_to_end_api.py` | 11:17 | 7 | **New.** Real app over TestClient: decide -> plan -> approve -> apply -> restore. |
| `tests/integration/test_end_to_end_review.py` | 10:31 | 4 | End-to-end deterministic analysis pipeline (milestone 2). |
| `tests/corpus/test_corpus_smoke.py` | 10:42 | 4 | **New.** Opt-in real-corpus smoke tests (skipped by default). |
| `tests/corpus/conftest.py` | 10:43 | n/a | Corpus fixtures/opt-in gate. |
| `tests/corpus/corpus_audit_plugin.py` | 11:07 | n/a | Corpus audit plugin. |

### Docs, web and root

| File | Time | Purpose |
| --- | --- | --- |
| `docs/adr/0002-per-operation-source-identity.md` | 10:45 | ADR for the per-operation source identity. |
| `docs/corpus.md` | 10:43 | Real-corpus usage and opt-in policy. |
| `docs/review-artifacts/*.html`, `*.txt` | 11:23-11:26 | Review/screenshot artifacts (4 files). |
| `web/templates/report.html` | 11:19 | Browser report template. |
| `web/assets/report.css`, `report.js`, `manifest.json` | 11:24-11:25 | Browser assets and their hash manifest. |
| `requirements.lock` | 10:38 | Locked dependency set. |
| `pyproject.toml` | 10:40 | Project metadata, deps, pytest config. |
| `README.md` | 10:43 | Project README. |

## 6. NOT VERIFIED by this work

This is the list of things this work did not exercise. It is not a claim that any
of them is broken; it is what was not established.

* **Everything in the live OpenClaw gate.** All 11 tests in
  `tests/integration/test_openclaw_live.py` were skipped. No live route, no real
  model output, no route attestation against a real gateway, no timeout, no
  fail-closed behaviour on a real route failure was observed. Not verified by this
  work.
* **The API layer beyond the specific tests that exist.** This work re-ran
  `tests/integration/test_end_to_end_api.py` (7 passed) and the API unit suites as
  part of the full run, but did not design or review them; it cannot vouch that
  every endpoint is covered. Not verified by this work.
* **The real-corpus path.** All 4 `tests/corpus/` tests were skipped (opt-in). No
  real resume was ingested. Not verified by this work.
* **Real model output quality, prompt effectiveness, token accounting, and the
  one-repair-turn budget.** The suite substitutes stub or fake clients. Not
  verified by this work.
* **The route-policy denial path on a live route** (`assert_inference_allowed`
  refusing an under-attested route). Only construction-time refusals are covered
  offline. Not verified by this work.
* **Concurrency.** Two simultaneous mutations from separate sessions are not
  exercised anywhere I ran. Not verified by this work.
* **The Windows zero-inode identity fallback.** `_identity_agrees` returns `None`
  when a filesystem reports inode 0; no test forces that condition. Not verified
  by this work.
* **Report rendering and publication, and the browser client**, end to end. Unit
  tests render payloads, but no browser or HTTP publish path was exercised here.
  Not verified by this work.
* **Asset integrity as a stable property.** The report-asset hash tests passed in
  runs A and B but failed mid-session while the assets were being rewritten.
  This work did not establish that asset integrity is stable under concurrent
  edits. Not verified by this work.
* **The cause of the two Defect-B tests being left stale.** I read the tests and
  the code, but I did not determine the identity work's intended acceptance
  criteria for them. Not established by this work.
* **Whether any *further* files changed after my final run.** The tree was still
  being modified at 11:26; later edits by sibling agents are outside this
  measurement. Not verified by this work.

## 7. Remaining unimplemented or unverified controls, ranked

Ranked by how much each would matter to a reviewer who has to trust this
application. These are the honest inputs to an acceptance report.

1. **The deterministic suite is red (10 failures).** Until Defect A
   (`SCHEMA_VERSION`) and Defect B (stale reconciliation tests) are resolved,
   every other claim about the application is undermined: a reviewer cannot treat
   a red suite as evidence of correctness, and repeat `setup_instance` is
   genuinely broken for any real workspace, not just in tests.
2. **The live inference path has never been exercised.** The application's core
   promise — evidence-backed analysis through a restricted OpenClaw route — rests
   entirely on 11 skipped tests. Route attestation enforcement against a real
   endpoint, the analysis skill loading, per-agent tool policy, session
   separation, and fail-closed timeout behaviour are all unverified. This is the
   single largest trust gap after the red suite.
3. **No acceptance report exists.** `docs/acceptance-report.md` is absent, yet
   `AGENTS.md` and PRD AT-40 require every claim there to cite a test id and a
   recorded result. The honesty requirement is not yet satisfied by an artifact.
4. **Real applicant-corpus ingestion is unverified by default.** The 4 corpus
   tests are opt-in and were skipped. The real-PII path (parsing, normalization,
   PII containment) has no default-run evidence.
5. **Report/asset integrity was observed to drift.** The committed
   `web/assets/manifest.json` and the on-disk assets disagreed during this
   session, and only re-converged after an external rewrite. Asset hashing is a
   real integrity control; its stability is not established.
6. **No HTTP login route exists.** Authentication is real at the store/session
   level but has no transport, so a reviewer cannot actually sign in over HTTP;
   the session/cookie path is only exercised by tests that inject a cookie.
7. **Concurrency is untested.** No simultaneous-mutation, lock-contention, or
   double-apply race scenario is exercised, despite `state_revision` and
   single-owner locking being load-bearing invariants.
8. **Model output quality and budget are untested.** Prompt effectiveness, the
   repair-turn budget, and token accounting are unverified behind stubs; a
   reviewer cannot judge from this suite whether real analysis is any good.
9. **The route-policy denial path is unverified on a live route.** That a
   missing or incomplete attestation blocks inference is asserted offline, not
   observed against a live gateway.
10. **Browser/report publication is unverified end to end.** Rendering is unit
    tested, but no publish-and-view flow was exercised.
11. **The zero-inode filesystem fallback is unverified.** On a filesystem that
    reports inode 0, reconciliation degrades to `IDENTITY_UNVERIFIED`; no test
    forces that branch.

## 8. Reproducing

```bash
# Full suite (three consecutive runs produced identical counts)
.venv/Scripts/python.exe -m pytest

# Skip reasons, to confirm the live gate and the corpus/CLI skips
.venv/Scripts/python.exe -m pytest -rs

# The two defect groups in isolation
.venv/Scripts/python.exe -m pytest tests/integration/test_setup.py tests/unit/test_repository.py::test_migration_applies_cleanly tests/unit/test_file_operation_identity.py::test_migration_0002_applies_and_is_idempotent
.venv/Scripts/python.exe -m pytest tests/unit/test_executor.py::test_a_crash_after_the_move_is_reconciled_not_re_moved tests/unit/test_executor_adversarial.py::test_crash_reconciliation_without_identity_evidence_blocks_rather_than_guesses
```

Note: because other agents were editing the tree concurrently, a fresh run may
report a different `passed` total and may transiently fail the report-asset tests
until the manifest is regenerated. The 10 failing ids above are stable.
