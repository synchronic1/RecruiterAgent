"""Allowlisted filter compiler for folder-scoped chat and filtering.

Authority: PRD section 9.2 ("Query pipeline"), 9.3 ("Proposed filter contract"),
9.4 ("Context isolation"), 7.4 ("Approved criteria and change management") and
11.2 ("Required invariants").

The model proposes; the helper compiles. This module is the security boundary
between a model-proposed filter tree and the ``documents`` query, so it obeys one
rule without exception:

    The compiler never executes, interpolates, or evaluates model-produced SQL,
    regular expressions, JavaScript, shell commands, or filesystem paths.

Everything a caller supplies is either a value bound as a ``?`` parameter or a
name looked up in a closed registry. A name that is not in the registry is
refused with :class:`~resume_review.errors.InvalidInput`; it is never pasted into
SQL text. Values are never compared against a field they do not belong to: an
enum member must be one of that field's published members, a timestamp must
parse, and a text value has a bounded length.

Three-valued evidence semantics (PRD 9.3)
-----------------------------------------
A criterion predicate is not a boolean column. It is true, false, or *unknown*.
An assessment is **known** only when the document has a current, non-stale
profile computed against the criteria version being filtered and that profile
carries an evidence row for the criterion with a recorded result. A missing or
stale assessment — no current profile, a profile flagged ``stale``, a profile
computed against a different ``criteria_version``, or no evidence row for the
criterion — evaluates to UNKNOWN. It never evaluates to false, because
``not_found`` means "not established in this document" and the system must never
translate that into "does not have" (PRD 9.2).

``unknown_policy`` decides what happens to an unknown row:

* ``include_with_warning`` (the default) includes it, so a filter can never
  silently hide unprocessed submissions. The compiled filter also carries
  ``unknown_where_sql``, an exact predicate over the rows that are in the result
  *because* their value is unknown, so the interface can show a count and a link.
* ``exclude`` ("show only supported") drops those rows, and the same
  ``unknown_where_sql`` reports how many were dropped.

No generic negation operator is offered (PRD 9.3). ``not`` is refused, as is any
operator, node type, or key this module does not register.

Layout of the emitted fragment
------------------------------
``CompiledFilter.where_sql`` is a self-contained boolean fragment intended for
the ``WHERE`` clause of a query over :data:`DOCUMENT_FILTER_FROM_SQL`. It refers
to the aliases that fragment establishes (``d`` documents, ``dec`` decisions,
``ai`` action intents). Pagination is stable because ``order_sql`` always ends
with the deterministic ``d.id`` tie-breaker (PRD 11.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any, Mapping, Sequence

from ..errors import Code, InvalidInput, ResumeReviewError
from ..models import (
    ExecutionState,
    Location,
    PendingIntent,
    ProcessingState,
    ReviewState,
    TaskState,
    UnknownPolicy,
    Warning,
    jsonable,
)
from ..util import parse_iso

__all__ = [
    "CompiledFilter",
    "FieldSpec",
    "SavedFilterStatus",
    "DEFAULT_FIELDS",
    "DOCUMENT_FILTER_FROM_SQL",
    "SORT_FIELDS",
    "CRITERION_OPS",
    "compile_filter",
    "validate_saved_filter",
    "unknown_count_sql",
]


# ---------------------------------------------------------------------------
# Bounds (PRD 9.3: "Bound tree depth to three and predicates to twenty")
# ---------------------------------------------------------------------------
MAX_DEPTH = 3
MAX_PREDICATES = 20
MAX_SORT_KEYS = 3
MAX_TEXT_LENGTH = 256
MAX_IN_VALUES = 100
MAX_CRITERION_ID_LENGTH = 128

#: Canonical base the emitted fragment is written against. The caller supplies
#: the instance scoping (``d.instance_id = ?``) and the SELECT list; this module
#: supplies the joins the fragment depends on.
DOCUMENT_FILTER_FROM_SQL = (
    "FROM documents d "
    "LEFT JOIN decisions dec ON dec.document_id = d.id "
    "LEFT JOIN action_intents ai ON ai.document_id = d.id"
)

#: Operators that address a criterion assessment rather than a column value.
CRITERION_OPS = frozenset(
    {"is_supported", "is_not_found", "is_unclear", "needs_manual_review"}
)

#: Node keys that must never be honored. Their presence is refused rather than
#: ignored, so a model that tries to smuggle an executable payload gets a visible
#: error instead of a silently dropped instruction.
_FORBIDDEN_KEYS = frozenset(
    {
        "sql",
        "raw_sql",
        "query",
        "regex",
        "regex_flags",
        "javascript",
        "js",
        "script",
        "shell",
        "command",
        "cmd",
        "exec",
        "eval",
        "path",
        "file",
        "filesystem",
    }
)

_GROUP_TYPES = frozenset({"and", "or"})


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """One registered filterable field.

    ``kind`` selects the compiler strategy:

    * ``scalar``   — the field is a SQL expression producing one value.
    * ``set_exists`` — the field is true for a document if it has a related row
      whose value is in the requested set (used by ``task_state``).
    * ``criterion`` — the field is a criterion assessment with three-valued
      semantics; it takes a criterion-op instead of a value.

    ``unknown_sql`` is a boolean expression that is true when the field is
    unknown. ``None`` means the field always has a value.
    """

    name: str
    kind: str
    ops: frozenset[str]
    value_sql: str | None = None
    value_type: str = "text"  # text | enum | bool | timestamp
    values: tuple[str, ...] | None = None
    unknown_sql: str | None = None
    exists_from: str | None = None
    exists_column: str | None = None


def _enum_values(enum_cls: Any) -> tuple[str, ...]:
    return tuple(str(member.value) for member in enum_cls)


DEFAULT_FIELDS: dict[str, FieldSpec] = {
    # Human decision. Absence of a decision row is ``unreviewed``, never unknown.
    "review_state": FieldSpec(
        name="review_state",
        kind="scalar",
        ops=frozenset({"in", "eq"}),
        value_sql="COALESCE(dec.disposition, 'unreviewed')",
        value_type="enum",
        values=_enum_values(ReviewState),
    ),
    "processing_state": FieldSpec(
        name="processing_state",
        kind="scalar",
        ops=frozenset({"in", "eq"}),
        value_sql="d.processing_state",
        value_type="enum",
        values=_enum_values(ProcessingState),
    ),
    "location": FieldSpec(
        name="location",
        kind="scalar",
        ops=frozenset({"in", "eq"}),
        value_sql="d.location",
        value_type="enum",
        values=_enum_values(Location),
    ),
    "pending_intent": FieldSpec(
        name="pending_intent",
        kind="scalar",
        ops=frozenset({"in", "eq"}),
        value_sql="COALESCE(ai.intent, 'none')",
        value_type="enum",
        values=_enum_values(PendingIntent),
    ),
    # The execution state of the batch this document most recently joined. A
    # document that has never been in a batch is unknown, not "canceled".
    "execution_state": FieldSpec(
        name="execution_state",
        kind="scalar",
        ops=frozenset({"in", "eq"}),
        value_sql=(
            "(SELECT ab.execution_state FROM file_operations fo "
            "JOIN action_batches ab ON ab.id = fo.batch_id "
            "WHERE fo.document_id = d.id AND fo.instance_id = d.instance_id "
            "ORDER BY fo.created_at DESC, fo.id DESC LIMIT 1)"
        ),
        value_type="enum",
        values=_enum_values(ExecutionState),
        unknown_sql=(
            "(SELECT ab.execution_state FROM file_operations fo "
            "JOIN action_batches ab ON ab.id = fo.batch_id "
            "WHERE fo.document_id = d.id AND fo.instance_id = d.instance_id "
            "ORDER BY fo.created_at DESC, fo.id DESC LIMIT 1) IS NULL"
        ),
    ),
    "ingested_at": FieldSpec(
        name="ingested_at",
        kind="scalar",
        ops=frozenset({"lt", "lte", "gt", "gte", "between"}),
        value_sql="d.ingested_at",
        value_type="timestamp",
    ),
    # NULL submitted_at is genuinely unknown and must not be coerced to a date.
    "submitted_at": FieldSpec(
        name="submitted_at",
        kind="scalar",
        ops=frozenset({"lt", "lte", "gt", "gte", "between"}),
        value_sql="d.submitted_at",
        value_type="timestamp",
        unknown_sql="d.submitted_at IS NULL",
    ),
    "original_filename": FieldSpec(
        name="original_filename",
        kind="scalar",
        ops=frozenset({"eq", "in", "contains"}),
        value_sql="d.original_filename",
        value_type="text",
    ),
    "display_name": FieldSpec(
        name="display_name",
        kind="scalar",
        ops=frozenset({"eq", "in", "contains"}),
        value_sql="d.display_name",
        value_type="text",
        unknown_sql="d.display_name IS NULL",
    ),
    "duplicate_content": FieldSpec(
        name="duplicate_content",
        kind="scalar",
        ops=frozenset({"eq"}),
        value_sql="d.duplicate_content",
        value_type="bool",
    ),
    "decision_needs_recheck": FieldSpec(
        name="decision_needs_recheck",
        kind="scalar",
        ops=frozenset({"eq"}),
        value_sql="(d.decision_needs_recheck OR COALESCE(dec.needs_recheck, 0))",
        value_type="bool",
    ),
    # A document with no tasks has no established task state. Under the default
    # policy that is unknown and the row is kept, never silently hidden.
    "task_state": FieldSpec(
        name="task_state",
        kind="set_exists",
        ops=frozenset({"in", "eq"}),
        value_type="enum",
        values=_enum_values(TaskState),
        exists_from="review_tasks t",
        exists_column="t.state",
        unknown_sql=(
            "NOT EXISTS (SELECT 1 FROM review_tasks t "
            "WHERE t.document_id = d.id AND t.instance_id = d.instance_id)"
        ),
    ),
}


#: Sortable fields. Values are closed SQL fragments, so a caller-supplied sort
#: key can never reach the query as text (PRD 11.2). Criterion fields are not
#: sortable: an assessment has no single scalar order.
SORT_FIELDS: dict[str, str] = {
    "ingested_at": "d.ingested_at",
    "submitted_at": "d.submitted_at",
    "original_filename": "d.original_filename",
    "display_name": "d.display_name",
    "current_rel_path": "d.current_rel_path",
    "size_bytes": "d.size_bytes",
    "processing_state": "d.processing_state",
    "review_state": "COALESCE(dec.disposition, 'unreviewed')",
    "location": "d.location",
    "pending_intent": "COALESCE(ai.intent, 'none')",
    "document_id": "d.id",
}

_DEFAULT_SORT: tuple[dict[str, str], ...] = ({"field": "ingested_at", "direction": "asc"},)


# ---------------------------------------------------------------------------
# Compiled result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompiledFilter:
    """A parameterized fragment, ready to embed in a documents query.

    ``where_sql`` already reflects ``unknown_policy``. ``unknown_where_sql`` is
    the exact set of rows whose membership depends on unknown handling — those
    included *because* unknown under ``include_with_warning``, or omitted under
    ``exclude``. Its parameters are in ``unknown_params``.
    """

    where_sql: str
    params: tuple[Any, ...]
    order_sql: str
    unknown_policy: UnknownPolicy
    warnings: tuple[Warning, ...] = ()
    unknown_where_sql: str = "0"
    unknown_params: tuple[Any, ...] = ()
    unknown_predicates: int = 0
    predicate_count: int = 0
    depth: int = 0
    criteria_version: int = 0

    def describe(self) -> dict[str, Any]:
        """A JSON-safe view of the interpreted filter, for the review UI.

        The browser shows the interpreted conditions and unknown-value treatment
        before applying them (PRD 9.2), so this returns the compiled shape and
        never only the original expression.
        """
        return {
            "where_sql": self.where_sql,
            "params": [jsonable(p) for p in self.params],
            "order_sql": self.order_sql,
            "unknown_policy": self.unknown_policy.value,
            "unknown_where_sql": self.unknown_where_sql,
            "unknown_params": [jsonable(p) for p in self.unknown_params],
            "unknown_predicates": self.unknown_predicates,
            "predicate_count": self.predicate_count,
            "depth": self.depth,
            "criteria_version": self.criteria_version,
            "warnings": [jsonable(w) for w in self.warnings],
        }


def unknown_count_sql(
    compiled: CompiledFilter, *, instance_id: str | None = None
) -> tuple[str, tuple[Any, ...]]:
    """A ``COUNT(*)`` statement over :data:`DOCUMENT_FILTER_FROM_SQL` counting the
    rows whose membership depends on unknown handling.

    This is how the interface gets the count and the link the PRD requires: the
    number included because unknown under the default policy, or omitted when the
    reviewer explicitly chooses show-only-supported.
    """
    clause = compiled.unknown_where_sql
    params: tuple[Any, ...] = compiled.unknown_params
    if instance_id is not None:
        clause = "d.instance_id = ? AND (" + clause + ")"
        params = (instance_id, *params)
    return (
        "SELECT COUNT(*) " + DOCUMENT_FILTER_FROM_SQL + " WHERE " + clause,
        params,
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def _refuse(message: str, *, code: str, **detail: Any) -> InvalidInput:
    return InvalidInput(message, code=code, detail=dict(detail))


def _refuse_field(name: Any) -> InvalidInput:
    return _refuse(
        "That field is not registered for filtering.",
        code=Code.FILTER_FIELD_NOT_ALLOWED,
        field=name if isinstance(name, str) else repr(name),
    )


def _refuse_op(name: Any, field_name: str | None = None) -> InvalidInput:
    return _refuse(
        "That operator is not registered for this field.",
        code=Code.FILTER_OPERATOR_NOT_ALLOWED,
        op=name if isinstance(name, str) else repr(name),
        field=field_name,
    )


def _check_keys(node: Mapping[str, Any]) -> None:
    offending = sorted(k for k in node.keys() if str(k).lower() in _FORBIDDEN_KEYS)
    if offending:
        raise _refuse(
            "A filter node may not carry executable or filesystem keys.",
            code=Code.INVALID_INPUT,
            keys=offending,
        )


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------
def _coerce_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise _refuse(
            "A timestamp filter value must be an ISO-8601 string.",
            code=Code.FILTER_UNKNOWN_VALUE,
            value_type=type(value).__name__,
        )
    try:
        parsed = parse_iso(value)
    except (TypeError, ValueError):
        raise _refuse(
            "A timestamp filter value must be an ISO-8601 string.",
            code=Code.FILTER_UNKNOWN_VALUE,
        ) from None
    # ISO-8601 strings compare lexicographically only when they share one offset,
    # and the database stores timestamps as UTC, so the bound value is normalized
    # to UTC rather than trusted in whatever offset the caller supplied.
    return parsed.astimezone(timezone.utc).isoformat()


def _coerce_bool(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return 1 if value.lower() == "true" else 0
    raise _refuse(
        "A boolean filter value must be true, false, 1, or 0.",
        code=Code.FILTER_UNKNOWN_VALUE,
        value_type=type(value).__name__,
    )


def _coerce_enum(value: Any, spec: FieldSpec) -> str:
    if not isinstance(value, str):
        raise _refuse(
            "An enum filter value must be a string.",
            code=Code.FILTER_UNKNOWN_VALUE,
            field=spec.name,
        )
    allowed = spec.values or ()
    if value not in allowed:
        raise _refuse(
            "That value is not a member of this field.",
            code=Code.FILTER_UNKNOWN_VALUE,
            field=spec.name,
        )
    return value


def _coerce_text(value: Any) -> str:
    if not isinstance(value, str):
        raise _refuse(
            "A text filter value must be a string.",
            code=Code.FILTER_UNKNOWN_VALUE,
            value_type=type(value).__name__,
        )
    if not value or len(value) > MAX_TEXT_LENGTH:
        raise _refuse(
            "A text filter value must be between 1 and "
            f"{MAX_TEXT_LENGTH} characters.",
            code=Code.FILTER_UNKNOWN_VALUE,
            length=len(value),
        )
    return value


def _as_value_list(value: Any, spec: FieldSpec) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _refuse(
            "The 'in' operator takes a list of values.",
            code=Code.INVALID_INPUT,
            field=spec.name,
        )
    items = list(value)
    if not items:
        raise _refuse(
            "The 'in' operator takes a non-empty list of values.",
            code=Code.INVALID_INPUT,
            field=spec.name,
        )
    if len(items) > MAX_IN_VALUES:
        raise _refuse(
            f"The 'in' operator accepts at most {MAX_IN_VALUES} values.",
            code=Code.FILTER_TOO_WIDE,
            field=spec.name,
            count=len(items),
        )
    return items


def _like_pattern(value: str) -> str:
    """Escape LIKE wildcards, then wrap. The caller's text is never pattern syntax."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Fragment accumulation
