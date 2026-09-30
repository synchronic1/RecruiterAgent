# Acceptance report

The original sections below record an earlier working-tree snapshot. The
repository is now under Git; desktop companion evidence from the current
working tree is recorded separately in section 9. Historical counts and gaps
are retained rather than silently relabeled as current results.

Authority: `docs/PRD.md` (OpenClaw Resume Review PRD v1.0), section 20, which names this
file as a required deliverable, and `docs/AGENT_BUILD_HANDOFF.md`. The repository-root
`AGENTS.md` -- the build-time instruction file for coding agents, never loaded at runtime --
states the rule this document exists to satisfy.

**The rule this file follows.** From `AGENTS.md`, "Honesty rules":

> Do not claim a component works because its interface, prompt, or mock exists. Every claim
> in `docs/acceptance-report.md` cites a test ID and a recorded result. Unfinished controls
> stay labeled `NOT IMPLEMENTED`.

Two consequences shape everything below. First, a status records **whether a test
demonstrates the requirement**, not whether the feature is built: an interface, a prompt
template, a JSON schema, or a stub is not evidence that a behaviour works. Second, a status
is not upgraded because a test exists somewhere in the tree; the cited test has to exercise
the requirement that AT names.

Every node id in section 5 was resolved against a live collection of the suite
(`pytest --collect-only`), not transcribed from memory. Where no test asserts a
requirement, the status is `NOT COVERED`, and the Gap line says so rather than a citation
being invented to fill the cell.

---

## 1. Environment of record

| Item | Value |
| --- | --- |
| Python | 3.14.0 (repository `.venv`) |
| SQLite | 3.50.4 |
| Operating system | Windows 11 Pro, build 26200 (`Windows-11-10.0.26200-SP0`) |
| Database schema version | 2 (`SCHEMA_VERSION`) |
| Date of the recorded run | 2026-09-29 |
| Version control | None. This working tree is **not** a git repository, so the recorded revision cannot be pinned by commit hash. The result below is bound to the working tree as it stood on the date given. |

## 2. How to reproduce

```powershell
./.venv/Scripts/python.exe -m pytest -p no:cacheprovider --timeout=900
```

`pyproject.toml` sets `addopts = "-q --strict-markers"`; do not pass a second `-q`, which
would switch pytest into its per-file count summary. Add `-rs` to print skip reasons. The
registered markers are `live`, `slow`, and `corpus`.

The deterministic review path needs no inference and no credentials, so this command runs
without configuration. The two optional groups are described in section 3.

## 3. Recorded result

Run on 2026-09-29, command as in section 2 with `-rs`. Full output is recorded at
`docs/review-artifacts/acceptance-report-suite.txt`.

```text
1092 passed, 15 skipped, 425 warnings in 80.04s (0:01:20)
```

That line is quoted verbatim from the recorded file. pytest omits the `failed` count when
nothing fails, so its absence is expected rather than concealed; the command exited `0`,
which is what establishes that no test failed. The warning count is dominated by a single
Starlette deprecation notice about `httpx` under `TestClient`.

The fifteen skips are not silent gaps. Both groups are named here so the numbers cannot be
read as fuller coverage than they are:

| Skipped | Tests | Cause | Consequence for this report |
| --- | --- | --- | --- |
| 11 | `tests/integration/test_openclaw_live.py` | `pytestmark = pytest.mark.live`. All eleven skip when `RESUME_REVIEW_LIVE_*` is unset, reporting "no live OpenClaw route is configured ... to run the PRD 14.3 gate". | The PRD 14.3 live gate did not run. Every requirement that names live adapter evidence is `PARTIAL` at best, and AT-35 and AT-36 say so in their own Gap lines. |
| 4 | `tests/corpus/test_corpus_smoke.py` | `corpus` marker. Skipped unless `RESUME_REVIEW_REAL_CORPUS=1`; the default suite is synthetic-only. | Evidence drawn from real resume text (the semantic half of AT-11, and part of AT-06) was not exercised. **The real corpus was not run for this report.** |

A Node harness backs several UI claims about the report page. Its result is recorded at
`docs/review-artifacts/report-js-harness.txt`; the command was
`node tests/browser/check_report_js.mjs`, exit code `0`, and its final line is:

```text
54 passed, 0 failed
```

## 4. Status summary

| AT | Requirement | Status |
| --- | --- | --- |
| AT-01 | Repeat setup | **COVERED** |
| AT-02 | Safe collision | **COVERED** |
| AT-03 | Two independent jobs | **PARTIAL** |
| AT-04 | Asset integrity | **COVERED** |
| AT-05 | Upgrade preservation | **PARTIAL** |
| AT-06 | Complete census | **PARTIAL** |
| AT-07 | Incremental cache | **COVERED** |
| AT-08 | Copy in progress | **COVERED** |
| AT-09 | Duplicate handling | **NOT COVERED** |
| AT-10 | Unknown is not failure | **PARTIAL** |
| AT-11 | Evidence validation | **COVERED** |
| AT-12 | Date uncertainty | **NOT COVERED** |
| AT-13 | Human-state preservation | **PARTIAL** |
| AT-14 | Job restart | **COVERED** |
| AT-15 | Table controls | **PARTIAL** |
| AT-16 | Save versus move | **COVERED** |
| AT-17 | Bulk scope | **COVERED** |
| AT-18 | Conflict display | **COVERED** |
| AT-19 | Snapshot mode | **COVERED** |
| AT-20 | Snapshot interruption | **COVERED** |
| AT-21 | Chat interpretation | **PARTIAL** |
| AT-22 | Chat isolation | **COVERED** |
| AT-23 | Correct approval | **COVERED** |
| AT-24 | Replay protection | **COVERED** |
| AT-25 | Stale authorization | **PARTIAL** |
| AT-26 | Destination collision | **COVERED** |
| AT-27 | Escape and volume boundaries | **COVERED** |
| AT-28 | Crash points | **COVERED** |
| AT-29 | Partial execution | **COVERED** |
| AT-30 | Trash and restore | **COVERED** |
| AT-31 | Ambiguous recovery | **COVERED** |
| AT-32 | Host topology | **COVERED** |
| AT-33 | Authentication | **COVERED** |
| AT-34 | Injection containment | **COVERED** |
| AT-35 | Restricted OpenClaw | **PARTIAL** |
| AT-36 | Private-mode failure | **COVERED** |
| AT-37 | Owner locking | **COVERED** |
| AT-38 | Backup and relocation | **PARTIAL** |
| AT-39 | Fairness/accessibility checks | **PARTIAL** |
| AT-40 | Release evidence | **PARTIAL** |

Counts across the forty requirements: 26 COVERED, 12 PARTIAL, 2 NOT COVERED

No AT is graded `NOT IMPLEMENTED`. That is a statement about *requirement coverage*, not
about the product: several controls named elsewhere in the documentation are genuinely not
built, and they are labelled `NOT IMPLEMENTED` in section 7. The distinction matters. An AT
can be `COVERED` because tests demonstrate the requirement, while a related operator
convenience remains unbuilt; and an AT can be `NOT COVERED` because nothing tests it even
though the code path exists.

`NOT COVERED` means no test asserts the requirement. It does not mean the behaviour is
absent, and it does not mean the behaviour is present. It means this report has no evidence
either way, which for two requirements (AT-09, AT-12) is the honest answer.

### 4.1 The two PRD gates

`docs/PRD.md` groups these requirements into two gates. Reporting only the per-AT statuses
would hide the gate outcome, so both are stated here.

| Gate | PRD text | Outcome |
| --- | --- | --- |
| Milestone gate, `docs/PRD.md:838` | "AT-01 through AT-05 and applicable review/snapshot tests pass. The operator can review 400 rows without any model connection." | **Not met.** AT-03 and AT-05 are PARTIAL. AT-03 has no test that provisions two populated folders holding identically named documents and shows isolation in both directions. AT-05 has no test that upgrades a populated older-schema database after backup and shows decisions, notes, completed tasks, IDs, and history all survive. The "400 rows without a model connection" half is separately untested at that scale (section 6.3). |
| Fault-injection gate, `docs/PRD.md:844` | "AT-23 through AT-31 pass under injected faults. No testing on real recruiting folders until these gates pass." | **Not met.** AT-25 is PARTIAL: no test changes the registered root between planning and execution, so one of its six named dimensions is unexercised. The other eight, AT-23, AT-24, and AT-26 through AT-31, are COVERED. Read that with section 6.8: faults are injected by mutating planner inputs and the filesystem, never by killing a process at a chosen crash point. |

Neither gate is claimed as passed.

## 5. Evidence, AT-01 to AT-40

Requirement text is quoted from `docs/PRD.md`. Each entry names the tests, the status, and
the gap.

