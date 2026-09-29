# Milestone 2 report: analysis pipeline and action executor

Scope: an end-to-end integration test of the deterministic phase-2 path and a
truthful account of what that path does and does not do. Authority: `docs/PRD.md`
(sections 6.3, 6.4, 10.1, 12.2, 13.1, 13.2, 13.3) and `docs/AGENT_BUILD_HANDOFF.md`.

Everything below was produced by running the code, not by reading it. Where a
claim rests on a test, the test is named. Where a behaviour was not exercised,
it is called out as unverified rather than described as working.

Note on file ownership: this report is a new file required by the build task, so
it was created under the otherwise read-only `docs/` directory. No pre-existing
document was edited. The tension with the "docs/** frozen" rule is recorded in
the task's structured notes.

## Commands run and observed results

| Command | Observed result |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -q tests/integration/test_end_to_end_review.py` | `4 passed` (exit 0) |
| `.venv/Scripts/python.exe -m pytest tests/integration/test_end_to_end_review.py -v` | `4 passed in 1.27s` |
| `.venv/Scripts/python.exe -m pytest` (full deterministic suite) | `680 passed, 11 skipped in 19.86s` (exit 0) |
| `.venv/Scripts/python.exe -m pytest --ignore=tests/integration/test_end_to_end_review.py` | `676 passed, 11 skipped` (exit 0) |
| `.venv/Scripts/python.exe -m pytest -m live --collect-only` | 11 tests collected in `tests/integration/test_openclaw_live.py` |

The environment reports Python 3.14.0 and pytest 8.4.2. The full suite is green
with and without the new file, so adding the end-to-end test broke nothing; the
new file contributes 4 passing tests (676 -> 680). The task's stated earlier
baseline of 219 passing tests does not match what the tree now reports (676
before this change); the discrepancy is reported rather than reconciled, because
the suite is green in the observed tree either way.

## 1. IMPLEMENTED and independently tested

All of the following are exercised by
`tests/integration/test_end_to_end_review.py` against real modules, a real
migrated SQLite database, and a real on-disk workspace provisioned by
`setup_instance`. Every test builds its instance from synthetic fixtures only
(`tests/fixtures/synth.py`); no real applicant data is used.

| Capability | Test | What it proves |
| --- | --- | --- |
| Workspace provisioning | `test_full_deterministic_review_journey` | `setup_instance` creates the instance, database, manifests, and reserved directories. |
| Discovery + extraction | same | 4 synthetic documents (3 TXT, 1 PDF) are discovered and extracted; `ScanSummary` reports `discovered=4 created=4 revisions_added=4 extractions=4`. |
| Two-level caching, zero-call rescan | same (step 7) | After one analysis, a rescan reports `revisions_added=0`, `analyses_enqueued=0`, `cache_hits=3`, `reused_assessments=3`, and the stub records `calls=0`. |
| Evidence validation and round trip | same (step 3) | Every committed `validation="verified"` quote, re-read from the database (including through a second `Database` connection), still occurs in the span it cites after `normalize_ws` on both sides. |
| `not_found` is neutral | same (step 3) | A `not_found` assessment persists as an evidence row with `result="not_found"` and is not converted to a negative finding. |
| Human decisions | same (step 4) | `set_decision` records reject, hold, and keep; the three dimensions remain separate. |
| Planner decision -> file behaviour | same (step 5) | Only the reject is planned (`MOVE_REJECTED`, destination `Rejected/<doc-id>/alpha.txt`); hold, keep, and unreviewed are skipped with reasons `no_op_hold`, `no_op_keep_active`, `no_op_unreviewed`. |
| Approval gate, plan-hash binding | same (steps 5-6) | `create_batch` + `create_file_operations` + `approve_batch` move the batch to `approved`; `apply_batch` runs only because the stored approval matches the plan hash. The refusal path itself (`APPROVAL_REQUIRED` / `APPROVAL_EXPIRED`) is covered by `tests/unit/test_executor.py` and `tests/unit/test_executor_adversarial.py`, not by this file. |
| No-clobber move, unchanged bytes | same (step 5) | The rejected file leaves the active path and appears at the planned destination with byte-identical content; the held/keep/unreviewed files do not move. |
| Five independent state dimensions | same (steps 5-6) | Before the move a row reads `decision=reject` with `location=active` and the file still at its active path; after the move the same row reads `location=rejected` with `current_rel_path` at the destination, `pending_intent=none`, batch `completed`, operation `committed`. Interface-visible state never claims the move happened before it did. |
| Partial-batch stop, no-clobber on collision | `test_a_destination_occupied_between_plan_and_apply_stops_the_batch` | A destination occupied between approval and apply stops the batch: state `partial`, code `BATCH_PARTIAL`, `moved=1`, `remaining=2`; operation 1 stays committed, operation 2 becomes `needs_reconciliation` with a reconciliation task, operation 3 stays `planned`, and the occupant's bytes are never overwritten. |
| Crash reconciliation (PRD 13.3 row 2) | `test_crash_between_move_and_commit_is_reconciled_not_repeated` | A crash injected between the filesystem move and the location commit leaves the file at the destination, the document `active`, and the operation `file_moved`. `plan_recovery(dry_run=True)` mutates nothing and reports `commit` for the interrupted operation and `resume` for the untouched one; `plan_recovery(dry_run=False)` commits the performed move (location -> `rejected`, operation -> `committed`) without a second move. Diagnosis evidence shows `namespace_owned`, `content_matches`, and `identity_matches` all true. |
| Idempotent replay after recovery | same | A replayed `apply_batch` reports the reconciled operation as `already_completed` and moves only the remaining one; the already-moved file is not moved again. |
| Adversarial: fabricated quote | `test_a_fabricated_quote_does_not_commit_an_assessment` | A model answer citing a quote that is in no span is rejected as non-repairable: status `manual_review`, one model call, no current profile, no evidence rows, document `manual_review` with a manual-review task, and no decision written. |

## 2. MOCKED or stubbed, and precisely what is therefore unverified

The single substituted piece is the **model client**. The deterministic suite
must not use a live route (PRD 6.4), so every test passes a local `StubAdapter`
that implements the pipeline's `AnalysisClient.complete` protocol, returns
scripted schema-shaped JSON, and counts calls. This is an explicit stub
(marked as such in the source, per PRD constraint 10).

Consequences of the substitution — these are NOT verified by these tests:

* The OpenClaw Chat Completions adapter (`resume_review.openclaw_adapter`) is
  never invoked. `AdapterCompletionClient`, the async adapter bridge, HTTP
  transport, authentication, the allowlisted agent target, route attestation
  enforcement against a real endpoint, timeouts, and fail-closed behaviour on
  route failure are all unexercised here.
* Real model output quality, prompt effectiveness, token accounting from a real
  route, and the one-repair-turn budget measured against a real model are
  unverified; only the deterministic validation/commit path downstream of the
  client is verified.
* The `RoutePolicy` is constructed as a fully attested `LOCAL_ONLY` policy with
  `restricted=True`. The gate `assert_inference_allowed()` was therefore always
  satisfied; the denial path (a missing attestation blocking inference) is not
  exercised here.

Not mocked, and real in these tests: bootstrap, discovery, sniffing, the TXT and
PDF extractors, the extraction cache, the database and its migrations, the
analysis validator, the planner, the executor, the no-clobber move primitive,
and reconciliation.

## 3. NOT IMPLEMENTED, or not established by this work

Nothing in the phase-2 path was found to be absent, and this report does not
claim otherwise. The honest list is of things this work did **not** establish.
"Not verified by these tests" is not the same as "not implemented"; the items
below are the former unless they are explicitly identified as live-only.

* Live OpenClaw integration: no route is configured in this environment, so no
  live behaviour was observed. See section 4.
* The HTTP API and session layer (`resume_review.api`, `resume_review.auth`):
  endpoints, role enforcement, CSRF, and Origin/Host checks are not exercised by
  these tests, which call the domain modules directly.
* Report rendering and publication (`Pipeline.publish_snapshot`,
  `resume_review.reporting`): not exercised by these tests.
* The product CLI (`python -m resume_review.cli`): not exercised.
* Only one clean PDF is exercised. Parser-failure and manual-review routing for
  damaged or unsupported files is covered by the existing ingest suite, not by
  this end-to-end file.
* No suitability score, no sensitive-trait inference, and no automatic rejection
  exist to test: the only file move in the exercised path is one a human
  explicitly approved.

## 4. UNTESTED against a live OpenClaw route

The live suite is marked and excluded by default. `.venv/Scripts/python.exe -m
pytest -m live --collect-only` collects 11 tests in
`tests/integration/test_openclaw_live.py`; all are skipped without a configured,
attested route. They cover, by name:

* endpoint activation, authentication, and the allowlisted agent target;
* authentication actually being enforced;
* the PRD 14.3 route-verification gate (`verify_route`);
* the analysis skill loading and the helper being authorized to invoke it;
* session separation between conversation users;
* tool denial for a tool-demanding request;
* timeout honouring with no fallback to another route;
* provider route recorded out of band and attached by the adapter;
* safe reporting of route failures with inference failing closed;
* incomplete attestation blocking inference on a live route;
* the route reporting its declared host class.

None of these were run. No claim in this report depends on any of them.

## 5. Reproducing

```bash
.venv/Scripts/python.exe -m pytest -q tests/integration/test_end_to_end_review.py
.venv/Scripts/python.exe -m pytest
```