# ---------------------------------------------------------------------------
@dataclass
class _Node:
    """One compiled subtree in both unknown policies, plus what the compiler needs
    to know for the unknown-only count."""

    include_sql: str
    include_params: list[Any]
    exclude_sql: str
    exclude_params: list[Any]
    has_unknown: bool
    unknown_predicates: int


@dataclass
class _State:
    """Mutable counters carried through the walk so bounds fail as they are met."""

    criteria_version: int
    predicates: int = 0
    max_depth: int = 0


def _scalar_predicate(spec: FieldSpec, op: str, value: Any, state: _State) -> _Node:
    column = spec.value_sql or ""
    true_sql: str
    true_params: list[Any]

    if op == "eq":
        if spec.value_type == "enum":
            true_sql, true_params = f"{column} = ?", [_coerce_enum(value, spec)]
        elif spec.value_type == "bool":
            true_sql, true_params = f"{column} = ?", [_coerce_bool(value)]
        elif spec.value_type == "timestamp":
            true_sql, true_params = f"{column} = ?", [_coerce_timestamp(value)]
        else:
            true_sql, true_params = f"{column} = ?", [_coerce_text(value)]
    elif op == "in":
        items = _as_value_list(value, spec)
        if spec.value_type == "enum":
            coerced = [_coerce_enum(v, spec) for v in items]
        elif spec.value_type == "bool":
            coerced = [_coerce_bool(v) for v in items]
        elif spec.value_type == "timestamp":
            coerced = [_coerce_timestamp(v) for v in items]
        else:
            coerced = [_coerce_text(v) for v in items]
        placeholders = ", ".join("?" for _ in coerced)
        true_sql, true_params = f"{column} IN ({placeholders})", list(coerced)
    elif op == "contains":
        if spec.value_type != "text":
            raise _refuse_op(op, spec.name)
        true_sql = f"{column} LIKE ? ESCAPE '\\'"
        true_params = [_like_pattern(_coerce_text(value))]
    elif op in ("lt", "lte", "gt", "gte"):
        symbol = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[op]
        true_sql = f"{column} {symbol} ?"
        true_params = [_coerce_timestamp(value)]
    elif op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise _refuse(
                "The 'between' operator takes exactly two timestamp values.",
                code=Code.INVALID_INPUT,
                field=spec.name,
            )
        low, high = _coerce_timestamp(value[0]), _coerce_timestamp(value[1])
        true_sql = f"{column} BETWEEN ? AND ?"
        true_params = [low, high]
    else:  # pragma: no cover - ops are gated by the registry before we get here
        raise _refuse_op(op, spec.name)

    unknown_sql = spec.unknown_sql
    if unknown_sql:
        return _Node(
            include_sql=f"({true_sql} OR {unknown_sql})",
            include_params=[*true_params],
            exclude_sql=true_sql,
            exclude_params=[*true_params],
            has_unknown=True,
            unknown_predicates=1,
        )
    return _Node(
        include_sql=true_sql,
        include_params=[*true_params],
        exclude_sql=true_sql,
        exclude_params=[*true_params],
        has_unknown=False,
        unknown_predicates=0,
    )