### AT-01 — Repeat setup
**Requirement.** Running setup again over a populated instance keeps IDs, human state, versions, and history, with a single owner.
**Evidence.** `tests/integration/test_setup.py::test_setup_twice_preserves_state_and_leaves_one_owner` (seeds human state, reruns setup, asserts same `instance_id`, `created is False`, `state_revision` 7 before and after, identical row counts, `preserved is True`, and the owner lock released then reacquirable); `tests/integration/test_setup.py::test_repeat_setup_does_not_rewrite_the_job_row_when_unchanged` (job id and `updated_at` unchanged by a repeat setup); `tests/integration/test_setup.py::test_instance_manifest_marker_and_ids_are_not_path_derived` (instance ids are opaque and stable across folders); `tests/unit/test_cli.py::test_repeat_setup_preserves_human_state` (a saved decision survives a second `setup`; confirmed not skipped on this host).
**Status.** COVERED
**Gap.** none identified

### AT-02 — Safe collision
**Requirement.** With unrelated `review.html`, `.review`, or `Rejected` entries already present, setup refuses destructive adoption and reports the conflict.
**Evidence.** `tests/integration/test_setup.py::test_unrelated_reserved_entry_causes_collision` (an unrelated reserved entry makes setup refuse); `tests/integration/test_setup.py::test_collision_report_names_the_conflict` (the refusal names the conflicting entry); `tests/integration/test_setup.py::test_collision_error_message_carries_no_absolute_path` (the report leaks no host path).
**Status.** COVERED
**Gap.** none identified

### AT-03 — Two independent jobs
**Requirement.** Two provisioned folders holding identically named files share no data, filters, conversations, or approvals.
**Evidence.** `tests/unit/test_api_core.py::test_wrong_instance_session_is_rejected` (a genuine session bound to another instance is refused on this instance); `tests/unit/test_api_adversarial.py::test_a_second_instance_cannot_share_a_database` (the `create_instance` invariant that one database holds exactly one instance is pinned); `tests/integration/test_setup.py::test_instance_manifest_marker_and_ids_are_not_path_derived` (two folders get distinct, non-path-derived instance ids).
**Status.** PARTIAL
**Gap.** No test provisions two populated instance folders with identically named documents and then asserts that documents, filters, chat conversations, and approvals do not cross. Isolation is demonstrated only in the reverse direction (a foreign-instance session is rejected) and by the single-instance-per-database invariant.

### AT-04 — Asset integrity
**Requirement.** Modifying a deployed executable or template is caught by manifest verification, blocking unapproved execution or reporting a controlled repair need.
**Evidence.** `tests/integration/test_setup.py::test_verify_manifest_passes_a_clean_bundle_and_reports_a_modified_one` (a changed bundle is reported); `tests/integration/test_setup.py::test_verify_manifest_reports_missing_and_extra_files`; `tests/integration/test_setup.py::test_setup_blocks_execution_when_a_deployed_template_is_modified` (a modified deployed template blocks execution); `tests/integration/test_setup.py::test_deploy_records_trusted_manifest_in_the_registry_first`; `tests/unit/test_web_assets.py::test_manifest_hashes_match_the_committed_bytes` and `::test_manifest_bundle_hash_follows_the_documented_rule` (committed asset hashes match the manifest).
**Status.** COVERED
**Gap.** none identified

### AT-05 — Upgrade preservation
**Requirement.** After backup, upgrading a previous schema keeps decisions, notes, completed tasks, IDs, and history; an unsupported downgrade is refused.
**Evidence.** `tests/unit/test_cli.py::test_setup_refuses_downgrade` (a database whose recorded migration version exceeds `SCHEMA_VERSION` makes setup refuse with `DOWNGRADE_REFUSED`); `tests/unit/test_migration_version.py::test_a_freshly_migrated_database_is_not_refused` (a current-version database is accepted); `tests/unit/test_migration_version.py::test_a_database_from_a_newer_build_is_still_refused`; `tests/unit/test_api_analysis.py::test_administrator_backup_creates_a_verified_copy` (a backup file is produced and its size verified).
**Status.** PARTIAL
**Gap.** The refusal half is demonstrated. No test takes a database at an older schema version seeded with decisions, notes, completed tasks, and history, applies the upgrade after a backup, and asserts each of those survives. `tests/unit/test_migration_version.py` exercises version arithmetic and refusal, not populated-state preservation across an upgrade.

### AT-06 — Complete census
**Requirement.** Scanning the 400-document fixture shows every expected submission, including unsupported or failed ones.
**Evidence.** `tests/unit/test_ingest_discovery.py::test_census_accounts_for_every_expected_submission` (discovered relative paths equal the fixture manifest exactly, every skip carries a reason, and `total_entries` balances); `tests/unit/test_ingest_discovery.py::test_exceeding_the_size_limit_is_flagged_not_dropped`; `tests/unit/test_pipeline.py::test_parser_failure_keeps_document_listed_and_stales_profile` (an unparsable document stays listed and gains a manual-review task); `tests/corpus/test_corpus_smoke.py::test_discovery_over_bounded_real_sample` (opt-in real-corpus discovery, skipped by default without the corpus).
**Status.** PARTIAL
**Gap.** The census assertions run against the standard synthetic fixture, which `tests/fixtures/synth.py::STANDARD_SPEC` defines at 18 documents; the 400-document census the requirement names is not exercised by any test on the default path. The failure-visibility half is demonstrated.

### AT-07 — Incremental cache
**Requirement.** Adding one document and rescanning twice analyzes only the new document once, unless a relevant version changes.
**Evidence.** `tests/unit/test_pipeline.py::test_scan_then_drain_commits_and_rescan_is_free` (a rescan after a completed drain performs no further work); `tests/unit/test_analysis_adversarial.py::test_no_op_rescan_makes_zero_model_calls` (a no-op rescan makes zero model calls); `tests/unit/test_ingest_extract.py::test_cache_hit_avoids_reparsing_and_miss_reparses` (an unchanged hash serves from cache, a changed hash reparses); `tests/unit/test_pipeline.py::test_superseded_result_never_becomes_current` and `tests/unit/test_analysis_adversarial.py::test_revision_change_supersedes_a_pending_result` (a changed revision invalidates the pending result).
**Status.** COVERED
**Gap.** none identified

### AT-08 — Copy in progress
**Requirement.** A file still growing during discovery is not parsed until its bytes stabilize, and no partial profile is committed as final.
**Evidence.** `tests/unit/test_ingest_discovery.py::test_file_growing_during_discovery_stays_pending` (a growing file stays `REASON_STILL_GROWING`); `tests/unit/test_ingest_discovery.py::test_source_changed_during_copy_is_refused` (a source altered mid-copy is refused with `REASON_SOURCE_CHANGED`); `tests/unit/test_ingest_discovery.py::test_stable_file_snapshots_hash_and_cleans_up`; `tests/unit/test_ingest_discovery.py::test_stabilize_missing_source_stays_pending`.
**Status.** COVERED
**Gap.** none identified

### AT-09 — Duplicate handling
**Requirement.** Two paths with identical bytes remain separate submissions, carry duplicate flags, and take independent decisions.
**Evidence.** No test exercises the requirement. `tests/unit/test_repository.py::test_every_repository_method_runs` calls `Repository.set_duplicate_flags`, and the generator defines the byte-identical pair (`tests/fixtures/synth.py`, `DUPLICATE_A_NAME`, `kind="txt_duplicate"`), but neither asserts the requirement.
**Status.** NOT COVERED
**Gap.** No test exercises the requirement. `Repository.set_duplicate_flags` is called from no production module (grep: defined at `src/resume_review/db/repository.py:600`, called only from a repository smoke test), so nothing detects identical bytes at two paths, and no test asserts two such paths remain separate submissions, carry the flag, or take independent decisions. The fixture pair is generated but no test consumes it.

### AT-10 — Unknown is not failure
**Requirement.** Missing credentials, scanned input, encrypted input, and parser errors become unknown/manual-review, never an automatic Reject.
**Evidence.** `tests/unit/test_ingest_sniff.py::test_encrypted_pdf_detected_without_decoding` and `::test_scan_only_pdf_detected_without_decoding`; `tests/unit/test_ingest_extract.py::test_pdf_scan_only_is_unsupported_manual_review`, `::test_pdf_encrypted_is_unsupported_manual_review`, `::test_pdf_corrupt_returns_failed_without_raising`, `::test_docx_broken_package_returns_failed_without_raising`, `::test_adapters_never_raise_on_garbage`; `tests/unit/test_pipeline.py::test_non_repairable_error_skips_repair_and_goes_to_manual_review` (state becomes `MANUAL_REVIEW` with a `manual_review` task); `tests/unit/test_pipeline.py::test_parser_failure_keeps_document_listed_and_stales_profile`; `tests/unit/test_queue.py::test_manual_review_is_terminal_not_retried`; `tests/unit/test_analysis_adversarial.py::test_not_found_never_sets_a_decision` (an unknown outcome never writes a decision).
**Status.** PARTIAL
**Gap.** The missing-credentials branch is not demonstrated. `tests/unit/test_openclaw_adapter.py` maps connection failure and auth refusal to `route_unavailable` and covers a missing secret file, but no test asserts that an unavailable or unauthenticated model route ends as unknown/manual-review rather than a rejection.

