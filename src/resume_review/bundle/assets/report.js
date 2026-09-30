/*
 * resume-review browser client.
 *
 * Authority: PRD section 8 (review page and interaction requirements) and the
 * frozen seam in docs/contracts/report-payload.md.
 *
 * Two rules shape this file:
 *
 * 1. Mode is decided before anything else. In snapshot mode the client makes no
 *    network request at all - not even a HEAD - because a file: document has an
 *    opaque origin and cannot reliably fetch a neighbouring file. Sorting and
 *    filtering run purely in memory over the embedded payload.
 *
 * 2. Applicant-sourced strings are data. Every one of them reaches the DOM
 *    through textContent or a createElement property assignment. The only
 *    string-building path in this module is buildHeaderHtml(), whose input is the
 *    frozen COLUMNS constant; escapeHtml() is applied there anyway.
 *
 * The pure helpers (sorting, filtering, escaping, selection set maths, unknown
 * value rendering, filter-tree evaluation) are exported so tests/browser/
 * check_report_js.mjs can load this module without a DOM. Every DOM access sits
 * behind a `typeof document` check and inside boot().
 */

export const SCHEMA_VERSION = "1.0";
export const PAGE_SIZE_DEFAULT = 50;

/** Whole-instance count keys the contract guarantees, with display labels. */
export const COUNT_FIELDS = Object.freeze([
  ["total", "Total"],
  ["processed", "Processed"],
  ["unreviewed", "Unreviewed"],
  ["keep", "Keep"],
  ["reject", "Reject"],
  ["hold", "Hold"],
  ["manual_review", "Manual review"],
  ["pending_action", "Pending action"],
  ["needs_recheck", "Decision needs recheck"],
  ["open_tasks", "Open review tasks"],
]);

/** Table columns, fixed order (contract section 4). sortKey is null when the
 *  contract defines no sort for that column - notably `evidence`, because there
 *  is no aggregate score and none may be introduced. */
export const COLUMNS = Object.freeze([
  { key: "select", label: "Select", sortKey: null, className: "rr-col-select" },
  { key: "submission", label: "Submission", sortKey: "original_filename", className: null },
  { key: "summary", label: "Summary", sortKey: null, className: "rr-col-summary" },
  { key: "evidence", label: "Relevant evidence", sortKey: null, className: null },
  { key: "tasks", label: "Review tasks", sortKey: "open_task_count", className: null },
  { key: "decision", label: "Decision", sortKey: "review_state", className: null },
  { key: "action", label: "File action", sortKey: null, className: null },
  { key: "access", label: "File access", sortKey: null, className: null },
]);

/** Sort keys the contract allows (contract section 4). */
export const SORT_KEYS = Object.freeze([
  "ingested_at",
  "original_filename",
  "display_name",
  "processing_state",
  "review_state",
  "open_task_count",
  "current_rel_path",
  "document_id",
]);

export const DEFAULT_SORT = Object.freeze({ sort: "ingested_at", direction: "asc" });

export const DISPOSITIONS = Object.freeze(["unreviewed", "keep", "reject", "hold"]);
export const DISPOSITION_LABELS = Object.freeze({
  unreviewed: "Unreviewed",
  keep: "Keep",
  reject: "Reject",
  hold: "Hold",
});

export const UNKNOWN_LABEL = "Unknown";
export const UNKNOWN_TITLE = "The system looked and could not establish a value.";
export const BLANK_LABEL = "Blank";
export const BLANK_TITLE = "The value is present but empty.";

/** Frozen wording. "not established" is never rendered as "does not have". */
export const NOT_FOUND_LABEL = "Not established in this document";
export const NOT_FOUND_SHORT = "Not established";
export const NOT_FOUND_TITLE =
  "The processed text did not establish this criterion. This is not a negative finding.";

export const PENDING_INTENT_LABELS = Object.freeze({
  none: "No pending action",
  move_rejected: "Pending: move to Rejected",
  restore_active: "Pending: return to active folder",
  move_trash: "Pending: move to Trash",
  restore_previous: "Pending: restore previous location",
});

export const FILE_ACTION_KIND_LABELS = Object.freeze({
  move_rejected: "Move to Rejected",
  restore_active: "Return to active folder",
  move_trash: "Move to Trash",
  restore_previous: "Restore previous location",
});

/** Operation and batch states. Only a committed/completed state may be shown as
 *  a completed move (PRD 10: a rejection folder move must not display before it
 *  happens). */
export const FILE_ACTION_STATE_LABELS = Object.freeze({
  planned: "Planned",
  approved: "Approved",
  applying: "Applying",
  intent_recorded: "Intent recorded",
  file_moved: "File moved, not yet committed",
  committed: "Committed",
  completed: "Completed",
  partial: "Partially completed",
  blocked: "Blocked",
  canceled: "Canceled",
  failed: "Failed",
  skipped: "Skipped",
  needs_reconciliation: "Needs reconciliation",
});

const COMMITTED_ACTION_STATES = Object.freeze(["committed", "completed"]);

/* =========================================================================
 * Value rendering: unknown is a first-class value
 * ========================================================================= */

export function isUnknown(value) {
  return value === null || value === undefined;
}

export function isBlank(value) {
  return value === "";
}

/** Classify a payload value into the four renderings that must stay distinct:
 *  unknown (null), blank (""), a numeric zero, and an ordinary value. */
export function classifyValue(value) {
  if (isUnknown(value)) return "unknown";
  if (isBlank(value)) return "blank";
  if (typeof value === "number" && Number.isNaN(value)) return "unknown";
  return "value";
}

export function describeValue(value) {
  switch (classifyValue(value)) {
    case "unknown":
      return { kind: "unknown", text: UNKNOWN_LABEL, title: UNKNOWN_TITLE, isUnknown: true, isBlank: false };
    case "blank":
      return { kind: "blank", text: BLANK_LABEL, title: BLANK_TITLE, isUnknown: false, isBlank: true };
    default:
      if (typeof value === "boolean") {
        return { kind: "value", text: value ? "Yes" : "No", title: "", isUnknown: false, isBlank: false };
      }
      return { kind: "value", text: String(value), title: "", isUnknown: false, isBlank: false };
  }
}

/** Whole numbers keep their digits; null stays null so the caller renders the
 *  unknown marker rather than a misleading zero. */
export function formatCount(value) {
  if (isUnknown(value)) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return String(Math.trunc(value));
}

const BYTE_UNITS = Object.freeze(["B", "KiB", "MiB", "GiB"]);

