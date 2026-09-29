/*
 * Pure-logic checks for web/assets/report.js.
 *
 * There is no browser in this environment, so this harness exercises the
 * exported pure surface - sorting, filtering, escaping, unknown-value rendering,
 * bulk-selection set maths, filter-tree evaluation, mode resolution, and the
 * request-body builders - directly against the payload shape documented in
 * docs/contracts/report-payload.md.
 *
 * Run: node tests/browser/check_report_js.mjs
 * Exits 0 when every check passes, 1 otherwise, and prints a count either way.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const modulePath = join(here, "..", "..", "web", "assets", "report.js");
const source = readFileSync(modulePath, "utf8");
// Dynamic import needs a file:// URL on Windows, where a drive-letter path is
// not a valid ESM specifier.
const Report = await import(pathToFileURL(modulePath).href);

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok   ${name}`);
  } catch (error) {
    failures.push({ name, error });
    console.log(`FAIL ${name}`);
    console.log(`     ${error && error.message ? error.message : error}`);
  }
}

/* ------------------------------------------------------------------ fixture */

const payload = {
  schema_version: "1.0",
  mode: "snapshot",
  generated_at: "2026-09-29T12:00:00+00:00",
  instance: {
    instance_id: "inst_demo",
    job_title: "Operations Manager",
    app_version: "0.1.0",
    schema_version: 1,
    state_revision: 412,
    last_analysis_at: "2026-09-29T11:58:00+00:00",
    storage_mode: "local",
    criteria_version: 3,
    criteria: [
      { criterion_id: "cr_01", version: 3, definition: "Coordinate subcontractors on commercial sites", label: "required", rationale: "" },
      { criterion_id: "cr_02", version: 3, definition: "Hold a current site safety certification", label: "preferred", rationale: "" },
    ],
  },
  counts: {
    total: 400, filtered: 400, processed: 388, unreviewed: 350, keep: 20, reject: 25,
    hold: 5, manual_review: 12, pending_action: 8, needs_recheck: 1, open_tasks: 31,
  },
  documents: [],
};

function row(overrides = {}) {
  return {
    document_id: "doc_0001",
    display_name: null,
    original_filename: "candidate-001.pdf",
    current_rel_path: "candidate-001.pdf",
    media_type: "pdf",
    size_bytes: 182004,
    ingested_at: "2026-09-29T10:04:11+00:00",
    submitted_at: null,
    processing_state: "ready",
    processing_detail: null,
    location: "active",
    location_version: 1,
    review_state: "unreviewed",
    decision_revision: 0,
    decision_needs_recheck: false,
    recheck_reason: null,
    disposition_frozen: false,
    pending_intent: "none",
    intent_revision: 0,
    duplicate_content: false,
    duplicate_of: null,
    open_task_count: 1,
    task_warning: false,
    summary_text: "Reports commercial renovation coordination experience.",
    summary_stale: false,
    criteria: [],
    evidence: [],
    tasks: [],
    notes: [],
    decision_history: [],
    file_actions: [],
    document_link: "./candidate-001.pdf",
    warnings: [],
    ...overrides,
  };
}

/* ------------------------------------------- contract shape of the table --- */

test("COLUMNS match the contract order and keys exactly", () => {
  assert.deepEqual(
    Report.COLUMNS.map((column) => column.key),
    ["select", "submission", "summary", "evidence", "tasks", "decision", "action", "access"],
  );
  assert.equal(Report.COLUMNS.length, 8);
});

test("SORT_KEYS match the contract allowlist exactly", () => {
  assert.deepEqual(
    [...Report.SORT_KEYS].sort(),
    ["current_rel_path", "display_name", "document_id", "ingested_at", "open_task_count", "original_filename", "processing_state", "review_state"].sort(),
  );
});

test("the evidence column offers no sort (there is no aggregate score)", () => {
  const evidence = Report.COLUMNS.find((column) => column.key === "evidence");
  assert.equal(evidence.sortKey, null);
});

test("the selection column is not a decision and offers no sort", () => {
  const select = Report.COLUMNS.find((column) => column.key === "select");
  assert.equal(select.sortKey, null);
});