### AT-11 — Evidence validation
**Requirement.** Nonexistent source spans, fabricated quotations, invalid criteria, and wrong input revisions are rejected; semantic correctness is tested separately.
**Evidence.** `tests/unit/test_analysis_validate.py::test_span_not_present_fails_and_is_not_repairable`, `::test_quote_from_another_span_is_rejected`, `::test_reordered_quote_fails`, `::test_invented_criterion_id_fails_and_is_repairable`, `::test_invalid_result_value_fails_and_is_repairable`, `::test_superseded_source_revision_fails`, `::test_superseded_criteria_version_fails`, `::test_supported_with_no_evidence_fails_and_is_not_repairable`, `::test_dangling_evidence_reference_fails`; `tests/unit/test_analysis_adversarial.py::test_a_quote_assembled_from_two_non_adjacent_parts_fails`, `::test_a_homoglyph_quote_fails`, `::test_a_quote_that_matches_only_after_stripping_bidi_fails`; `tests/unit/test_pipeline.py::test_adapter_completion_client_maps_adapter_result`; `tests/integration/test_end_to_end_review.py::test_a_fabricated_quote_does_not_commit_an_assessment`.
**Status.** COVERED
**Gap.** Semantic correctness on real text is only covered by the opt-in corpus test `tests/corpus/test_corpus_smoke.py::test_anti_fabrication_validator_on_real_extracted_text`, which skips without a real corpus; that is the "separately" half and is skipped on the default path.

### AT-12 — Date uncertainty
**Requirement.** Overlapping roles and partial dates do not produce fabricated exact experience totals or inferred ages.
**Evidence.** No test asserts this. The only related code is the prompt line in `src/resume_review/openclaw_adapter/prompts.py` about overlapping roles, and the `clarify_date_overlap` problem code in `src/resume_review/schemas/analysis_result.schema.json`.
**Status.** NOT COVERED
**Gap.** The whole requirement. No test asserts that an overlapping-role or partial-date input yields `not_found`/unknown rather than a fabricated total, and no test asserts an age is never inferred. Grep for `experience_years`, `inferred age`, `overlapping`, `partial date` finds no assertion anywhere under `tests/`.

### AT-13 — Human-state preservation
**Requirement.** Regenerating profiles and changing criteria keeps notes, decisions, and completed tasks, while dependent assessments are marked stale.
**Evidence.** `tests/unit/test_repository.py::test_mark_profiles_stale_never_deletes` (superseded profiles are flagged, not deleted); `tests/unit/test_pipeline.py::test_parser_failure_keeps_document_listed_and_stales_profile` (the prior profile stays visible with `stale is True`); `tests/unit/test_analysis_adversarial.py::test_criteria_version_change_supersedes_a_pending_result`; `tests/unit/test_api_requisition.py::test_save_preserves_approved_criteria_and_human_decision` (saving the requisition leaves the active criteria version and the decision revision intact).
**Status.** PARTIAL
**Gap.** No test regenerates all profiles and asserts that notes and completed tasks survive. The `decision_needs_recheck` flag written by the scan path (`src/resume_review/analysis/pipeline.py:456`) is never asserted end-to-end: the only tests that set the flag call the repository directly (`tests/unit/test_repository.py`, `tests/unit/test_planner.py`), so "dependent assessments are correctly marked stale after a criteria or source change" is not demonstrated through the pipeline.

### AT-14 — Job restart
**Requirement.** Interrupting extraction or analysis lets a restart reclaim expired work without accepting superseded results or duplicating tasks.
**Evidence.** `tests/unit/test_queue.py::test_lease_survives_a_crash_and_is_reclaimable`, `::test_reexhausted_lease_becomes_terminal_not_stuck`, `::test_transient_failure_retries_at_most_three_times_with_backoff`; `tests/unit/test_repository.py::test_expired_lease_is_reaped_and_reclaimable`, `::test_claim_job_skips_a_live_lease`, `::test_upsert_task_is_idempotent_and_does_not_reopen`, `::test_enqueue_job_is_idempotent`; `tests/unit/test_analysis_adversarial.py::test_lease_is_reaped_and_reclaimable`, `::test_revision_change_supersedes_a_pending_result`, `::test_criteria_version_change_supersedes_a_pending_result`, `::test_no_op_rescan_makes_zero_model_calls`; `tests/unit/test_api_analysis.py::test_result_for_a_job_without_a_live_lease_is_refused` and `::test_result_for_a_superseded_revision_is_refused`.
**Status.** COVERED
**Gap.** none identified

### AT-15 — Table controls
**Requirement.** Sorting, filtering, and paginating a 400-record table keep totals, ordering, keyboard access, and selected IDs correct.
**Evidence.** `tests/unit/test_api_documents.py::test_pagination_totals_and_stable_order_across_pages` (stable order and correct totals across pages, no duplicates or drops), `::test_pagination_is_independent_of_page_size`, `::test_page_size_over_max_is_refused`, `::test_unknown_sort_key_is_refused`, `::test_explicit_document_ids_filter_is_exact`, `::test_review_state_filter_reflects_decisions`; `tests/unit/test_web_assets.py::test_report_js_parses_under_node` and `::test_pure_logic_harness_passes` (54 harness checks); harness checks `pagination pages 50 rows by default and reports a visible total`, `default sort is ingested_at ascending with a document_id tie-breaker`, `unknown values sort last in both directions`, `buildHeaderHtml renders the eight contract columns in order` (asserts `aria-sort` on each sortable column); `tests/unit/test_web_assets.py::test_css_never_conveys_state_by_colour_alone` (asserts `:focus-visible` is defined).
**Status.** PARTIAL
**Gap.** Two parts are not demonstrated. No test drives keyboard interaction (no `keydown`, `focus()`, or tab-order assertion exists in `tests/`; only `aria-sort` attributes and a `:focus-visible` CSS rule are asserted). And no test sorts, filters, or paginates at the 400-record scale the requirement names; the API tests use small synthetic sets and the harness uses 120 rows.

### AT-16 — Save versus move
**Requirement.** Saving Reject and then refreshing, sorting, or chatting moves no original; only a successful approval workflow moves a file.
**Evidence.** `tests/unit/test_bulk_ui_contract.py::test_bulk_checkbox_selection_builds_atomic_reviewer_decisions_without_file_moves` and `::test_single_decision_builder_uses_the_patch_contract_without_a_file_move` (the UI body carries decisions only, no move); `tests/unit/test_api_actions.py::test_apply_before_approve_is_refused_and_moves_nothing`, `::test_action_intent_saves_and_cancels_without_moving`, `::test_restore_plan_mints_an_inverse_plan_without_moving`; `tests/unit/test_api_chat.py` (chat proposes but never moves, approves, or records intent); `tests/unit/test_chat.py` (the filesystem snapshot is byte-identical after a chat proposal); `tests/unit/test_connected_ui_contract.py::test_connected_refresh_uses_documents_pages_and_rejects_a_changed_revision`; harness checks `a pending intent is reported without claiming a move` and `a rejection-folder move is not shown as completed before it happens`; `tests/integration/test_end_to_end_review.py::test_full_deterministic_review_journey` (a held file does not move until the approved batch).
**Status.** COVERED
**Gap.** none identified

### AT-17 — Bulk scope
**Requirement.** Selecting a page and selecting all matching results are separate, and only the confirmed immutable set is affected even if new files arrive.
**Evidence.** `tests/unit/test_api_documents.py::test_bulk_decisions_affect_only_the_supplied_set`, `::test_bulk_decisions_are_atomic_on_one_stale_revision`, `::test_bulk_decisions_replay_is_idempotent`; harness checks `select this page unions the rendered rows`, `select all matching results resolves to an immutable set at confirmation time`, `the resolved set carries the decision revision captured at confirmation`, `toggling a row adds and removes it, and never merges with a frozen set`, `a frozen selection says so in its note`, `hidden-selected counts rows selected while not matching the filter`; enforced as present by `tests/unit/test_web_assets.py::test_harness_covers_the_required_behaviours`.
**Status.** COVERED
**Gap.** none identified