export function formatBytes(value) {
  if (isUnknown(value) || typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const text = unit === 0 ? String(size) : size.toFixed(1);
  return `${text} ${BYTE_UNITS[unit]}`;
}

/** ISO-8601 with offset renders as "YYYY-MM-DD HH:MM UTC" when the offset is
 *  UTC, which is what every helper-produced timestamp carries. Null means
 *  unknown, and unknown returns null rather than an epoch or "now". */
export function formatTimestamp(value) {
  if (isUnknown(value) || value === "") return null;
  const text = String(value);
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?(Z|[+-]\d{2}:?\d{2})?/.exec(text);
  if (!match) return text;
  const zone = !match[3] || match[3] === "Z" || match[3] === "+00:00" || match[3] === "+0000" ? "UTC" : match[3];
  return `${match[1]} ${match[2]} ${zone}`;
}

/** Explicit escaping helper, matching security.untrusted.escape_html (which also
 *  escapes `/`, `` ` `` and `=` so a value cannot close a script or attribute). */
export function escapeHtml(value) {
  const text = isUnknown(value) ? "" : String(value);
  let out = "";
  for (const ch of text) {
    switch (ch) {
      case "&": out += "&amp;"; break;
      case "<": out += "&lt;"; break;
      case ">": out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case "'": out += "&#x27;"; break;
      case "/": out += "&#x2F;"; break;
      case "`": out += "&#x60;"; break;
      case "=": out += "&#x3D;"; break;
      default: out += ch;
    }
  }
  return out;
}

/**
 * Accept only a reference that stays inside the job folder.
 *
 * A payload link is applicant-adjacent data: a scheme (javascript:, data:), a
 * network-path reference, an absolute path, a backslash, or a `..` segment could
 * point at another folder or execute script. Anything else returns null and the
 * caller shows the plain path plus instructions instead of a dead link.
 */
export function safeRelativeLink(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (/^[a-zA-Z][a-zA-Z0-9+.\-]*:/.test(trimmed)) return null;
  if (trimmed.startsWith("//") || trimmed.startsWith("/") || trimmed.startsWith("\\")) return null;
  if (trimmed.includes("\\")) return null;
  const segments = trimmed.split("/");
  if (segments.some((segment) => segment === ".." || segment === "~" || segment === "")) return null;
  return segments.map((segment) => encodeURIComponent(segment)).join("/");
}

/* =========================================================================
 * Sorting
 * ========================================================================= */

/** Unknown always sorts last, in both directions. An unknown is neither a low
 *  nor a high value, so reversing the sort must not surface it first. */
export function compareValues(a, b) {
  const aUnknown = isUnknown(a);
  const bUnknown = isUnknown(b);
  if (aUnknown && bUnknown) return 0;
  if (aUnknown) return 1;
  if (bUnknown) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return (a ? 1 : 0) - (b ? 1 : 0);
  return String(a).localeCompare(String(b), "en", { sensitivity: "base", numeric: true });
}

/** Read a sort key from a row. `open_task_count` is a count, so it compares
 *  numerically; everything else compares as text or as the payload's own type. */
export function sortValue(row, key) {
  if (!row || typeof row !== "object") return null;
  if (key === "open_task_count") {
    const raw = row.open_task_count;
    if (isUnknown(raw) || typeof raw !== "number") return null;
    return raw;
  }
  if (!(key in row)) return null;
  const value = row[key];
  return isUnknown(value) ? null : value;
}

export function compareRows(a, b, key, direction) {
  const aValue = sortValue(a, key);
  const bValue = sortValue(b, key);
  const aUnknown = isUnknown(aValue);
  const bUnknown = isUnknown(bValue);
  if (aUnknown !== bUnknown) return aUnknown ? 1 : -1;
  if (!aUnknown) {
    const primary = compareValues(aValue, bValue);
    if (primary !== 0) return direction === "desc" ? -primary : primary;
  }
  // Deterministic tie-breaker, always ascending: stable pagination needs it.
  return compareValues(sortValue(a, "document_id"), sortValue(b, "document_id"));
}

export function sortRows(rows, key = DEFAULT_SORT.sort, direction = DEFAULT_SORT.direction) {
  // A key outside the contract allowlist is refused rather than honoured: the
  // server side of this seam compiles only the documented keys, so silently
  // sorting by something else would make the table disagree with the API.
  const safeKey = SORT_KEYS.includes(key) ? key : DEFAULT_SORT.sort;
  const safeDirection = direction === "desc" ? "desc" : "asc";
  return (Array.isArray(rows) ? rows.slice() : []).sort((a, b) => compareRows(a, b, safeKey, safeDirection));
}

/* =========================================================================
 * Filter tree (contract section 9.3, schemas/filter.schema.json)
 * ========================================================================= */

export const FILTER_OPS = Object.freeze([
  "eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte",
  "contains", "not_contains", "is_null", "is_not_null", "is_true", "is_false",
]);

export const EVIDENCE_OPS = Object.freeze([
  "is_supported", "is_not_found", "is_unclear", "is_needs_manual_review",
]);

const EVIDENCE_OP_RESULT = Object.freeze({
  is_supported: "supported",
  is_not_found: "not_found",
  is_unclear: "unclear",
  is_needs_manual_review: "needs_manual_review",
});

export function criterionAssessment(row, criterionId) {
  const list = row && Array.isArray(row.criteria) ? row.criteria : [];
  for (const item of list) {
    if (item && item.criterion_id === criterionId) return item;
  }
  return null;
}

/** Three-valued predicate evaluation: true, false, or null for unknown.
 *  A missing or stale assessment is unknown, never false. */
export function evaluateFilterNode(node, row) {
  if (!node || typeof node !== "object") return null;

  if (node.type === "and" || node.type === "or") {
    const children = Array.isArray(node.children) ? node.children : [];
    if (children.length === 0) return null;
    const results = children.map((child) => evaluateFilterNode(child, row));
    if (node.type === "and") {
      if (results.some((result) => result === false)) return false;
      if (results.some((result) => result === null)) return null;
      return true;
    }
    if (results.some((result) => result === true)) return true;
    if (results.some((result) => result === null)) return null;
    return false;
  }

  if (node.type !== "predicate") return null;
  const field = String(node.field || "");
  const op = String(node.op || "");

  if (field.startsWith("criterion:")) {
    const criterionId = field.slice("criterion:".length);
    if (!EVIDENCE_OPS.includes(op)) return null;
    const assessment = criterionAssessment(row, criterionId);
    const result = assessment && typeof assessment.result === "string" ? assessment.result : null;
    if (isUnknown(result)) return null;
    return result === EVIDENCE_OP_RESULT[op];
  }

  const value = row && field in row ? row[field] : undefined;

  switch (op) {
    case "is_null":
      return isUnknown(value);
    case "is_not_null":
      return !isUnknown(value);
    case "is_true":
      return isUnknown(value) ? null : value === true;
    case "is_false":
      return isUnknown(value) ? null : value === false;
    default:
      break;
  }

  if (isUnknown(value)) return null;
  const operand = node.value;

  switch (op) {
    case "eq":
      return typeof value === "number" && typeof operand === "number" ? value === operand : String(value) === String(operand);
    case "ne":
      return typeof value === "number" && typeof operand === "number" ? value !== operand : String(value) !== String(operand);
    case "in":
      return Array.isArray(operand) ? operand.some((item) => String(item) === String(value)) : false;
    case "not_in":
      return Array.isArray(operand) ? !operand.some((item) => String(item) === String(value)) : false;
    case "lt":
      return compareValues(value, operand) < 0;
    case "lte":
      return compareValues(value, operand) <= 0;
    case "gt":
      return compareValues(value, operand) > 0;
    case "gte":
      return compareValues(value, operand) >= 0;
    case "contains":
      return String(value).toLowerCase().includes(String(operand).toLowerCase());
    case "not_contains":
      return !String(value).toLowerCase().includes(String(operand).toLowerCase());
    default:
      return null;
  }
}

/** Plain-language rendering of a filter, shown before it is applied. Always
 *  returns an array of lines so callers can concatenate nested groups. */
export function describeFilterNode(node) {
  if (!node || typeof node !== "object") return ["Unrecognised filter condition"];
  if (node.type === "and" || node.type === "or") {
    const children = Array.isArray(node.children) ? node.children : [];
    if (children.length === 0) return ["No conditions"];
    const joined = children.flatMap((child) => describeFilterNode(child));
    if (joined.length === 1) return joined;
    const conjunction = node.type === "and" ? "all of" : "any of";
    return [`Match ${conjunction}:`, ...joined.map((line) => `  - ${line}`)];
  }
  if (node.type !== "predicate") return ["Unrecognised filter condition"];

  const field = String(node.field || "");
  const op = String(node.op || "");
  const operand = Array.isArray(node.value) ? node.value.join(", ") : node.value;

  if (field.startsWith("criterion:")) {
    const criterionId = field.slice("criterion:".length);
    switch (op) {
      case "is_supported": return [`Criterion ${criterionId} is supported`];
      case "is_not_found": return [`Criterion ${criterionId} is not established in this document`];
      case "is_unclear": return [`Criterion ${criterionId} is unclear`];
      case "is_needs_manual_review": return [`Criterion ${criterionId} needs manual review`];
      default: return [`Criterion ${criterionId} has an unsupported condition (${op})`];
    }
  }

  const fieldLabels = {
    review_state: "Reviewer state",
    processing_state: "Processing state",
    location: "Location",
    pending_intent: "Pending action intent",
    open_task_count: "Open review task count",
    original_filename: "Original filename",
    display_name: "Display name",
    current_rel_path: "Current path",
    duplicate_content: "Duplicate content",
    decision_needs_recheck: "Decision needs recheck",
    ingested_at: "Ingestion date",
    document_id: "Document ID",
    summary_text: "Summary",
  };
  const label = fieldLabels[field] || field;
  switch (op) {
    case "eq": return [`${label} is ${operand}`];
    case "ne": return [`${label} is not ${operand}`];
    case "in": return [`${label} is one of ${operand}`];
    case "not_in": return [`${label} is none of ${operand}`];
    case "lt": return [`${label} is before/under ${operand}`];
    case "lte": return [`${label} is at or before/under ${operand}`];
    case "gt": return [`${label} is after/over ${operand}`];
    case "gte": return [`${label} is at or after/over ${operand}`];
    case "contains": return [`${label} contains "${operand}"`];
    case "not_contains": return [`${label} does not contain "${operand}"`];
    case "is_null": return [`${label} is unknown`];
    case "is_not_null": return [`${label} is known`];
    case "is_true": return [`${label} is true`];
    case "is_false": return [`${label} is false`];
    default: return [`${label} has an unsupported condition (${op})`];
  }
}

/* =========================================================================
 * Filtering
 * ========================================================================= */

export const DEFAULT_FILTER = Object.freeze({
  search: "",
  location: "any",
  processing_state: "any",
  review_state: "any",
  task: "any",
  unknown_policy: "include_with_warning",
  node: null,
});

export function emptyFilter() {
  return { ...DEFAULT_FILTER };
}

function matchesSearch(row, term) {
  if (!term) return true;
  const needle = term.trim().toLowerCase();
  if (needle === "") return true;
  const haystack = [row.display_name, row.original_filename, row.current_rel_path, row.summary_text]
    .filter((value) => typeof value === "string")
    .join("\n")
    .toLowerCase();
  return haystack.includes(needle);
}

function matchesTaskFilter(row, task) {
  if (task === "any") return true;
  const count = typeof row.open_task_count === "number" ? row.open_task_count : null;
  switch (task) {
    case "attention":
      return row.task_warning === true;
    case "open":
      return count !== null && count > 0;
    case "none":
      return count === 0;
    default:
      return true;
  }
}

/**
 * Apply the toolbar filter and an optional filter tree, in memory.
 *
 * Returns the surviving rows plus a complete account of what was omitted and
 * why. Unknown is never a silent exclusion: when unknown_policy is
 * include_with_warning the row stays and is counted in `unknown_included`; when
 * it is exclude the row is dropped and counted in `unknown_excluded`.
 */
export function applyFilter(rows, filter = DEFAULT_FILTER) {
  const source = Array.isArray(rows) ? rows : [];
  const config = { ...DEFAULT_FILTER, ...(filter || {}) };
  const unknownPolicy = config.unknown_policy === "exclude" ? "exclude" : "include_with_warning";
  const omitted = {
    total: 0,
    by_reason: { search: 0, location: 0, processing_state: 0, review_state: 0, task: 0, node: 0 },
    unknown_excluded: 0,
    unknown_included: 0,
  };
  const kept = [];

  for (const row of source) {
    if (!matchesSearch(row, config.search)) { omitted.by_reason.search += 1; omitted.total += 1; continue; }
    if (config.location !== "any" && row.location !== config.location) { omitted.by_reason.location += 1; omitted.total += 1; continue; }
    if (config.processing_state !== "any" && row.processing_state !== config.processing_state) { omitted.by_reason.processing_state += 1; omitted.total += 1; continue; }
    if (config.review_state !== "any" && row.review_state !== config.review_state) { omitted.by_reason.review_state += 1; omitted.total += 1; continue; }
    if (!matchesTaskFilter(row, config.task)) { omitted.by_reason.task += 1; omitted.total += 1; continue; }

    if (config.node) {
      const verdict = evaluateFilterNode(config.node, row);
      if (verdict === false) { omitted.by_reason.node += 1; omitted.total += 1; continue; }
      if (verdict === null) {
        if (unknownPolicy === "exclude") {
          omitted.by_reason.node += 1;
          omitted.total += 1;
          omitted.unknown_excluded += 1;
          continue;
        }
        omitted.unknown_included += 1;
      }
    }
    kept.push(row);
  }
  return { rows: kept, omitted };
}

/** Condition strings for the toolbar chips and the chat preview. */
export function describeFilter(filter = DEFAULT_FILTER) {
  const config = { ...DEFAULT_FILTER, ...(filter || {}) };
  const conditions = [];
  if (config.search && config.search.trim() !== "") conditions.push(`Text contains "${config.search.trim()}"`);
  if (config.location !== "any") conditions.push(`Location is ${config.location}`);
  if (config.processing_state !== "any") conditions.push(`Processing state is ${config.processing_state}`);
  if (config.review_state !== "any") conditions.push(`Reviewer state is ${DISPOSITION_LABELS[config.review_state] || config.review_state}`);
  if (config.task !== "any") {
    const taskLabels = {
      attention: "Has a review task needing attention",
      open: "Has at least one open review task",
      none: "Has no open review task",
    };
    conditions.push(taskLabels[config.task] || `Review task filter ${config.task}`);
  }
  if (config.node) conditions.push(...describeFilterNode(config.node));
  return conditions;
}

export function isFilterActive(filter = DEFAULT_FILTER) {
  return describeFilter(filter).length > 0;
}

/** Unknown-value treatment, with the count of rows it affects (contract
 *  section 6). The count comes from a real evaluation of the filter. */
export function unknownTreatment(filter, omitted) {
  const config = { ...DEFAULT_FILTER, ...(filter || {}) };
  const counts = omitted || { unknown_excluded: 0, unknown_included: 0 };
  if (config.unknown_policy === "exclude") {
    return {
      policy: "exclude",
      text: "Rows where this filter cannot be evaluated are excluded from the view.",
      affected: counts.unknown_excluded,
      note: "Excluded rows are not hidden: they are counted here and remain reachable by clearing the filter.",
    };
  }
  return {
    policy: "include_with_warning",
    text: "Rows where this filter cannot be evaluated are included and flagged, because an unknown is not a negative finding.",
    affected: counts.unknown_included,
    note: "These rows are still shown; their criterion state is unknown.",
  };
}

/* =========================================================================
 * Criterion indicators
 * ========================================================================= */

export const CRITERION_RESULTS = Object.freeze({
  supported: { status: "good", label: "Supported", short: "Supported", title: "The processed document establishes this criterion." },
  not_found: { status: "neutral", label: NOT_FOUND_LABEL, short: NOT_FOUND_SHORT, title: NOT_FOUND_TITLE },
  unclear: { status: "warning", label: "Unclear", short: "Unclear", title: "The processed document is ambiguous on this criterion." },
  needs_manual_review: { status: "serious", label: "Needs manual review", short: "Manual review", title: "This criterion needs a person to read the source." },
});

export const NO_ASSESSMENT = Object.freeze({
  status: "unknown",
  label: UNKNOWN_LABEL,
  short: UNKNOWN_LABEL,
  title: "No assessment for this criterion is available in this payload.",
});

export function describeCriterionResult(result) {
  if (typeof result !== "string") return { ...NO_ASSESSMENT, key: "unknown" };
  const entry = CRITERION_RESULTS[result];
  if (!entry) return { ...NO_ASSESSMENT, key: "unknown" };
  return { ...entry, key: result };
}

/* =========================================================================
 * Decision control and save feedback
 * ========================================================================= */

/** Freeze reason, or null when disposition writes are allowed (contract
 *  section 5; PRD 8.3). */
export function dispositionFreezeReason(row) {
  if (!row) return null;
  if (row.pending_intent === "move_trash") return "Decision locked: a Move to Trash request is pending. Review or cancel the action to edit.";
  if (row.location === "trash") return "Decision locked: this file is in Trash. Restore it first.";
  if (row.disposition_frozen === true) return "Decision locked while a file action is pending. Review or cancel the action to edit.";
  return null;
}

/** One segmented control per row: exactly one disposition is ever selected. */
export function describeDecisionControl(row) {
  const reason = dispositionFreezeReason(row);
  const selected = DISPOSITIONS.includes(row && row.review_state) ? row.review_state : "unreviewed";
  return {
    selected,
    disabled: reason !== null,
    reason,
    options: DISPOSITIONS.map((value) => ({
      value,
      label: DISPOSITION_LABELS[value],
      checked: value === selected,
      disabled: reason !== null,
    })),
  };
}

/**
 * Exactly one save state per row: saved, saving, conflict, or failed.
 *
 * A failed or conflicted write is never reported as committed: `committed` is
 * true only for the saved state, and the caller keeps the row's previous value
 * until a retry succeeds.
 */
export function describeSaveState(entry, row) {
  const revision = row && typeof row.decision_revision === "number" ? row.decision_revision : null;
  const savedLabel = revision === null ? "Saved" : `Saved (revision ${revision})`;

  if (!entry) {
    return { state: "saved", label: savedLabel, detail: null, tone: "good", retry: false, committed: true };
  }
  switch (entry.state) {
    case "saving":
      return { state: "saving", label: "Saving...", detail: null, tone: "neutral", retry: false, committed: false };
    case "conflict": {
      const current = DISPOSITION_LABELS[entry.current_value] || UNKNOWN_LABEL;
      const actor = entry.current_actor ? String(entry.current_actor) : UNKNOWN_LABEL;
      return {
        state: "conflict",
        label: "Conflict",
        detail: `Not saved. The current value is ${current}, last set by ${actor}. Reload to work from the current value.`,
        tone: "warning",
        retry: false,
        committed: false,
      };
    }
    case "failed":
      return {
        state: "failed",
        label: "Failed",
        detail: entry.message ? `Not saved. ${entry.message}` : "Not saved. The write did not reach the helper.",
        tone: "critical",
        retry: true,
        committed: false,
      };
    default:
      return { state: "saved", label: savedLabel, detail: null, tone: "good", retry: false, committed: true };
  }
}

/* =========================================================================
 * File action column
 * ========================================================================= */

export function describeAction(row) {
  const intent = row && typeof row.pending_intent === "string" ? row.pending_intent : "none";
  const intentText = PENDING_INTENT_LABELS[intent] || `Pending action: ${intent}`;
  const actions = row && Array.isArray(row.file_actions) ? row.file_actions : [];
  const last = actions.length > 0 ? actions[actions.length - 1] : null;

  if (!last) {
    return { intent, intentText, lastText: "No file action has been executed", lastTone: "neutral", committed: false, state: null };
  }
  const state = typeof last.state === "string" ? last.state : "planned";
  const stateLabel = FILE_ACTION_STATE_LABELS[state] || `State: ${state}`;
  const kindLabel = FILE_ACTION_KIND_LABELS[last.kind] || (last.kind ? String(last.kind) : "File action");
  const destination = last.destination ? ` to ${last.destination}` : "";
  const errorCode = last.error_code ? ` (${last.error_code})` : "";
  const committed = COMMITTED_ACTION_STATES.includes(state);

  let tone = "neutral";
  if (committed) tone = "good";
  else if (state === "partial" || state === "needs_reconciliation") tone = "serious";
  else if (state === "blocked" || state === "failed") tone = "critical";

  return {
    intent,
    intentText,
    lastText: `${kindLabel}: ${stateLabel}${destination}${errorCode}`,
    lastTone: tone,
    committed,
    state,
  };
}

/* =========================================================================
 * Bulk selection: two distinct actions, immutable resolved set
 * ========================================================================= */

export function emptySelection() {
  return { scope: "page", frozen: false, pairs: [] };
}

export function selectionPairs(rows) {
  const list = Array.isArray(rows) ? rows : [];
  return list
    .filter((row) => row && typeof row.document_id === "string")
    .map((row) => ({ document_id: row.document_id, decision_revision: row.decision_revision }));
}

export function selectionKey(pair) {
  return pair ? `${pair.document_id}@${pair.decision_revision}` : "";
}

export function isRowSelected(selection, row) {
  if (!selection || !row) return false;
  return selection.pairs.some((pair) => pair.document_id === row.document_id);
}

/** Toggle one row. A frozen (resolved "all matching") selection is returned
 *  unchanged: it was fixed at confirmation time and must not be edited by a
 *  later row toggle. */
export function toggleSelection(selection, row) {
  const current = selection || emptySelection();
  if (current.frozen || !row || typeof row.document_id !== "string") return current;
  const exists = current.pairs.some((pair) => pair.document_id === row.document_id);
  const pairs = exists
    ? current.pairs.filter((pair) => pair.document_id !== row.document_id)
    : current.pairs.concat([{ document_id: row.document_id, decision_revision: row.decision_revision }]);
  return { scope: "page", frozen: false, pairs };
}

/** Union the rendered page into the selection. */
export function selectPage(selection, rows) {
  const current = selection || emptySelection();
  if (current.frozen) return current;
  const byId = new Map(current.pairs.map((pair) => [pair.document_id, pair]));
  for (const pair of selectionPairs(rows)) {
    if (!byId.has(pair.document_id)) byId.set(pair.document_id, pair);
  }
  return { scope: "page", frozen: false, pairs: [...byId.values()] };
}

/**
 * Resolve "select all matching results" to an immutable, explicit set.
 *
 * `rows` is a snapshot of the currently matching rows taken at confirmation
 * time. The returned set is frozen and copied: adding a submission or changing a
 * filter afterwards cannot extend it.
 */
export function resolveAllMatching(rows) {
  const byId = new Map();
  for (const pair of selectionPairs(rows)) {
    if (!byId.has(pair.document_id)) byId.set(pair.document_id, pair);
  }
  const pairs = [...byId.values()]
    .sort((a, b) => compareValues(a.document_id, b.document_id))
    .map((pair) => Object.freeze({ document_id: pair.document_id, decision_revision: pair.decision_revision }));
  return Object.freeze({ scope: "matching", frozen: true, pairs: Object.freeze(pairs) });
}

export function deselectAll() {
  return emptySelection();
}

/** Selected pairs whose document is not in the rendered page. */
export function hiddenSelectedPairs(selection, visibleRows) {
  if (!selection) return [];
  const visible = new Set(selectionPairs(visibleRows).map((pair) => pair.document_id));
  return selection.pairs.filter((pair) => !visible.has(pair.document_id));
}

export function describeSelection(selection, visibleRows) {
  const current = selection || emptySelection();
  const hidden = hiddenSelectedPairs(current, visibleRows);
  return {
    count: current.pairs.length,
    hidden: hidden.length,
    frozen: current.frozen === true,
    scope: current.scope,
    note: current.frozen
      ? "This selection was fixed when you confirmed it. Adding submissions or changing the filter will not extend it."
      : "",
    pairs: current.pairs,
  };
}

/* =========================================================================
 * Pagination
 * ========================================================================= */

export function paginate(rows, page = 1, pageSize = PAGE_SIZE_DEFAULT) {
  const list = Array.isArray(rows) ? rows : [];
  const size = Number.isFinite(pageSize) && pageSize > 0 ? Math.trunc(pageSize) : PAGE_SIZE_DEFAULT;
  const total = list.length;
  const pageCount = Math.max(1, Math.ceil(total / size));
  const requested = Number.isFinite(page) ? Math.trunc(page) : 1;
  const current = Math.min(Math.max(1, requested), pageCount);
  const start = (current - 1) * size;
  return {
    items: list.slice(start, start + size),
    page: current,
    pageSize: size,
    total,
    pageCount,
    start: total === 0 ? 0 : start + 1,
    end: Math.min(start + size, total),
    hasPrevious: current > 1,
    hasNext: current < pageCount,
  };
}

export function describePageSlice(slice) {
  if (!slice || slice.total === 0) return "No rows";
  return `Showing ${slice.start}-${slice.end} of ${slice.total} (page ${slice.page} of ${slice.pageCount})`;
}

/* =========================================================================
 * Mode resolution and the connected API surface
 * ========================================================================= */

export function parseJsonObject(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (trimmed === "") return null;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch (error) {
    return null;
  }
}

export function instanceIdFromHref(href, search) {
  const query = new URLSearchParams(search || "");
  const fromQuery = query.get("instance");
  if (fromQuery) return fromQuery;
  const match = /\/(?:api\/v1\/)?instances\/([^/?#]+)/.exec(String(href || ""));
  return match ? decodeURIComponent(match[1]) : null;
}

export function isFileOrigin(href) {
  return typeof href === "string" && href.slice(0, 5).toLowerCase() === "file:";
}

/**
 * Decide the delivery mode from the document alone.
 *
 * A file: origin is always snapshot mode, even when the payload is missing: a
 * file: document must never attempt a network request, so the only safe default
 * there is "snapshot, show the problem".
 */
export function resolveMode({ snapshotText, bootstrapText, href, search } = {}) {
  const snapshot = parseJsonObject(snapshotText);
  if (isFileOrigin(href)) {
    return { mode: "snapshot", snapshot, bootstrap: null, instance_id: null };
  }
  const bootstrap = parseJsonObject(bootstrapText);
  const declared = bootstrap && typeof bootstrap.mode === "string" ? bootstrap.mode : null;
  if (declared === "snapshot") {
    return { mode: "snapshot", snapshot, bootstrap: null, instance_id: null };
  }
  if (snapshot && snapshot.mode === "snapshot" && declared !== "connected") {
    return { mode: "snapshot", snapshot, bootstrap: null, instance_id: null };
  }
  const instanceId =
    (bootstrap && typeof bootstrap.instance_id === "string" && bootstrap.instance_id) ||
    instanceIdFromHref(href, search);
  return { mode: "connected", snapshot: null, bootstrap, instance_id: instanceId || null };
}

export function modeAllowsNetwork(mode) {
  return mode === "connected";
}

export function apiPath(apiBase, instanceId, suffix) {
  const base = String(apiBase || "/api/v1/instances").replace(/\/+$/, "");
  const id = encodeURIComponent(String(instanceId || ""));
  const tail = suffix ? (suffix.startsWith("/") ? suffix : `/${suffix}`) : "";
  return `${base}/${id}${tail}`;
}

export function documentPath(documentId) {
  return `/documents/${encodeURIComponent(String(documentId || ""))}`;
}

export function documentsQuery({ page, pageSize, sort, direction, filter } = {}) {
  const params = new URLSearchParams();
  if (page) params.set("page", String(page));
  if (pageSize) params.set("page_size", String(pageSize));
  if (sort) params.set("sort", String(sort));
  if (direction) params.set("direction", String(direction));
  if (filter && filter.search && filter.search.trim() !== "") params.set("filter", filter.search.trim());
  const query = params.toString();
  return query ? `/documents?${query}` : "/documents";
}

/** Identity comes from the session. An `actor` field is never built here. */
export function decisionWriteBody({ decision, expectedRevision }) {
  return { disposition: decision, expected_revision: expectedRevision };
}

export function bulkDecisionBody(selection, decision) {
  const pairs = selection && Array.isArray(selection.pairs) ? selection.pairs : [];
  return {
    items: pairs.map((pair) => ({
      document_id: pair.document_id,
      disposition: decision,
      expected_revision: pair.decision_revision,
    })),
  };
}

export function newIdempotencyKey(prefix = "idem") {
  const cryptoObject = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  let random;
  if (cryptoObject && typeof cryptoObject.randomUUID === "function") {
    random = cryptoObject.randomUUID().replace(/-/g, "");
  } else if (cryptoObject && typeof cryptoObject.getRandomValues === "function") {
    const buffer = new Uint8Array(16);
    cryptoObject.getRandomValues(buffer);
    random = Array.from(buffer, (byte) => byte.toString(16).padStart(2, "0")).join("");
  } else {
    random = `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
  }
  return `${prefix}_${random}`;
}

/* =========================================================================
 * Header rendering (the only string-building path in this module)
 * ========================================================================= */

/** Build the static header row from COLUMNS. Input is the frozen module
 *  constant; no payload value reaches this function. */
export function buildHeaderHtml() {
  return COLUMNS.map((column) => {
    const className = column.className ? ` class="${escapeHtml(column.className)}"` : "";
    const key = escapeHtml(column.key);
    if (!column.sortKey) {
      return `<th scope="col"${className} data-column="${key}">${escapeHtml(column.label)}</th>`;
    }
    return (
      `<th scope="col"${className} data-column="${key}" data-sortable="true" data-sort-key="${escapeHtml(column.sortKey)}" aria-sort="none">` +
      `<button type="button">${escapeHtml(column.label)}` +
      `<span class="rr-sort-indicator" aria-hidden="true"></span></button></th>`
    );
  }).join("");
}

/* =========================================================================
 * DOM application
 * ========================================================================= */

const DOM = {
  doc: null,
  state: {
    mode: "snapshot",
    instanceId: null,
    apiBase: "/api/v1/instances",
    role: "viewer",
    payload: null,
    rows: [],
    filter: { ...emptyFilter(), ...DEFAULT_SORT },
    selection: emptySelection(),
    page: 1,
    pageSize: PAGE_SIZE_DEFAULT,
    saveStates: new Map(),
    chat: [],
    chatBusy: false,
    bulkBusy: false,
    bulkMessage: "",
    bulkRetry: null,
    requisition: null,
    requisitionLoaded: false,
    requisitionRevision: null,
    requisitionBusy: false,
    requisitionDirty: false,
    requisitionMessage: "",
    proposal: null,
    plan: null,
    actionBusy: false,
    actionRetry: null,
  },
};

function byId(id) {
  return DOM.doc ? DOM.doc.getElementById(id) : null;
}

function setText(element, value) {
  if (!element) return;
  element.textContent = isUnknown(value) ? "" : String(value);
}

function setCount(element, value) {
  if (!element) return;
  const text = formatCount(value);
  if (text === null) {
    const marker = DOM.doc.createElement("span");
    marker.className = "rr-unknown";
    marker.title = UNKNOWN_TITLE;
    marker.textContent = UNKNOWN_LABEL;
    element.replaceChildren(marker);
    return;
  }
  element.textContent = text;
}

function make(tagName, className, text) {
  const node = DOM.doc.createElement(tagName);
  if (className) node.className = className;
  if (!isUnknown(text)) node.textContent = String(text);
  return node;
}

/** Render an unknown/blank/value through the distinct markers. */
function valueNode(value) {
  const described = describeValue(value);
  if (described.kind === "unknown") {
    const node = make("span", "rr-unknown", described.text);
    node.title = described.title;
    return node;
  }
  if (described.kind === "blank") {
    const node = make("span", "rr-blank", described.text);
    node.title = described.title;
    return node;
  }
  return make("span", null, described.text);
}

function announce(message, isError = false) {
  const status = byId("rr-status");
  const alert = byId("rr-alert");
  if (isError && alert) {
    alert.hidden = false;
    alert.textContent = message;
    if (status) status.textContent = "";
    return;
  }
  if (alert) alert.hidden = true;
  setText(status, message);
}

/**
 * Run a re-render and put keyboard focus back where it was.
 *
 * The table is redrawn wholesale, which detaches the focused control. Without
 * this a keyboard-only reviewer would be dropped back to the document body every
 * time a checkbox or decision radio is used (AT-39).
 */
function preserveFocus(action) {
  const active = DOM.doc.activeElement;
  const token = active && active.getAttribute ? active.getAttribute("data-focus-token") : null;
  const rowId = active && active.closest ? active.closest("tr")?.dataset.documentId : null;
  action();
  if (!token) return;
  const escaped = token.replace(/["\\]/g, "\\$&");
  const next = DOM.doc.querySelector(`[data-focus-token="${escaped}"]`);
  if (next && typeof next.focus === "function") next.focus();
  else if (rowId) {
    const fallback = DOM.doc.querySelector(`tr[data-document-id="${rowId.replace(/["\\]/g, "\\$&")}"] input[type="checkbox"]`);
    if (fallback && typeof fallback.focus === "function") fallback.focus();
  }
}

/* ------------------------------------------------------------ status strip - */

function renderHeader() {
  const payload = DOM.state.payload || {};
  const instance = payload.instance || {};
  setText(byId("rr-job-title"), instance.job_title ? String(instance.job_title) : "Resume review");
  setText(byId("rr-instance-id"), DOM.state.instanceId || instance.instance_id || UNKNOWN_LABEL);
  setCount(byId("rr-state-revision"), instance.state_revision);
  const lastAnalysis = formatTimestamp(instance.last_analysis_at);
  const lastAnalysisEl = byId("rr-last-analysis");
  if (lastAnalysisEl) {
    if (lastAnalysis === null) {
      const marker = make("span", "rr-unknown", UNKNOWN_LABEL);
      marker.title = "No analysis run has been recorded for this instance.";
      lastAnalysisEl.replaceChildren(marker);
    } else {
      lastAnalysisEl.textContent = lastAnalysis;
    }
  }

  const badge = byId("rr-mode-badge");
  if (badge) {
    badge.dataset.mode = DOM.state.mode;
    badge.textContent = DOM.state.mode === "connected" ? "Connected review" : "Snapshot (read only)";
  }
  const detail = byId("rr-mode-detail");
  if (DOM.state.mode === "snapshot") {
    const generated = formatTimestamp(payload.generated_at) || UNKNOWN_LABEL;
    setText(detail, `Snapshot generated ${generated}. Edits, chat, scan, and file actions are disabled.`);
  } else {
    setText(detail, `State comes from the helper. Role: ${DOM.state.role}.`);
  }

  const banner = byId("rr-snapshot-banner");
  if (banner) {
    if (DOM.state.mode === "snapshot") {
      banner.hidden = false;
      banner.replaceChildren();
      banner.appendChild(make("span", null, "Snapshot mode. "));
      banner.appendChild(
        make("span", null, `Generated ${formatTimestamp(payload.generated_at) || UNKNOWN_LABEL}. This file contains sensitive recruiting information; its visibility follows filesystem permissions.`)
      );
      const link = make("a", null, "Open connected review");
      link.href = "about:blank";
      link.title = "The helper prints the connected review address when it starts. This link carries no credential.";
      link.setAttribute("aria-disabled", "true");
      link.addEventListener("click", (event) => {
        event.preventDefault();
        announce("Open the connected review address printed by the helper. This snapshot carries no credential and no address.");
      });
      banner.appendChild(make("span", null, " "));
      banner.appendChild(link);
    } else {
      banner.hidden = true;
    }
  }

  const source = byId("rr-footer-source");
  const schemaVersion = payload.schema_version ? String(payload.schema_version) : UNKNOWN_LABEL;
  const appVersion = instance.app_version ? String(instance.app_version) : UNKNOWN_LABEL;
  setText(source, `Payload schema ${schemaVersion}. Helper version ${appVersion}. Storage mode ${instance.storage_mode || UNKNOWN_LABEL}.`);
}

function renderCounts(visibleRows) {
  const counts = (DOM.state.payload && DOM.state.payload.counts) || {};
  for (const key of ["total", "unreviewed", "keep", "reject", "hold"]) setCount(byId(`rr-overview-${key}`), counts[key]);
  for (const [key] of COUNT_FIELDS) {
    setCount(byId(`rr-count-${key.replace(/_/g, "-")}`), counts[key]);
  }
  const filtered = byId("rr-count-filtered");
  if (filtered) setCount(filtered, DOM.state.filteredTotal);
  setCount(byId("rr-count-page"), visibleRows.length);
  const omittedEl = byId("rr-count-omitted");
  if (omittedEl) {
    const omitted = DOM.state.omitted ? DOM.state.omitted.total : 0;
    omittedEl.textContent = String(omitted);
  }
  setCount(byId("rr-count-selected"), DOM.state.selection.pairs.length);
}

/* ------------------------------------------------------------------- table - */

function mountHeader() {
  const headRow = byId("rr-head-row");
  if (!headRow) return;
  // The single raw-markup assignment in this file. Its argument is
  // buildHeaderHtml(), which reads only the frozen COLUMNS constant; no payload
  // value reaches it, so this cannot become an injection path.
  headRow.innerHTML = buildHeaderHtml();
  headRow.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      const header = button.closest("th");
      const key = header ? header.dataset.sortKey : null;
      if (!key) return;
      const current = DOM.state.filter;
      const direction = current.sort === key && current.direction === "asc" ? "desc" : "asc";
      setFilter({ ...current, sort: key, direction });
    });
  });
  updateSortIndicators();
}