test("buildHeaderHtml renders the eight contract columns in order", () => {
  const html = Report.buildHeaderHtml();
  const keys = [...html.matchAll(/data-column="([a-z]+)"/g)].map((match) => match[1]);
  assert.deepEqual(keys, ["select", "submission", "summary", "evidence", "tasks", "decision", "action", "access"]);
  assert.equal((html.match(/aria-sort="none"/g) || []).length, Report.COLUMNS.filter((column) => column.sortKey).length);
});

test("buildHeaderHtml is fed only module constants, never payload data", () => {
  const start = source.indexOf("export function buildHeaderHtml()");
  assert.ok(start > 0, "buildHeaderHtml must exist");
  const body = source.slice(start, source.indexOf("\n}", start));
  for (const forbidden of ["row", "payload", "document.", "state"]) {
    assert.ok(!body.includes(forbidden), `buildHeaderHtml must not read ${forbidden}`);
  }
});

test("buildHeaderHtml escapes its constant labels", () => {
  assert.ok(Report.buildHeaderHtml().includes("Relevant evidence"));
  assert.ok(!Report.buildHeaderHtml().includes("<script"));
});

/* ----------------------------------------------------- escaping and markup -- */

test("an applicant string with an img tag is escaped, not treated as markup", () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const escaped = Report.escapeHtml(hostile);
  assert.ok(!escaped.includes("<"), "no raw angle bracket survives");
  assert.ok(!escaped.includes(">"), "no raw angle bracket survives");
  assert.ok(escaped.includes("&lt;img"));
  assert.ok(!escaped.includes("onerror=alert(1)>"));
});