### AT-18 — Conflict display
**Requirement.** When two reviewers write against the same decision revision, one succeeds and the stale write receives a visible conflict without overwriting.
**Evidence.** `tests/unit/test_api_documents.py::test_stale_decision_write_is_refused_and_not_overwritten`; `tests/unit/test_repository.py::test_set_decision_rejects_stale_revision_and_applies_nothing`; harness check `a conflict shows the current value and its actor and does not auto-retry` and `a saved row reports its revision`; `tests/unit/test_api_analysis.py::test_activation_requires_the_current_expected_revision`.
**Status.** COVERED
**Gap.** none identified

### AT-19 — Snapshot mode
**Requirement.** Opening `review.html` with the helper stopped gives working summaries and in-memory filters, visible timestamps, and no persistent edits or chat.
**Evidence.** `tests/unit/test_reporting.py::test_mode_is_forced_to_snapshot`, `::test_rendered_document_makes_no_external_request`, `::test_snapshot_header_shows_timestamp_revision_and_credential_free_link` (the header exposes the timestamp and revision); `tests/unit/test_web_assets.py::test_snapshot_boot_makes_no_network_call`, `::test_every_request_goes_through_the_guarded_entry_point`, `::test_a_file_origin_resolves_to_snapshot`; harness checks `the snapshot path makes no network call`, `a file: origin is snapshot mode and can never reach the network`, `connected mode resolves its instance id from the bootstrap or the URL`, `the toolbar filter narrows by location, state and task`, `search covers name, filename, path and summary`.
**Status.** COVERED
**Gap.** none identified

### AT-20 — Snapshot interruption
**Requirement.** An interrupted publish or a temporarily locked report leaves the prior valid snapshot in place and visibly reports staleness.
**Evidence.** `tests/unit/test_reporting.py::test_blocked_replacement_keeps_prior_report_and_reports_staleness` (a blocked replacement keeps the prior report and reports staleness), `::test_failed_first_publish_is_not_reported_as_stale` (a failed first publish is not mislabelled stale), `::test_temporary_filename_never_contains_applicant_content`, `::test_write_snapshot_publishes_and_reports_metadata`.
**Status.** COVERED
**Gap.** none identified

### AT-21 — Chat interpretation
**Requirement.** Natural-language filters yield validated criteria with unknown values shown, and a filter undo restores the previous view without touching decisions.
**Evidence.**
- `tests/unit/test_api_chat.py::test_criteria_proposal_is_recorded_but_never_activated` — a chat-proposed criterion is recorded but cannot become active on its own.
- `tests/unit/test_chat.py::test_invalid_criteria_proposal_is_rejected_and_warned` — a malformed proposal is rejected and surfaced.
- `tests/unit/test_filters.py::test_default_policy_is_include_with_warning` — the default unknown policy is include-with-warning.
- `tests/unit/test_filters.py::test_exclude_policy_drops_the_unknown_branch_but_keeps_the_omitted_count` — unknowns are visibly counted, not silently dropped.
- `tests/unit/test_filters.py::test_saved_filter_referring_to_a_removed_criterion_is_visibly_invalid` — a saved filter that no longer resolves is reported invalid with a reason code.
**Status.** PARTIAL
**Gap.** No test demonstrates filter undo. Nothing asserts that undoing a filter restores the
previous view, and nothing asserts that a filter change leaves decisions unchanged. No test
drives a chat-produced filter into an applied view end to end.

### AT-22 — Chat isolation
**Requirement.** Concurrent reviewers/jobs never share unintended session context, and an all-folder answer discloses incomplete coverage.
**Evidence.**
- `tests/unit/test_chat.py::test_cross_reviewer_isolation_no_history_leak` — one reviewer's history is not visible to another.
- `tests/unit/test_chat.py::test_scope_document_from_another_instance_is_refused` — an out-of-instance scope document is refused.
- `tests/unit/test_chat.py::test_conversation_id_is_opaque_and_exchange_is_persisted` — conversation ids are opaque and the exchange is server-bound.
- `tests/unit/test_chat.py::test_provider_visible_identifiers_are_opaque` — identifiers the provider sees carry no internal ids.
- `tests/unit/test_chat.py::test_prior_turns_replayed_are_the_newest_not_the_oldest` — the history window is bounded and deterministic.
- `tests/unit/test_chat.py::test_all_folder_question_states_partial_coverage_when_capped` — an all-folder answer reports partial coverage when capped.
- `tests/unit/test_api_chat.py::test_conversation_id_bound_to_another_thread_is_refused` — a conversation id bound elsewhere is refused.
- `tests/unit/test_api_chat.py::test_partial_coverage_is_disclosed_over_http` — partial coverage is disclosed in the HTTP response.
- `tests/unit/test_api_chat.py::test_unknown_scope_document_is_refused` — an unknown scope document is refused.
**Status.** COVERED
**Gap.** none identified.

### AT-23 — Correct approval
**Requirement.** An authenticated reviewer approves a concrete plan and only those operations become executable; model text cannot create approval.
**Evidence.**
- `tests/unit/test_api_actions.py::test_apply_before_approve_is_refused_and_moves_nothing` — apply without approval is refused and no file moves.
- `tests/unit/test_api_actions.py::test_approval_requires_the_exact_plan_hash` — approval binds to the exact plan hash.
- `tests/unit/test_api_actions.py::test_a_non_human_principal_cannot_approve` — a non-human principal cannot approve.
- `tests/unit/test_api_actions.py::test_a_mutation_without_csrf_is_refused` — approval requires CSRF.
- `tests/unit/test_executor.py::test_plan_hash_mismatch_is_refused` — the executor refuses a mismatched plan hash.
- `tests/unit/test_executor.py::test_expired_approval_moves_nothing` — an expired approval moves nothing.
- `tests/unit/test_executor.py::test_a_viewer_principal_is_refused_by_role` and `::test_a_plain_actor_string_is_not_accepted` — role and actor identity come from authentication, not text.
- `tests/unit/test_api_adversarial.py::test_a_body_supplied_actor_is_refused_not_honoured` — a caller-supplied actor field is refused.
- `tests/unit/test_api_adversarial.py::test_approved_apply_actually_moves_the_file_and_leaks_no_path` — an approved apply does move the file, so the gate is not vacuous.
- `tests/unit/test_chat.py::test_action_request_creates_a_plan_proposal_and_moves_nothing` and `::test_action_detector_reads_the_question_not_the_model` — a chat action request only proposes; the detector reads the question, not model output.
**Status.** COVERED
**Gap.** none identified.

### AT-24 — Replay protection
**Requirement.** Replaying decision, plan, and apply with the same idempotency key causes no duplicate side effect, and a changed payload with a reused key conflicts.
**Evidence.**
- `tests/unit/test_api_actions.py::test_a_repeated_plan_request_replays_without_a_second_batch` — a repeated plan request creates no second batch.
- `tests/unit/test_api_actions.py::test_reusing_an_idempotency_key_with_a_different_payload_conflicts` — key reuse with a different payload conflicts.
- `tests/unit/test_api_actions.py::test_a_post_without_an_idempotency_key_is_refused` — a key is mandatory.
- `tests/unit/test_api_adversarial.py::test_plan_replay_does_not_create_a_second_batch` and `::test_scan_replay_does_not_repeat_the_side_effect` — plan and scan replays are side-effect free.
- `tests/unit/test_api_adversarial.py::test_same_key_with_a_different_payload_conflicts` — payload mismatch conflicts.
- `tests/unit/test_api_adversarial.py::test_idempotency_key_scoped_to_a_principal_is_not_usable_by_another` and `::test_idempotency_key_is_scoped_to_the_route` — keys are scoped to principal and route.
- `tests/unit/test_executor.py::test_replaying_an_apply_repeats_nothing` — a replayed apply repeats nothing at the executor.
- `tests/unit/test_executor_adversarial.py::test_apply_twice_is_a_no_op_the_second_time` — a second apply is a no-op.
**Status.** COVERED
**Gap.** none identified.

### AT-25 — Stale authorization
**Requirement.** Changing source bytes, decision, location, intent, criteria, or root after planning causes the stale action to be rejected.
**Evidence.**
- `tests/unit/test_api_actions.py::test_stale_authorization_is_rejected_and_moves_nothing` — a stale authorization is rejected at the API and moves nothing.
- `tests/unit/test_api_actions.py::test_a_plan_mutated_after_approval_is_refused_and_moves_nothing` — a mutated plan is refused.
- `tests/unit/test_executor.py::test_a_stale_source_blocks_the_whole_plan_and_moves_nothing` — a stale source blocks the entire plan.
- `tests/unit/test_executor.py::test_a_decision_revision_change_blocks` and `::test_a_changed_criteria_version_blocks` — decision revision and criteria version changes block.
- `tests/unit/test_executor.py::test_a_missing_source_at_execution_time_blocks` — a missing source blocks.
- `tests/unit/test_executor_adversarial.py::test_a_source_whose_bytes_changed_is_not_moved` — changed source bytes block the move.
- `tests/unit/test_executor_adversarial.py::test_flipped_decision_between_plan_and_apply_blocks`, `::test_bumped_decision_revision_blocks`, `::test_bumped_location_version_blocks`, `::test_changed_criteria_version_blocks`, `::test_deleted_source_blocks` — each precondition dimension blocks independently.
**Status.** PARTIAL
**Gap.** No test changes the registered root between planning and execution.
`tests/unit/test_api_actions.py::test_the_registered_root_is_authoritative_over_the_database_location`
asserts root authority in general but does not exercise a root change after planning, so the
"or root" clause is not demonstrated.

