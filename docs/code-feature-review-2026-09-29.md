# RecruiterAgent code and feature review

Review date: 2026-09-29. This is a development review, not release acceptance.
No real applicant corpus or live inference route was used. The working directory
has no Git metadata, so findings describe the observed working tree rather than a
commit. Other build work may continue after this review.

## Direction

The overall architecture is reasonable. Keep the deterministic folder application,
local database, bounded analysis route, evidence validation, and exact human-approved
file plans. In particular, separating a review decision, a pending action, and actual
file location is the right choice. Operation-bound file identity is also a useful
improvement: a matching copy must not count as proof of a completed move.

I would change the build sequence now. The project has considerable module and unit
test coverage, but too many integration boundaries remain unfinished. Concentrate on
one installed workflow: setup twice, start one owner, authenticate, open the connected
page, scan, review, submit feedback, approve an exact plan, apply, recover, and restore.
Use the default application factory and real browser in that test. Tests that replace
the service factory, mutate runtime collaborators, or select only one route module
are useful unit checks but cannot prove the shipped workflow.

## Findings, in priority order

1. **High: file containment has a junction race.**
   `src/resume_review/actions/executor.py:805` validates paths before directory
   preparation and the eventual move at lines 825-838. The primitive in
   `storage/no_clobber.py:401` rejects reparse points on final nodes, but does not
   anchor all ancestor traversal. The safety reviewer reproduced swapping a
   validated destination parent for a junction: the move succeeded outside the
   workspace. Anchor moves to verified directory handles and add a regression at
   the actual operation boundary. Repeating a path-string check alone does not
   eliminate this race.

2. **High: stale assessments can appear current after source changes.**
   `db/repository.py:715` advances the document revision without staling profiles.
   Successful rescans do not mark the earlier profile stale, and
   `reporting/payload.py:258` accepts `is_current=1` without matching the source
   revision. Synthetic reproduction: document revision 2, profile revision 1,
   `profile_stale=False`, and `summary_stale=False`. The safety reviewer also
   reproduced a revision advance between the check in `analysis/pipeline.py:819`
   and insertion in `db/repository_analysis.py:438`. Check versions and commit the
   profile in one transaction; require matching versions on read as defense in depth.

3. **High: fresh setup prevents repeat setup.**
   `src/resume_review/__init__.py:31` declares schema version 1 while migration
   0002 is bundled and applied. `db/migrations.py:152` then treats that database as
   an unsupported downgrade. Reproduced by
   `tests/integration/test_setup.py::test_setup_twice_preserves_state_and_leaves_one_owner`
   and seven related setup/migration assertions. Update release/schema metadata
   coherently; do not weaken the downgrade protection.

4. **High: the normal helper lifecycle is incomplete.**
   `cli.py:797` constructs and serves an API without holding `InstanceLock`, creating
   a reviewer session, configuring OpenClaw chat, or mounting a connected HTML page.
   Setup briefly holds the lock and releases it. The API factory has no helper
   ownership lifespan. Shared deployment is also unfinished: the CLI binds loopback
   and does not persist its actual service address. These are integration blockers,
   even though API and session primitives exist.

5. **Medium: idempotency omits path parameters.**
   `api/idempotency.py:61` scopes requests by route name, actor and role; action
   handlers hash bodies without the target batch ID. Reusing an apply key for
   batch B can replay batch A's successful response while leaving B untouched.
   Include normalized resource identity in the idempotency scope or request hash.

6. **Medium: recovery can record a path known to be absent.**
   The source-only changed-content case correctly blocks at `actions/recovery.py:598`,
   but the mutation at line 955 assigns the destination path anyway. Keep the
   observed source path when only the source exists; represent ambiguity explicitly.

7. **Medium: the recheck override cannot execute.**
   `actions/planner.py:208` allows an explicit reconfirmation/override, but
   `actions/executor.py:634` still blocks the unchanged recheck flag. Persist the
   human reconfirmation as revisioned state or bind it into the approved operation.

8. **Medium: overlapping chat implementations and frontend contracts drifted.**
   `api/app.py:227` registers a core `/chat` bridge before the richer route in
   `api/chat.py`; normal `chat_adapter=` construction shadows queued chat support.
   The original browser client also sent fields forbidden by both request models,
   causing validation failures. The browser request mismatch is fixed in this work.
   Consolidate the server route and test the default factory rather than bypassing it.

9. **Release gaps and overstated documentation.**
   Backups currently copy SQLite only, despite the skill's coordinated-originals
   claim. Restore/relocation is unfinished. The skill directory is authored but not
   included in package data. README links to an absent acceptance report and shared
   operation runbook. Live tool-denial/privacy tests and the evidence-quality gate
   remain unverified. These must not be represented as completed integrations.

## Changes made during this review

- Added RecruiterAgent branding, a compact RA mark, and an OpenClaw skill descriptor
  in the shared dashboard and generated snapshots.
- Added the prominent folder-wide feedback entry. It submits immediately through
  authenticated `/chat`, independently of the selected rows. It does not queue a
  future analysis run or silently activate new assessment criteria.
- Aligned chat request fields with the actual API, bounded selection to 50 IDs,
  prevented duplicate concurrent submissions, retained failed drafts, and displayed
  response coverage and citations. Snapshot and viewer feedback are disabled.
- Fixed the connected sort defaults, chat toggle accessibility state, and feedback
  button hover contrast observed during browser checks.
- Kept packaged assets and their integrity manifest synchronized.
- Added a reusable standalone synthetic design preview generator. Its local demo
  acknowledgement is explicitly separate from a live OpenClaw response.

## Verification and limits

Focused command:

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_feedback_contract.py tests/unit/test_web_assets.py tests/unit/test_reporting.py tests/unit/test_packaging.py tests/unit/test_api_chat.py --tb=short
```

Result: **81 passed**. Recorded output: `review-artifacts/feedback-tests.txt`.
The new contract tests execute the JavaScript request builder and submit its output
through the authenticated default FastAPI factory with a clearly synthetic adapter.

Browser checks used a loopback-only fixture with no applicant data and no model:
reviewer feedback reached the synthetic adapter and opened chat; a forced 503 retained
the feedback text; a viewer session disabled input and submission. These checks prove
the UI/helper connection, not a live OpenClaw integration.

Full synthetic suite result: **1038 passed, 10 failed, 1 skipped, 15 deselected,
1 expected failure**. Output is saved in `review-artifacts/synthetic-suite.txt`.
Eight failures reflect the schema-version mismatch below; two reflect stale test
fixtures. This is a failing release gate, not an all-green acceptance result.
Two crash-recovery failures are outdated fixtures after operation identity was added:
`test_a_crash_after_the_move_is_reconciled_not_re_moved` creates a fresh destination
copy, which correctly fails identity matching; the supposed identity-free fixture in
`test_crash_reconciliation_without_identity_evidence_blocks_rather_than_guesses`
now has identity captured during planning. Update the scenarios rather than weakening
the identity requirement.

The safety findings above are targeted synthetic reproductions reported by the
independent reviewer. They need permanent regression tests and fixes before release.
No production-readiness claim is made.