def _set_exists_predicate(spec: FieldSpec, op: str, value: Any, state: _State) -> _Node:
    items = _as_value_list(value, spec) if op == "in" else [value]
    coerced = [_coerce_enum(v, spec) for v in items]
    placeholders = ", ".join("?" for _ in coerced)
    subquery = (
        "EXISTS (SELECT 1 FROM " + str(spec.exists_from)
        + " WHERE t.document_id = d.id AND t.instance_id = d.instance_id AND "
        + str(spec.exists_column) + f" IN ({placeholders}))"
    )
    unknown_sql = spec.unknown_sql or ""
    return _Node(
        include_sql=f"({subquery} OR {unknown_sql})" if unknown_sql else subquery,
        include_params=list(coerced),
        exclude_sql=subquery,
        exclude_params=list(coerced),
        has_unknown=bool(unknown_sql),
        unknown_predicates=1 if unknown_sql else 0,
    )


def _criterion_exists_sql(result: str | None) -> str:
    """A correlated EXISTS over the document's current, fresh assessment.

    ``result is None`` means "any recorded result for this criterion", which is
    what makes a missing assessment distinguishable from a false one.
    """
    clause = (
        "EXISTS (SELECT 1 FROM profiles p JOIN evidence e ON e.profile_id = p.id "
        "WHERE p.document_id = d.id AND p.instance_id = d.instance_id "
        "AND p.is_current = 1 AND p.stale = 0 AND p.criteria_version = ? "
        "AND e.criterion_id = ? AND e.result IS NOT NULL"
    )
    if result is None:
        return clause + ")"
    return clause + " AND e.result = ?)"