### AT-26 — Destination collision
**Requirement.** A destination created after planning is never overwritten, remaining work stops, and a revised plan is required.
**Evidence.**
- `tests/unit/test_planner.py::test_destination_collision_is_reported_not_planned_over` — the planner reports a collision instead of planning over it.
- `tests/unit/test_executor_adversarial.py::test_preregistered_destination_is_never_overwritten` — a destination created after planning is not overwritten.
- `tests/unit/test_executor_adversarial.py::test_a_directory_at_the_destination_is_not_replaced` — a directory at the destination is not replaced.
- `tests/unit/test_executor_adversarial.py::test_collision_on_the_second_operation_stops_after_the_first` — the batch stops at the collision.
- `tests/integration/test_apply_batch.py::test_a_mid_execution_collision_stops_the_batch_and_leaves_earlier_work` — a mid-execution collision stops the batch and preserves earlier work.
- `tests/unit/test_executor.py::test_a_concurrent_writer_is_detected_not_overwritten` — a concurrent writer is detected, not overwritten.
- `tests/unit/test_restore.py::test_occupied_destination_is_reported_not_overwritten` — the same holds for a restore destination.
**Status.** COVERED
**Gap.** none identified.

### AT-27 — Escape and volume boundaries
**Requirement.** Traversal, symlinks, junctions, malicious filenames, and cross-volume destinations cannot escape or trigger a copy-and-delete fallback.
**Evidence.**
- `tests/unit/test_executor_adversarial.py::test_a_destination_with_a_parent_traversal_is_refused` — a parent-traversal destination is refused.
- `tests/unit/test_executor_adversarial.py::test_an_absolute_destination_is_refused` — an absolute destination is refused.
- `tests/unit/test_executor_adversarial.py::test_a_link_planted_between_planning_and_execution_is_refused` — a directory link planted after planning is refused.
- `tests/unit/test_executor_adversarial.py::test_a_link_at_the_final_destination_component_is_refused` — a link at the final destination component is refused.
- `tests/unit/test_executor_adversarial.py::test_a_raising_kernel_primitive_does_not_degrade_to_a_copy` and `::test_a_failed_move_result_blocks_without_a_copy` — a failed move blocks and never degrades to copy-and-delete.
- `tests/unit/test_executor_adversarial.py::test_the_move_primitive_has_no_copy_or_read_write_move` — the primitive contains no copy path.
- `tests/unit/test_executor.py::test_a_cross_volume_move_blocks_and_does_not_degrade` — a cross-volume destination blocks rather than falling back.
- `tests/unit/test_executor.py::test_an_unsupported_move_blocks` and `::test_the_executor_contains_no_overwriting_or_shell_move` — unsupported moves block; no overwrite or shell move exists.
- `tests/unit/test_planner.py::test_adversarial_filename_cannot_escape_the_root` — a hostile filename stays inside the root.
- `tests/unit/test_recovery_adversarial.py::test_crafted_document_id_that_escapes_is_refused`, `::test_adversarial_original_filename_stays_inside_the_root`, `::test_crafted_previous_recorded_path_cannot_escape` — recovery-side crafted ids, filenames, and recorded paths stay contained.
**Status.** COVERED
**Gap.** The junction/symlink denial is host-dependent. `tests/unit/test_executor_adversarial.py`
skips via `make_dir_link` ("this host cannot create a directory link") and
`tests/unit/test_ingest_discovery.py` skips when the host cannot create a symlink or junction.
Neither skipped in the recorded run (the 15 skips are 4 opt-in corpus tests and 11 live tests), so
they did execute on this Windows host; on a host without link support the reparse-point clause is
unproven, not just unrun.

### AT-28 — Crash points
**Requirement.** Crashes before intent persistence, after intent, after move, and before database commit never lose a file or falsely report success.
**Evidence.**
- `tests/unit/test_journal.py::test_intent_is_durable_before_any_move_step` — intent is durable before any move step.
- `tests/unit/test_journal.py::test_write_is_atomic_temp_fsync_then_replace` — journal writes are atomic via temp+fsync+replace.
- `tests/unit/test_journal.py::test_failed_replace_leaves_the_previous_journal_intact` — a failed replace leaves the previous journal intact.
- `tests/unit/test_journal.py::test_journal_is_loadable_after_a_restart` and `::test_corrupt_journal_is_reported_not_raised` — the journal survives restart; corruption is reported, not raised.
- `tests/unit/test_executor_adversarial.py::test_intent_is_durable_on_disk_before_the_move_is_attempted` — durable intent precedes the move attempt.
- `tests/unit/test_executor_adversarial.py::test_crash_after_move_before_commit_is_reconciled_not_repeated` — a crash after the move and before commit is reconciled, not re-moved.
- `tests/unit/test_executor_adversarial.py::test_crash_reconciliation_without_identity_evidence_blocks_rather_than_guesses` — without identity evidence reconciliation blocks rather than guessing.
- `tests/unit/test_executor_adversarial.py::test_a_foreign_file_at_the_destination_with_the_source_gone_blocks` — a foreign destination with the source gone blocks.
- `tests/unit/test_recovery_adversarial.py::test_intent_is_on_disk_before_the_executor_can_touch_the_file` and `::test_corrupt_or_truncated_journal_is_reported` — durability and corruption reporting.
- `tests/unit/test_recovery.py::test_source_absent_verified_destination_commits` — a verified destination is committed rather than duplicated.
**Status.** COVERED
**Gap.** The "before intent persistence" point is demonstrated as a failed/atomic journal write
leaving the prior journal intact, not as a process kill before any intent is written. There is no
test that kills the process in that window; the atomic-replace property is the evidence offered.

### AT-29 — Partial execution
**Requirement.** A batch that fails at the third operation records completed work, stops the remainder, and reports partial completion accurately.
**Evidence.**
- `tests/unit/test_executor_adversarial.py::test_collision_on_the_second_operation_stops_after_the_first` — a collision stops the batch after the first operation.
- `tests/unit/test_executor_adversarial.py::test_a_later_operations_missing_source_stops_there_leaving_earlier_work` — a later operation's missing source stops there, leaving earlier work.
- `tests/unit/test_executor_adversarial.py::test_a_later_operations_stale_decision_blocks_the_whole_batch` — a later stale decision blocks the whole batch.
- `tests/integration/test_apply_batch.py::test_a_mid_execution_collision_stops_the_batch_and_leaves_earlier_work` — the same at integration level.
- `tests/unit/test_api_actions.py::test_cancel_stops_unstarted_work_and_apply_then_refuses` — cancel stops unstarted work and a later apply refuses.
- `tests/unit/test_connected_operations.py::test_apply_retains_report_restore_is_fresh_and_partial_evidence_survives` — the client renders `execution_state == "partial"` with counts `{"moved": 1, "blocked": 1}`.
**Status.** COVERED
**Gap.** The failure is injected at the first and second operations; no test injects a failure at
a third operation specifically. The partial-completion reporting assertion is made against the
Node harness with synthetic responses, not against a real partial batch over HTTP.

### AT-30 — Trash and restore
**Requirement.** Trashing a kept and a rejected submission restores each to its recorded location and earlier decision, and conflicts block rather than overwrite.
**Evidence.**
- `tests/unit/test_planner.py::test_move_trash_uses_batch_scoped_destination` — trash uses a batch-scoped destination.
- `tests/unit/test_planner.py::test_restore_previous_uses_recorded_source_path` — restore targets the recorded source path.
- `tests/unit/test_restore.py::test_restore_from_trash_uses_previous_path_and_preserves_decision` — restoring from trash returns to the previous path and preserves the decision.
- `tests/unit/test_restore.py::test_restore_active_from_rejected_returns_to_recorded_active_path` — a rejected-then-restored document returns to its recorded active path.
- `tests/unit/test_restore.py::test_restore_previous_may_legitimately_return_to_rejected` — the recorded decision dimension is honoured.
- `tests/unit/test_restore.py::test_occupied_destination_is_reported_not_overwritten` — a restore destination conflict is reported, not overwritten.
- `tests/unit/test_recovery_adversarial.py::test_prd_13_3_table_row_source_absent_trash_destination_verified` and `::test_restore_will_not_plan_over_an_occupied_old_path` — recovery-side restore conflict blocking.
**Status.** COVERED
**Gap.** none identified.