test("escapeHtml neutralises quotes, slashes, backticks and equals", () => {
  const escaped = Report.escapeHtml(`"'/\`=`);
  assert.ok(!/["'/<>=`]/.test(escaped), `unexpected raw character in ${escaped}`);
  assert.ok(escaped.includes("&#x27;") && escaped.includes("&#x60;") && escaped.includes("&#x3D;"));
});

test("escapeHtml never closes a script element", () => {
  const escaped = Report.escapeHtml("</script><script>alert(1)</script>");
  assert.ok(!escaped.includes("</script>"));
  assert.ok(escaped.includes("&#x2F;script"));
});

test("the module contains no innerHTML fed by payload data", () => {
  const occurrences = [...source.matchAll(/innerHTML/g)];
  assert.equal(occurrences.length, 1, `expected exactly one innerHTML token, saw ${occurrences.length}`);
  const line = source.slice(0, occurrences[0].index).split("\n").pop() + source.slice(occurrences[0].index).split("\n")[0];
  assert.ok(line.includes("buildHeaderHtml()"), `the only innerHTML must be the static header, saw: ${line.trim()}`);
  assert.ok(line.includes("headRow"), "the only innerHTML must target the header row");
});

test("the snapshot path makes no network call", () => {
  const start = source.indexOf("function snapshotBoot(");
  const end = source.indexOf("async function connectedBoot(");
  assert.ok(start > 0 && end > start, "snapshotBoot must exist before connectedBoot");
  const body = source.slice(start, end);
  for (const forbidden of ["fetch(", "XMLHttpRequest", "sendBeacon", "apiRequest(", "EventSource", "WebSocket"]) {
    assert.ok(!body.includes(forbidden), `snapshotBoot must not reference ${forbidden}`);
  }
});

test("every network call is behind the connected-only guard", () => {
  const start = source.indexOf("async function apiRequest(");
  const body = source.slice(start, source.indexOf("\nasync function writeDecision", start));
  assert.ok(body.includes('modeAllowsNetwork(DOM.state.mode)'), "apiRequest must refuse snapshot mode");
  assert.equal((source.match(/await DOM\.window\.fetch\(/g) || []).length, 1, "exactly one fetch call site");
});

test("a file: origin is snapshot mode and can never reach the network", () => {
  const resolved = Report.resolveMode({ snapshotText: "", bootstrapText: "", href: "file:///C:/Job/review.html", search: "" });
  assert.equal(resolved.mode, "snapshot");
  assert.equal(Report.modeAllowsNetwork(resolved.mode), false);
});

test("connected mode resolves its instance id from the bootstrap or the URL", () => {
  const fromBootstrap = Report.resolveMode({
    snapshotText: "",
    bootstrapText: JSON.stringify({ mode: "connected", instance_id: "inst_a", api_base: "/api/v1/instances", role: "reviewer" }),
    href: "http://127.0.0.1:8765/review",
    search: "",
  });
  assert.equal(fromBootstrap.mode, "connected");
  assert.equal(fromBootstrap.instance_id, "inst_a");

  assert.equal(Report.instanceIdFromHref("/instances/inst_b/review", ""), "inst_b");
  assert.equal(Report.instanceIdFromHref("/review", "?instance=inst_c"), "inst_c");
});

/* ----------------------------------------------- unknown vs zero vs blank --- */

test("null renders as unknown and stays distinct from zero and empty string", () => {
  assert.equal(Report.classifyValue(null), "unknown");
  assert.equal(Report.classifyValue(undefined), "unknown");
  assert.equal(Report.classifyValue(""), "blank");
  assert.equal(Report.classifyValue(0), "value");
  assert.equal(Report.describeValue(0).text, "0");
  assert.equal(Report.describeValue("").text, "Blank");
  assert.equal(Report.describeValue(null).text, "Unknown");
  assert.notEqual(Report.describeValue(0).text, Report.describeValue(null).text);
  assert.notEqual(Report.describeValue("").text, Report.describeValue(null).text);
});

test("counts keep null as unknown rather than printing zero", () => {
  assert.equal(Report.formatCount(null), null);
  assert.equal(Report.formatCount(0), "0");
  assert.equal(Report.formatCount(31), "31");
});

test("timestamps and sizes render unknown as unknown", () => {
  assert.equal(Report.formatTimestamp(null), null);
  assert.equal(Report.formatTimestamp("2026-09-29T12:00:00+00:00"), "2026-09-29 12:00 UTC");
  assert.equal(Report.formatBytes(null), null);
  assert.equal(Report.formatBytes(182004), "177.7 KiB");
  assert.equal(Report.formatBytes(0), "0 B");
});

test("not_found never renders as a negative claim", () => {
  const described = Report.describeCriterionResult("not_found");
  assert.equal(described.label, "Not established in this document");
  assert.ok(!/does not have|doesn't have|no\b/i.test(described.label), described.label);
  assert.equal(Report.describeCriterionResult(null).label, "Unknown");
  assert.equal(Report.describeCriterionResult("supported").label, "Supported");
});

test("describeFilterNode never turns not_found into does not have", () => {
  const lines = Report.describeFilterNode({
    type: "predicate",
    field: "criterion:cr_01",
    op: "is_not_found",
  });
  assert.deepEqual(lines, ["Criterion cr_01 is not established in this document"]);
  assert.ok(!lines.join(" ").includes("does not have"));
});

/* ------------------------------------------------------------------ sorting - */

test("default sort is ingested_at ascending with a document_id tie-breaker", () => {
  const rows = [
    row({ document_id: "doc_b", ingested_at: "2026-09-29T10:00:00+00:00" }),
    row({ document_id: "doc_a", ingested_at: "2026-09-29T10:00:00+00:00" }),
    row({ document_id: "doc_c", ingested_at: "2026-09-29T09:00:00+00:00" }),
  ];
  assert.deepEqual(Report.sortRows(rows).map((item) => item.document_id), ["doc_c", "doc_a", "doc_b"]);
  assert.equal(Report.DEFAULT_SORT.sort, "ingested_at");
  assert.equal(Report.DEFAULT_SORT.direction, "asc");
});

test("unknown values sort last in both directions", () => {
  const rows = [
    row({ document_id: "doc_null", display_name: null }),
    row({ document_id: "doc_beta", display_name: "Beta" }),
    row({ document_id: "doc_alpha", display_name: "Alpha" }),
  ];
  const ascending = Report.sortRows(rows, "display_name", "asc").map((item) => item.document_id);
  assert.deepEqual(ascending, ["doc_alpha", "doc_beta", "doc_null"]);
  const descending = Report.sortRows(rows, "display_name", "desc").map((item) => item.document_id);
  assert.deepEqual(descending, ["doc_beta", "doc_alpha", "doc_null"]);
});

test("an unsupported sort key falls back to the default rather than throwing", () => {
  const rows = [row({ document_id: "doc_b" }), row({ document_id: "doc_a" })];
  assert.deepEqual(Report.sortRows(rows, "summary_text", "asc").map((item) => item.document_id), ["doc_a", "doc_b"]);
});

test("filenames sort naturally so candidate-2 precedes candidate-10", () => {
  const rows = [
    row({ document_id: "doc_10", original_filename: "candidate-10.pdf" }),
    row({ document_id: "doc_2", original_filename: "candidate-2.pdf" }),
  ];
  assert.deepEqual(Report.sortRows(rows, "original_filename", "asc").map((item) => item.document_id), ["doc_2", "doc_10"]);
});

/* ----------------------------------------------------------------- filtering - */

test("the toolbar filter narrows by location, state and task", () => {
  const rows = [
    row({ document_id: "doc_1", location: "active", review_state: "unreviewed", open_task_count: 0 }),
    row({ document_id: "doc_2", location: "rejected", review_state: "reject", open_task_count: 2, task_warning: true }),
    row({ document_id: "doc_3", location: "trash", review_state: "hold", open_task_count: 1 }),
  ];
  const active = Report.applyFilter(rows, { ...Report.emptyFilter(), location: "active" });
  assert.deepEqual(active.rows.map((item) => item.document_id), ["doc_1"]);
  assert.equal(active.omitted.total, 2);
  assert.equal(active.omitted.by_reason.location, 2);

  const attention = Report.applyFilter(rows, { ...Report.emptyFilter(), task: "attention" });
  assert.deepEqual(attention.rows.map((item) => item.document_id), ["doc_2"]);
});

test("search covers name, filename, path and summary", () => {
  const rows = [
    row({ document_id: "doc_1", summary_text: "Coordinated subcontractors" }),
    row({ document_id: "doc_2", original_filename: "nurse-cv.txt", summary_text: "Clinical work" }),
  ];
  const result = Report.applyFilter(rows, { ...Report.emptyFilter(), search: "subcontractors" });
  assert.deepEqual(result.rows.map((item) => item.document_id), ["doc_1"]);
});

test("a missing criterion assessment evaluates unknown, never false", () => {
  const node = { type: "predicate", field: "criterion:cr_01", op: "is_supported" };
  const withoutAssessment = row({ criteria: [] });
  assert.equal(Report.evaluateFilterNode(node, withoutAssessment), null);
  const notFound = row({ criteria: [{ criterion_id: "cr_01", result: "not_found" }] });
  assert.equal(Report.evaluateFilterNode(node, notFound), false);
  assert.equal(Report.evaluateFilterNode({ type: "predicate", field: "criterion:cr_01", op: "is_not_found" }, notFound), true);
});

test("include_with_warning keeps unknown rows and counts them", () => {
  const rows = [
    row({ document_id: "doc_known", criteria: [{ criterion_id: "cr_01", result: "supported" }] }),
    row({ document_id: "doc_unknown", criteria: [] }),
    row({ document_id: "doc_no", criteria: [{ criterion_id: "cr_01", result: "not_found" }] }),
  ];
  const tree = { type: "predicate", field: "criterion:cr_01", op: "is_supported" };
  const included = Report.applyFilter(rows, { ...Report.emptyFilter(), node: tree, unknown_policy: "include_with_warning" });
  assert.deepEqual(included.rows.map((item) => item.document_id).sort(), ["doc_known", "doc_unknown"]);
  assert.equal(included.omitted.unknown_included, 1);
  assert.equal(included.omitted.unknown_excluded, 0);
  assert.equal(included.omitted.total, 1);

  const excluded = Report.applyFilter(rows, { ...Report.emptyFilter(), node: tree, unknown_policy: "exclude" });
  assert.deepEqual(excluded.rows.map((item) => item.document_id), ["doc_known"]);
  assert.equal(excluded.omitted.unknown_excluded, 1);
  assert.equal(excluded.omitted.total, 2);
});

test("the unknown-value treatment reports the count it affects", () => {
  const treatment = Report.unknownTreatment({ unknown_policy: "include_with_warning" }, { unknown_included: 7, unknown_excluded: 0 });
  assert.equal(treatment.affected, 7);
  assert.ok(treatment.text.includes("included and flagged"));
  const excluding = Report.unknownTreatment({ unknown_policy: "exclude" }, { unknown_included: 0, unknown_excluded: 3 });
  assert.equal(excluding.affected, 3);
  assert.ok(excluding.text.includes("excluded"));
});

test("an and-group is three-valued: unknown propagates, false wins", () => {
  const node = {
    type: "and",
    children: [
      { type: "predicate", field: "review_state", op: "eq", value: "unreviewed" },
      { type: "predicate", field: "criterion:cr_01", op: "is_supported" },
    ],
  };
  assert.equal(Report.evaluateFilterNode(node, row({ review_state: "unreviewed", criteria: [] })), null);
  assert.equal(Report.evaluateFilterNode(node, row({ review_state: "keep", criteria: [] })), false);
  assert.equal(
    Report.evaluateFilterNode(node, row({ review_state: "unreviewed", criteria: [{ criterion_id: "cr_01", result: "supported" }] })),
    true,
  );
});

/* ----------------------------------------------------------- save feedback -- */

test("describeSaveState yields exactly one of saved, saving, conflict, failed", () => {
  const allowed = new Set(["saved", "saving", "conflict", "failed"]);
  for (const entry of [null, { state: "saving" }, { state: "conflict", current_value: "keep", current_actor: "reviewer@host" }, { state: "failed", message: "offline" }]) {
    const described = Report.describeSaveState(entry, row({ decision_revision: 7 }));
    assert.ok(allowed.has(described.state), `unexpected state ${described.state}`);
  }
});

test("a saved row reports its revision", () => {
  const described = Report.describeSaveState(null, row({ decision_revision: 7 }));
  assert.equal(described.state, "saved");
  assert.equal(described.label, "Saved (revision 7)");
  assert.equal(described.committed, true);
});

test("a failed request is never rendered as committed", () => {
  const described = Report.describeSaveState({ state: "failed", message: "network down" }, row({ decision_revision: 7 }));
  assert.equal(described.state, "failed");
  assert.equal(described.committed, false);
  assert.equal(described.retry, true);
  assert.ok(!described.label.toUpperCase().startsWith("SAVED"));
  assert.ok(described.detail.includes("Not saved"));
});

test("a conflict shows the current value and its actor and does not auto-retry", () => {
  const described = Report.describeSaveState({ state: "conflict", current_value: "keep", current_actor: "reviewer@host" }, row());
  assert.equal(described.state, "conflict");
  assert.equal(described.retry, false);
  assert.equal(described.committed, false);
  assert.ok(described.detail.includes("Keep"));
  assert.ok(described.detail.includes("reviewer@host"));
});

test("the decision control is mutually exclusive", () => {
  const control = Report.describeDecisionControl(row({ review_state: "hold" }));
  assert.equal(control.options.filter((option) => option.checked).length, 1);
  assert.equal(control.options.find((option) => option.checked).value, "hold");
  assert.deepEqual(control.options.map((option) => option.value), ["unreviewed", "keep", "reject", "hold"]);
});

test("disposition is frozen while a Trash request is pending, with a reason", () => {
  const frozen = Report.describeDecisionControl(row({ pending_intent: "move_trash" }));
  assert.equal(frozen.disabled, true);
  assert.ok(frozen.reason.includes("Trash request is pending"));
  assert.ok(frozen.options.every((option) => option.disabled));

  const inTrash = Report.describeDecisionControl(row({ location: "trash" }));
  assert.equal(inTrash.disabled, true);
  assert.ok(inTrash.reason.includes("in Trash"));

  const free = Report.describeDecisionControl(row({ disposition_frozen: true }));
  assert.equal(free.disabled, true);
});

test("a rejection-folder move is not shown as completed before it happens", () => {
  const planned = Report.describeAction(row({ pending_intent: "move_rejected", file_actions: [{ batch_id: "batch_1", kind: "move_rejected", state: "applying", destination: "Rejected/doc_1/candidate-001.pdf", at: null, error_code: null }] }));
  assert.equal(planned.committed, false);
  assert.ok(planned.lastText.includes("Applying"));

  const committed = Report.describeAction(row({ file_actions: [{ batch_id: "batch_1", kind: "move_rejected", state: "committed", destination: "Rejected/doc_1/candidate-001.pdf", at: null, error_code: null }] }));
  assert.equal(committed.committed, true);
  assert.ok(committed.lastText.includes("Committed"));
});

test("a pending intent is reported without claiming a move", () => {
  const described = Report.describeAction(row({ pending_intent: "move_trash" }));
  assert.equal(described.intentText, "Pending: move to Trash");
  assert.equal(described.committed, false);
});

/* --------------------------------------------------------- selection maths -- */

test("select this page unions the rendered rows", () => {
  const rows = [row({ document_id: "doc_1", decision_revision: 3 }), row({ document_id: "doc_2", decision_revision: 0 })];
  const selection = Report.selectPage(Report.emptySelection(), rows);
  assert.deepEqual(selection.pairs, [
    { document_id: "doc_1", decision_revision: 3 },
    { document_id: "doc_2", decision_revision: 0 },
  ]);
  const again = Report.selectPage(selection, rows);
  assert.equal(again.pairs.length, 2, "selecting the same page twice must not duplicate pairs");
});

test("toggling a row adds and removes it, and never merges with a frozen set", () => {
  let selection = Report.emptySelection();
  selection = Report.toggleSelection(selection, row({ document_id: "doc_1", decision_revision: 1 }));
  assert.equal(selection.pairs.length, 1);
  selection = Report.toggleSelection(selection, row({ document_id: "doc_1", decision_revision: 1 }));
  assert.equal(selection.pairs.length, 0);

  const frozen = Report.resolveAllMatching([row({ document_id: "doc_9", decision_revision: 4 })]);
  const afterToggle = Report.toggleSelection(frozen, row({ document_id: "doc_10" }));
  assert.equal(afterToggle, frozen, "a frozen selection must be returned unchanged");
});

test("select all matching results resolves to an immutable set at confirmation time", () => {
  const matching = [row({ document_id: "doc_1", decision_revision: 2 }), row({ document_id: "doc_2", decision_revision: 5 })];
  const resolved = Report.resolveAllMatching(matching);

  // The source list changes afterwards: a new submission arrives, a filter drops
  // a row. The resolved set must not move.
  matching.push(row({ document_id: "doc_3", decision_revision: 0 }));
  matching.shift();
  assert.deepEqual(resolved.pairs.map((pair) => pair.document_id), ["doc_1", "doc_2"]);
  assert.equal(resolved.frozen, true);
  assert.ok(Object.isFrozen(resolved));
  assert.ok(Object.isFrozen(resolved.pairs));
});

test("the resolved set carries the decision revision captured at confirmation", () => {
  const resolved = Report.resolveAllMatching([row({ document_id: "doc_1", decision_revision: 11 })]);
  assert.deepEqual(resolved.pairs[0], { document_id: "doc_1", decision_revision: 11 });
});

test("hidden-selected counts rows selected while not matching the filter", () => {
  const pageOne = [row({ document_id: "doc_1" }), row({ document_id: "doc_2" })];
  let selection = Report.selectPage(Report.emptySelection(), pageOne);
  selection = Report.toggleSelection(selection, row({ document_id: "doc_3" }));
  const summary = Report.describeSelection(selection, [pageOne[0]]);
  assert.equal(summary.count, 3);
  assert.equal(summary.hidden, 2);
  assert.equal(summary.frozen, false);
  assert.deepEqual(Report.hiddenSelectedPairs(selection, [pageOne[0]]).map((pair) => pair.document_id), ["doc_2", "doc_3"]);
});

test("a frozen selection says so in its note", () => {
  const summary = Report.describeSelection(Report.resolveAllMatching([row({ document_id: "doc_1" })]), []);
  assert.equal(summary.frozen, true);
  assert.ok(summary.note.includes("will not extend it"));
});

test("deselect all clears everything", () => {
  const cleared = Report.deselectAll();
  assert.deepEqual(cleared.pairs, []);
  assert.equal(cleared.frozen, false);
});

/* ---------------------------------------------------------------- pagination */

test("pagination pages 50 rows by default and reports a visible total", () => {
  const rows = Array.from({ length: 120 }, (_, index) => row({ document_id: `doc_${String(index).padStart(4, "0")}` }));
  const first = Report.paginate(rows, 1, Report.PAGE_SIZE_DEFAULT);
  assert.equal(first.items.length, 50);
  assert.equal(first.total, 120);
  assert.equal(first.pageCount, 3);
  assert.equal(first.hasPrevious, false);
  assert.equal(first.hasNext, true);
  assert.equal(Report.describePageSlice(first), "Showing 1-50 of 120 (page 1 of 3)");

  const last = Report.paginate(rows, 3, Report.PAGE_SIZE_DEFAULT);
  assert.equal(last.items.length, 20);
  assert.equal(last.hasNext, false);
  assert.equal(last.hasPrevious, true);

  const clamped = Report.paginate(rows, 99, Report.PAGE_SIZE_DEFAULT);
  assert.equal(clamped.page, 3);
  assert.equal(Report.describePageSlice(Report.paginate([], 1, 50)), "No rows");
});

/* ------------------------------------------------------------- api surface -- */

test("the decision write body carries no actor field", () => {
  const body = Report.decisionWriteBody({ decision: "reject", expectedRevision: 7 });
  assert.deepEqual(body, { disposition: "reject", expected_revision: 7 });
  assert.ok(!("actor" in body));
});

test("bulk decisions send an explicit document and revision set with no actor", () => {
  const selection = Report.resolveAllMatching([row({ document_id: "doc_1", decision_revision: 3 })]);
  const body = Report.bulkDecisionBody(selection, "keep");
  assert.deepEqual(body.items, [{ document_id: "doc_1", disposition: "keep", expected_revision: 3 }]);
  assert.ok(!("decision" in body));
  assert.ok(!("actor" in body));
});

test("api paths stay under the instance scope", () => {
  assert.equal(Report.apiPath("/api/v1/instances", "inst_demo", "/status"), "/api/v1/instances/inst_demo/status");
  assert.equal(Report.apiPath("/api/v1/instances/", "inst/a", "/documents"), "/api/v1/instances/inst%2Fa/documents");
  assert.equal(Report.documentPath("doc_1"), "/documents/doc_1");
  assert.equal(Report.decisionWritePath({ document_id: "doc_1" }), "/documents/doc_1/decision");
});

test("the documents query includes pagination, sort and direction", () => {
  const query = Report.documentsQuery({ page: 2, pageSize: 50, sort: "ingested_at", direction: "desc" });
  assert.ok(query.startsWith("/documents?"));
  assert.ok(query.includes("page=2"));
  assert.ok(query.includes("page_size=50"));
  assert.ok(query.includes("sort=ingested_at"));
  assert.ok(query.includes("direction=desc"));
});

test("idempotency keys are unique per attempt", () => {
  const first = Report.newIdempotencyKey("decide");
  const second = Report.newIdempotencyKey("decide");
  assert.notEqual(first, second);
  assert.ok(first.startsWith("decide_"));
});

/* ------------------------------------------------------ document link safety */

test("only a relative in-folder document link is accepted", () => {
  assert.equal(Report.safeRelativeLink("./candidate-001.pdf"), "./candidate-001.pdf");
  assert.equal(Report.safeRelativeLink("Rejected/doc_1/a b.pdf"), "Rejected/doc_1/a%20b.pdf");
  assert.equal(Report.safeRelativeLink("javascript:alert(1)"), null);
  assert.equal(Report.safeRelativeLink("data:text/html,<script>"), null);
  assert.equal(Report.safeRelativeLink("https://example.invalid/x.pdf"), null);
  assert.equal(Report.safeRelativeLink("//example.invalid/x.pdf"), null);
  assert.equal(Report.safeRelativeLink("/etc/passwd"), null);
  assert.equal(Report.safeRelativeLink("../../secrets.pdf"), null);
  assert.equal(Report.safeRelativeLink("C:\\Users\\x.pdf"), null);
  assert.equal(Report.safeRelativeLink(null), null);
});

test("the module never builds a markup string from a payload value", () => {
  assert.ok(!/innerHTML\s*=\s*(?!\s*buildHeaderHtml)/.test(source), "every innerHTML assignment must go through buildHeaderHtml");
  for (const sink of ["outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("]) {
    assert.ok(!source.includes(sink), `${sink} must not appear in the report client`);
  }
  assert.equal((source.match(/innerHTML/g) || []).length, 1);
});

test("describeFilter returns whole condition strings, never characters", () => {
  const lines = Report.describeFilter({
    ...Report.emptyFilter(),
    review_state: "hold",
    node: { type: "predicate", field: "criterion:cr_01", op: "is_not_found" },
  });
  assert.deepEqual(lines, ["Reviewer state is Hold", "Criterion cr_01 is not established in this document"]);
  for (const line of lines) assert.ok(line.length > 1, `condition looks like a character: ${line}`);
});

/* ------------------------------------------------------------------ summary - */

console.log("");
if (failures.length > 0) {
  console.log(`${passed} passed, ${failures.length} failed`);
  for (const failure of failures) {
    console.log(`  - ${failure.name}`);
  }
  process.exit(1);
}
console.log(`${passed} passed, 0 failed`);
process.exit(0);