_CRITERION_TARGET = {
    "is_supported": "supported",
    "is_not_found": "not_found",
    "is_unclear": "unclear",
    "needs_manual_review": "needs_manual_review",
}


def _criterion_predicate(spec: FieldSpec, op: str, criterion_id: str, state: _State) -> _Node:
    target = _CRITERION_TARGET[op]
    cv = state.criteria_version
    true_sql = _criterion_exists_sql(target)
    true_params = [cv, criterion_id, target]
    any_sql = _criterion_exists_sql(None)
    any_params = [cv, criterion_id]
    # Included when the assessment is positively the target, or when there is no
    # assessment to contradict it. Never when the assessment is known non-target.
    include_sql = f"({true_sql} OR NOT {any_sql})"
    return _Node(
        include_sql=include_sql,
        include_params=[*true_params, *any_params],
        exclude_sql=true_sql,
        exclude_params=[*true_params],
        has_unknown=True,
        unknown_predicates=1,
    )


def _criterion_id(name: str) -> str | None:
    """Return the criterion id when ``name`` has the registered ``criterion:`` form."""
    if not name.startswith("criterion:"):
        return None
    criterion_id = name[len("criterion:") :]
    if not criterion_id or len(criterion_id) > MAX_CRITERION_ID_LENGTH:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
    if any(ch not in allowed for ch in criterion_id):
        return None
    return criterion_id