### AT-31 — Ambiguous recovery
**Requirement.** When both source and destination exist, or neither does, recovery demands reconciliation and never silently deletes a duplicate.
**Evidence.**
- `tests/unit/test_recovery.py::test_both_exist_stops_for_reconciliation_and_deletes_nothing` — both-present stops for reconciliation and deletes nothing.
- `tests/unit/test_recovery.py::test_neither_exists_marks_missing_and_requests_human` — neither-present marks missing and requests a human.
- `tests/unit/test_recovery_adversarial.py::test_both_present_repair_leaves_both_files_byte_identical` — repair leaves both files byte-identical.
- `tests/unit/test_recovery_adversarial.py::test_both_present_same_inode_still_stops_and_deletes_nothing` — even a same-inode pair stops.
- `tests/unit/test_recovery_adversarial.py::test_repair_never_removes_the_source_as_a_tidy_up` — repair never removes the source.
- `tests/unit/test_recovery_adversarial.py::test_prd_13_3_table_row_both_present` and `::test_prd_13_3_table_row_neither_present` — both ambiguity rows of the PRD table are exercised.
- `tests/unit/test_recovery_adversarial.py::test_no_module_contains_a_deletion_or_document_move_path` — no deletion path exists in the recovery modules.
- `tests/unit/test_journal.py::test_reconcile_reports_disagreement_with_database_as_authoritative` — journal/database disagreement is reported.
**Status.** COVERED
**Gap.** none identified.

### AT-32 — Host topology
**Requirement.** Local-disk and storage-host-local shared modes pass, and a remote-mounted live SQLite database is rejected by setup.
**Evidence.**
- `tests/integration/test_setup.py::test_setup_refuses_a_network_topology_path` — a network topology path is refused by setup.
- `tests/integration/test_setup.py::test_unknown_topology_needs_explicit_confirmation` — an unknown topology is not silently accepted.
- `tests/integration/test_setup.py::test_setup_rejects_a_relative_root` — a relative root is rejected.
- `tests/integration/test_setup.py::test_setup_provisions_the_documented_layout` and `::test_instance_manifest_marker_and_ids_are_not_path_derived` — the supported provisioning path succeeds.
**Status.** COVERED
**Gap.** The rejection of a remote-mounted live database is well covered. No test asserts a
storage-host-local *shared* topology passing as a distinct configuration; only the generic
provisioning tests stand in for the two positive modes.

### AT-33 — Authentication
**Requirement.** Unauthenticated, wrong-role, wrong-instance, forged-Origin, and CSRF attempts fail, and shared identity comes from authentication rather than request text.
**Evidence.**
- `tests/security/test_auth.py::test_wrong_password_unknown_user_and_tampered_session_fail` — credential and session failures.
- `tests/security/test_auth.py::test_session_for_one_instance_is_refused_for_another` — cross-instance session refusal.
- `tests/security/test_auth.py::test_forged_origin_is_refused`, `::test_unexpected_host_is_refused`, `::test_csrf_missing_and_wrong_tokens_fail` — Origin, Host, and CSRF checks.
- `tests/security/test_auth.py::test_viewer_cannot_review_and_reviewer_cannot_administer` — role separation.
- `tests/unit/test_api_adversarial.py::test_unauthenticated_read_is_rejected`, `::test_unauthenticated_mutation_is_rejected`, `::test_no_route_answers_an_unauthenticated_caller_with_success` — no route succeeds unauthenticated.
- `tests/unit/test_api_adversarial.py::test_session_bound_to_another_instance_is_refused`, `::test_forged_origin_is_rejected_on_a_mutation`, `::test_forged_host_is_rejected`, `::test_wrong_csrf_token_is_rejected` — the same at the HTTP surface.
- `tests/unit/test_api_adversarial.py::test_a_body_supplied_actor_is_refused_not_honoured` — identity cannot be supplied in request text.
**Status.** COVERED
**Gap.** none identified.

### AT-34 — Injection containment
**Requirement.** Resume instructions, HTML payloads, rogue `AGENTS.md`, and model-generated commands cannot execute code, read secrets, or mutate other records.
**Evidence.**
- `tests/security/test_filter_injection.py::test_injection_through_a_text_value_is_bound_never_interpolated`, `::test_injection_through_a_contains_value_has_its_wildcards_escaped`, `::test_injection_through_an_in_list_value_is_bound`, `::test_injection_that_would_drop_a_table_leaves_the_schema_intact` — filter values are bound, never interpolated.
- `tests/security/test_filter_injection.py::test_executable_keys_on_a_node_are_refused_not_ignored` — executable keys are refused.
- `tests/security/test_filter_adversarial.py::test_database_integrity_survives_the_injection_battery` — the database survives an injection battery.
- `tests/unit/test_ingest_discovery.py::test_instruction_looking_files_are_data_not_configuration` — a rogue `AGENTS.md`/`SKILL.md` is flagged as data and never loaded as configuration.
- `tests/unit/test_web_assets.py::test_no_raw_markup_sink_can_receive_payload_data` and `::test_applicant_text_is_written_with_text_content` — no raw markup sink; applicant text is written as text.
- `tests/unit/test_web_assets.py::test_shell_has_no_inline_executable_script` and `::test_document_links_are_restricted_to_relative_references` — inline scripts absent; links restricted.
- `tests/unit/test_openclaw_adapter.py::test_applicant_text_is_framed_as_untrusted_data`, `::test_envelope_cannot_be_closed_by_the_document`, `::test_chat_template_literals_cannot_forge_a_role_boundary` — applicant text cannot close the untrusted envelope or forge a role boundary.
- `tests/unit/test_openclaw_adapter.py::test_tool_call_from_the_route_is_refused` and `::test_secret_never_appears_in_repr_describe_logs_or_errors` — model-generated tool calls are refused and secrets never leak into logs or errors.
- `tests/unit/test_api_actions.py::test_no_endpoint_accepts_a_caller_supplied_destination` and `tests/unit/test_api_adversarial.py::test_no_endpoint_executes_a_caller_path_or_command` — no endpoint accepts a caller path or command.
**Status.** COVERED
**Gap.** none identified.

### AT-35 — Restricted OpenClaw
**Requirement.** Live adapter tests prove dangerous tools are denied and browser requests cannot choose arbitrary agents, credentials, tools, or model overrides.
**Evidence.**
- `tests/unit/test_openclaw_adapter.py::test_tool_call_from_the_route_is_refused` — a tool call returned by the route is refused.
- `tests/unit/test_openclaw_adapter.py::test_agent_id_injection_attempts_are_refused` — injected agent ids are refused.
- `tests/unit/test_openclaw_adapter.py::test_analysis_request_has_no_field_that_could_carry_a_model_or_agent` — the request has no model/agent field at all.
- `tests/unit/test_openclaw_adapter.py::test_forwards_the_allowlisted_agent_target_and_nothing_else` — only the allowlisted agent target is forwarded.
- `tests/unit/test_openclaw_adapter.py::test_endpoint_path_from_config_is_never_forwarded` and `::test_base_url_credentials_and_public_ingress_are_refused` — endpoint path and credentials cannot be chosen by a request.
- `tests/integration/test_openclaw_live.py::test_tool_denial_holds_for_a_tool_demanding_request` (live-marked, skipped by default) — the only test that proves tool denial against the real endpoint.
- `tests/integration/test_openclaw_live.py::test_endpoint_is_activated_authenticated_and_targets_the_allowlisted_agent` (live-marked, skipped by default) — live agent-target confirmation.
**Status.** PARTIAL
**Gap.** The requirement names *live* adapter tests, and the live suite
(`tests/integration/test_openclaw_live.py`, 11 tests, `pytestmark = pytest.mark.live`, fixture skips
without `RESUME_REVIEW_LIVE_*` env) accounts for 11 of the 15 skips in the recorded run. The
default-run evidence is unit tests over a recording stub. Dangerous-tool denial and the
allowlisted-agent target are therefore asserted against a stub, not proven live.

