"""Shared, dependency-free contracts for every layer of resume-review.

This module is the seam between layers. ``db``, ``storage``, ``actions``, ``api``,
``ingest``, ``analysis`` and ``reporting`` all import from here and never from each
other across a layer boundary (enforced by ``tests/unit/test_layering.py``).

Authority: PRD section 10 ("Keep five concepts independent") and section 11.

Design rules encoded here:

* The five state dimensions are separate enums. Do not collapse them. A row can
  legitimately be ``decision=reject`` + ``location=active`` + ``intent=move_rejected``
  + ``batch=applying``, and the UI must show it that way.
* Unknown is a first-class value, distinct from both zero and empty string.
* Nothing in this module performs I/O. It is pure data.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


# ===========================================================================
# Enumerations
# ===========================================================================
class StrEnum(str, enum.Enum):
    """A str-valued enum that serializes to its plain value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    def to_json(self) -> str:
        return str(self.value)


class ProcessingState(StrEnum):
    """Where the helper's pipeline is with this document. Owned by the pipeline."""

    DISCOVERED = "discovered"
    EXTRACTING = "extracting"
    ANALYZING = "analyzing"
    READY = "ready"
    MANUAL_REVIEW = "manual_review"
    ERROR = "error"
    STALE = "stale"


class ReviewState(StrEnum):
    """The human's disposition. Owned by the reviewer. Never set by a model."""

    UNREVIEWED = "unreviewed"
    KEEP = "keep"
    REJECT = "reject"
    HOLD = "hold"


class Location(StrEnum):
    """Where the file actually is, as verified by filesystem reconciliation."""

    ACTIVE = "active"
    REJECTED = "rejected"
    TRASH = "trash"
    MISSING = "missing"
    CONFLICT = "conflict"


class PendingIntent(StrEnum):
    """A saved human request or an unapproved agent proposal. Moves nothing."""

    NONE = "none"
    MOVE_REJECTED = "move_rejected"
    RESTORE_ACTIVE = "restore_active"
    MOVE_TRASH = "move_trash"
    RESTORE_PREVIOUS = "restore_previous"


class ExecutionState(StrEnum):
    """Lifecycle of an approved batch of file operations."""

    PLANNED = "planned"
    APPROVED = "approved"
    APPLYING = "applying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CANCELED = "canceled"


class OperationState(StrEnum):
    """Durable per-operation step. Written to the journal before touching files."""

    PLANNED = "planned"
    INTENT_RECORDED = "intent_recorded"
    FILE_MOVED = "file_moved"
    COMMITTED = "committed"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    FAILED = "failed"
    SKIPPED = "skipped"


class CriterionResult(StrEnum):
    """Criterion assessment outcome.

    ``NOT_FOUND`` means the processed document did not establish the criterion. It
    is not "unqualified" and never directly sets Keep or Reject.
    """

    SUPPORTED = "supported"
    NOT_FOUND = "not_found"
    UNCLEAR = "unclear"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class UnknownPolicy(StrEnum):
    """How a filter treats unknown values. Never silently hide unprocessed rows."""

    INCLUDE_WITH_WARNING = "include_with_warning"
    EXCLUDE = "exclude"