function updateSortIndicators() {
  const headRow = byId("rr-head-row");
  if (!headRow) return;
  headRow.querySelectorAll("th").forEach((header) => {
    const indicator = header.querySelector(".rr-sort-indicator");
    const active = header.dataset.sortKey === DOM.state.filter.sort;
    if (header.hasAttribute("aria-sort")) {
      header.setAttribute("aria-sort", active ? (DOM.state.filter.direction === "desc" ? "descending" : "ascending") : "none");
    }
    if (indicator) indicator.textContent = active ? (DOM.state.filter.direction === "desc" ? "v" : "^") : "";
  });
}

function renderSubmissionCell(row) {
  const cell = make("td");
  cell.dataset.label = "Submission";
  const name = row.display_name ? String(row.display_name) : String(row.original_filename || UNKNOWN_LABEL);
  const button = make("button", "rr-name", name);
  button.type = "button";
  button.addEventListener("click", () => openDetail(row.document_id));
  cell.appendChild(button);
  const pathText = row.current_rel_path ? String(row.current_rel_path) : null;
  if (pathText) cell.appendChild(make("span", "rr-path", pathText));
  else cell.appendChild(valueNode(null));
  return cell;
}

function renderSummaryCell(row) {
  const cell = make("td");
  cell.dataset.label = "Summary";
  const summary = make("span", "rr-line-clamp");
  if (isUnknown(row.summary_text) || row.summary_text === "") {
    cell.appendChild(valueNode(row.summary_text));
  } else {
    summary.textContent = String(row.summary_text);
    cell.appendChild(summary);
  }
  if (row.summary_stale === true) {
    const stale = make("span", "rr-save-detail", "Summary is stale: the source or criteria changed after it was generated.");
    cell.appendChild(stale);
  }
  return cell;
}

