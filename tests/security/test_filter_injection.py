"""Adversarial tests for the filter compiler.

Authority: AGENTS.md constraint 7 ("No unrestricted Gateway proxy. Analysis runs
on a restricted route") and PRD section 9.2 ("Compile a small allowlisted filter
tree into parameterized queries. Never execute model-produced SQL, regular
expressions without limits, JavaScript, shell commands, or filesystem paths.").

The model proposes; the helper compiles. These tests attack the compiler the way
a compromised or confused model would: injection through values, through field
names, through sort fields, through operators and node shapes, and through the
bounds. Every injection payload is not only compiled but *executed* against a
migrated database, and the test asserts that the dangerous outcome did not occur
(a table still exists, no extra row is returned, the payload survives only as a
bound parameter).

Synthetic data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.filters import (
    DOCUMENT_FILTER_FROM_SQL,
    compile_filter,
    unknown_count_sql,
    validate_saved_filter,
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
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "filter_sec.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_sec", __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def seeded(repo: Repository) -> Repository:
    repo.create_document(
        original_filename="alpha.pdf",
        rel_path="alpha.pdf",
        media_type=MediaType.PDF,
        size_bytes=10,
        content_sha256=None,
        fs_identity=None,
    )
    repo.create_document(
        original_filename="beta.pdf",
        rel_path="beta.pdf",
        media_type=MediaType.PDF,
        size_bytes=20,
        content_sha256=None,
        fs_identity=None,
    )
    return repo


def document_count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM documents", (), default=0) or 0)


def execute(db: Database, compiled, instance_id: str) -> list[str]:
    sql = (
        "SELECT d.id "
        + DOCUMENT_FILTER_FROM_SQL
        + " WHERE d.instance_id = ? AND ("
        + compiled.where_sql
        + ") ORDER BY "
        + compiled.order_sql
    )
    return [str(r["id"]) for r in db.query(sql, (instance_id, *compiled.params))]


def execute_unknown_count(db: Database, compiled, instance_id: str) -> int:
    sql, params = unknown_count_sql(compiled, instance_id=instance_id)
    return int(db.scalar(sql, params, default=0) or 0)


def predicate(field: str, op: str, value=None) -> dict:
    node = {"type": "predicate", "field": field, "op": op}
    if value is not None:
        node["value"] = value
    return node


# ---------------------------------------------------------------------------
# Injection through values
# ---------------------------------------------------------------------------
INJECTION_PAYLOADS = [
    "x' OR '1'='1",
    "x' OR 1=1 --",
    "'; DROP TABLE documents; --",
    "x'; DELETE FROM documents WHERE 1=1; --",
    "x' UNION SELECT id FROM decisions --",
    "%' OR original_filename LIKE '%",
    "_' OR 1=1 --",
    "x\\' OR 1=1 --",
    "x' AND (SELECT COUNT(*) FROM sqlite_master) > 0 --",
    "x\x00' OR 1=1 --",
]


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_through_a_text_value_is_bound_never_interpolated(seeded: Repository, db: Database, payload: str):
    compiled = compile_filter(
        predicate("original_filename", "eq", payload), criteria_version=1
    )

    # The payload is a parameter; the fragment carries only a placeholder.
    assert compiled.params == (payload,)
    assert payload not in compiled.where_sql
    assert compiled.where_sql.count("?") == 1

    # Executing it matches nothing and removes nothing: there is no filename that
    # equals the payload.
    assert execute(db, compiled, seeded.instance_id) == []
    assert document_count(db) == 2


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_through_a_contains_value_has_its_wildcards_escaped(seeded: Repository, db: Database, payload: str):
    compiled = compile_filter(
        predicate("original_filename", "contains", payload), criteria_version=1
    )

    # The payload's own wildcards and escapes are neutralized before it is wrapped
    # in the two unescaped wildcards that implement "contains".
    neutralized = payload.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    assert compiled.params == (f"%{neutralized}%",)
    assert payload not in compiled.where_sql

    # The payload is treated as literal text inside the LIKE pattern, so no
    # document matches and none is returned by an injection.
    assert execute(db, compiled, seeded.instance_id) == []
    assert document_count(db) == 2
    # The unknown-only companion statement is also parameterized.
    sql, params = unknown_count_sql(compiled, instance_id=seeded.instance_id)
    assert payload not in sql
    assert execute_unknown_count(db, compiled, seeded.instance_id) == 0


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_through_an_in_list_value_is_bound(seeded: Repository, db: Database, payload: str):
    compiled = compile_filter(
        predicate("review_state", "in", ["keep", "hold"]), criteria_version=1
    )
    assert "keep" not in compiled.where_sql

    text_compiled = compile_filter(
        predicate("original_filename", "in", [payload]), criteria_version=1
    )
    assert text_compiled.params == (payload,)
    assert execute(db, text_compiled, seeded.instance_id) == []
    assert document_count(db) == 2


def test_injection_that_would_drop_a_table_leaves_the_schema_intact(seeded: Repository, db: Database):
    payload = "'; DROP TABLE documents; --"
    compiled = compile_filter(predicate("original_filename", "contains", payload), criteria_version=1)

    execute(db, compiled, seeded.instance_id)
    execute_unknown_count(db, compiled, seeded.instance_id)

    # The table still exists and still holds its rows.
    assert document_count(db) == 2
    assert db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'") == 1
    assert db.integrity_check() == "ok"


def test_injection_through_a_timestamp_value_is_refused_not_bound(seeded: Repository, db: Database):
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            predicate("ingested_at", "gte", "2024-01-01T00:00:00+00:00' OR '1'='1"),
            criteria_version=1,
        )
    assert err.value.code == Code.FILTER_UNKNOWN_VALUE


# ---------------------------------------------------------------------------
# Injection through identifiers
# ---------------------------------------------------------------------------
BAD_FIELD_NAMES = [
    "review_state' OR 1=1 --",
    "review_state = 'keep' OR 1=1",
    "d.id",
    "documents",
    "id",
    "content_sha256",
    "current_rel_path",
    "sql",
    "1",
    "",
    "review_state ",
    "REVIEW_STATE",
    "criterion:",
    "criterion:x' OR 1=1 --",
    "criterion:cr_01); DROP TABLE documents; --",
    "__proto__",
    "task_state; DROP TABLE documents",
]


@pytest.mark.parametrize("field", BAD_FIELD_NAMES)
def test_unregistered_field_name_is_refused(field: str):
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate(field, "eq", "keep"), criteria_version=1)
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED
    assert err.value.http_status == 422


BAD_SORT_FIELDS = [
    "ingested_at; DROP TABLE documents; --",
    "d.id",
    "content_sha256",
    "1=1",
    "review_state,",
]


@pytest.mark.parametrize("field", BAD_SORT_FIELDS)
def test_unregistered_sort_field_is_refused(field: str):
    with pytest.raises(InvalidInput) as err:
        compile_filter(None, criteria_version=0, sort=[{"field": field}])
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED


def test_sort_field_injection_cannot_reach_sql_and_the_table_survives(seeded: Repository, db: Database):
    with pytest.raises(InvalidInput):
        compile_filter(
            None,
            criteria_version=0,
            sort=[{"field": "ingested_at; DROP TABLE documents; --"}],
        )
    assert document_count(db) == 2


# ---------------------------------------------------------------------------
# Operators, node shapes, and the refused negation
# ---------------------------------------------------------------------------
BAD_OPERATORS = [
    "not",
    "neq",
    "ne",
    "like",
    "regex",
    "raw",
    "exists",
    "is_null",
    "similar_to",
    "1=1",
    "",
]


@pytest.mark.parametrize("op", BAD_OPERATORS)
def test_unregistered_operator_is_refused(op: str):
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate("review_state", op, "keep"), criteria_version=1)
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


def test_the_string_not_is_refused_as_an_operator():
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate("review_state", "not", "keep"), criteria_version=1)
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


def test_a_not_node_is_refused_as_an_operator():
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            {"type": "not", "children": [predicate("review_state", "eq", "keep")]},
            criteria_version=1,
        )
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


def test_criterion_predicates_accept_no_other_operator():
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate("criterion:cr_01", "eq", "supported"), criteria_version=1)
    assert err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


@pytest.mark.parametrize(
    "node",
    [
        {"type": "sql", "children": []},
        {"type": "raw", "sql": "SELECT 1"},
        "review_state = 'keep'",
        ["review_state", "eq", "keep"],
        42,
    ],
)
def test_non_node_shapes_are_refused(node):
    with pytest.raises(InvalidInput):
        compile_filter(node, criteria_version=1)


@pytest.mark.parametrize(
    "key",
    ["sql", "raw_sql", "query", "regex", "regex_flags", "javascript", "shell", "command", "path", "file", "exec", "eval"],
)
def test_executable_keys_on_a_node_are_refused_not_ignored(key):
    node = predicate("review_state", "eq", "keep")
    node[key] = "anything"
    with pytest.raises(InvalidInput) as err:
        compile_filter(node, criteria_version=1)
    assert err.value.code == Code.INVALID_INPUT
    assert key in err.value.detail["keys"]


def test_executable_key_on_a_sort_entry_is_refused():
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            None,
            criteria_version=0,
            sort=[{"field": "ingested_at", "sql": "1=1 OR"}],
        )
    assert err.value.code == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------
def _nested(levels: int) -> dict:
    node: dict = predicate("review_state", "eq", "keep")
    for _ in range(levels - 1):
        node = {"type": "and", "children": [node]}
    return node


def test_four_deep_tree_is_refused_with_the_depth_code():
    with pytest.raises(InvalidInput) as err:
        compile_filter(_nested(4), criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_DEEP
    assert err.value.detail["depth"] == 4


def test_three_deep_tree_is_accepted():
    compiled = compile_filter(_nested(3), criteria_version=1)
    assert compiled.depth == 3


def test_twenty_one_predicates_are_refused_not_truncated():
    expression = {
        "type": "or",
        "children": [predicate("review_state", "eq", "keep") for _ in range(21)],
    }
    with pytest.raises(InvalidInput) as err:
        compile_filter(expression, criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_WIDE
    assert err.value.detail["predicates"] == 21


# ---------------------------------------------------------------------------
# Evidence predicates: stale assessment is unknown, never "does not have"
# ---------------------------------------------------------------------------
def _add_profile(repo: Repository, document_id: str, *, criteria_version: int, result, stale: bool = False):
    repo.insert_profile(
        AnalysisResult(
            schema_version="1.0",
            document_id=document_id,
            source_revision=1,
            criteria_version=criteria_version,
            summary_text="synthetic",
            evidence=[
                EvidenceItem(
                    id="ev_1",
                    span_id="span_1",
                    quote="synthetic quote",
                    criterion_id="cr_01",
                    result=result,
                )
            ],
        ),
        {"prompt_version": "p1", "model_route": "fixture"},
    )
    if stale:
        repo.mark_profiles_stale(document_id, reason="test")


def test_stale_assessment_is_unknown_included_and_counted(repo: Repository, db: Database):
    fresh = repo.create_document(
        original_filename="fresh.pdf", rel_path="fresh.pdf",
        media_type=MediaType.PDF, size_bytes=1, content_sha256=None, fs_identity=None,
    )
    stale = repo.create_document(
        original_filename="stale.pdf", rel_path="stale.pdf",
        media_type=MediaType.PDF, size_bytes=1, content_sha256=None, fs_identity=None,
    )
    _add_profile(repo, fresh.id, criteria_version=3, result=CriterionResult.SUPPORTED)
    # The assessment exists but was invalidated by a change; it must read unknown.
    _add_profile(repo, stale.id, criteria_version=3, result=CriterionResult.SUPPORTED, stale=True)

    compiled = compile_filter(predicate("criterion:cr_01", "is_supported"), criteria_version=3)

    returned = set(execute(db, compiled, repo.instance_id))
    assert returned == {fresh.id, stale.id}
    # Exactly one row is present because its assessment is stale, and the count
    # is reported so the interface can link to it.
    assert execute_unknown_count(db, compiled, repo.instance_id) == 1
    assert compiled.warnings and compiled.warnings[0].detail["unknown_predicates"] == 1

    # Under show-only-supported the stale row is omitted and the same count is
    # available as the omitted total.
    excluded = compile_filter(
        predicate("criterion:cr_01", "is_supported"),
        criteria_version=3,
        unknown_policy="exclude",
    )
    assert execute(db, excluded, repo.instance_id) == [fresh.id]
    assert execute_unknown_count(db, excluded, repo.instance_id) == 1


def test_criterion_assessment_from_another_criteria_version_is_unknown(repo: Repository, db: Database):
    document = repo.create_document(
        original_filename="old.pdf", rel_path="old.pdf",
        media_type=MediaType.PDF, size_bytes=1, content_sha256=None, fs_identity=None,
    )
    _add_profile(repo, document.id, criteria_version=2, result=CriterionResult.SUPPORTED)

    compiled = compile_filter(predicate("criterion:cr_01", "is_supported"), criteria_version=3)
    assert execute(db, compiled, repo.instance_id) == [document.id]
    assert execute_unknown_count(db, compiled, repo.instance_id) == 1


def test_filter_referencing_a_removed_criterion_is_visibly_invalid():
    class _Criterion:
        def __init__(self, criterion_id, version):
            self.criterion_id = criterion_id
            self.version = version

    saved = {
        "id": "flt_sec",
        "name": "Removed criterion",
        "definition": {
            "expression": predicate("criterion:cr_removed", "is_supported"),
            "unknown_policy": "include_with_warning",
        },
        "criteria_version": 3,
    }
    status = validate_saved_filter(saved, active_criteria=[_Criterion("cr_01", 3)])
    assert status.valid is False
    assert status.reason_codes == (Code.FILTER_INVALID_FOR_CRITERIA,)
    assert status.missing_criteria == ("cr_removed",)