### AT-36 — Private-mode failure
**Requirement.** With the approved local model unavailable, processing stops with no external fallback and no candidate-data egress.
**Evidence.**
- `tests/unit/test_openclaw_adapter.py::test_local_only_never_attempts_a_second_route_after_a_route_failure` — after a local-route failure no second route is attempted.
- `tests/unit/test_openclaw_adapter.py::test_local_only_blocks_fallback_on_timeout_too` — the same under timeout.
- `tests/unit/test_openclaw_adapter.py::test_local_only_content_failure_keeps_its_own_code` — a content failure is not reclassified into a fallback.
- `tests/unit/test_openclaw_adapter.py::test_local_only_route_requires_a_loopback_endpoint` and `::test_policy_refuses_unrestricted_routes` — a non-loopback or unrestricted route is refused.
- `tests/unit/test_chat.py::test_unavailable_route_fails_closed_with_exactly_one_attempt` — exactly one attempt is made and the turn fails closed.
- `tests/unit/test_chat.py::test_adapter_failure_persists_nothing_and_does_not_fall_back` — nothing is persisted and no fallback occurs.
- `tests/unit/test_api_chat.py::test_direct_turn_fails_closed_before_any_write` and `::test_adapter_failure_fails_the_question_and_persists_no_exchange` — the HTTP path fails closed before any write.
**Status.** COVERED
**Gap.** Egress containment is asserted by observing that the recorder saw no second route, i.e.
against a stub. The live-route confirmation
(`tests/integration/test_openclaw_live.py::test_route_failures_are_reported_safely_and_inference_fails_closed`
and `::test_timeout_is_honoured_and_never_falls_back_to_another_route`) is live-marked and not run
by default, so no packet-level or real-endpoint proof of no egress is presented.

### AT-37 — Owner locking
**Requirement.** A duplicate helper or second host cannot acquire the same live instance, and a normal restart after controlled shutdown succeeds.
**Evidence.**
- `tests/integration/test_setup.py::test_second_owner_cannot_acquire_and_can_after_release` — a second owner is refused, and after release a restart reacquires (the normal-restart case is asserted in the same test).
- `tests/integration/test_setup.py::test_setup_refuses_while_another_owner_holds_the_instance` — setup refuses while another owner holds the instance.
- `tests/integration/test_setup.py::test_lock_backend_is_a_real_os_lock` — the lock is an OS lock, not an in-process flag.
- `tests/unit/test_cli.py::test_stop_refuses_while_a_helper_holds_the_lock` — the CLI refuses to stop while the lock is held.
**Status.** COVERED
**Gap.** The "second host" case is exercised as a second lock holder on the same filesystem path,
not as a genuinely separate host; the OS-lock test is the nearest available proxy.

### AT-38 — Backup and relocation
**Requirement.** A coordinated database/original-file backup restored onto a supported host keeps IDs, evidence, human state, and document links consistent.
**Evidence.**
- `tests/unit/test_cli.py::test_backup_creates_verified_copy` — the CLI produces a verified backup file with non-zero byte size.
- `tests/unit/test_api_analysis.py::test_administrator_backup_creates_a_verified_copy` — the API produces a verified copy reporting `integrity == "ok"` and matching byte size.
- `tests/unit/test_api_analysis.py::test_backup_requires_an_administrator` — backup is administrator-only.
**Status.** PARTIAL
**Gap.** Only the backup half is demonstrated. No test restores a backup onto another host, and no
test asserts that IDs, evidence, human state, or document links remain consistent after
relocation. Whether the backup includes the original files rather than the database alone is not
asserted by any test.

### AT-39 — Fairness/accessibility checks
**Requirement.** Irrelevant identity-cue changes do not change qualification results, and keyboard-only users can inspect, decide, and approve without relying on colour alone.
**Evidence.**
- `tests/unit/test_web_assets.py::test_css_never_conveys_state_by_colour_alone` — each evidence status draws a distinct shape as well as a colour, and `:focus-visible` is present in the stylesheet.
- `tests/unit/test_web_assets.py::test_shell_is_an_application_with_every_required_region` — the shell exposes every required region.
- `tests/browser/check_report_js.mjs` (run via `tests/unit/test_web_assets.py::test_pure_logic_harness_passes`) — asserts `aria-sort="none"` on sortable columns.
**Status.** PARTIAL
**Gap.** No test demonstrates keyboard-only inspect/decide/approve: the only keyboard-related
assertion is that `:focus-visible` appears in the CSS text, which is a static string check and not
an exercised interaction. No test asserts that identity-cue changes leave qualification results
unchanged; the fairness half of AT-39 has no evidence at all.

### AT-40 — Release evidence
**Requirement.** Produce benchmark results, gold-set evaluation, live integration evidence, dependency lockfiles, and an explicit known-limitations list, with unimplemented controls labelled incomplete.
**Evidence.**
- `requirements.lock` exists at the repository root (dependency lockfile).
- `docs/review-artifacts/*.txt` hold recorded suite output (`synthetic-suite.txt`, `requisition-workspace-suite.txt`, `candidate-action-tests.txt`, `connected-list-contract-tests.txt`, `feedback-tests.txt`, `requisition-tests.txt`).
- `docs/milestone-2-report.md`, `docs/milestone-3-report.md`, `docs/backup-and-recovery.md`, `docs/compatibility.md`, `docs/local-operation.md`, `docs/shared-host-operation.md`, `docs/privacy-and-retention.md` exist.
**Status.** PARTIAL
**Gap.** Missing artifacts: no benchmark results anywhere in `docs/`; no gold-set evaluation
(`docs/corpus.md` mentions golden files only to forbid real data in them, and defines no gold set);
no recorded live-integration evidence (`tests/integration/test_openclaw_live.py` is live-marked and
skipped in the default run, and no run artifact of it was found); no explicit known-limitations
list under that or an equivalent name; and `docs/acceptance-report.md`, which `AGENTS.md` names as
the file whose claims must each cite a test id, does not exist. No test asserts any AT-40 artifact.

## 6. Consolidated known limitations

Section 5 carries a gap line per requirement. This section groups the recurring ones,
because eight of them share a root cause and an operator should be able to see that in one
place rather than infer it from twelve separate Gap lines.

### 6.1 No live OpenClaw route was exercised

The default run skips all eleven live tests (section 3). A live route was not configured
while producing this report, and no packet-level or real-endpoint evidence is presented
anywhere in it. Affected: **AT-35** (dangerous-tool denial and the allowlisted agent target
are asserted against a recording stub, not proven live), **AT-36** (no-egress is asserted by
observing that the stub recorder saw no second route). The unit tests over the stub are real
and do demonstrate the policy logic; what is missing is confirmation that the real endpoint
behaves as the stub predicts.

### 6.2 The real corpus is opt-in and was not run

`RESUME_REVIEW_REAL_CORPUS=1` was not set. Affected: the "semantic correctness is tested
separately" half of **AT-11**, and the real-text half of **AT-06**. A real PII corpus exists
in `resume/` under a deliberate opt-in (kept off the default test path), so this remains a
run that was not performed, not a capability that is missing.

### 6.3 Requirements that name a scale the fixtures do not reach

- **AT-06** names a 400-document census. The census assertions run against the standard
  synthetic fixture, which `tests/fixtures/synth.py:442` defines at **18** documents.
- **AT-15** names a 400-record table. The API tests use small synthetic sets; the Node
  harness uses 120 rows.

Balances, totals, and ordering are asserted at the smaller scale. Whether they hold at the
named scale is untested.

### 6.4 Interaction behaviour asserted statically or through the Node harness

Several UI requirements are evidenced by static string checks or by the Node logic harness
rather than by a real browser session. That is genuine evidence for pure logic, and weaker
evidence for interaction.

- No test drives keyboard interaction. There is no `keydown`, `focus()`, or tab-order
  assertion anywhere under `tests/`. Affected: **AT-15** and **AT-39**. In both cases the
  keyboard claim rests on an `aria-sort` attribute and a `:focus-visible` CSS rule being
  present in the source text, which is a static check and not an exercised interaction.
- **AT-39**'s fairness half (identity-cue changes leaving qualification results unchanged)
  has **no evidence at all**.
- **AT-29**'s partial-completion rendering is asserted against harness responses with
  synthetic data, not against a real partial batch over HTTP.

### 6.5 Two requirements with no evidence

- **AT-09** (duplicate handling). `Repository.set_duplicate_flags` is defined at
  `src/resume_review/db/repository.py:600` and called from exactly one place,
  `tests/unit/test_repository.py:165`. **No production module calls it**, so nothing detects
  identical bytes at two paths. The fixture generator defines a byte-identical pair
  (`DUPLICATE_A_NAME`, `kind="txt_duplicate"`) and no test consumes it.
- **AT-12** (date uncertainty). No test asserts that overlapping roles or partial dates
  yield an unknown rather than a fabricated total, and no test asserts an age is never
  inferred. The only related code is a prompt line in
  `src/resume_review/openclaw_adapter/prompts.py` and a `clarify_date_overlap` problem code
  in the analysis schema. A prompt instruction is not an application control.

### 6.6 Durability across upgrade and relocation is unproven