def _compile_node(
    node: Any,
    *,
    depth: int,
    fields: Mapping[str, FieldSpec],
    state: _State,
) -> _Node:
    if depth > MAX_DEPTH:
        raise _refuse(
            f"A filter tree may be at most {MAX_DEPTH} levels deep.",
            code=Code.FILTER_TOO_DEEP,
            depth=depth,
        )
    state.max_depth = max(state.max_depth, depth)

    if not isinstance(node, Mapping):
        raise _refuse(
            "A filter node must be a JSON object.",
            code=Code.INVALID_INPUT,
            node_type=type(node).__name__,
        )
    _check_keys(node)

    node_type = node.get("type")
    if node_type in _GROUP_TYPES:
        for key in ("field", "op"):
            if key in node:
                raise _refuse(
                    "A group node takes children, not a field or operator.",
                    code=Code.INVALID_INPUT,
                    node_type=str(node_type),
                    key=key,
                )
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, (list, tuple)) or isinstance(children, (str, bytes)):
            raise _refuse(
                "A group node's children must be a list.",
                code=Code.INVALID_INPUT,
                node_type=str(node_type),
            )
        if not children:
            raise _refuse(
                "A group node must have at least one child.",
                code=Code.INVALID_INPUT,
                node_type=str(node_type),
            )
        compiled = [
            _compile_node(child, depth=depth + 1, fields=fields, state=state)
            for child in children
        ]
        joiner = " AND " if node_type == "and" else " OR "
        include_sql = "(" + joiner.join(c.include_sql for c in compiled) + ")"
        exclude_sql = "(" + joiner.join(c.exclude_sql for c in compiled) + ")"
        include_params: list[Any] = []
        exclude_params: list[Any] = []
        for child in compiled:
            include_params.extend(child.include_params)
            exclude_params.extend(child.exclude_params)
        return _Node(
            include_sql=include_sql,
            include_params=include_params,
            exclude_sql=exclude_sql,
            exclude_params=exclude_params,
            has_unknown=any(c.has_unknown for c in compiled),
            unknown_predicates=sum(c.unknown_predicates for c in compiled),
        )

    if node_type == "not" or node_type == "negation":
        raise _refuse(
            "A generic negation operator is not available; its unknown behaviour "
            "is not implemented.",
            code=Code.FILTER_OPERATOR_NOT_ALLOWED,
            node_type=str(node_type),
        )

    if node_type != "predicate":
        raise _refuse(
            "A filter node must be 'and', 'or', or 'predicate'.",
            code=Code.INVALID_INPUT,
            node_type=str(node_type),
        )

    state.predicates += 1
    if state.predicates > MAX_PREDICATES:
        raise _refuse(
            f"A filter may contain at most {MAX_PREDICATES} predicates.",
            code=Code.FILTER_TOO_WIDE,
            predicates=state.predicates,
        )

    if "children" in node:
        raise _refuse(
            "A predicate node may not carry children.",
            code=Code.INVALID_INPUT,
        )

    name = node.get("field")
    if not isinstance(name, str) or not name:
        raise _refuse_field(name)
    op = node.get("op")
    if not isinstance(op, str) or not op:
        raise _refuse_op(op, name if isinstance(name, str) else None)

    criterion_id = _criterion_id(name)
    if criterion_id is not None:
        spec = FieldSpec(
            name=name,
            kind="criterion",
            ops=CRITERION_OPS,
            value_type="criterion",
        )
    else:
        spec = fields.get(name)
        if spec is None:
            raise _refuse_field(name)

    if op not in spec.ops:
        raise _refuse_op(op, spec.name)

    if spec.kind == "criterion":
        # A value alongside a criterion operator is meaningless and is ignored;
        # the criterion id travels as a bound parameter, never as SQL text.
        return _criterion_predicate(spec, op, criterion_id, state)

    value = node.get("value")
    if spec.kind == "set_exists":
        return _set_exists_predicate(spec, op, value, state)
    return _scalar_predicate(spec, op, value, state)