class Role(StrEnum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMINISTRATOR = "administrator"

    @property
    def rank(self) -> int:
        return {"viewer": 0, "reviewer": 1, "administrator": 2}[str(self.value)]

    def at_least(self, other: "Role") -> bool:
        return self.rank >= other.rank


class ActorKind(StrEnum):
    HUMAN = "human"
    HELPER = "helper"
    AGENT = "agent"
    SYSTEM = "system"


class TaskState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    DISMISSED = "dismissed"


class TaskOrigin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class MediaType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class StorageMode(StrEnum):
    LOCAL = "local"
    SHARED_HOST_LOCAL = "shared_host_local"


class ModelRoute(StrEnum):
    """Approved processing route. There is no silent fallback between these."""

    LOCAL_ONLY = "local_only"
    APPROVED_PROVIDER = "approved_provider"
    FIXTURE = "fixture"
    UNAVAILABLE = "unavailable"


class ChatKind(StrEnum):
    CHAT = "chat"
    ANALYSIS = "analysis"


# ===========================================================================
# Small value types
# ===========================================================================
class Unknown:
    """Sentinel type for "we do not know", distinct from ``None`` and from ``0``.

    ``None`` means "no value supplied". ``UNKNOWN`` means "the system looked and
    could not establish a value". The UI renders these differently and so must the
    API. Use :func:`is_unknown` rather than identity comparisons on raw ``None``.
    """

    _instance: "Unknown | None" = None

    def __new__(cls) -> "Unknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN = Unknown()


def is_unknown(value: Any) -> bool:
    return value is UNKNOWN


def jsonable(value: Any) -> Any:
    """Recursively convert a value to something ``json.dumps`` accepts.

    ``UNKNOWN`` becomes ``None`` and carries an explicit ``unknown`` marker at the
    container level only where callers ask for it; here it degrades to ``None``.
    """
    if value is UNKNOWN:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 preserved.

    This is the serialization used for plan hashes and idempotency request hashes,
    so it must be stable across processes and Python versions.
    """
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


# ===========================================================================
# Extraction
# ===========================================================================
@dataclass(frozen=True)
class Span:
    """One addressable unit of extracted source text.

    ``span_id`` is stable for a given (content hash, parser version) pair, which is
    what makes evidence validation possible: the helper can check that a quoted
    excerpt really occurs inside the span the model claims it came from.

    ``locator`` carries only locations the format actually defines. Pages are
    present for PDFs; DOCX uses paragraph/table locators; TXT uses line ranges. We
    never invent a page number for a format without reliable pagination.
    """

    span_id: str
    text: str
    locator: dict[str, Any] = field(default_factory=dict)
    kind: str = "text"  # text | paragraph | table_row | line_block | page

    @property
    def normalized(self) -> str:
        return normalize_ws(self.text)


@dataclass
class ExtractedDocument:
    """Result of the deterministic extraction stage (no model involved)."""

    media_type: MediaType
    parser_name: str
    parser_version: str
    state: str  # 'ok' | 'partial' | 'failed' | 'unsupported'
    detail: str = ""
    spans: list[Span] = field(default_factory=list)
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    # Free-form hints the analysis stage may use; never authoritative facts.
    hints: dict[str, Any] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.spans)

    @property
    def text(self) -> str:
        return "\n".join(s.text for s in self.spans)

    def span_map(self) -> dict[str, Span]:
        return {s.span_id: s for s in self.spans}


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip.

    Quote validation normalizes both sides so that a legitimate quote is not
    rejected over a line-wrap difference, while a fabricated quote still fails.
    """
    return " ".join(text.split())


# ===========================================================================
# Documents, decisions, tasks
# ===========================================================================
@dataclass
class DocumentRecord:
    id: str
    instance_id: str
    original_filename: str
    current_rel_path: str
    first_seen_rel_path: str
    display_name: str | None = None
    media_type: MediaType = MediaType.UNKNOWN
    size_bytes: int | None = None
    content_sha256: str | None = None
    current_revision: int = 0
    fs_identity: str | None = None
    processing_state: ProcessingState = ProcessingState.DISCOVERED
    processing_detail: str | None = None
    location: Location = Location.ACTIVE
    location_version: int = 0
    decision_needs_recheck: bool = False
    recheck_reason: str | None = None
    duplicate_content: bool = False
    duplicate_of: str | None = None
    submitted_at: str | None = None  # None means unknown; do not fabricate
    ingested_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    archived_at: str | None = None

    @property
    def has_identity(self) -> bool:
        return bool(self.content_sha256)


@dataclass
class DecisionRecord:
    document_id: str
    disposition: ReviewState = ReviewState.UNREVIEWED
    decision_revision: int = 0
    actor: str = ""
    decided_at: str | None = None
    needs_recheck: bool = False
    disposition_frozen: bool = False


@dataclass
class NoteRecord:
    id: str
    document_id: str
    body: str
    author: str
    note_revision: int = 1
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TaskRecord:
    id: str
    document_id: str
    title: str
    task_type: str = "general"
    criterion_id: str | None = None
    source_revision: int | None = None
    dedupe_key: str = ""
    origin: TaskOrigin = TaskOrigin.HUMAN
    detail: str = ""
    state: TaskState = TaskState.OPEN
    severity: str = "normal"
    resolution: str | None = None
    resolution_note: str | None = None
    closed_by: str | None = None
    closed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


def task_dedupe_key(
    task_type: str, document_id: str, criterion_id: str | None, source_revision: int | None
) -> str:
    """Dedupe key = task type + document + criterion + relevant source revision.

    Regeneration must not create a second copy of a task that already exists for
    the same inputs, and must not reopen one a human already closed. A genuinely
    new source revision *is* a new key, so new material evidence legitimately
    produces a linked new task.
    """
    parts = [task_type, document_id, criterion_id or "-", str(source_revision if source_revision is not None else "-")]
    return sha256_hex("\x1f".join(parts))[:32]


@dataclass
class IntentRecord:
    document_id: str
    intent: PendingIntent = PendingIntent.NONE
    intent_revision: int = 0
    state: str = "saved"
    requester: str = ""
    origin_batch_id: str | None = None
    note: str | None = None


# ===========================================================================
# Analysis
# ===========================================================================
@dataclass
class Criterion:
    criterion_id: str
    version: int
    definition: str
    rationale: str = ""
    evidence_rule: str = ""
    label: str | None = None  # 'required' | 'preferred' | None
    created_by: str = ""
    approved_by: str | None = None
    approved_at: str | None = None
    origin: str = "human"

    @property
    def approved(self) -> bool:
        return bool(self.approved_by and self.approved_at)


@dataclass
class EvidenceItem:
    """One locatable factual claim produced by analysis."""

    id: str
    span_id: str
    quote: str
    locator: dict[str, Any] = field(default_factory=dict)
    criterion_id: str | None = None
    result: CriterionResult | None = None
    claim_kind: str = "criterion"  # 'summary' | 'criterion'
    # Validation is performed by the helper, never self-reported by the model.
    validation: str = "unchecked"
    validation_detail: str | None = None


@dataclass
class CriterionAssessment:
    criterion_id: str
    result: CriterionResult
    explanation: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class SuggestedTask:
    type: str
    title: str
    criterion_id: str | None = None
    detail: str = ""


@dataclass
class AnalysisResult:
    """Validated model output, bound to the exact inputs it is valid for."""

    schema_version: str
    document_id: str
    source_revision: int
    criteria_version: int
    summary_text: str
    summary_evidence_ids: list[str] = field(default_factory=list)
    criteria: list[CriterionAssessment] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    suggested_tasks: list[SuggestedTask] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProfileRecord:
    id: str
    document_id: str
    source_revision: int
    criteria_version: int
    prompt_version: str
    schema_version: str
    model_route: str
    model_version: str | None
    summary_text: str
    validation_state: str
    validation_detail: str | None = None
    is_fixture: bool = False
    is_current: bool = False
    stale: bool = False
    generated_at: str = ""
    run_request_id: str | None = None
    run_started_at: str | None = None
    run_ended_at: str | None = None
    token_usage: int | None = None


# ===========================================================================
# Actions
# ===========================================================================
@dataclass(frozen=True)
class PlannedOperation:
    """One concrete, exactly-specified filesystem move.

    Every field is a precondition the executor revalidates immediately before the
    move. If any of them no longer matches current state, the operation is stale
    and the batch stops rather than guessing.
    """

    operation_id: str
    document_id: str
    kind: PendingIntent
    source: str  # root-relative
    destination: str  # root-relative
    source_revision: int
    expected_sha256: str
    expected_size: int | None
    decision_revision: int
    intent_revision: int
    location_version: int
    expected_previous_location: str | None = None
    origin_batch_id: str | None = None


@dataclass(frozen=True)
class SkippedOperation:
    document_id: str
    reason: str
    kind: str | None = None


@dataclass
class ActionPlan:
    """Immutable plan. ``plan_hash`` covers every other field."""

    schema_version: str
    instance_id: str
    batch_id: str
    criteria_version: int
    operations: list[PlannedOperation]
    plan_hash: str = ""
    created_at: str | None = None
    requested_by: str | None = None
    skipped: list[SkippedOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def compute_hash(self) -> str:
        payload = {
            k: v
            for k, v in jsonable(self).items()
            if k not in ("plan_hash", "created_at", "requested_by", "counts")
        }
        return canonical_hash(payload)


@dataclass
class ApprovalRecord:
    """Plan-bound human authorization. Model output can never construct one."""

    actor: str
    plan_hash: str
    approved_at: str
    expires_at: str

    def is_expired(self, now_iso: str) -> bool:
        return now_iso > self.expires_at


@dataclass
class FileOperationRecord:
    id: str
    batch_id: str
    document_id: str
    sequence: int
    kind: PendingIntent
    source_rel_path: str
    destination_rel_path: str
    expected_sha256: str
    expected_size: int | None
    source_revision: int
    decision_revision: int
    intent_revision: int
    location_version: int
    state: OperationState = OperationState.PLANNED
    observed_source_state: str | None = None
    observed_destination_state: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


# ===========================================================================
# Principal / session
# ===========================================================================
@dataclass(frozen=True)
class Principal:
    """An authenticated actor.

    ``display_name`` comes from authentication, never from a text field typed into
    the page. ``None`` role means "not authenticated for this instance".
    """

    actor_ref: str
    role: Role
    session_id: str
    instance_id: str
    is_local_owner: bool = False

    def require(self, minimum: Role) -> None:
        from .errors import Forbidden

        if not self.role.at_least(minimum):
            raise Forbidden(
                "Your role does not permit this operation.",
                code="ROLE_INSUFFICIENT",
                detail={"required": str(minimum.value), "actual": str(self.role.value)},
            )

    @property
    def is_agent(self) -> bool:
        return self.actor_ref.startswith("agent:")

    @property
    def is_worker(self) -> bool:
        return self.actor_ref.startswith("worker:")


# ===========================================================================
# API envelope
# ===========================================================================
@dataclass
class Warning:
    code: str
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Envelope:
    """Standard response envelope (PRD section 12.2)."""

    ok: bool = True
    code: str = "OK"
    instance_id: str | None = None
    request_id: str = ""
    state_revision: int | None = None
    data: Any = None
    warnings: list[Warning] = field(default_factory=list)
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "code": self.code, "request_id": self.request_id}
        if self.instance_id is not None:
            out["instance_id"] = self.instance_id
        if self.state_revision is not None:
            out["state_revision"] = self.state_revision
        if self.job_id is not None:
            out["job_id"] = self.job_id
        out["data"] = jsonable(self.data)
        out["warnings"] = [jsonable(w) for w in self.warnings]
        return out


# ===========================================================================
# Pagination
# ===========================================================================
@dataclass
class Page:
    """Stable pagination. Always carries a deterministic ID tie-breaker."""

    items: list[Any]
    total: int
    page: int = 1
    page_size: int = 50
    sort: str = "ingested_at"
    direction: str = "asc"

    @property
    def page_count(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_more(self) -> bool:
        return self.page * self.page_size < self.total


def stable_page_slice(rows: Sequence[Any], page: int, page_size: int) -> list[Any]:
    """Slice an already-deterministically-ordered sequence. Never re-sorts."""
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50
    start = (page - 1) * page_size
    return list(rows[start : start + page_size])


# ===========================================================================
# Resource limits (PRD section 6.1)
# ===========================================================================
@dataclass(frozen=True)
class ResourceLimits:
    """Proposed defaults, adjustable. Exceeding one creates a review task and a
    visible reason; it never silently truncates and never auto-rejects."""

    max_source_bytes: int = 25 * 1024 * 1024
    max_pages: int = 50
    max_extracted_chars: int = 200_000
    stabilize_interval_seconds: float = 0.6
    stabilize_required_observations: int = 2
    max_analysis_attempts: int = 3
    max_repair_attempts: int = 1
    approval_lifetime_seconds: int = 900  # 15 minutes (PRD section 13.1)
    page_size_default: int = 50
    filter_max_depth: int = 3
    filter_max_predicates: int = 20


DEFAULT_LIMITS = ResourceLimits()


# ===========================================================================
# Reserved names and protected directories (PRD section 4)
# ===========================================================================
REVIEW_DIR = ".review"
REJECTED_DIR = "Rejected"
TRASH_DIR = "Trash"
REPORT_FILENAME = "review.html"
SNAPSHOT_FILENAME = "snapshot.json"

RESERVED_DIRS = (REVIEW_DIR, REJECTED_DIR, TRASH_DIR)

#: Subdirectories of .review, created during setup.
REVIEW_SUBDIRS = (
    "app",
    "app/templates",
    "app/assets",
    "extracted",
    "exports",
    "journals",
    "backups",
    "migrations",
    "locks",
    "tmp",
)

DISCOVERY_EXCLUDED_DIRS = (REVIEW_DIR, REJECTED_DIR, TRASH_DIR)
DISCOVERY_EXCLUDED_PREFIXES = ("~$", ".~lock.", "~")
DISCOVERY_EXCLUDED_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".swp", ".bak")


def iter_root_protected_names() -> Iterable[str]:
    yield from RESERVED_DIRS
    yield REPORT_FILENAME
