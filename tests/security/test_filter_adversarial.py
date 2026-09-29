"""Adversarial tests that try to falsify the allowlisted filter compiler.

Authority: AGENTS.md constraint 7 ("No unrestricted Gateway proxy. Analysis runs
on a restricted route"), PRD section 9.2 ("Compile a small allowlisted filter
tree into parameterized queries. Never execute model-produced SQL ..."), 9.3
("Allow only registered fields and operators. Bound tree depth to three and
predicates to twenty ... a missing or stale assessment evaluates unknown. Do not
introduce a generic negation operator ...") and 11.2 (stable pagination).

Unlike :mod:`tests.security.test_filter_injection`, which attacks single vectors
against a seeded instance, this file attacks the compiler as a *whole*: it tries
to smuggle caller text into every SQL fragment the compiler emits, drives every
registered field/operator pair to check that placeholders and bound parameters
cannot drift apart, and proves the unknown-value contract by *executing* the
generated SQL against a database migrated from ``migrations/0001_initial.sql``.
Compiled SQL that cannot be executed is not evidence, so every clause this file
asserts about is also run.

Synthetic data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.filters import (
    CRITERION_OPS,
    DEFAULT_FIELDS,
    DOCUMENT_FILTER_FROM_SQL,
    compile_filter,
    unknown_count_sql,
)
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.errors import Code, InvalidInput
from resume_review.models import (
    AnalysisResult,
    CriterionResult,
    EvidenceItem,
    MediaType,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "filter_adv.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_adv", __version__, SCHEMA_VERSION)
    return repository


def predicate(field: str, op: str, value=None) -> dict:
    node = {"type": "predicate", "field": field, "op": op}
    if value is not None:
        node["value"] = value
    return node


def execute(db: Database, compiled, instance_id: str) -> list[str]:
    """Run a compiled fragment exactly as a caller would embed it."""
    sql = (
        "SELECT d.id "
        + DOCUMENT_FILTER_FROM_SQL
        + " WHERE d.instance_id = ? AND ("
        + compiled.where_sql
        + ") ORDER BY "
        + compiled.order_sql
    )
    return [str(row["id"]) for row in db.query(sql, (instance_id, *compiled.params))]


def execute_unknown_count(db: Database, compiled, instance_id: str) -> int:
    sql, params = unknown_count_sql(compiled, instance_id=instance_id)
    return int(db.scalar(sql, params, default=0) or 0)


def make_document(repo: Repository, name: str):
    return repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=1,
        content_sha256=None,
        fs_identity=None,
    )


def add_profile(
    repo: Repository,
    document_id: str,
    *,
    criteria_version: int,
    result=CriterionResult.SUPPORTED,
    criterion_id: str = "cr_01",
    stale: bool = False,
    result_is_null: bool = False,
):
    """Commit one current profile, optionally with a NULL-result evidence row."""
    evidence = [
        EvidenceItem(
            id=f"ev_{document_id}",
            span_id="span_1",
            quote="synthetic quote",
            criterion_id=criterion_id,
            result=None if result_is_null else result,
        )
    ]
    repo.insert_profile(
        AnalysisResult(
            schema_version="1.0",
            document_id=document_id,
            source_revision=1,
            criteria_version=criteria_version,
            summary_text="synthetic",
            evidence=evidence,
        ),
        {"prompt_version": "p1", "model_route": "fixture"},
    )
    if stale:
        repo.mark_profiles_stale(document_id, reason="synthetic")


#: Payloads chosen so that none is a substring of a registry constant: a match in
#: emitted SQL therefore means genuine interpolation, not a false positive on the
#: compiler's own ``d.id`` / ``review_state`` text.
INJECTION_PAYLOADS = (
    "x' OR '1'='1",
    "x' OR 1=1 --",
    "'; DROP TABLE documents; --",
    "x' UNION SELECT disposition FROM decisions --",
    "%' OR current_rel_path LIKE '%",
    "_' OR 1=1 --",
    "x' AND (SELECT COUNT(*) FROM sqlite_master) > 0 --",
)


# ---------------------------------------------------------------------------
# 1. Injection: caller text is bound, never interpolated, in every fragment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_text_value_appears_only_in_params_never_in_any_fragment(seeded_instance, payload):
    db, instance_id = seeded_instance
    compiled = compile_filter(
        predicate("original_filename", "eq", payload), criteria_version=1
    )
    assert compiled.params == (payload,)
    for fragment in (compiled.where_sql, compiled.order_sql, compiled.unknown_where_sql):
        assert payload not in fragment
    assert compiled.where_sql.count("?") == len(compiled.params)
    # Executes, matches nothing, and drops nothing.
    assert execute(db, compiled, instance_id) == []


@pytest.fixture
def seeded_instance(db: Database, repo: Repository):
    make_document(repo, "alpha.pdf")
    make_document(repo, "beta.pdf")
    return db, repo.instance_id


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_contains_payload_is_escaped_and_bound(seeded_instance, payload):
    db, instance_id = seeded_instance
    compiled = compile_filter(
        predicate("original_filename", "contains", payload), criteria_version=1
    )
    escaped = payload.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    assert compiled.params == (f"%{escaped}%",)
    assert payload not in compiled.where_sql
    sql, params = unknown_count_sql(compiled, instance_id=instance_id)
    assert payload not in sql
    assert execute(db, compiled, instance_id) == []


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_sort_field_payload_is_refused_and_never_reaches_sql(seeded_instance, payload):
    db, instance_id = seeded_instance
    with pytest.raises(InvalidInput) as err:
        compile_filter(None, criteria_version=0, sort=[{"field": payload}])
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED
    # The schema is untouched and the two documents survive.
    assert db.scalar("SELECT COUNT(*) FROM documents") == 2


def test_sort_direction_cannot_be_smuggled_into_order_sql():
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            None,
            criteria_version=0,
            sort=[{"field": "ingested_at", "direction": "asc; DROP TABLE documents"}],
        )
    # A direction that is not exactly asc/desc is refused; nothing was compiled.
    assert err.value.code == Code.INVALID_INPUT
    compiled = compile_filter(
        None, criteria_version=0, sort=[{"field": "ingested_at", "direction": "asc"}]
    )
    assert compiled.order_sql == "d.ingested_at ASC, d.id ASC"


def test_criterion_id_is_bound_not_interpolated_and_executes(seeded_instance):
    db, instance_id = seeded_instance
    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"), criteria_version=3
    )
    # The id travels as a parameter; the fragment carries no trace of it.
    assert "cr_01" not in compiled.where_sql
    assert compiled.params == (3, "cr_01", "supported", 3, "cr_01")
    assert compiled.where_sql.count("?") == len(compiled.params)
    # Both documents have no assessment, so both are unknown and both are returned.
    returned = set(execute(db, compiled, instance_id))
    assert returned == {str(r["id"]) for r in db.query("SELECT id FROM documents")}


@pytest.mark.parametrize(
    "field",
    [
        "criterion:cr_01) OR (1=1",
        "criterion:cr_01' OR '1'='1",
        "criterion:cr_01; DROP TABLE documents",
        "criterion:",
        "criterion:" + "a" * 129,
        "criterion:cr 01",
    ],
)
def test_malformed_criterion_id_is_refused_not_pasted(field: str):
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate(field, "is_supported"), criteria_version=3)
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED


def test_forbidden_key_nested_in_a_child_node_is_refused_not_honored():
    # A model that hides an executable key deeper in the tree is refused, so the
    # key can never be silently ignored and then acted on elsewhere.
    nested = {
        "type": "and",
        "children": [
            {
                "type": "or",
                "children": [predicate("review_state", "eq", "keep", ) | {"sql": "1=1"}],
            }
        ],
    }
    with pytest.raises(InvalidInput) as err:
        compile_filter(nested, criteria_version=1)
    assert err.value.code == Code.INVALID_INPUT
    assert "sql" in err.value.detail["keys"]


def test_value_that_is_a_dict_is_refused_for_a_text_field():
    with pytest.raises(InvalidInput):
        compile_filter(
            predicate("original_filename", "eq", {"$ne": None}), criteria_version=1
        )


def test_every_field_and_operator_keeps_placeholders_and_params_in_step():
    """Drive the whole registry: a drifted placeholder count is an injection bug."""
    known = {
        "review_state": "keep",
        "processing_state": "ready",
        "location": "active",
        "pending_intent": "none",
        "execution_state": "planned",
        "original_filename": "a.pdf",
        "display_name": "a",
        "duplicate_content": True,
        "decision_needs_recheck": False,
        "task_state": "open",
    }
    checked = 0
    for field, spec in DEFAULT_FIELDS.items():
        for op in spec.ops:
            if op == "between":
                value = ["2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00"]
            elif op == "in":
                value = [known[field]]
            elif op in ("lt", "lte", "gt", "gte"):
                value = "2024-01-01T00:00:00+00:00"
            elif op == "contains":
                value = "manager"
            elif spec.value_type == "bool":
                value = True
            else:
                value = known[field]
            compiled = compile_filter(
                predicate(field, op, value), criteria_version=3
            )
            assert compiled.where_sql.count("?") == len(compiled.params), (field, op)
            if compiled.unknown_where_sql != "0":
                assert compiled.unknown_where_sql.count("?") == len(
                    compiled.unknown_params
                ), (field, op)
            checked += 1
    for op in CRITERION_OPS:
        compiled = compile_filter(
            predicate("criterion:cr_01", op), criteria_version=3
        )
        assert compiled.where_sql.count("?") == len(compiled.params), op
        assert compiled.unknown_where_sql.count("?") == len(compiled.unknown_params), op
        checked += 1
    assert checked >= 20


# ---------------------------------------------------------------------------
# 2. Bounds: refusal with a code, nothing silently truncated or dropped
# ---------------------------------------------------------------------------
def _nested(levels: int) -> dict:
    node: dict = predicate("review_state", "eq", "keep")
    for _ in range(levels - 1):
        node = {"type": "and", "children": [node]}
    return node


def test_depth_three_compiles_and_depth_four_is_refused_with_a_code():
    assert compile_filter(_nested(3), criteria_version=1).depth == 3
    with pytest.raises(InvalidInput) as err:
        compile_filter(_nested(4), criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_DEEP
    assert err.value.http_status == 422
    assert err.value.detail["depth"] == 4


def test_twenty_predicates_compile_and_twenty_one_refused_none_silently_dropped():
    def group(count: int) -> dict:
        return {
            "type": "or",
            "children": [predicate("review_state", "eq", "keep") for _ in range(count)],
        }

    compiled = compile_filter(group(20), criteria_version=1)
    assert compiled.predicate_count == 20
    assert compiled.where_sql.count("?") == 20

    with pytest.raises(InvalidInput) as err:
        compile_filter(group(21), criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_WIDE
    assert err.value.detail["predicates"] == 21


def test_predicate_budget_is_counted_across_nested_groups_not_per_group():
    # 3 groups x 7 leaves would pass a per-group count; the shared counter refuses.
    expression = {
        "type": "or",
        "children": [
            {
                "type": "and",
                "children": [predicate("review_state", "eq", "keep") for _ in range(7)],
            }
            for _ in range(3)
        ],
    }
    with pytest.raises(InvalidInput) as err:
        compile_filter(expression, criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_WIDE
    assert err.value.detail["predicates"] == 21


def test_in_list_length_bound_is_refused_with_the_wide_code():
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            predicate("original_filename", "in", ["x"] * 101), criteria_version=1
        )
    assert err.value.code == Code.FILTER_TOO_WIDE
    assert err.value.detail["count"] == 101


# ---------------------------------------------------------------------------
# 3. No negation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "node",
    [
        {"type": "not", "children": [predicate("review_state", "eq", "keep")]},
        {"type": "negation", "children": [predicate("review_state", "eq", "keep")]},
    ],
)
def test_a_negation_node_is_refused_with_the_operator_code(node):
    with pytest.raises(InvalidInput) as err:
        compile_filter(node, criteria_version=1)
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


def test_no_operator_in_the_registry_expresses_negation():
    forbidden = {"not", "negate", "negation", "except", "exclude_if"}
    for spec in DEFAULT_FIELDS.values():
        assert not (spec.ops & forbidden), spec.name
        assert not spec.ops & {"like", "regex", "raw", "is_null"}


@pytest.mark.parametrize("field", ["review_state", "original_filename", "task_state"])
def test_not_as_an_operator_is_refused(field: str):
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate(field, "not", "keep"), criteria_version=1)
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 4. Unknown semantics: missing/stale is unknown, included and counted
# ---------------------------------------------------------------------------
def test_missing_assessment_is_unknown_and_the_row_is_still_returned(repo, db):
    instance = repo.instance_id
    assessed = make_document(repo, "assessed.pdf")
    unassessed = make_document(repo, "unassessed.pdf")  # no profile at all
    add_profile(repo, assessed.id, criteria_version=3, result=CriterionResult.SUPPORTED)

    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"), criteria_version=3
    )
    returned = set(execute(db, compiled, instance))
    assert assessed.id in returned
    # The unprocessed applicant is not hidden and is counted as unknown.
    assert unassessed.id in returned
    assert execute_unknown_count(db, compiled, instance) == 1


def test_stale_assessment_is_unknown_never_false_and_is_included_and_counted(repo, db):
    instance = repo.instance_id
    fresh = make_document(repo, "fresh.pdf")
    stale = make_document(repo, "stale.pdf")
    add_profile(repo, fresh.id, criteria_version=3, result=CriterionResult.SUPPORTED)
    add_profile(
        repo, stale.id, criteria_version=3, result=CriterionResult.SUPPORTED, stale=True
    )

    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"), criteria_version=3
    )
    returned = set(execute(db, compiled, instance))
    assert returned == {fresh.id, stale.id}
    assert execute_unknown_count(db, compiled, instance) == 1

    # Under show-only-supported the stale row is omitted *by explicit choice*, and
    # the same count reports it as omitted -- no silent disappearance.
    excluded = compile_filter(
        predicate("criterion:cr_01", "is_supported"),
        criteria_version=3,
        unknown_policy="exclude",
    )
    assert execute(db, excluded, instance) == [fresh.id]
    assert execute_unknown_count(db, excluded, instance) == 1


def test_assessment_of_another_version_and_a_null_result_are_both_unknown(repo, db):
    instance = repo.instance_id
    other_version = make_document(repo, "other-version.pdf")
    null_result = make_document(repo, "null-result.pdf")
    add_profile(repo, other_version.id, criteria_version=2, result=CriterionResult.SUPPORTED)
    add_profile(repo, null_result.id, criteria_version=3, result_is_null=True)

    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"), criteria_version=3
    )
    returned = set(execute(db, compiled, instance))
    assert returned == {other_version.id, null_result.id}
    assert execute_unknown_count(db, compiled, instance) == 2


def test_a_known_non_matching_assessment_is_false_and_excluded(repo, db):
    instance = repo.instance_id
    non_match = make_document(repo, "non-match.pdf")
    absent = make_document(repo, "absent.pdf")
    add_profile(repo, non_match.id, criteria_version=3, result=CriterionResult.NOT_FOUND)

    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"), criteria_version=3
    )
    returned = set(execute(db, compiled, instance))
    # "not_found" is a known result, not an unknown: it is a false match here and
    # correctly excluded; the unassessed row remains visible as unknown.
    assert non_match.id not in returned
    assert returned == {absent.id}


# ---------------------------------------------------------------------------
# 5. Stable pagination
# ---------------------------------------------------------------------------
def test_equal_sort_keys_keep_a_stable_order_across_calls(repo, db):
    instance = repo.instance_id
    for index in range(6):
        repo.create_document(
            original_filename=f"same-{index}.pdf",
            rel_path=f"same-{index}.pdf",
            media_type=MediaType.PDF,
            size_bytes=1,
            content_sha256=None,
            fs_identity=None,
            submitted_at="2024-01-01T00:00:00+00:00",
        )
    compiled = compile_filter(
        None,
        criteria_version=0,
        sort=[{"field": "submitted_at", "direction": "asc"}],
    )
    # The tie-breaker is present even though the caller supplied only one key.
    assert compiled.order_sql.endswith("d.id ASC")
    first = execute(db, compiled, instance)
    second = execute(db, compiled, instance)
    assert first == second
    assert len(first) == 6
    # The order is genuinely by id, not the insertion order that happens to match.
    assert first == sorted(first)
    assert first != sorted(first, reverse=True)


def test_sort_on_document_id_is_deterministic_without_a_duplicate_tie_breaker():
    compiled = compile_filter(
        None, criteria_version=0, sort=[{"field": "document_id", "direction": "desc"}]
    )
    assert compiled.order_sql == "d.id DESC"
    assert compiled.order_sql.count("d.id") == 1


def test_database_integrity_survives_the_injection_battery(seeded_instance):
    db, instance_id = seeded_instance
    for payload in INJECTION_PAYLOADS:
        for op in ("eq", "contains", "in"):
            value = [payload] if op == "in" else payload
            compiled = compile_filter(
                predicate("original_filename", op, value), criteria_version=1
            )
            execute(db, compiled, instance_id)
            execute_unknown_count(db, compiled, instance_id)
    assert db.scalar("SELECT COUNT(*) FROM documents") == 2
    assert db.scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'"
    ) == 1
    assert db.integrity_check() == "ok"