def _compile_sort(sort: Any) -> str:
    if sort is None:
        keys: Sequence[Any] = list(_DEFAULT_SORT)
    else:
        if isinstance(sort, Mapping):
            keys = [sort]
        elif isinstance(sort, (list, tuple)) and not isinstance(sort, (str, bytes)):
            keys = list(sort)
        else:
            raise _refuse(
                "Sort must be a list of {field, direction} objects.",
                code=Code.INVALID_INPUT,
            )
    if len(keys) > MAX_SORT_KEYS:
        raise _refuse(
            f"At most {MAX_SORT_KEYS} sort keys are allowed.",
            code=Code.FILTER_TOO_WIDE,
            count=len(keys),
        )

    fragments: list[str] = []
    last_field: str | None = None
    for entry in keys:
        if not isinstance(entry, Mapping):
            raise _refuse(
                "A sort key must be a {field, direction} object.",
                code=Code.INVALID_INPUT,
            )
        _check_keys(entry)
        sort_field = entry.get("field")
        if not isinstance(sort_field, str) or sort_field not in SORT_FIELDS:
            raise _refuse_field(sort_field)
        direction = entry.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise _refuse(
                "A sort direction must be 'asc' or 'desc'.",
                code=Code.INVALID_INPUT,
                direction=direction,
            )
        fragments.append(f"{SORT_FIELDS[sort_field]} {direction.upper()}")
        last_field = sort_field

    # Stable pagination always ends with the deterministic document-id tie-breaker
    # (PRD 11.2), so two rows sharing a sort value keep a reproducible order.
    if last_field != "document_id":
        fragments.append("d.id ASC")
    return ", ".join(fragments)


def _coerce_policy(unknown_policy: Any) -> UnknownPolicy:
    if unknown_policy is None:
        return UnknownPolicy.INCLUDE_WITH_WARNING
    if isinstance(unknown_policy, UnknownPolicy):
        return unknown_policy
    text = str(unknown_policy)
    if text == UnknownPolicy.INCLUDE_WITH_WARNING.value:
        return UnknownPolicy.INCLUDE_WITH_WARNING
    # The reviewer's explicit "show only supported" mode.
    if text in (UnknownPolicy.EXCLUDE.value, "show_only_supported"):
        return UnknownPolicy.EXCLUDE
    raise _refuse(
        "Unsupported unknown-value policy.",
        code=Code.INVALID_INPUT,
        unknown_policy=text,
    )