function renderEvidenceCell(row) {
  const cell = make("td");
  cell.dataset.label = "Relevant evidence";
  const criteria = (DOM.state.payload && DOM.state.payload.instance && DOM.state.payload.instance.criteria) || [];
  const list = make("ul", "rr-evidence-list");
  if (!Array.isArray(criteria) || criteria.length === 0) {
    list.appendChild(make("li", null, "No approved criteria for this instance"));
    cell.appendChild(list);
    return cell;
  }
  for (const criterion of criteria) {
    const described = describeCriterionResult(criterionAssessment(row, criterion.criterion_id)?.result);
    const item = make("li", "rr-ind");
    item.dataset.status = described.status;
    item.appendChild(make("span", "rr-ind-text", described.short));
    item.appendChild(make("span", "rr-ind-criterion", criterion.criterion_id));
    item.title = `${criterion.definition || criterion.criterion_id}: ${described.label}. ${described.title}`;
    list.appendChild(item);
  }
  cell.appendChild(list);
  return cell;
}

function renderTasksCell(row) {
  const cell = make("td");
  cell.dataset.label = "Review tasks";
  const count = typeof row.open_task_count === "number" ? row.open_task_count : null;
  if (count === null) {
    cell.appendChild(valueNode(null));
  } else {
    cell.appendChild(make("span", null, count === 1 ? "1 open task" : `${count} open tasks`));
  }
  if (row.task_warning === true) {
    const flag = make("span", "rr-flag", "Needs attention");
    flag.dataset.status = "serious";
    flag.title = "At least one open review task is marked needing attention. This is not a negative assessment of the applicant.";
    cell.appendChild(flag);
  }
  return cell;
}

function renderDecisionCell(row) {
  const cell = make("td");
  cell.dataset.label = "Decision";
  const control = describeDecisionControl(row);
  const fieldset = make("fieldset", "rr-decision");
  fieldset.appendChild(make("legend", null, "Disposition"));
  const group = make("div", "rr-seg");
  const groupName = `rr-decision-${row.document_id}`;
  for (const option of control.options) {
    const label = make("label");
    const input = make("input");
    input.type = "radio";
    input.name = groupName;
    input.value = option.value;
    input.checked = option.checked;
    input.disabled = option.disabled || !canEditReview() || DOM.state.bulkBusy;
    input.setAttribute("data-focus-token", `${row.document_id}:decision:${option.value}`);
    input.addEventListener("change", () => writeDecision(row, option.value));
    label.appendChild(input);
    label.appendChild(make("span", null, option.label));
    group.appendChild(label);
  }
  fieldset.appendChild(group);
  if (control.reason) fieldset.appendChild(make("span", "rr-frozen", control.reason));
  cell.appendChild(fieldset);

  const save = describeSaveState(DOM.state.saveStates.get(row.document_id), row);
  const saveNode = make("span", "rr-save");
  saveNode.dataset.state = save.state;
  saveNode.appendChild(make("span", null, save.label));
  cell.appendChild(saveNode);
  if (save.detail) cell.appendChild(make("span", "rr-save-detail", save.detail));
  if (save.retry) {
    const retry = make("button", null, "Retry");
    retry.type = "button";
    retry.setAttribute("data-focus-token", `${row.document_id}:retry`);
    retry.addEventListener("click", () => writeDecision(row, control.selected, true));
    cell.appendChild(retry);
  }
  return cell;
}

function renderActionCell(row) {
  const cell = make("td");
  cell.dataset.label = "File action";
  const action = describeAction(row);
  cell.appendChild(make("span", "rr-action-intent", action.intentText));
  const last = make("span", "rr-action-last", action.lastText);
  last.dataset.tone = action.lastTone;
  cell.appendChild(last);
  return cell;
}

function renderAccessCell(row) {
  const cell = make("td");
  cell.dataset.label = "File access";
  const wrap = make("div", "rr-access");

  let link = make("a", null, "Open original");
  const relative = safeRelativeLink(row.document_link);
  if (DOM.state.mode === "connected") {
    link.href = apiPath(DOM.state.apiBase, DOM.state.instanceId, `/documents/${encodeURIComponent(String(row.document_id))}/original`);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  } else if (relative) {
    link.href = relative;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "If the browser blocks a file: link, use the path shown in the detail drawer.";
  } else {
    link = make("button", null, "Original unavailable");
    link.type = "button";
    link.disabled = true;
    link.title = "No original file link is attached. Open detail to see the recorded path.";
  }
  wrap.appendChild(link);

  const detailButton = make("button", null, "Open detail");
  detailButton.type = "button";
  detailButton.addEventListener("click", () => openDetail(row.document_id));
  wrap.appendChild(detailButton);

  if (relative && !isUnknown(row.current_rel_path)) {
    wrap.appendChild(make("span", "rr-path", String(row.current_rel_path)));
  }
  cell.appendChild(wrap);
  return cell;
}

function renderRows() {
  const body = byId("rr-rows");
  if (!body) return;
  const slice = paginate(DOM.state.visibleRows, DOM.state.page, DOM.state.pageSize);
  DOM.state.page = slice.page;
  body.replaceChildren();

  for (const row of slice.items) {
    const tr = make("tr");
    tr.dataset.documentId = row.document_id;
    tr.dataset.selected = isRowSelected(DOM.state.selection, row) ? "true" : "false";
    tr.dataset.locked = dispositionFreezeReason(row) ? "true" : "false";

    const selectCell = make("td", "rr-col-select");
    selectCell.dataset.label = "Select";
    const checkbox = make("input");
    checkbox.type = "checkbox";
    checkbox.checked = isRowSelected(DOM.state.selection, row);
    checkbox.disabled = DOM.state.selection.frozen === true || DOM.state.bulkBusy;
    checkbox.setAttribute("data-focus-token", `${row.document_id}:select`);
    checkbox.setAttribute("aria-label", `Select ${row.display_name || row.original_filename || row.document_id} for a bulk action. This is not a decision.`);
    checkbox.addEventListener("change", () => {
      preserveFocus(() => {
        DOM.state.selection = toggleSelection(DOM.state.selection, row);
        renderTable();
      });
    });
    selectCell.appendChild(checkbox);
    tr.appendChild(selectCell);

    tr.appendChild(renderSubmissionCell(row));
    tr.appendChild(renderSummaryCell(row));
    tr.appendChild(renderEvidenceCell(row));
    tr.appendChild(renderTasksCell(row));
    tr.appendChild(renderDecisionCell(row));
    tr.appendChild(renderActionCell(row));
    tr.appendChild(renderAccessCell(row));
    body.appendChild(tr);
  }

  const empty = byId("rr-empty");
  if (empty) {
    if (slice.items.length === 0) {
      empty.hidden = false;
      const omitted = DOM.state.omitted ? DOM.state.omitted.total : 0;
      empty.textContent = DOM.state.rows.length === 0
        ? "This payload contains no submissions."
        : `No submissions match the current filters. ${omitted} of ${DOM.state.rows.length} loaded rows are hidden by them.`;
    } else {
      empty.hidden = true;
    }
  }

  setText(byId("rr-page-info"), describePageSlice(slice));
  setText(byId("rr-page-number"), `Page ${slice.page} of ${slice.pageCount}`);
  const prev = byId("rr-btn-prev");
  const next = byId("rr-btn-next");
  if (prev) prev.disabled = !slice.hasPrevious;
  if (next) next.disabled = !slice.hasNext;
}

function renderSelectionUi() {
  const summary = describeSelection(DOM.state.selection, DOM.state.visibleRows);
  setText(byId("rr-sel-count"), `${summary.count} selected`);
  setText(byId("rr-sel-hidden"), `${summary.hidden} hidden by the current filter`);
  setText(byId("rr-sel-note"), summary.note);
  setCount(byId("rr-count-selected"), summary.count);
  const matching = byId("rr-btn-select-matching");
  if (matching) matching.disabled = DOM.state.filteredTotal === 0 || DOM.state.bulkBusy;
  const pageButton = byId("rr-btn-select-page");
  if (pageButton) pageButton.disabled = DOM.state.selection.frozen === true || DOM.state.bulkBusy;
  const deselect = byId("rr-btn-deselect");
  if (deselect) deselect.disabled = DOM.state.bulkBusy;
  renderBulkActions();
}

function canEditReview() {
  return DOM.state.mode === "connected" && ["reviewer", "administrator"].includes(DOM.state.role);
}

function renderBulkActions() {
  const bar = byId("rr-bulk-bar");
  if (!bar) return;
  const pairs = DOM.state.selection.pairs;
  const count = pairs.length;
  const selectedIds = new Set(pairs.map((pair) => pair.document_id));
  const locked = DOM.state.rows.filter((row) => selectedIds.has(row.document_id) && dispositionFreezeReason(row)).length;
  const saving = pairs.some((pair) => DOM.state.saveStates.get(pair.document_id)?.state === "saving");
  const hidden = hiddenSelectedPairs(DOM.state.selection, DOM.state.visibleRows).length;
  bar.dataset.active = String(count > 0);
  bar.dataset.saving = String(DOM.state.bulkBusy);
  bar.setAttribute("aria-busy", String(DOM.state.bulkBusy));
  bar.hidden = count === 0 && !DOM.state.bulkBusy;
  setText(byId("rr-bulk-count"), count ? `${count} candidate${count === 1 ? "" : "s"} selected` : "No candidates selected");
  setText(byId("rr-bulk-scope"), count
    ? hidden ? `Includes ${hidden} selected candidate${hidden === 1 ? "" : "s"} hidden by your filter.` : "Apply a decision to this exact selection."
    : "Select candidates using the checkboxes below.");
  for (const button of bar.querySelectorAll("[data-bulk-decision]")) {
    button.disabled = !canEditReview() || count === 0 || locked > 0 || saving || DOM.state.bulkBusy;
  }
  const message = !canEditReview()
    ? DOM.state.mode === "snapshot" ? "Read-only snapshot. Open the connected review page to save decisions." : "A reviewer or administrator session is required."
    : locked ? `${locked} selected candidate${locked === 1 ? " is" : "s are"} locked by a file action. Deselect locked candidates to continue.`
    : saving ? "Waiting for the selected candidates' decisions to finish saving."
    : DOM.state.bulkMessage || "Keep / advance marks candidates for further review. Decisions do not move files.";
  setText(byId("rr-bulk-status"), message);
}

function renderChips() {
  setText(byId("rr-filter-summary"), isFilterActive(DOM.state.filter) ? "Filters and tools \u00b7 Filter active" : "Filters and tools");
  const list = byId("rr-filter-chips");
  if (!list) return;
  list.replaceChildren();
  for (const condition of describeFilter(DOM.state.filter)) {
    list.appendChild(make("li", null, condition));
  }
}

function renderToolbarState() {
  renderChatControls();
  renderRequisitionControls();
  const filter = DOM.state.filter;
  const search = byId("rr-search");
  if (search && search.value !== filter.search) search.value = filter.search;
  for (const [id, value] of [
    ["rr-filter-location", filter.location],
    ["rr-filter-processing", filter.processing_state],
    ["rr-filter-review", filter.review_state],
    ["rr-filter-tasks", filter.task],
    ["rr-sort", filter.sort],
    ["rr-sort-direction", filter.direction],
  ]) {
    const control = byId(id);
    if (control) control.value = value;
  }
  const snapshotOps = byId("rr-ops-snapshot");
  const connectedOps = byId("rr-ops-connected");
  const isSnapshot = DOM.state.mode === "snapshot";
  if (snapshotOps) snapshotOps.hidden = !isSnapshot;
  if (connectedOps) connectedOps.hidden = isSnapshot;
  const roleNote = byId("rr-ops-role");
  if (roleNote) {
    const role = DOM.state.role;
    roleNote.textContent = isSnapshot
      ? "Read-only snapshot."
      : role === "viewer"
        ? "Viewer role: decisions and file actions are disabled."
        : `Role: ${role}.`;
  }
}

function renderTable() {
  DOM.state.visibleRows = sortRows(DOM.state.filteredRows, DOM.state.filter.sort, DOM.state.filter.direction);
  renderRows();
  renderSelectionUi();
  renderCounts(DOM.state.visibleRows);
  updateSortIndicators();
}

function recompute() {
  const result = applyFilter(DOM.state.rows, DOM.state.filter);
  DOM.state.filteredRows = result.rows;
  DOM.state.omitted = result.omitted;
  DOM.state.filteredTotal = result.rows.length;
  renderTable();
  renderChips();
}

function setFilter(filter) {
  DOM.state.filter = { ...DOM.state.filter, ...filter };
  DOM.state.page = 1;
  renderToolbarState();
  recompute();
}

/* ------------------------------------------------------------------ drawer - */

function definitionRow(list, term, value) {
  list.appendChild(make("dt", null, term));
  const dd = make("dd");
  if (value instanceof Node) dd.appendChild(value);
  else if (typeof value === "string") dd.textContent = value;
  else dd.appendChild(valueNode(value));
  list.appendChild(dd);
}

