"""Compile- and behaviour-level tests for the allowlisted filter compiler.

Authority: PRD section 9.2 ("Query pipeline"), 9.3 ("Proposed filter contract"),
7.4 ("Approved criteria and management of change") and 11.2 ("Required
invariants").

Every test here asserts on the generated SQL text and bound parameters, or on the
result of executing that SQL against a migrated database. Nothing asserts merely
that a call did not raise: a fragment that silently drops a predicate or inlines a
value would pass such a test, and those are exactly the failures this module
exists to prevent.

Synthetic data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_review import SCHEMA_VERSION, __version__
from resume_review.analysis.filters import (
    DEFAULT_FIELDS,
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
    ReviewState,
    UnknownPolicy,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(DbConfig(path=tmp_path / "filter.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance("inst_filter", __version__, SCHEMA_VERSION)
    return repository


def make_document(repo: Repository, name: str):
    return repo.create_document(
        original_filename=name,
        rel_path=name,
        media_type=MediaType.PDF,
        size_bytes=1024,
        content_sha256=None,
        fs_identity=None,
    )


def add_profile(repo: Repository, document_id: str, *, criteria_version: int, result, stale: bool = False):
    """Commit one current profile with a single criterion evidence row."""
    analysis = AnalysisResult(
        schema_version="1.0",
        document_id=document_id,
        source_revision=1,
        criteria_version=criteria_version,
        summary_text="synthetic",
        criteria=[],
        evidence=[
            EvidenceItem(
                id="ev_1",
                span_id="span_1",
                quote="synthetic quote",
                criterion_id="cr_01",
                result=result,
            )
        ],
    )
    profile = repo.insert_profile(analysis, {"prompt_version": "p1", "model_route": "fixture"})
    if stale:
        repo.mark_profiles_stale(document_id, reason="synthetic")
    return profile


def run(db: Database, compiled, instance_id: str) -> list[str]:
    """Execute a compiled fragment exactly as a caller would."""
    sql = (
        "SELECT d.id "
        + DOCUMENT_FILTER_FROM_SQL
        + " WHERE d.instance_id = ? AND ("
        + compiled.where_sql
        + ") ORDER BY "
        + compiled.order_sql
    )
    return [str(r["id"]) for r in db.query(sql, (instance_id, *compiled.params))]


def scalar(db: Database, compiled, instance_id: str) -> int:
    sql, params = unknown_count_sql(compiled, instance_id=instance_id)
    return int(db.scalar(sql, params, default=0) or 0)


def predicate(field: str, op: str, value=None) -> dict:
    node = {"type": "predicate", "field": field, "op": op}
    if value is not None:
        node["value"] = value
    return node


# ---------------------------------------------------------------------------
# The PRD 9.3 contract example compiles, parameterized and stable
# ---------------------------------------------------------------------------
def test_prd_example_filter_compiles_with_bound_params():
    expression = {
        "type": "and",
        "children": [
            predicate("review_state", "in", ["unreviewed", "keep", "hold"]),
            predicate("criterion:cr_01", "is_supported"),
        ],
    }
    compiled = compile_filter(expression, criteria_version=3)

    assert compiled.unknown_policy is UnknownPolicy.INCLUDE_WITH_WARNING
    assert compiled.params == (
        "unreviewed",
        "keep",
        "hold",
        3,
        "cr_01",
        "supported",
        3,
        "cr_01",
    )
    assert "COALESCE(dec.disposition, 'unreviewed') IN (?, ?, ?)" in compiled.where_sql
    assert compiled.unknown_predicates == 1
    assert compiled.predicate_count == 2
    assert compiled.depth == 2
    # No caller-supplied value is interpolated into the SQL text. The only string
    # literal in the fragment is the registry's own COALESCE default for a missing
    # decision row, which is a constant of this module, not caller data.
    for value in ("keep", "hold", "cr_01", "supported"):
        assert f"'{value}'" not in compiled.where_sql
        assert value not in compiled.where_sql
    assert "'unreviewed'" in compiled.where_sql  # registry default, not a bound value
    # Placeholder count matches the bound parameter count.
    assert compiled.where_sql.count("?") == len(compiled.params)


def test_full_prd_filter_object_is_accepted_and_its_policy_and_sort_apply():
    envelope = {
        "schema_version": "1.0",
        "instance_id": "inst_demo",
        "criteria_version": 3,
        "expression": {
            "type": "predicate",
            "field": "review_state",
            "op": "in",
            "value": ["keep"],
        },
        "unknown_policy": "include_with_warning",
        "sort": [{"field": "ingested_at", "direction": "asc"}],
    }
    compiled = compile_filter(envelope, criteria_version=3)
    assert compiled.order_sql == "d.ingested_at ASC, d.id ASC"
    assert compiled.params == ("keep",)


def test_compile_reports_json_describable_shape():
    compiled = compile_filter(
        predicate("criterion:cr_09", "needs_manual_review"), criteria_version=4
    )
    described = compiled.describe()
    assert described["where_sql"] == compiled.where_sql
    assert described["params"] == list(compiled.params)
    assert described["unknown_policy"] == "include_with_warning"
    assert described["unknown_predicates"] == 1
    assert described["criteria_version"] == 4
    assert sorted(described) == [
        "criteria_version",
        "depth",
        "order_sql",
        "params",
        "predicate_count",
        "unknown_params",
        "unknown_policy",
        "unknown_predicates",
        "unknown_where_sql",
        "warnings",
        "where_sql",
    ]


def test_no_filter_expression_is_match_all_with_a_stable_order():
    compiled = compile_filter(None, criteria_version=0)
    assert compiled.where_sql == "1 = 1"
    assert compiled.params == ()
    assert compiled.order_sql == "d.ingested_at ASC, d.id ASC"
    assert compiled.unknown_where_sql == "0"
    assert compiled.warnings == ()


# ---------------------------------------------------------------------------
# Field and operator registry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,op,value",
    [
        ("processing_state", "eq", "ready"),
        ("location", "in", ["active", "rejected"]),
        ("pending_intent", "eq", "move_rejected"),
        ("execution_state", "eq", "blocked"),
        ("ingested_at", "gte", "2024-01-01T00:00:00+00:00"),
        ("ingested_at", "between", ["2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00"]),
        ("submitted_at", "lt", "2024-01-01T00:00:00+00:00"),
        ("original_filename", "contains", "manager"),
        ("duplicate_content", "eq", True),
        ("decision_needs_recheck", "eq", False),
        ("task_state", "eq", "open"),
    ],
)
def test_registered_fields_and_operators_compile(field, op, value):
    compiled = compile_filter(predicate(field, op, value), criteria_version=1)
    assert "?" in compiled.where_sql
    assert len(compiled.params) >= 1


def test_operators_are_field_specific():
    # contains is not a timestamp operator, and between is not an enum operator.
    with pytest.raises(InvalidInput) as contains_err:
        compile_filter(predicate("ingested_at", "contains", "2024"), criteria_version=1)
    assert contains_err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED

    with pytest.raises(InvalidInput) as between_err:
        compile_filter(
            predicate("location", "between", ["a", "b"]), criteria_version=1
        )
    assert between_err.value.code == Code.FILTER_OPERATOR_NOT_ALLOWED


def test_enum_member_that_is_not_registered_is_refused():
    with pytest.raises(InvalidInput) as err:
        compile_filter(predicate("review_state", "eq", "hired"), criteria_version=1)
    assert err.value.code == Code.FILTER_UNKNOWN_VALUE


def test_boolean_field_accepts_true_false_and_one_zero():
    compiled = compile_filter(predicate("duplicate_content", "eq", "true"), criteria_version=1)
    assert compiled.params == (1,)
    compiled = compile_filter(predicate("duplicate_content", "eq", 0), criteria_version=1)
    assert compiled.params == (0,)


def test_timestamp_value_is_normalized_to_utc_and_bound():
    compiled = compile_filter(
        predicate("submitted_at", "gte", "2024-01-01T05:00:00+05:00"), criteria_version=1
    )
    assert compiled.params == ("2024-01-01T00:00:00+00:00",)
    assert compiled.where_sql.count("?") == 1


def test_contains_escapes_like_wildcards():
    compiled = compile_filter(
        predicate("original_filename", "contains", "100%_done"), criteria_version=1
    )
    assert compiled.params == ("%100\\%\\_done%",)
    assert "ESCAPE '\\'" in compiled.where_sql


# ---------------------------------------------------------------------------
# Bounds (PRD 9.3): depth three, twenty predicates, hard refusal
# ---------------------------------------------------------------------------
def test_depth_three_compiles_but_four_is_refused():
    leaf = predicate("review_state", "eq", "keep")
    depth_three = {"type": "and", "children": [{"type": "or", "children": [leaf]}]}
    compiled = compile_filter(depth_three, criteria_version=1)
    assert compiled.depth == 3

    depth_four = {
        "type": "and",
        "children": [{"type": "or", "children": [{"type": "and", "children": [leaf]}]}],
    }
    with pytest.raises(InvalidInput) as err:
        compile_filter(depth_four, criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_DEEP
    assert err.value.detail["depth"] == 4


def test_twenty_predicates_compile_and_twenty_one_are_refused():
    def group(count: int) -> dict:
        return {
            "type": "and",
            "children": [predicate("review_state", "eq", "keep") for _ in range(count)],
        }

    compiled = compile_filter(group(20), criteria_version=1)
    assert compiled.predicate_count == 20
    assert len(compiled.params) == 20

    with pytest.raises(InvalidInput) as err:
        compile_filter(group(21), criteria_version=1)
    assert err.value.code == Code.FILTER_TOO_WIDE
    assert err.value.detail["predicates"] == 21


def test_group_requires_at_least_one_child():
    with pytest.raises(InvalidInput) as err:
        compile_filter({"type": "and", "children": []}, criteria_version=1)
    assert err.value.code == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Unknown-value semantics (PRD 9.2, 9.3)
# ---------------------------------------------------------------------------
def test_default_policy_is_include_with_warning():
    compiled = compile_filter(predicate("submitted_at", "lt", "2024-01-01T00:00:00+00:00"), criteria_version=1)
    assert compiled.unknown_policy is UnknownPolicy.INCLUDE_WITH_WARNING
    # The unknown branch is present in the emitted clause.
    assert "d.submitted_at IS NULL" in compiled.where_sql
    assert compiled.warnings
    assert compiled.warnings[0].code == Code.FILTER_UNKNOWN_VALUE


def test_exclude_policy_drops_the_unknown_branch_but_keeps_the_omitted_count():
    expression = predicate("submitted_at", "lt", "2024-01-01T00:00:00+00:00")
    included = compile_filter(expression, criteria_version=1)
    excluded = compile_filter(expression, criteria_version=1, unknown_policy="exclude")

    assert "IS NULL" not in excluded.where_sql
    assert excluded.where_sql == "d.submitted_at < ?"
    # Both modes can report the rows whose membership depends on unknown handling.
    assert included.unknown_where_sql == excluded.unknown_where_sql
    assert excluded.unknown_params == included.unknown_params


def test_show_only_supported_alias_selects_exclude():
    compiled = compile_filter(
        predicate("criterion:cr_01", "is_supported"),
        criteria_version=3,
        unknown_policy="show_only_supported",
    )
    assert compiled.unknown_policy is UnknownPolicy.EXCLUDE


def test_filter_without_unknown_capable_fields_reports_no_unknowns():
    compiled = compile_filter(predicate("review_state", "eq", "keep"), criteria_version=1)
    assert compiled.unknown_where_sql == "0"
    assert compiled.unknown_params == ()
    assert compiled.unknown_predicates == 0
    assert compiled.warnings == ()


# ---------------------------------------------------------------------------
# Evidence predicates against a real database
# ---------------------------------------------------------------------------
def test_stale_and_wrong_version_assessments_are_unknown_not_false(repo: Repository, db: Database):
    instance = repo.instance_id
    known = make_document(repo, "known.pdf")
    stale = make_document(repo, "stale.pdf")
    old_version = make_document(repo, "old-version.pdf")
    absent = make_document(repo, "absent.pdf")
    other_criterion = make_document(repo, "other-criterion.pdf")

    add_profile(repo, known.id, criteria_version=3, result=CriterionResult.SUPPORTED)
    add_profile(repo, stale.id, criteria_version=3, result=CriterionResult.SUPPORTED, stale=True)
    add_profile(repo, old_version.id, criteria_version=2, result=CriterionResult.SUPPORTED)
    # ``absent`` gets no profile at all.
    repo.insert_profile(
        AnalysisResult(
            schema_version="1.0",
            document_id=other_criterion.id,
            source_revision=1,
            criteria_version=3,
            summary_text="synthetic",
            evidence=[
                EvidenceItem(
                    id="ev_2",
                    span_id="span_1",
                    quote="synthetic quote",
                    criterion_id="cr_99",
                    result=CriterionResult.SUPPORTED,
                )
            ],
        ),
        {"prompt_version": "p1", "model_route": "fixture"},
    )

    compiled = compile_filter(predicate("criterion:cr_01", "is_supported"), criteria_version=3)

    # Under the default policy nothing is hidden: the one known row plus every
    # unknown row is returned.
    returned = set(run(db, compiled, instance))
    assert returned == {known.id, stale.id, old_version.id, absent.id, other_criterion.id}

    # The unknown-only count names exactly the four rows that were included
    # because their assessment was not established.
    assert scalar(db, compiled, instance) == 4

    # Show-only-supported returns the single positively assessed row and reports
    # the same four as omitted.
    excluded = compile_filter(
        predicate("criterion:cr_01", "is_supported"),
        criteria_version=3,
        unknown_policy="exclude",
    )
    assert run(db, excluded, instance) == [known.id]
    assert scalar(db, excluded, instance) == 4


def test_a_known_non_matching_assessment_is_false_and_is_excluded(repo: Repository, db: Database):
    instance = repo.instance_id
    not_found = make_document(repo, "not-found.pdf")
    unclear = make_document(repo, "unclear.pdf")
    unassessed = make_document(repo, "unassessed.pdf")
    add_profile(repo, not_found.id, criteria_version=3, result=CriterionResult.NOT_FOUND)
    add_profile(repo, unclear.id, criteria_version=3, result=CriterionResult.UNCLEAR)

    compiled = compile_filter(predicate("criterion:cr_01", "is_supported"), criteria_version=3)
    returned = set(run(db, compiled, instance))
    # not_found and unclear are known non-matches: false, and excluded.
    assert not_found.id not in returned
    assert unclear.id not in returned
    # The document with no assessment at all is unknown, so it stays visible.
    assert returned == {unassessed.id}
    assert scalar(db, compiled, instance) == 1


def test_criterion_operators_address_the_recorded_result(repo: Repository, db: Database):
    instance = repo.instance_id
    supported = make_document(repo, "supported.pdf")
    not_found = make_document(repo, "not-found.pdf")
    add_profile(repo, supported.id, criteria_version=3, result=CriterionResult.SUPPORTED)
    add_profile(repo, not_found.id, criteria_version=3, result=CriterionResult.NOT_FOUND)

    compiled = compile_filter(predicate("criterion:cr_01", "is_not_found"), criteria_version=3)
    assert run(db, compiled, instance) == [not_found.id]
    assert compiled.params == (3, "cr_01", "not_found", 3, "cr_01")


def test_nullable_column_unknown_rows_are_included_under_the_default_policy(repo: Repository, db: Database):
    instance = repo.instance_id
    dated = repo.create_document(
        original_filename="dated.pdf",
        rel_path="dated.pdf",
        media_type=MediaType.PDF,
        size_bytes=10,
        content_sha256=None,
        fs_identity=None,
        submitted_at="2024-01-01T00:00:00+00:00",
    )
    undated = make_document(repo, "undated.pdf")

    compiled = compile_filter(
        predicate("submitted_at", "lt", "2024-06-01T00:00:00+00:00"), criteria_version=1
    )
    returned = set(run(db, compiled, instance))
    assert returned == {dated.id, undated.id}
    assert scalar(db, compiled, instance) == 1


def test_task_state_unknown_for_documents_with_no_tasks(repo: Repository, db: Database):
    instance = repo.instance_id
    with_task = make_document(repo, "tasked.pdf")
    without = make_document(repo, "untasked.pdf")
    repo.upsert_task(document_id=with_task.id, task_type="verify", title="Verify")

    compiled = compile_filter(predicate("task_state", "eq", "open"), criteria_version=1)
    returned = set(run(db, compiled, instance))
    assert with_task.id in returned
    assert without.id in returned
    assert scalar(db, compiled, instance) == 1


# ---------------------------------------------------------------------------
# Sorting (PRD 11.2)
# ---------------------------------------------------------------------------
def test_default_sort_is_deterministic():
    compiled = compile_filter(None, criteria_version=0)
    assert compiled.order_sql == "d.ingested_at ASC, d.id ASC"


def test_explicit_sort_always_ends_with_the_document_tie_breaker():
    compiled = compile_filter(
        None,
        criteria_version=0,
        sort=[{"field": "original_filename", "direction": "desc"}],
    )
    assert compiled.order_sql == "d.original_filename DESC, d.id ASC"


def test_sort_on_document_id_is_not_duplicated():
    compiled = compile_filter(
        None, criteria_version=0, sort=[{"field": "document_id", "direction": "desc"}]
    )
    assert compiled.order_sql == "d.id DESC"


def test_sort_field_must_be_registered():
    with pytest.raises(InvalidInput) as err:
        compile_filter(None, criteria_version=0, sort=[{"field": "content_sha256"}])
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED


def test_sort_direction_must_be_asc_or_desc():
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            None, criteria_version=0, sort=[{"field": "ingested_at", "direction": "sideways"}]
        )
    assert err.value.code == Code.INVALID_INPUT


# ---------------------------------------------------------------------------
# Saved filters (PRD 7.4)
# ---------------------------------------------------------------------------
class _Criterion:
    """A minimal stand-in for an active Criterion row."""

    def __init__(self, criterion_id: str, version: int) -> None:
        self.criterion_id = criterion_id
        self.version = version


def test_saved_filter_referring_to_a_removed_criterion_is_visibly_invalid():
    saved = {
        "id": "flt_1",
        "name": "Supported only",
        "definition": {
            "expression": predicate("criterion:cr_removed", "is_supported"),
            "unknown_policy": "include_with_warning",
        },
        "criteria_version": 3,
    }
    status = validate_saved_filter(saved, active_criteria=[_Criterion("cr_01", 3)])

    assert status.valid is False
    assert status.missing_criteria == ("cr_removed",)
    assert status.reason_codes == (Code.FILTER_INVALID_FOR_CRITERIA,)
    assert status.message
    assert status.to_dict()["valid"] is False


def test_saved_filter_that_still_resolves_is_valid_with_a_version_note():
    saved = {
        "id": "flt_2",
        "name": "Supported only",
        "definition": predicate("criterion:cr_01", "is_supported"),
        "criteria_version": 3,
    }
    status = validate_saved_filter(saved, active_criteria=[_Criterion("cr_01", 4)])
    assert status.valid is True
    assert status.missing_criteria == ()
    assert status.reason_codes == ()
    assert status.criteria_version == 3
    assert status.active_criteria_version == 4
    assert [w.code for w in status.warnings] == [Code.ANALYSIS_STALE_RESULT]


def test_saved_filter_that_no_longer_compiles_is_invalid():
    saved = {
        "id": "flt_3",
        "definition": predicate("review_state", "nonsense", "keep"),
        "criteria_version": 1,
    }
    status = validate_saved_filter(saved, active_criteria=[])
    assert status.valid is False
    assert status.reason_codes == (Code.FILTER_OPERATOR_NOT_ALLOWED,)


def test_saved_filter_with_malformed_tree_is_reported_not_raised():
    saved = {"id": "flt_4", "definition": {"type": "not", "children": []}, "criteria_version": 1}
    status = validate_saved_filter(saved, active_criteria=[])
    assert status.valid is False
    assert status.reason_codes == (Code.FILTER_OPERATOR_NOT_ALLOWED,)


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------
def test_registry_covers_the_required_fields():
    required = {
        "review_state",
        "processing_state",
        "location",
        "pending_intent",
        "execution_state",
        "ingested_at",
        "submitted_at",
        "original_filename",
        "duplicate_content",
        "decision_needs_recheck",
        "task_state",
    }
    assert required <= set(DEFAULT_FIELDS)
    assert ReviewState.KEEP.value in DEFAULT_FIELDS["review_state"].values


def test_custom_field_registry_restricts_the_allowed_set():
    narrowed = {"review_state": DEFAULT_FIELDS["review_state"]}
    with pytest.raises(InvalidInput) as err:
        compile_filter(
            predicate("location", "eq", "active"), criteria_version=1, fields=narrowed
        )
    assert err.value.code == Code.FILTER_FIELD_NOT_ALLOWED


def test_unknown_count_sql_is_scoped_and_runnable(repo: Repository, db: Database):
    instance = repo.instance_id
    make_document(repo, "a.pdf")
    compiled = compile_filter(predicate("submitted_at", "lt", "2024-01-01T00:00:00+00:00"), criteria_version=1)
    sql, params = unknown_count_sql(compiled, instance_id=instance)
    assert sql.startswith("SELECT COUNT(*) " + DOCUMENT_FILTER_FROM_SQL)
    assert "d.instance_id = ?" in sql
    assert params[0] == instance
    assert int(db.scalar(sql, params, default=0) or 0) == 1