def compile_filter(
    expression: Any,
    *,
    criteria_version: int,
    fields: Mapping[str, FieldSpec] | None = DEFAULT_FIELDS,
    unknown_policy: Any = None,
    sort: Any = None,
) -> CompiledFilter:
    """Compile a model-proposed filter into a parameterized SQL fragment.

    ``expression`` is a small tree of ``and`` / ``or`` groups and leaf predicates
    (PRD 9.3). ``None`` means "no filter" and compiles to a match-all fragment.

    ``criteria_version`` is the version a criterion predicate is evaluated
    against; a profile computed against any other version is unknown. Every
    refusal raises :class:`~resume_review.errors.InvalidInput` with a stable
    ``FILTER_*`` code — nothing is silently truncated or dropped.
    """
    if isinstance(criteria_version, bool) or not isinstance(criteria_version, int):
        raise _refuse(
            "A criteria version must be an integer.",
            code=Code.INVALID_INPUT,
            field="criteria_version",
        )
    if criteria_version < 0:
        raise _refuse(
            "A criteria version must not be negative.",
            code=Code.INVALID_INPUT,
            field="criteria_version",
        )
    registry = DEFAULT_FIELDS if fields is None else fields

    # Also accept the full proposed-filter object (PRD 9.3), whose ``expression``
    # key carries the tree and whose ``unknown_policy`` / ``sort`` travel with it.
    # A group or predicate node always has a ``type``, so the shapes cannot be
    # confused. The criteria version is deliberately never read from the model's
    # object: the helper binds it from the instance.
    if isinstance(expression, Mapping) and "expression" in expression and "type" not in expression:
        envelope = expression
        expression = envelope.get("expression")
        if unknown_policy is None and envelope.get("unknown_policy") is not None:
            unknown_policy = envelope["unknown_policy"]
        if sort is None and envelope.get("sort") is not None:
            sort = envelope["sort"]

    policy = _coerce_policy(unknown_policy)
    order_sql = _compile_sort(sort)

    if expression is None:
        return CompiledFilter(
            where_sql="1 = 1",
            params=(),
            order_sql=order_sql,
            unknown_policy=policy,
            warnings=(),
            unknown_where_sql="0",
            unknown_params=(),
            unknown_predicates=0,
            predicate_count=0,
            depth=0,
            criteria_version=int(criteria_version),
        )

    state = _State(criteria_version=int(criteria_version))
    root = _compile_node(expression, depth=1, fields=registry, state=state)

    if policy is UnknownPolicy.INCLUDE_WITH_WARNING:
        where_sql, params = root.include_sql, list(root.include_params)
    else:
        where_sql, params = root.exclude_sql, list(root.exclude_params)

    if root.has_unknown:
        # Exact "membership depends on unknown handling" set: the difference
        # between the default policy's result and show-only-supported. It is
        # written as include AND not-exclude rather than a boolean subtraction of
        # the two fragments, because an unknown leaf evaluates to NULL inside a
        # NOT and would otherwise disappear from the difference instead of being
        # counted. COALESCE collapses that NULL to "not excluded".
        unknown_where_sql = f"({root.include_sql}) AND NOT COALESCE({root.exclude_sql}, 0)"
        unknown_params: list[Any] = [*root.include_params, *root.exclude_params]
    else:
        unknown_where_sql = "0"
        unknown_params = []

    warnings: list[Warning] = []
    if root.has_unknown:
        if policy is UnknownPolicy.INCLUDE_WITH_WARNING:
            warnings.append(
                Warning(
                    code=Code.FILTER_UNKNOWN_VALUE,
                    message=(
                        "Rows whose value is unknown are included; the result "
                        "reports how many were included because they were unknown."
                    ),
                    detail={
                        "unknown_predicates": root.unknown_predicates,
                        "unknown_count_sql": unknown_where_sql,
                    },
                )
            )
        else:
            warnings.append(
                Warning(
                    code=Code.FILTER_UNKNOWN_VALUE,
                    message=(
                        "Show-only-supported is active: rows whose value is unknown "
                        "are omitted. The omitted count remains available."
                    ),
                    detail={"unknown_predicates": root.unknown_predicates},
                )
            )

    return CompiledFilter(
        where_sql=where_sql,
        params=tuple(params),
        order_sql=order_sql,
        unknown_policy=policy,
        warnings=tuple(warnings),
        unknown_where_sql=unknown_where_sql,
        unknown_params=tuple(unknown_params),
        unknown_predicates=root.unknown_predicates,
        predicate_count=state.predicates,
        depth=state.max_depth,
        criteria_version=int(criteria_version),
    )