function openDetail(documentId) {
  const row = DOM.state.rows.find((item) => item.document_id === documentId);
  const drawer = byId("rr-detail");
  const body = byId("rr-detail-body");
  if (!row || !drawer || !body) return;
  body.replaceChildren();

  setText(byId("rr-detail-title"), row.display_name ? String(row.display_name) : String(row.original_filename || row.document_id));

  const list = make("dl");
  definitionRow(list, "Display name", row.display_name);
  definitionRow(list, "Original filename", row.original_filename);
  definitionRow(list, "Current path in the job folder", row.current_rel_path);
  definitionRow(list, "Document link", row.document_link);
  definitionRow(list, "Media type", row.media_type);
  definitionRow(list, "Size", formatBytes(row.size_bytes) === null ? null : `${formatBytes(row.size_bytes)} (${row.size_bytes} bytes)`);
  definitionRow(list, "Ingested", formatTimestamp(row.ingested_at) || null);
  definitionRow(list, "Submitted", isUnknown(row.submitted_at) ? null : formatTimestamp(row.submitted_at) || row.submitted_at);
  definitionRow(list, "Processing state", row.processing_state);
  definitionRow(list, "Processing detail", row.processing_detail);
  definitionRow(list, "Processing limitations", row.warnings && row.warnings.length > 0 ? row.warnings.map((warning) => `${warning.code}: ${warning.message}`).join(" | ") : null);
  definitionRow(list, "Actual location", row.location);
  definitionRow(list, "Reviewer decision", DISPOSITION_LABELS[row.review_state] || row.review_state);
  definitionRow(list, "Decision revision", row.decision_revision);
  definitionRow(list, "Decision needs recheck", row.decision_needs_recheck === true ? "Yes" : row.decision_needs_recheck === false ? "No" : null);
  definitionRow(list, "Recheck reason", row.recheck_reason);
  definitionRow(list, "Pending action intent", PENDING_INTENT_LABELS[row.pending_intent] || row.pending_intent);
  definitionRow(list, "Duplicate content", row.duplicate_content === true ? `Yes, duplicate of ${row.duplicate_of || UNKNOWN_LABEL}` : row.duplicate_content === false ? "No" : null);
  body.appendChild(list);

  const summaryHeading = make("h3", null, "Summary");
  body.appendChild(summaryHeading);
  const summaryText = make("p", null, isUnknown(row.summary_text) || row.summary_text === "" ? "No summary is available for this document." : String(row.summary_text));
  body.appendChild(summaryText);

  const evidenceHeading = make("h3", null, "Criterion by criterion");
  body.appendChild(evidenceHeading);
  const criteria = (DOM.state.payload && DOM.state.payload.instance && DOM.state.payload.instance.criteria) || [];
  const evidenceList = make("dl");
  for (const criterion of criteria) {
    const assessment = criterionAssessment(row, criterion.criterion_id);
    const described = describeCriterionResult(assessment ? assessment.result : null);
    const marker = make("span", "rr-ind");
    marker.dataset.status = described.status;
    marker.appendChild(make("span", "rr-ind-text", described.label));
    definitionRow(evidenceList, criterion.definition ? String(criterion.definition) : criterion.criterion_id, marker);
    if (assessment && assessment.explanation) {
      const note = make("dd", "rr-save-detail", String(assessment.explanation));
      evidenceList.appendChild(note);
    }
    const evidence = Array.isArray(row.evidence) ? row.evidence.filter((item) => item.criterion_id === criterion.criterion_id) : [];
    for (const item of evidence) {
      const quote = make("blockquote", "rr-quote", isUnknown(item.quote) ? "" : String(item.quote));
      const locator = item.locator && !isUnknown(item.locator.page) ? `page ${item.locator.page}` : "locator unknown";
      const meta = make("span", "rr-save-detail", `${locator}; span ${item.span_id || UNKNOWN_LABEL}; validation ${item.validation || UNKNOWN_LABEL}`);
      const holder = make("dd");
      holder.appendChild(quote);
      holder.appendChild(meta);
      evidenceList.appendChild(holder);
    }
  }
  body.appendChild(evidenceList);

  const noteHeading = make("h3", null, "Human notes");
  body.appendChild(noteHeading);
  const notes = Array.isArray(row.notes) ? row.notes : [];
  if (notes.length === 0) body.appendChild(make("p", null, "No human notes on this document."));
  for (const note of notes) {
    body.appendChild(make("p", "rr-chat-text", `${note.author || UNKNOWN_LABEL} (${formatTimestamp(note.updated_at) || UNKNOWN_LABEL}): ${note.body || ""}`));
  }

  const taskHeading = make("h3", null, "Review tasks");
  body.appendChild(taskHeading);
  const tasks = Array.isArray(row.tasks) ? row.tasks : [];
  if (tasks.length === 0) body.appendChild(make("p", null, "No review tasks on this document."));
  for (const task of tasks) {
    body.appendChild(make("p", null, `[${task.state || UNKNOWN_LABEL}] ${task.title || ""} (origin: ${task.origin || UNKNOWN_LABEL}${task.criterion_id ? `, criterion ${task.criterion_id}` : ""})${task.severity === "attention" ? " - needs attention" : ""}`));
  }

  const historyHeading = make("h3", null, "Decision history");
  body.appendChild(historyHeading);
  const history = Array.isArray(row.decision_history) ? row.decision_history : [];
  if (history.length === 0) body.appendChild(make("p", null, "No recorded decision changes."));
  for (const entry of history) {
    body.appendChild(make("p", null, `${DISPOSITION_LABELS[entry.disposition] || entry.disposition} by ${entry.actor || UNKNOWN_LABEL} at ${formatTimestamp(entry.at) || UNKNOWN_LABEL} (revision ${entry.decision_revision})`));
  }

  const actionsHeading = make("h3", null, "File action history");
  body.appendChild(actionsHeading);
  const actions = Array.isArray(row.file_actions) ? row.file_actions : [];
  if (actions.length === 0) body.appendChild(make("p", null, "No file action has been executed for this document."));
  for (const action of actions) {
    body.appendChild(make("p", null, `${FILE_ACTION_KIND_LABELS[action.kind] || action.kind}: ${FILE_ACTION_STATE_LABELS[action.state] || action.state}${action.destination ? ` to ${action.destination}` : ""}${action.error_code ? ` (${action.error_code})` : ""} at ${formatTimestamp(action.at) || UNKNOWN_LABEL}`));
  }

  drawer.hidden = false;
  const close = byId("rr-detail-close");
  if (close) close.focus();
}

function closeDrawer(id) {
  const drawer = byId(id);
  if (drawer) drawer.hidden = true;
  if (id === "rr-chat") {
    const toggle = byId("rr-btn-chat");
    if (toggle) { toggle.setAttribute("aria-expanded", "false"); toggle.focus(); }
  }
}

/* ------------------------------------------------------------ requisition - */