- **AT-05**: the refusal half is demonstrated (a database from a newer build is refused).
  No test takes a populated older-schema database, upgrades it after a backup, and asserts
  that decisions, notes, completed tasks, IDs, and history all survive.
- **AT-38**: only the backup half is demonstrated. No test restores a backup onto another
  host, and no test asserts that IDs, evidence, human state, or document links stay
  consistent after relocation. Whether the backup includes the original files rather than
  the database alone is **not asserted by any test**.

Given that constraint 8 requires human state and evidence to survive migrations, this is the
most consequential gap in the report.

### 6.7 Host-dependent denial

- **AT-27**: the junction and symlink denials skip via `make_dir_link` ("this host cannot
  create a directory link") and equivalent guards in `test_ingest_discovery.py`. On this
  host those tests ran and passed; on a host without link support the reparse-point clause
  is unproven rather than merely unrun.
- **AT-37**: the "second host" case is exercised as a second lock holder on the same
  filesystem path, not as a genuinely separate machine.

### 6.8 Crash realism

- **AT-28**: the "before intent persistence" crash point is demonstrated as an atomic
  journal write leaving the prior journal intact, not as a process killed in that window.
  No test kills the process at any crash point; the atomicity property is the evidence
  offered, and it is a sound proxy but not the same claim.
- **AT-29**: a failure is injected at the first and second operations. No test injects a
  failure specifically at a third operation.

### 6.9 Missing release artifacts

See AT-40 in section 5 and section 7. No benchmark results and no gold-set evaluation exist
anywhere in `docs/`. Recorded live-integration evidence does not exist, because the live
suite has never been run in this environment.

## 7. Unimplemented controls (NOT IMPLEMENTED)

Each entry below was confirmed against the source tree on 2026-09-29 by searching for a
caller of the named code path, not by reading the documentation. These are operator-facing
controls that do not exist in this build.

| Control | Evidence that it is not implemented |
| --- | --- |
| Duplicate detection between submissions | `set_duplicate_flags` (`db/repository.py:600`) has no production caller. The byte-comparison feature is **NOT IMPLEMENTED**; the repository method exists unused. |
| Reviewer account creation as a workflow | `AccountStore.create_user` (`auth/store.py:276`) has no caller anywhere under `src/`. The method and its scrypt verifiers work; no CLI command and no endpoint invokes them. |
| Launch/pairing URL printing from the CLI | `PairingManager` (`auth/pairing.py:122`) is defined and re-exported, and is called from no code path. `cli.py start` does not call it. |
| Restore as a single command | Restore is a planned action through plan/approve/apply. There is no restore subcommand. |
| Purge or retention scheduler | No purge, prune, or retention scheduler exists. Consistent with constraint 5 (recoverable Trash only, no automatic purging). |
| File watching | No watcher (`watchdog`, `inotify`, `ReadDirectoryChangesW`) is present under `src/`. Scanning is explicitly invoked. |
| HTTPS serving / remote reviewer access | `start` binds loopback HTTP only; no `ssl` or `certfile` appears in `cli.py` or `api/`. Serving remote reviewers over authenticated HTTPS is **NOT IMPLEMENTED**. |
| Benchmark and gold-set evaluation tooling | No benchmark artifact and no gold set exist. `docs/corpus.md` mentions golden files only to forbid real data in them. |

## 8. What this report does not establish

- **This is not a security audit.** It records that specific adversarial tests pass. It is
  not a penetration test, and it makes no claim about defects outside the tested surface.
- **Passing tests are not live integration.** With the live route unconfigured and the real
  corpus unrun, no claim here rests on a real OpenClaw endpoint or on real resume text.
- **This is not a suitability judgement about any person.** The application deliberately
  produces no opaque score and no automatic rejection; this report says nothing about
  whether any candidate should be hired.
- **AT-40 is not satisfied by this document.** AT-40 requires benchmark results, a gold-set
  evaluation, live integration evidence, lockfiles, and an explicit known-limitations list.
  Writing this file supplies the known-limitations list and the recorded suite result, and
  `requirements.lock` exists. The benchmark, the gold set, and recorded live-integration
  evidence remain absent, so AT-40 stays `PARTIAL`.
- **The tree is not under version control**, so the recorded result is bound to a date and a
  working tree, not to a commit. A later edit to `src/` or `tests/` invalidates the match
  between the numbers in section 3 and the code they describe.

## 9. Desktop companion implementation evidence (2026-09-29)

Recorded command:

```powershell
.\.venv\Scripts\python.exe -m pytest -m 'not live and not corpus' -p no:cacheprovider --timeout=900 --basetemp docs/review-artifacts/desktop-full-suite-final
```

Recorded result, exit code 0:

```text
1131 passed, 15 deselected, 425 warnings in 91.88s (0:01:31)
```

Output: `docs/review-artifacts/desktop-companion-suite.txt`. The 15 live/corpus
tests were explicitly deselected. This result includes the 22 synthetic desktop
helper tests in `tests/unit/test_desktop_helper.py` and the existing deterministic
suite. Warnings are the existing Starlette/httpx deprecations.

| Implemented behavior | Recorded test ID |
| --- | --- |
| Instance-bound operator profile, credentials outside applicant folders, safe diagnostics | `test_profile_is_bound_and_secret_is_not_reported`, `test_applicant_owned_config_and_credentials_refused` |
| Exact approved hosted HTTPS origin; local-only and incomplete attestation fail closed | `test_exact_origin_and_local_only_boundaries`, `test_incomplete_or_string_attestation_refused`, `test_unsafe_profile_refused` |
| Authenticated dashboard/assets, one-use pairing, HttpOnly session and CSRF/Origin checks | `test_connected_launch_auth_assets_and_csrf` |
| Real helper API → durable scan → injected hosted envelope → validated stored analysis → direct/queued feedback; decisions and originals preserved | `test_dashboard_to_worker_to_hosted_envelopes` |
| Browser criteria draft and confirmation produce bodies accepted by actual API routes; draft is not approval | `test_desktop_criteria_ui_posts_real_contract_and_requires_human_approval` |
| Bounded transport retries, persistent work across worker recreation | `test_transient_hosted_failure_is_bounded_and_safe`, `test_real_persisted_analysis_runs_after_worker_recreation`, `test_offline_queue_survives_worker_restart` |
| Worker lifetime and exclusive OS-backed ownership | `test_worker_starts_and_stops_with_helper_lifespan`, `test_second_helper_cannot_take_the_same_folder` |
| Redirect refused and injected probe explicitly non-live | `test_mock_probe_is_not_live_and_redirect_is_not_followed` |

An additional actual subprocess/localhost HTTP smoke passed. It launched the CLI
on an ephemeral loopback port, exchanged a launch ticket, fetched the connected
dashboard and module asset, rejected a CSRF-less mutation, queued and completed a
scan, and listed one synthetic document. The original remained unchanged and
inference was unconfigured. Safe result:
`docs/review-artifacts/desktop-companion-http-smoke.json`.

These results establish the local helper and deterministic transport wiring.
They do not establish production Plow machine authentication, installed-version
compatibility, deployed tool denial, provider retention, or real applicant
analysis. The existing Plow browser dashboard is not automatically a supported
machine API. Live setup remains required; AT-35/AT-36/AT-40 are not upgraded by
these results. The published image and usage reporter were not modified or
deployed during this implementation. See `docs/adr/0003-desktop-hosted-analysis.md`
and `docs/desktop-companion.md`.

## 10. Hosted companion checks (2026-09-30)

Full default deterministic suite: `1137 passed, 15 skipped, 425 warnings in
96.63s`, exit 0. Recorded output:
`docs/review-artifacts/dashboard-suite-final-20260930.log`. The skipped tests
require opt-in live routes or the real corpus. The warnings are the existing
Starlette/httpx deprecations.

`tests/unit/test_hosted_companion.py`: six synthetic tests passed (exit 0).
They cover bridge authentication, protected demo and landing, instance session
issuance, connected assets, CSRF and exact origin, scan queuing, owner binding,
unknown instance refusal and public-origin validation.
`tests/unit/test_api_core.py`: 44 tests passed (exit 0) after resolving the
envelope schema from the installed package instead of a repository-relative path.

`submission/plow-image/hosted_probe.mjs` passed against the pinned Plow Gateway
with the updated compiled plugin registration and schema path mounted into the
candidate image. It exercised the real service, owner authentication, protected
landing/demo/review/assets, session/CSRF and a successful scan POST (202).
The publishing workflow repeats that check on the final built image without
overlays. This check uses synthetic fixtures and a synthetic trusted proxy;
it makes no inference call and is not evidence of live Plow ingress or usage
reporting. Source: ADR 0004. Live installation evidence remains separate.