# ---------------------------------------------------------------------------
# Saved filters (PRD 7.4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SavedFilterStatus:
    """Whether a saved filter may still be applied, and why not if it may not.

    A saved filter that refers to a removed criterion is *visibly invalid until
    revised* (PRD 7.4). ``valid`` is False and ``reason_codes`` names the
    machine-readable cause, so the interface can show the filter as broken rather
    than quietly returning a different set of rows.
    """

    filter_id: str | None
    name: str | None
    valid: bool
    criteria_version: int
    active_criteria_version: int
    missing_criteria: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    message: str = ""
    warnings: tuple[Warning, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_id": self.filter_id,
            "name": self.name,
            "valid": self.valid,
            "criteria_version": self.criteria_version,
            "active_criteria_version": self.active_criteria_version,
            "missing_criteria": list(self.missing_criteria),
            "reason_codes": list(self.reason_codes),
            "message": self.message,
            "warnings": [jsonable(w) for w in self.warnings],
        }


def _active_criterion_ids(active_criteria: Any) -> tuple[set[str], int]:
    ids: set[str] = set()
    version = 0
    for item in active_criteria or ():
        if isinstance(item, str):
            ids.add(item)
            continue
        criterion_id = getattr(item, "criterion_id", None)
        if criterion_id is None and isinstance(item, Mapping):
            criterion_id = item.get("criterion_id")
        if criterion_id is not None:
            ids.add(str(criterion_id))
        item_version = getattr(item, "version", None)
        if item_version is None and isinstance(item, Mapping):
            item_version = item.get("version")
        if isinstance(item_version, int) and not isinstance(item_version, bool):
            version = max(version, item_version)
    return ids, version


def _referenced_criteria(node: Any) -> set[str]:
    """Collect criterion ids a saved expression refers to, tolerating a malformed
    tree: an invalid filter must be reported, not crash the list view."""
    found: set[str] = set()
    if isinstance(node, Mapping):
        name = node.get("field")
        if isinstance(name, str):
            criterion_id = _criterion_id(name)
            if criterion_id is not None:
                found.add(criterion_id)
        children = node.get("children")
        if isinstance(children, (list, tuple)):
            for child in children:
                found |= _referenced_criteria(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            found |= _referenced_criteria(child)
    return found


def _saved_expression(saved: Any) -> Any:
    if not isinstance(saved, Mapping):
        return saved
    for key in ("definition", "expression", "tree"):
        if key in saved and saved[key] is not None:
            value = saved[key]
            if isinstance(value, Mapping) and "expression" in value:
                return value["expression"]
            return value
    return saved


def _saved_field(saved: Any, key: str) -> Any:
    if isinstance(saved, Mapping):
        return saved.get(key)
    return getattr(saved, key, None)


def validate_saved_filter(saved: Any, *, active_criteria: Any) -> SavedFilterStatus:
    """Report whether a saved filter still applies against the active criteria.

    Returns a status object rather than raising: a saved filter that references a
    removed criterion becomes visibly invalid until revised (PRD 7.4), and the
    list view must still render. Compiling with the active criteria version keeps
    the staleness rule consistent: a criterion that still exists but whose
    assessment was computed against a superseded version reads as unknown, and
    under the default policy those rows stay visible.
    """
    filter_id = _saved_field(saved, "filter_id") or _saved_field(saved, "id")
    name = _saved_field(saved, "name")
    saved_version = _saved_field(saved, "criteria_version")
    saved_version = int(saved_version) if isinstance(saved_version, int) and not isinstance(saved_version, bool) else 0

    active_ids, active_version = _active_criterion_ids(active_criteria)
    expression = _saved_expression(saved)
    referenced = _referenced_criteria(expression)
    missing = tuple(sorted(referenced - active_ids))

    warnings: list[Warning] = []
    reasons: list[str] = []
    message = ""

    if missing:
        reasons.append(Code.FILTER_INVALID_FOR_CRITERIA)
        message = (
            "This saved filter refers to a criterion that is no longer available; "
            "revise it before applying."
        )

    try:
        compile_filter(expression, criteria_version=active_version or saved_version)
    except ResumeReviewError as exc:
        reasons.append(exc.code)
        message = message or exc.message

    if not missing and saved_version and active_version and saved_version != active_version:
        warnings.append(
            Warning(
                code=Code.ANALYSIS_STALE_RESULT,
                message=(
                    "This filter was saved against criteria version "
                    f"{saved_version}; version {active_version} is now active, so "
                    "criterion results read as unknown until reassessed."
                ),
                detail={
                    "saved_criteria_version": saved_version,
                    "active_criteria_version": active_version,
                },
            )
        )

    valid = not reasons
    return SavedFilterStatus(
        filter_id=str(filter_id) if filter_id is not None else None,
        name=str(name) if name is not None else None,
        valid=valid,
        criteria_version=saved_version,
        active_criteria_version=active_version,
        missing_criteria=missing,
        reason_codes=tuple(dict.fromkeys(reasons)),
        message=message if not valid else "",
        warnings=tuple(warnings),
    )