export function requisitionWriteBody({ title, description_text, source_reference, expected_revision }) {
  const name = String(title || "").trim();
  const description = String(description_text || "").trim();
  const source = String(source_reference || "").trim();
  if (!name || name.length > 200) throw new Error("Enter a requisition title of up to 200 characters.");
  if (!description || description.length > 20000) throw new Error("Enter the requisition text (up to 20,000 characters).");
  if (source) {
    let parsed;
    try { parsed = new URL(source); } catch (error) { throw new Error("Use a complete HTTP or HTTPS source link."); }
    if (source.length > 2048 || !["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || /[\u0000-\u0020\u007f]/.test(source)) {
      throw new Error("Use an HTTP or HTTPS source link without credentials or spaces.");
    }
  }
  if (!Number.isInteger(expected_revision) || expected_revision < 0) throw new Error("Load the saved reference before editing it.");
  return { title: name, description_text: description, source_reference: source || null, expected_revision };
}

function requisitionDraft() {
  return requisitionWriteBody({
    title: byId("rr-requisition-title")?.value,
    description_text: byId("rr-requisition-text")?.value,
    source_reference: byId("rr-requisition-source")?.value,
    expected_revision: DOM.state.requisitionRevision,
  });
}

function requisitionStatus(message, isError = false) {
  DOM.state.requisitionMessage = message;
  const status = byId("rr-requisition-status");
  if (status) { status.textContent = message; status.dataset.error = String(isError); }
}

function fillRequisition(record) {
  for (const [id, value] of [
    ["rr-requisition-title", record?.title || ""],
    ["rr-requisition-text", record?.description_text || ""],
    ["rr-requisition-source", record?.source_reference || ""],
  ]) {
    const input = byId(id);
    if (input) input.value = value;
  }
  const file = byId("rr-requisition-file");
  if (file) file.value = "";
  DOM.state.requisitionDirty = false;
}

function renderRequisitionControls() {
  const form = byId("rr-requisition-form");
  if (!form) return;
  const enabled = canEditReview() && DOM.state.requisitionLoaded && !DOM.state.requisitionBusy;
  for (const input of form.querySelectorAll("input, textarea, button")) input.disabled = !enabled;
  const reload = byId("rr-requisition-reload");
  if (reload) reload.disabled = DOM.state.mode !== "connected" || DOM.state.requisitionBusy;
  form.setAttribute("aria-busy", String(DOM.state.requisitionBusy));
  const record = DOM.state.requisition;
  setText(byId("rr-requisition-badge"), DOM.state.requisitionDirty ? "Unsaved changes" : record ? "Reference saved" : DOM.state.requisitionLoaded ? "No reference yet" : "Not loaded");
  setText(byId("rr-requisition-summary"), record
    ? `${record.title} \u00b7 Available to OpenClaw chat. Approved review criteria remain in effect.`
    : DOM.state.mode === "snapshot" ? "Open the connected review page to view or save this folder's requisition reference."
      : "Give OpenClaw the job requirements to reference in this folder.");
  if (!DOM.state.requisitionMessage) requisitionStatus(DOM.state.mode === "snapshot"
    ? "Read-only snapshot. The requisition editor is available in connected review."
    : !canEditReview() ? "A reviewer or administrator session is required to edit the reference."
      : "Paste or import the requirements, then save the reference.");
}

async function loadRequisition(discardEdits = false) {
  if (DOM.state.mode !== "connected" || DOM.state.requisitionBusy) return;
  if (DOM.state.requisitionDirty && !discardEdits) return;
  DOM.state.requisitionBusy = true;
  renderRequisitionControls();
  try {
    const envelope = await apiRequest("GET", "/requisition");
    if (!envelope?.data || !("requisition" in envelope.data) || !Number.isInteger(envelope.state_revision)) throw new Error("The helper returned an invalid requisition response.");
    DOM.state.requisition = envelope.data.requisition;
    DOM.state.requisitionRevision = envelope.state_revision;
    DOM.state.requisitionLoaded = true;
    fillRequisition(DOM.state.requisition);
    if (DOM.state.requisition && DOM.state.payload?.instance) {
      DOM.state.payload.instance.job_title = DOM.state.requisition.title;
      renderHeader();
    }
    requisitionStatus(!canEditReview() ? "Reference is read only for this session." : DOM.state.requisition ? "Saved reference loaded. Changes are used in the next OpenClaw chat turn." : "Paste or import the requirements, then save the reference.");
  } catch (error) {
    requisitionStatus(`Could not load the reference: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.requisitionBusy = false;
    renderRequisitionControls();
  }
}

async function saveRequisition() {
  if (!canEditReview() || !DOM.state.requisitionLoaded || DOM.state.requisitionBusy) return;
  let body;
  try { body = requisitionDraft(); } catch (error) { requisitionStatus(error.message, true); return; }
  DOM.state.requisitionBusy = true;
  requisitionStatus("Saving the requisition reference...");
  renderRequisitionControls();
  try {
    const envelope = await apiRequest("PUT", "/requisition", { body });
    const record = envelope?.data?.requisition;
    if (!record || !Number.isInteger(envelope.state_revision)) throw new Error("The helper did not confirm the saved reference.");
    DOM.state.requisition = record;
    DOM.state.requisitionRevision = envelope.state_revision;
    DOM.state.requisitionDirty = false;
    if (DOM.state.payload?.instance) {
      DOM.state.payload.instance.job_title = record.title;
      DOM.state.payload.instance.state_revision = envelope.state_revision;
      renderHeader();
    }
    requisitionStatus("Reference saved. OpenClaw will receive it on the next chat turn. Approved criteria are unchanged.");
  } catch (error) {
    requisitionStatus(error.status === 409
      ? "The folder changed since this reference was loaded. Your draft is retained. Copy your edits before reloading the saved reference."
      : `Reference not confirmed: ${describeApiError(error)}. Your draft is retained.`, true);
  } finally {
    DOM.state.requisitionBusy = false;
    renderRequisitionControls();
  }
}

async function importRequisitionFile(file) {
  if (!file || !canEditReview() || DOM.state.requisitionBusy) return;
  try {
    if (!/\.(txt|md)$/i.test(file.name)) throw new Error("Import a .txt or .md file. Paste the text from a PDF or Word document.");
    if (file.size > 80000) throw new Error("The text file exceeds 80 KB. Paste the relevant requirements instead.");
    const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    if (!text.trim() || text.length > 20000 || text.includes("\u0000")) throw new Error("Use a non-empty text file with up to 20,000 characters.");
    byId("rr-requisition-text").value = text;
    if (!byId("rr-requisition-title").value.trim()) byId("rr-requisition-title").value = file.name.replace(/\.(txt|md)$/i, "").slice(0, 200);
    DOM.state.requisitionDirty = true;
    requisitionStatus(`Imported ${file.name} into the draft. Review the text, then choose Save reference.`);
  } catch (error) {
    requisitionStatus(error instanceof TypeError ? "The file must use UTF-8 text encoding." : error.message, true);
  }
  renderRequisitionControls();
}

/* ------------------------------------------------------------- chat panel - */

function renderProposal(proposal) {
  const container = make("div", "rr-chat-block");
  container.appendChild(make("h4", null, "Interpreted conditions (not applied yet)"));
  const list = make("ul", "rr-chat-conditions");
  for (const condition of proposal.conditions) {
    list.appendChild(make("li", null, condition));
  }
  container.appendChild(list);

  container.appendChild(make("h4", null, "Unknown-value treatment"));
  container.appendChild(make("p", null, `${proposal.treatment.text} Rows affected: ${proposal.treatment.affected}.`));
  container.appendChild(make("p", "rr-save-detail", proposal.treatment.note));

  container.appendChild(make("h4", null, "Omitted rows"));
  container.appendChild(make("p", null, `This filter would omit ${proposal.omittedTotal} of ${DOM.state.rows.length} loaded rows.`));

  const controls = make("p", "rr-ops");
  const apply = make("button", null, "Apply this filter");
  apply.type = "button";
  apply.addEventListener("click", () => {
    setFilter({ ...DOM.state.filter, node: proposal.node, unknown_policy: proposal.policy });
    announce(`Filter applied. View changed only; no decision and no file was changed. ${proposal.omittedTotal} rows omitted.`);
  });
  const dismiss = make("button", null, "Do not apply");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    DOM.state.proposal = null;
    renderChat();
  });
  controls.appendChild(apply);
  controls.appendChild(dismiss);
  container.appendChild(controls);
  return container;
}

function renderChat() {
  const log = byId("rr-chat-log");
  if (!log) return;
  log.replaceChildren();
  for (const turn of DOM.state.chat) {
    const item = make("li", "rr-chat-turn");
    item.dataset.role = turn.role;
    item.appendChild(make("p", "rr-chat-role", turn.role === "reviewer" ? "You" : "Helper"));
    item.appendChild(make("p", "rr-chat-text", turn.text));
    if (turn.coverage) item.appendChild(make("p", "rr-hint", turn.coverage));
    for (const warning of turn.warnings || []) item.appendChild(make("p", "rr-hint", warning));
    for (const citation of turn.citations || []) {
      item.appendChild(make("p", "rr-hint", `${citation.document_id}: ${citation.quote}`));
    }
    if (turn.proposal) item.appendChild(renderProposal(turn.proposal));
    log.appendChild(item);
  }
}

export function chatRequestBody(message, documentIds = []) {
  const text = String(message || "").trim();
  if (!text || text.length > 20000) throw new Error("Enter feedback of 1 to 20,000 characters.");
  const ids = [...new Set(documentIds)];
  if (ids.length > 50) throw new Error("Select at most 50 submissions, or clear the selection to ask about the folder.");
  return { message: text, document_ids: ids };
}

function renderChatControls() {
  const allowed = DOM.state.mode === "connected" && ["reviewer", "administrator"].includes(DOM.state.role);
  for (const id of ["rr-feedback-input", "rr-feedback-send", "rr-chat-input", "rr-chat-send"]) {
    const control = byId(id);
    if (control) control.disabled = !allowed || DOM.state.chatBusy;
  }
  for (const id of ["rr-feedback-form", "rr-chat-form"]) {
    const form = byId(id);
    if (form) form.setAttribute("aria-busy", String(DOM.state.chatBusy));
  }
  if (!allowed) setText(byId("rr-feedback-status"), DOM.state.mode === "snapshot"
    ? "Read-only snapshot. Open the connected review page to send feedback."
    : "A reviewer or administrator session is required to send feedback.");
  const status = byId("rr-feedback-status");
  if (allowed && status && status.textContent === "Connect as a reviewer to send feedback.") {
    setText(status, "Applies to this job folder, regardless of the current selection.");
  }
}

async function sendChat(message, { wholeFolder = false } = {}) {
  const text = String(message || "").trim();
  if (!text || DOM.state.chatBusy) return false;
  if (DOM.state.mode !== "connected" || !["reviewer", "administrator"].includes(DOM.state.role)) return false;
  let body;
  try {
    body = chatRequestBody(text, wholeFolder ? [] : DOM.state.selection.pairs.map((pair) => pair.document_id));
  } catch (error) {
    announce(error.message, true);
    setText(byId("rr-feedback-status"), error.message);
    return false;
  }
  DOM.state.chatBusy = true;
  renderChatControls();
  DOM.state.chat.push({ role: "reviewer", text });
  renderChat();
  try {
    const envelope = await apiRequest("POST", "/chat", {
      body,
      idempotencyKey: newIdempotencyKey("chat"),
    });
    const data = (envelope && envelope.data) || {};
    const explanation = data.explanation || data.answer || "The helper returned no explanation.";
    const filterNode = data.filter || data.filter_proposal || null;
    let proposal = null;
    if (filterNode && filterNode.expression) {
      const preview = applyFilter(DOM.state.rows, { ...DOM.state.filter, node: filterNode.expression, unknown_policy: filterNode.unknown_policy || "include_with_warning" });
      proposal = {
        node: filterNode.expression,
        policy: filterNode.unknown_policy || "include_with_warning",
        conditions: describeFilterNode(filterNode.expression),
        treatment: unknownTreatment({ unknown_policy: filterNode.unknown_policy }, preview.omitted),
        omittedTotal: preview.omitted.total,
      };
    }
    DOM.state.chat.push({ role: "helper", text: String(explanation), proposal,
      coverage: data.coverage && data.coverage.message,
      citations: Array.isArray(data.citations) ? data.citations : [],
      warnings: [...new Set([...(Array.isArray(envelope.warnings) ? envelope.warnings : []), ...(Array.isArray(data.warnings) ? data.warnings : [])]
        .map((warning) => typeof warning === "string" ? warning : warning.message || warning.code).filter(Boolean))],
    });
    renderChat();
    return true;
  } catch (error) {
    const reason = error.status === 404 ? "OpenClaw chat is not configured on this helper." : describeApiError(error);
    DOM.state.chat.push({ role: "helper", text: `The helper could not answer: ${reason}` });
    renderChat();
    setText(byId("rr-feedback-status"), `Not sent successfully. ${reason} Your text has been kept for retry.`);
    return false;
  } finally {
    DOM.state.chatBusy = false;
    renderChatControls();
  }
}

/* ------------------------------------------------------- action review ---- */

function actionPlan(batch = DOM.state.plan) {
  return batch && batch.plan && typeof batch.plan === "object" ? batch.plan : {};
}

function canRunConnectedAction() {
  return canEditReview() && !DOM.state.actionBusy;
}

function actionRequest(kind, suffix, body) {
  const bodyKey = JSON.stringify(body);
  const retry = DOM.state.actionRetry;
  if (retry && retry.kind === kind && retry.suffix === suffix && retry.bodyKey === bodyKey) return retry;
  const request = { kind, suffix, body, bodyKey, idempotencyKey: newIdempotencyKey(kind) };
  DOM.state.actionRetry = request;
  return request;
}

function finishActionRequest(error = null) {
  // A response from the helper is definitive. A transport failure is ambiguous,
  // so retain the exact body and key for the reviewer's retry.
  if (!error || Number.isInteger(error.status)) DOM.state.actionRetry = null;
}

function planWarnings(batch) {
  const nested = actionPlan(batch);
  const report = batch?.execution_report || {};
  return [...(nested.warnings || []), ...(batch?.api_warnings || []), ...(report.warnings || [])];
}

function mergeExecutionReport(batch, report, apiWarnings = []) {
  if (!batch || !report || typeof report !== "object") return batch;
  return {
    ...batch,
    execution_state: report.state || batch.execution_state,
    execution_revision: Number.isInteger(report.execution_revision)
      ? report.execution_revision
      : batch.execution_revision,
    execution_report: report,
    api_warnings: Array.isArray(apiWarnings) ? apiWarnings : [],
    next_action: report.state === "completed"
      ? "Execution completed. Build a restore plan to propose reversing committed moves."
      : "Execution did not complete. Review the per-file report before taking another action.",
  };
}

function resetFinishedPlan() {
  if (DOM.state.actionBusy || !["canceled", "completed"].includes(DOM.state.plan?.execution_state)) return false;
  DOM.state.plan = null;
  DOM.state.actionRetry = null;
  renderActions();
  return true;
}

function renderActions() {
  const body = byId("rr-action-body");
  if (!body) return;
  body.replaceChildren();
  body.setAttribute("aria-busy", String(DOM.state.actionBusy));

  const selected = DOM.state.selection.pairs;
  const pending = DOM.state.rows.filter((row) => row.pending_intent && row.pending_intent !== "none");
  const mutable = canRunConnectedAction();

  body.appendChild(make("p", null, `${selected.length} rows are selected. ${pending.length} loaded rows carry a pending action intent.`));
  body.appendChild(make("p", "rr-hint", "A plan is built from an explicit document set. Sorting, filtering, or a chat message is never approval. The helper revalidates the whole plan before the first move and each remaining operation before it runs."));

  if (DOM.state.plan) {
    const batch = DOM.state.plan;
    const plan = actionPlan(batch);
    const state = batch.execution_state || "planned";
    const planned = Array.isArray(plan.operations) ? plan.operations : [];
    const skipped = Array.isArray(plan.skipped) ? plan.skipped : [];
    body.appendChild(make("h3", null, `Plan ${batch.batch_id || UNKNOWN_LABEL}`));
    body.appendChild(make("p", null, `State: ${FILE_ACTION_STATE_LABELS[state] || state}. Operations: ${planned.length}. Skipped: ${skipped.length}.`));
    if (batch.next_action) body.appendChild(make("p", "rr-hint", batch.next_action));
    const list = make("ul", "rr-plan-list");
    for (const operation of planned) {
      const item = make("li", "rr-plan-item");
      item.appendChild(make("span", null, `${FILE_ACTION_KIND_LABELS[operation.kind] || operation.kind}: ${operation.document_id}`));
      item.appendChild(make("span", "rr-plan-path", `${operation.source} \u2192 ${operation.destination}`));
      list.appendChild(item);
    }
    if (planned.length) body.appendChild(list);

    if (skipped.length) {
      body.appendChild(make("h4", null, "Skipped from this plan"));
      const skippedList = make("ul", "rr-plan-list");
      for (const item of skipped) {
        skippedList.appendChild(make("li", "rr-plan-item", `${item.document_id}: ${item.reason}${item.kind ? ` (${FILE_ACTION_KIND_LABELS[item.kind] || item.kind})` : ""}`));
      }
      body.appendChild(skippedList);
    }

    const report = batch.execution_report;
    if (report && typeof report === "object") {
      body.appendChild(make("h4", null, "Execution report"));
      const counts = report.counts && typeof report.counts === "object"
        ? Object.entries(report.counts).map(([key, value]) => `${key}: ${value}`).join(", ")
        : "No counts returned";
      body.appendChild(make("p", null, `${counts}. Remaining: ${Number.isInteger(report.remaining) ? report.remaining : UNKNOWN_LABEL}.`));
      const reportList = make("ul", "rr-plan-list");
      for (const outcome of report.operations || []) {
        const item = make("li", "rr-plan-item");
        item.appendChild(make("span", null, `${outcome.document_id}: ${outcome.outcome || outcome.state || UNKNOWN_LABEL}`));
        item.appendChild(make("span", "rr-plan-path", `${outcome.source} \u2192 ${outcome.destination}`));
        if (outcome.detail) item.appendChild(make("span", "rr-hint", outcome.detail));
        reportList.appendChild(item);
      }
      if ((report.operations || []).length) body.appendChild(reportList);
    }

    const warnings = planWarnings(batch);
    if (warnings.length) {
      body.appendChild(make("h4", null, "Warnings"));
      const warningList = make("ul", "rr-plan-list");
      for (const warning of warnings) warningList.appendChild(make("li", null, typeof warning === "string" ? warning : warning.message || warning.code || "Action warning"));
      body.appendChild(warningList);
    }

    const operations = make("p", "rr-ops");
    const approve = make("button", null, "Approve this exact plan");
    approve.type = "button";
    approve.disabled = !(mutable && state === "planned" && planned.length > 0);
    approve.addEventListener("click", () => approvePlan());
    const apply = make("button", null, "Apply approved plan");
    apply.type = "button";
    apply.disabled = !(mutable && state === "approved");
    apply.addEventListener("click", () => applyPlan());
    const cancel = make("button", null, "Cancel plan");
    cancel.type = "button";
    cancel.disabled = !(mutable && ["planned", "approved"].includes(state));
    cancel.addEventListener("click", () => cancelPlan());
    const restore = make("button", null, "Build restore plan");
    restore.type = "button";
    restore.disabled = !(mutable && ["completed", "partial", "blocked"].includes(state));
    restore.addEventListener("click", () => restorePlan());
    const buildNew = make("button", null, "Plan another selection");
    buildNew.type = "button";
    buildNew.disabled = !(mutable && ["canceled", "completed"].includes(state));
    buildNew.addEventListener("click", () => resetFinishedPlan());
    operations.appendChild(approve);
    operations.appendChild(apply);
    operations.appendChild(cancel);
    operations.appendChild(restore);
    operations.appendChild(buildNew);
    body.appendChild(operations);
  } else {
    const controls = make("p", "rr-ops");
    const build = make("button", null, "Build plan from selection");
    build.type = "button";
    build.disabled = !mutable || selected.length === 0;
    build.addEventListener("click", () => buildPlan());
    controls.appendChild(build);
    body.appendChild(controls);
  }
}

async function buildPlan() {
  if (!canRunConnectedAction()) return;
  if (DOM.state.selection.pairs.length === 0) {
    announce("Select at least one submission before building a plan.", true);
    return;
  }
  const body = { document_ids: DOM.state.selection.pairs.map((pair) => pair.document_id) };
  const request = actionRequest("plan", "/actions/plan", body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    const batch = envelope?.data;
    if (!batch?.batch_id || !batch?.plan?.plan_hash || !Array.isArray(batch.plan.operations)) throw new Error("The helper returned an invalid action plan.");
    DOM.state.plan = { ...batch, api_warnings: envelope.warnings || [] };
    finishActionRequest();
    announce(batch.plan.operations.length
      ? "Plan built. Nothing has moved. Review the exact operations, then approve."
      : "No file operation was planned. Review the skipped reasons; there is nothing to approve.");
  } catch (error) {
    finishActionRequest(error);
    announce(`Could not build the plan: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

async function approvePlan() {
  const plan = DOM.state.plan;
  const nested = actionPlan(plan);
  if (!canRunConnectedAction() || !plan || plan.execution_state !== "planned" || !(nested.operations || []).length) return;
  const confirmed = DOM.window.confirm(`Approve the exact plan ${plan.batch_id}? Nothing moves until you apply it.`);
  if (!confirmed) return;
  const body = { plan_hash: nested.plan_hash, expected_revision: plan.execution_revision };
  const suffix = `/actions/${encodeURIComponent(plan.batch_id)}/approve`;
  const request = actionRequest("approve", suffix, body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    const data = envelope?.data;
    if (data?.batch_id !== plan.batch_id || data?.plan_hash !== nested.plan_hash || data?.execution_state !== "approved" || !Number.isInteger(data.execution_revision)) {
      throw new Error("The helper did not confirm this exact plan approval.");
    }
    DOM.state.plan = { ...plan, ...data, plan: nested, api_warnings: envelope.warnings || [] };
    finishActionRequest();
    announce("Approval recorded against the plan hash. Nothing has moved yet.");
  } catch (error) {
    finishActionRequest(error);
    announce(`Approval failed: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

async function applyPlan() {
  const plan = DOM.state.plan;
  if (!canRunConnectedAction() || !plan || plan.execution_state !== "approved") return;
  const confirmed = DOM.window.confirm(`Execute the approved plan ${plan.batch_id}? Files will move.`);
  if (!confirmed) return;
  const body = { expected_revision: plan.execution_revision };
  const suffix = `/actions/${encodeURIComponent(plan.batch_id)}/apply`;
  const request = actionRequest("apply", suffix, body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    const data = envelope?.data;
    if (data?.batch_id !== plan.batch_id || !data.report || data.report.batch_id !== plan.batch_id) throw new Error("The helper returned an invalid execution report.");
    DOM.state.plan = mergeExecutionReport(plan, data.report, envelope.warnings || []);
    finishActionRequest();
    await refreshFromHelper();
    announce(data.state === "completed"
      ? "Execution completed. The document list and per-file report are refreshed."
      : `Execution ended in state ${data.state}. Review the per-file report.`);
  } catch (error) {
    finishActionRequest(error);
    const report = error?.envelope?.error?.detail?.report;
    if (report && typeof report === "object") {
      DOM.state.plan = mergeExecutionReport(plan, report, error.envelope.warnings || []);
      await refreshFromHelper();
      announce(`Execution ${report.state || "failed"}. Some files may have moved; review the retained per-file report.`, true);
    } else {
      announce(`Execution failed: ${describeApiError(error)}`, true);
    }
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

async function cancelPlan() {
  const plan = DOM.state.plan;
  if (!canRunConnectedAction() || !plan || !["planned", "approved"].includes(plan.execution_state)) return;
  const body = { expected_revision: plan.execution_revision };
  const suffix = `/actions/${encodeURIComponent(plan.batch_id)}/cancel`;
  const request = actionRequest("cancel", suffix, body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    const data = envelope?.data;
    if (data?.batch_id !== plan.batch_id || data?.execution_state !== "canceled" || !Number.isInteger(data.execution_revision)) throw new Error("The helper did not confirm cancellation.");
    DOM.state.plan = { ...plan, ...data, plan: actionPlan(plan), api_warnings: envelope.warnings || [] };
    finishActionRequest();
    announce("Cancellation requested. A completed move is not undone by this.");
  } catch (error) {
    finishActionRequest(error);
    announce(`Cancel failed: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

async function restorePlan() {
  const plan = DOM.state.plan;
  if (!canRunConnectedAction() || !plan || !["completed", "partial", "blocked"].includes(plan.execution_state)) return;
  const body = {};
  const suffix = `/actions/${encodeURIComponent(plan.batch_id)}/restore-plan`;
  const request = actionRequest("restore", suffix, body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    const batch = envelope?.data;
    if (!batch?.batch_id || batch.batch_id === plan.batch_id || batch.execution_state !== "planned" || !batch?.plan?.plan_hash || !Array.isArray(batch.plan.operations)) {
      throw new Error("The helper returned an invalid restore plan.");
    }
    DOM.state.plan = { ...batch, api_warnings: envelope.warnings || [] };
    finishActionRequest();
    announce("Inverse plan built. It still needs its own approval.");
  } catch (error) {
    finishActionRequest(error);
    announce(`Restore plan failed: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

/* ---------------------------------------------------------- connected API - */

export function describeApiError(error) {
  if (!error) return "unknown failure";
  if (error.status === 409) return "the helper reported a conflict; reload to see the current value";
  if (error.status === 403) return "your role does not permit this operation";
  if (error.status === 401) return "the session has expired; sign in again";
  if (error.message) return String(error.message);
  return "unknown failure";
}

/**
 * The single network entry point. Everything that could open a request goes
 * through here, and here alone, so snapshot mode cannot reach the network.
 */
async function apiRequest(method, suffix, options = {}) {
  if (!modeAllowsNetwork(DOM.state.mode)) {
    throw new Error("network access is disabled in snapshot mode");
  }
  if (!DOM.state.instanceId) {
    throw new Error("no instance id was provided to the page");
  }
  const headers = { "Accept": "application/json" };
  const init = { method, headers, credentials: "same-origin", redirect: "error" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  const csrf = DOM.state.csrfToken;
  if (csrf && method !== "GET") headers["X-CSRF-Token"] = csrf;

  const response = await DOM.window.fetch(apiPath(DOM.state.apiBase, DOM.state.instanceId, suffix), init);
  const envelope = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error((envelope && envelope.error && envelope.error.message) || `request failed with status ${response.status}`);
    error.status = response.status;
    error.code = envelope && envelope.code;
    error.envelope = envelope;
    throw error;
  }
  return envelope;
}

async function writeDecision(row, decision, isRetry = false) {
  const rerender = () => preserveFocus(() => renderTable());
  if (!canEditReview() || DOM.state.bulkBusy) {
    announce("Decisions require an available reviewer session.", true);
    rerender();
    return;
  }
  const previous = row.review_state;
  DOM.state.saveStates.set(row.document_id, { state: "saving" });
  rerender();
  try {
    const envelope = await apiRequest("PATCH", decisionWritePath(row), {
      body: decisionWriteBody({ decision, expectedRevision: row.decision_revision }),
    });
    if (envelope && typeof envelope.state_revision === "number") {
      if (DOM.state.payload && DOM.state.payload.instance) DOM.state.payload.instance.state_revision = envelope.state_revision;
    }
    const committed = (envelope && envelope.data && envelope.data.document) || {};
    applyCommittedDecisions([{
      document_id: row.document_id,
      disposition: committed.review_state || decision,
      expected_revision: row.decision_revision,
    }]);
    if (typeof committed.decision_revision === "number") row.decision_revision = committed.decision_revision;
    renderHeader();
    recompute();
    announce(isRetry ? "Retry saved." : `Decision saved as ${DISPOSITION_LABELS[decision]}. No file moved.`);
  } catch (error) {
    if (error.status === 409) {
      const current = (error.envelope && error.envelope.error && error.envelope.error.detail) || {};
      DOM.state.saveStates.set(row.document_id, {
        state: "conflict",
        current_value: current.current_value || null,
        current_actor: current.actor || current.current_actor || null,
        message: error.message,
      });
    } else {
      // The row keeps its previous value. A failed write is never committed.
      row.review_state = previous;
      DOM.state.saveStates.set(row.document_id, { state: "failed", message: error.message });
    }
    announce(`Decision not saved: ${describeApiError(error)}`, true);
  }
  rerender();
}

export function decisionWritePath(row) {
  return documentPath(row.document_id) + "/decision";
}

function applyCommittedDecisions(items) {
  const byDocument = new Map(items.map((item) => [item.document_id, item]));
  const counts = DOM.state.payload?.counts || {};
  for (const row of DOM.state.rows) {
    const item = byDocument.get(row.document_id);
    if (!item) continue;
    if (row.review_state !== item.disposition) {
      if (typeof counts[row.review_state] === "number") counts[row.review_state] = Math.max(0, counts[row.review_state] - 1);
      if (typeof counts[item.disposition] === "number") counts[item.disposition] += 1;
    }
    if (row.decision_needs_recheck && typeof counts.needs_recheck === "number") counts.needs_recheck = Math.max(0, counts.needs_recheck - 1);
    row.review_state = item.disposition;
    row.decision_revision = item.expected_revision + 1;
    row.decision_needs_recheck = false;
    DOM.state.saveStates.set(row.document_id, { state: "saved" });
  }
}

async function writeBulkDecision(decision) {
  if (!canEditReview() || DOM.state.bulkBusy || !DISPOSITIONS.includes(decision)) return;
  const pairs = DOM.state.selection.pairs.map((pair) => ({ ...pair }));
  if (!pairs.length) return;
  if (pairs.some((pair) => {
    const row = DOM.state.rows.find((item) => item.document_id === pair.document_id);
    return !row || dispositionFreezeReason(row) || DOM.state.saveStates.get(pair.document_id)?.state === "saving";
  })) {
    DOM.state.bulkMessage = "Some selected candidates are unavailable or locked. Refresh and review your selection.";
    renderBulkActions();
    return;
  }
  const body = bulkDecisionBody({ pairs }, decision);
  const fingerprint = JSON.stringify(body);
  if (DOM.state.bulkRetry?.fingerprint !== fingerprint) {
    DOM.state.bulkRetry = { fingerprint, key: newIdempotencyKey("bulk-decision") };
  }
  DOM.state.bulkBusy = true;
  DOM.state.bulkMessage = `Saving ${DISPOSITION_LABELS[decision]} for ${pairs.length} selected candidate${pairs.length === 1 ? "" : "s"}...`;
  renderTable();
  try {
    const envelope = await apiRequest("POST", "/decisions/bulk", {
      body, idempotencyKey: DOM.state.bulkRetry.key,
    });
    const data = envelope?.data || {};
    const returnedIds = new Set(data.document_ids || []);
    if (data.updated !== pairs.length || returnedIds.size !== pairs.length || pairs.some((pair) => !returnedIds.has(pair.document_id))) {
      throw new Error("The helper did not confirm the full selection. Refresh before trying again.");
    }
    applyCommittedDecisions(body.items);
    if (typeof envelope.state_revision === "number" && DOM.state.payload?.instance) DOM.state.payload.instance.state_revision = envelope.state_revision;
    DOM.state.selection = emptySelection();
    DOM.state.bulkRetry = null;
    DOM.state.bulkMessage = `${pairs.length} candidate${pairs.length === 1 ? "" : "s"} marked ${DISPOSITION_LABELS[decision]}. No files moved.`;
    announce(DOM.state.bulkMessage);
    renderHeader();
  } catch (error) {
    DOM.state.bulkMessage = error.status === 409
      ? "A selected candidate changed since selection. No decisions were overwritten. Refresh, clear the selection, and select again."
      : `Save not confirmed: ${describeApiError(error)}. Your selection is retained for retry.`;
    announce(DOM.state.bulkMessage, true);
  } finally {
    DOM.state.bulkBusy = false;
    recompute();
  }
}

async function refreshFromHelper() {
  try {
    const statusEnvelope = await apiRequest("GET", "/status");
    if (statusEnvelope && statusEnvelope.data) {
      const status = statusEnvelope.data;
      if (status.instance) DOM.state.payload.instance = status.instance;
      else DOM.state.payload.instance = {
        ...DOM.state.payload.instance,
        ...status.versions,
        instance_id: status.instance_id,
        state_revision: statusEnvelope.state_revision,
      };
      if (status.counts) DOM.state.payload.counts = status.counts;
      setText(byId("rr-health"), "Helper health: available");
    }
    const loaded = [];
    const seen = new Set();
    let listRevision = null;
    for (let page = 1; ; page += 1) {
      if (page > 1000) throw new Error("The folder is too large to load in one view.");
      const listEnvelope = await apiRequest("GET", documentsQuery({
        page, pageSize: 100, sort: "ingested_at", direction: "asc",
      }));
      const data = listEnvelope?.data || {};
      if (!Array.isArray(data.documents)) throw new Error("The helper returned an invalid document list.");
      if (page === 1) listRevision = listEnvelope.state_revision;
      else if (listEnvelope.state_revision !== listRevision) throw new Error("The folder changed while loading. Refresh to load a consistent list.");
      for (const row of data.documents) {
        if (seen.has(row.document_id)) throw new Error("The folder changed while loading. Refresh to load a consistent list.");
        seen.add(row.document_id);
        loaded.push(row);
      }
      if (!data.has_more) {
        if (data.counts) DOM.state.payload.counts = data.counts;
        break;
      }
      if (!data.documents.length) throw new Error("The helper returned an incomplete document list.");
    }
    DOM.state.rows = loaded;
    DOM.state.filteredTotal = loaded.length;
    if (typeof listRevision === "number") DOM.state.payload.instance.state_revision = listRevision;
    renderHeader();
    recompute();
    announce("Refreshed from the helper. No inference ran and nothing was approved.");
  } catch (error) {
    setText(byId("rr-health"), `Helper health: unavailable (${describeApiError(error)})`);
    announce(`Refresh failed: ${describeApiError(error)}`, true);
  }
}

async function queueOperation(suffix, message) {
  if (!canRunConnectedAction()) return;
  const isAnalysis = suffix === "/analysis/jobs";
  const selectedIds = [...new Set(DOM.state.selection.pairs.map((pair) => pair.document_id))];
  if (isAnalysis && selectedIds.length === 0) {
    announce("Select at least one candidate to summarize.", true);
    return;
  }
  if (isAnalysis && selectedIds.length > 200) {
    announce("Select at most 200 candidates for one summarize request.", true);
    return;
  }
  const body = isAnalysis ? { document_ids: selectedIds } : {};
  const kind = isAnalysis ? "analysis" : "scan";
  const request = actionRequest(kind, suffix, body);
  DOM.state.actionBusy = true;
  renderActions();
  try {
    const envelope = await apiRequest("POST", request.suffix, {
      body: request.body,
      idempotencyKey: request.idempotencyKey,
    });
    if (isAnalysis) {
      const queued = envelope?.data?.queued;
      const jobs = envelope?.data?.jobs;
      if (!Number.isInteger(queued) || !Array.isArray(jobs) || queued !== jobs.length) throw new Error("The helper returned an invalid analysis queue result.");
      finishActionRequest();
      const warningCount = Array.isArray(envelope.warnings) ? envelope.warnings.length : 0;
      announce(queued > 0
        ? `${queued} analysis ${queued === 1 ? "job" : "jobs"} queued for the selected candidates.${warningCount ? ` ${warningCount} warning${warningCount === 1 ? "" : "s"} returned.` : ""}`
        : `No analysis jobs were queued.${warningCount ? ` Review the ${warningCount} returned warning${warningCount === 1 ? "" : "s"}.` : ""}`,
      );
    } else {
      const jobId = envelope?.job_id ? String(envelope.job_id) : null;
      if (!jobId || envelope?.data?.state !== "queued") throw new Error("The helper did not confirm a queued scan.");
      finishActionRequest();
      announce(`${message} queued as job ${jobId}.`);
    }
  } catch (error) {
    finishActionRequest(error);
    announce(`${message} Failed: ${describeApiError(error)}`, true);
  } finally {
    DOM.state.actionBusy = false;
    renderActions();
  }
}

/* -------------------------------------------------------------------- boot - */

function readJsonSlot(id) {
  const element = byId(id);
  if (!element) return "";
  return element.textContent || "";
}

async function loadDesktopCriteria() {
  if (DOM.state.mode !== "connected") return;
  try {
    const envelope = await apiRequest("GET", "/criteria");
    const data = envelope.data;
    DOM.state.criteriaDraft = { ...data, expected_revision: envelope.state_revision };
    const list = byId("rr-criteria-draft-list");
    if (list) {
      list.replaceChildren();
      for (const criterion of data.active || []) list.appendChild(make("li", null, `Approved: ${criterion.definition}`));
      for (const criterion of data.proposals || []) list.appendChild(make("li", null, `Draft: ${criterion.definition}`));
    }
    const editable = canEditReview();
    if (byId("rr-criteria-input")) byId("rr-criteria-input").disabled = !editable;
    if (byId("rr-criteria-propose")) byId("rr-criteria-propose").disabled = !editable;
    if (byId("rr-criteria-approve")) byId("rr-criteria-approve").disabled = !editable || !data.pending_version;
    setText(byId("rr-criteria-status"), data.pending_version ? "Review the displayed draft before approving it." : data.active_version ? "Approved criteria are ready for analysis." : "Add and approve criteria before analyzing candidates.");
  } catch (error) {
    DOM.state.criteriaDraft = null;
    if (byId("rr-criteria-approve")) byId("rr-criteria-approve").disabled = true;
    setText(byId("rr-criteria-status"), `Criteria could not be loaded: ${describeApiError(error)}`);
  }
}

async function proposeDesktopCriteria() {
  if (!canEditReview() || DOM.state.criteriaBusy) return;
  const input = byId("rr-criteria-input");
  const text = input?.value || "";
  const definitions = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!definitions.length || definitions.length > 100 || definitions.some((line) => line.length > 8000)) {
    setText(byId("rr-criteria-status"), "Enter 1-100 requirements, one per line, each up to 8,000 characters.");
    return;
  }
  DOM.state.criteriaBusy = true;
  const request = DOM.state.criteriaRetry?.text === text ? DOM.state.criteriaRetry : {
    text, key: newIdempotencyKey("criteria"),
    body: { proposals: definitions.map((definition) => ({ criterion_id: newIdempotencyKey("cr"), definition })) },
  };
  DOM.state.criteriaRetry = request;
  try {
    await apiRequest("POST", "/criteria/proposals", { body: request.body, idempotencyKey: request.key });
    DOM.state.criteriaRetry = null;
    input.value = "";
    await loadDesktopCriteria();
  } catch (error) {
    setText(byId("rr-criteria-status"), `Draft was not confirmed: ${describeApiError(error)}. Retry to confirm the same request.`);
  } finally {
    DOM.state.criteriaBusy = false;
  }
}

async function approveDesktopCriteria() {
  const draft = DOM.state.criteriaDraft;
  if (!canEditReview() || DOM.state.criteriaBusy || !draft?.pending_version) return;
  if (!DOM.window.confirm("Approve the displayed criteria draft for analysis? Earlier results may need rechecking.")) return;
  DOM.state.criteriaBusy = true;
  try {
    await apiRequest("POST", `/criteria/${draft.pending_version}/activate`, {
      body: { expected_revision: draft.expected_revision },
      idempotencyKey: newIdempotencyKey("criteria-approval"),
    });
    await loadDesktopCriteria();
    await refreshFromHelper();
  } catch (error) {
    setText(byId("rr-criteria-status"), `Approval was not confirmed: ${describeApiError(error)}. Reload and review the current draft.`);
  } finally {
    DOM.state.criteriaBusy = false;
  }
}

function wireEvents() {
  byId("rr-criteria-draft-form")?.addEventListener("submit", (event) => { event.preventDefault(); proposeDesktopCriteria(); });
  byId("rr-criteria-approve")?.addEventListener("click", approveDesktopCriteria);
  byId("rr-criteria-reload")?.addEventListener("click", loadDesktopCriteria);
  const tabs = Array.from(DOM.doc.querySelectorAll("[data-workspace-tab]"));
  const showWorkspace = (name, moveFocus = false) => {
    for (const tab of tabs) {
      const active = tab.dataset.workspaceTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      const panel = byId(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !active;
      if (active && moveFocus) tab.focus();
    }
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => showWorkspace(tab.dataset.workspaceTab));
    tab.addEventListener("keydown", (event) => {
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
        : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
      if (next !== null) { event.preventDefault(); showWorkspace(tabs[next].dataset.workspaceTab, true); }
    });
  });
  const skip = DOM.doc.querySelector('a[href="#rr-table"]');
  if (skip) skip.addEventListener("click", () => showWorkspace("candidates"));
  const requisitionForm = byId("rr-requisition-form");
  if (requisitionForm) {
    requisitionForm.addEventListener("submit", (event) => { event.preventDefault(); saveRequisition(); });
    for (const input of requisitionForm.querySelectorAll('input:not([type="file"]), textarea')) {
      input.addEventListener("input", () => {
        DOM.state.requisitionDirty = true;
        requisitionStatus("Unsaved changes. Save the reference to make it available to OpenClaw.");
        renderRequisitionControls();
      });
    }
  }
  byId("rr-requisition-file")?.addEventListener("change", (event) => importRequisitionFile(event.target.files?.[0]));
  byId("rr-requisition-reload")?.addEventListener("click", () => loadRequisition(true));
  for (const button of DOM.doc.querySelectorAll("[data-bulk-decision]")) {
    button.addEventListener("click", () => writeBulkDecision(button.dataset.bulkDecision));
  }
  const search = byId("rr-search");
  if (search) {
    let timer = null;
    search.addEventListener("input", () => {
      if (timer) DOM.window.clearTimeout(timer);
      timer = DOM.window.setTimeout(() => {
        DOM.state.page = 1;
        DOM.state.filter = { ...DOM.state.filter, search: search.value };
        recompute();
      }, 120);
    });
  }
  for (const [id, field] of [
    ["rr-filter-location", "location"],
    ["rr-filter-processing", "processing_state"],
    ["rr-filter-review", "review_state"],
    ["rr-filter-tasks", "task"],
    ["rr-sort", "sort"],
    ["rr-sort-direction", "direction"],
  ]) {
    const control = byId(id);
    if (control) control.addEventListener("change", () => setFilter({ [field]: control.value }));
  }
  const clear = byId("rr-clear-filters");
  if (clear) {
    clear.addEventListener("click", () => {
      DOM.state.filter = { ...emptyFilter(), sort: DOM.state.filter.sort, direction: DOM.state.filter.direction };
      renderToolbarState();
      recompute();
      announce("Filters cleared. The view changed; no decision and no file changed.");
    });
  }
  const selectPageButton = byId("rr-btn-select-page");
  if (selectPageButton) {
    selectPageButton.addEventListener("click", () => {
      DOM.state.selection = selectPage(DOM.state.selection, paginate(DOM.state.visibleRows, DOM.state.page, DOM.state.pageSize).items);
      renderTable();
      announce("Added the rendered page to the selection.");
    });
  }
  const selectMatching = byId("rr-btn-select-matching");
  if (selectMatching) {
    selectMatching.addEventListener("click", () => {
      const snapshot = DOM.state.visibleRows.slice();
      const confirmed = DOM.window.confirm(`Select all ${snapshot.length} currently matching results? The set is fixed when you confirm it and will not grow if submissions or filters change later.`);
      if (!confirmed) return;
      DOM.state.selection = resolveAllMatching(snapshot);
      renderTable();
      announce(`Selection fixed at ${snapshot.length} rows. Deselect to choose again.`);
    });
  }
  const deselect = byId("rr-btn-deselect");
  if (deselect) {
    deselect.addEventListener("click", () => {
      DOM.state.selection = deselectAll();
      renderTable();
    });
  }
  const prev = byId("rr-btn-prev");
  if (prev) prev.addEventListener("click", () => { DOM.state.page -= 1; renderRows(); renderSelectionUi(); });
  const next = byId("rr-btn-next");
  if (next) next.addEventListener("click", () => { DOM.state.page += 1; renderRows(); renderSelectionUi(); });
  const pageSize = byId("rr-page-size");
  if (pageSize) {
    pageSize.addEventListener("change", () => {
      DOM.state.pageSize = Number(pageSize.value) || PAGE_SIZE_DEFAULT;
      DOM.state.page = 1;
      renderRows();
    });
  }
  for (const [id, drawerId] of [["rr-detail-close", "rr-detail"], ["rr-chat-close", "rr-chat"], ["rr-actions-close", "rr-actions"]]) {
    const button = byId(id);
    if (button) button.addEventListener("click", () => closeDrawer(drawerId));
  }
  const refresh = byId("rr-btn-refresh");
  if (refresh) refresh.addEventListener("click", () => refreshFromHelper());
  const scan = byId("rr-btn-scan");
  if (scan) scan.addEventListener("click", () => queueOperation("/scan", "Scan"));
  const summarize = byId("rr-btn-summarize");
  if (summarize) {
    summarize.textContent = "Summarize selected candidates";
    summarize.addEventListener("click", () => queueOperation("/analysis/jobs", "Summarize selected candidates"));
  }
  const actions = byId("rr-btn-actions");
  if (actions) actions.addEventListener("click", () => { renderActions(); const drawer = byId("rr-actions"); if (drawer) drawer.hidden = false; });
  const chatButton = byId("rr-btn-chat");
  if (chatButton) {
    chatButton.addEventListener("click", () => {
      const drawer = byId("rr-chat");
      if (!drawer) return;
      drawer.hidden = !drawer.hidden;
      chatButton.setAttribute("aria-expanded", drawer.hidden ? "false" : "true");
      if (!drawer.hidden) { renderChat(); const input = byId("rr-chat-input"); if (input) input.focus(); }
    });
  }
  const chatForm = byId("rr-chat-form");
  if (chatForm) {
    chatForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = byId("rr-chat-input");
      if (!input) return;
      const message = input.value;
      if (await sendChat(message)) input.value = "";
    });
  }
  const feedbackForm = byId("rr-feedback-form");
  if (feedbackForm) {
    feedbackForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = byId("rr-feedback-input");
      if (!input || !input.value.trim() || DOM.state.chatBusy) return;
      setText(byId("rr-feedback-status"), "Sending feedback to OpenClaw...");
      if (await sendChat(input.value, { wholeFolder: true })) {
        input.value = "";
        setText(byId("rr-feedback-status"), "Feedback processed. The response is in folder chat.");
        const drawer = byId("rr-chat");
        if (drawer) drawer.hidden = false;
        const toggle = byId("rr-btn-chat");
        if (toggle) toggle.setAttribute("aria-expanded", "true");
        const close = byId("rr-chat-close");
        if (close) close.focus();
      }
    });
  }
  const chatClear = byId("rr-chat-clear");
  if (chatClear) chatClear.addEventListener("click", () => { DOM.state.chat = []; renderChat(); });
  const theme = byId("rr-theme");
  if (theme) {
    let stored = null;
    try { stored = DOM.window.localStorage.getItem("rr-theme"); } catch (error) { stored = null; }
    if (stored === "light" || stored === "dark") {
      theme.value = stored;
      DOM.doc.documentElement.dataset.theme = stored;
    }
    theme.addEventListener("change", () => {
      const value = theme.value;
      if (value === "auto") delete DOM.doc.documentElement.dataset.theme;
      else DOM.doc.documentElement.dataset.theme = value;
      try { DOM.window.localStorage.setItem("rr-theme", value); } catch (error) { /* per-viewer convenience only */ }
    });
  }
}

function snapshotBoot(resolved) {
  const payload = resolved.snapshot;
  if (!payload) {
    announce("No snapshot payload was embedded in this document, so there is nothing to render. Open the connected review page instead.", true);
    return;
  }
  DOM.state.mode = "snapshot";
  DOM.state.payload = payload;
  DOM.state.instanceId = (payload.instance && payload.instance.instance_id) || null;
  DOM.state.rows = Array.isArray(payload.documents) ? payload.documents : [];
  DOM.state.filteredRows = DOM.state.rows;
  DOM.state.filteredTotal = DOM.state.rows.length;
  DOM.state.filter = { ...emptyFilter(), ...DEFAULT_SORT };
  DOM.state.omitted = { total: 0, by_reason: {}, unknown_excluded: 0, unknown_included: 0 };
  mountHeader();
  renderHeader();
  renderToolbarState();
  recompute();
  if (DOM.state.rows.length === 0) {
    announce("Snapshot loaded. It contains no submissions.");
  } else {
    announce(`Snapshot loaded: ${DOM.state.rows.length} submissions, sorted and filtered in memory. Read only.`);
  }
}

async function connectedBoot(resolved) {
  DOM.state.mode = "connected";
  DOM.state.instanceId = resolved.instance_id;
  DOM.state.payload = { schema_version: SCHEMA_VERSION, mode: "connected", instance: {}, counts: {}, documents: [] };
  const bootstrap = resolved.bootstrap || {};
  if (typeof bootstrap.api_base === "string") DOM.state.apiBase = bootstrap.api_base;
  if (typeof bootstrap.role === "string") DOM.state.role = bootstrap.role;
  DOM.state.rows = [];
  DOM.state.filteredRows = [];
  DOM.state.filteredTotal = 0;
  DOM.state.omitted = { total: 0, by_reason: {}, unknown_excluded: 0, unknown_included: 0 };
  DOM.state.csrfToken = readCsrfToken();
  mountHeader();
  renderHeader();
  renderToolbarState();
  recompute();
  if (!DOM.state.instanceId) {
    announce("This page did not receive an instance id, so the helper cannot be addressed. Open the connected review address printed by the helper.", true);
    return;
  }
  await refreshFromHelper();
  await loadRequisition();
  if (bootstrap.desktop_companion && byId("rr-connection-status")) {
    await loadDesktopCriteria();
    const updateConnection = async () => {
      try {
        const envelope = await apiRequest("GET", "/connection");
        const connection = envelope.data;
        const text = !connection.configured
          ? "Analysis is not connected. Manual review and file plans remain available."
          : connection.state === "ready"
            ? "Online analysis connection has processed a job."
            : connection.state === "retrying"
              ? "Online analysis is retrying. Your saved reviews remain available."
              : connection.state === "error"
                ? `Online analysis needs attention: ${connection.last_error_code || "connection error"}.`
                : "Online analysis configured. Select candidates to analyze or send feedback.";
        setText(byId("rr-connection-status"), text);
      } catch (error) {
        setText(byId("rr-connection-status"), "Connection status is unavailable. Refresh or relaunch the local helper.");
      }
    };
    await updateConnection();
    DOM.window.setInterval(updateConnection, 5000);
  }
}

function readCsrfToken() {
  if (!DOM.doc) return null;
  const meta = DOM.doc.querySelector('meta[name="csrf-token"]');
  const value = meta ? meta.getAttribute("content") : null;
  return value && value !== "" ? value : null;
}

/** Entry point. Only called when a real document exists. */
export function boot(doc) {
  DOM.doc = doc;
  DOM.window = doc.defaultView || (typeof window !== "undefined" ? window : null);
  if (!DOM.window) return;

  const warning = byId("rr-module-warning");
  if (warning) warning.hidden = true;

  const resolved = resolveMode({
    snapshotText: readJsonSlot("rr-snapshot") || readJsonSlot("SNAPSHOT"),
    bootstrapText: readJsonSlot("rr-bootstrap") || readJsonSlot("rr-bootstrap-record"),
    href: DOM.window.location ? DOM.window.location.href : "",
    search: DOM.window.location ? DOM.window.location.search : "",
  });

  doc.documentElement.dataset.rrMode = resolved.mode;
  wireEvents();

  if (resolved.mode === "snapshot") {
    snapshotBoot(resolved);
    return;
  }
  connectedBoot(resolved).catch((error) => {
    announce(`The helper could not be reached: ${describeApiError(error)}`, true);
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  const start = () => boot(document);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
}

/* The surface the Node harness imports. Everything here is pure: no DOM, no
 * network, no module state. */
export const ReportCore = Object.freeze({
  SCHEMA_VERSION,
  PAGE_SIZE_DEFAULT,
  COUNT_FIELDS,
  COLUMNS,
  SORT_KEYS,
  DEFAULT_SORT,
  DISPOSITIONS,
  DISPOSITION_LABELS,
  UNKNOWN_LABEL,
  NOT_FOUND_LABEL,
  NOT_FOUND_SHORT,
  PENDING_INTENT_LABELS,
  FILE_ACTION_KIND_LABELS,
  FILE_ACTION_STATE_LABELS,
  CRITERION_RESULTS,
  DEFAULT_FILTER,
  emptyFilter,
  isUnknown,
  isBlank,
  classifyValue,
  describeValue,
  formatCount,
  formatBytes,
  formatTimestamp,
  escapeHtml,
  safeRelativeLink,
  compareValues,
  sortValue,
  compareRows,
  sortRows,
  criterionAssessment,
  evaluateFilterNode,
  describeFilterNode,
  applyFilter,
  describeFilter,
  isFilterActive,
  unknownTreatment,
  describeCriterionResult,
  dispositionFreezeReason,
  describeDecisionControl,
  describeSaveState,
  describeAction,
  emptySelection,
  selectionPairs,
  selectionKey,
  isRowSelected,
  toggleSelection,
  selectPage,
  resolveAllMatching,
  deselectAll,
  hiddenSelectedPairs,
  describeSelection,
  paginate,
  describePageSlice,
  parseJsonObject,
  instanceIdFromHref,
  isFileOrigin,
  resolveMode,
  modeAllowsNetwork,
  apiPath,
  documentPath,
  documentsQuery,
  decisionWriteBody,
  decisionWritePath,
  bulkDecisionBody,
  newIdempotencyKey,
  describeApiError,
  buildHeaderHtml,
  boot,
});

export default ReportCore;
