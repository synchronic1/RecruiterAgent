"""Analysis-side data access: jobs, criteria, profiles, evidence, idempotency, conversations, audit.

Authority: PRD section 7 ("Evidence-backed analysis and criteria"), section 9
("Folder-scoped chat and filtering"), section 11 ("Database and persistence
contracts") and section 12.2 ("Mutation rules").

This module implements one half of the single ``Repository`` facade, as a mixin
that :mod:`resume_review.db.repository` composes with the core half. Splitting by
concern keeps each file readable while every method still lives on one class, so
callers import ``resume_review.db.Repository`` and see a coherent API.

Why the row coercion helpers live here
--------------------------------------
``repository.py`` imports this module to compose the mixin. If the helpers sat in
``repository.py`` and this module imported them, the two modules would form an
import cycle. They are therefore defined once, here, and imported downward.

Two rules this module never breaks:

* Every mutating method runs its statement inside ``Database.write(...)`` so the
  state-revision bump and the audit row share the mutation's transaction. A model
  result is data committed by the helper, never an authority to move a file.
* Nothing here reads or writes the Gateway credential, an absolute path, or a
  candidate name into an error message or an audit code.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from ..errors import Code, Conflict, NotFound, ValidationFailed
from ..models import (
    AnalysisResult,
    ChatKind,
    Criterion,
    ModelRoute,
    ProfileRecord,
)
from ..util import new_id, now_iso, seconds_from_now_iso
from .connection import Database, dump_row, encode_json, json_column

__all__ = [
    "AnalysisRepositoryMixin",
    "criterion_from_row",
    "profile_from_row",
    "coerce_enum",
]


# The single source of truth for the criteria version currently in force.
_ACTIVE_CRITERIA_SQL = (
    "SELECT COALESCE(MAX(version), 0) FROM criteria "
    "WHERE instance_id = ? AND approved_at IS NOT NULL"
)

_CRITERIA_LABELS = ("required", "preferred")


# ---------------------------------------------------------------------------
# Shared row coercion
# ---------------------------------------------------------------------------
def coerce_enum(enum_cls: Any, value: Any, default: Any) -> Any:
    """Coerce a stored TEXT column back into its enum member.

    The schema constrains every enum column, so a miss here means a hand-edited
    database or a future schema drift. Degrade to ``default`` rather than raising
    and making the whole row unreadable.
    """
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))
    except ValueError:
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def criterion_from_row(row: sqlite3.Row) -> Criterion:
    return Criterion(
        criterion_id=str(row["criterion_id"]),
        version=int(row["version"]),
        definition=str(row["definition"]),
        rationale=str(row["rationale"]),
        evidence_rule=str(row["evidence_rule"]),
        label=row["label"],
        created_by=str(row["created_by"]),
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        origin=str(row["origin"]),
    )


def profile_from_row(row: sqlite3.Row) -> ProfileRecord:
    return ProfileRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        source_revision=int(row["source_revision"]),
        criteria_version=int(row["criteria_version"]),
        prompt_version=str(row["prompt_version"]),
        schema_version=str(row["schema_version"]),
        model_route=str(row["model_route"]),
        model_version=row["model_version"],
        summary_text=str(row["summary_text"]),
        validation_state=str(row["validation_state"]),
        validation_detail=row["validation_detail"],
        is_fixture=_bool(row["is_fixture"]),
        is_current=_bool(row["is_current"]),
        stale=_bool(row["stale"]),
        generated_at=str(row["generated_at"]),
        run_request_id=row["run_request_id"],
        run_started_at=row["run_started_at"],
        run_ended_at=row["run_ended_at"],
        token_usage=row["token_usage"],
    )


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------
class AnalysisRepositoryMixin:
    """Criteria, profiles, evidence, idempotency, conversations and audit.

    The concrete class supplies ``self.db`` (a
    :class:`resume_review.db.connection.Database`) and inherits this mixin.
    """

    db: Database

    # -- shared accessors --------------------------------------------------
    @property
    def instance_id(self) -> str:
        return self.db.instance_id

    # ------------------------------------------------------------------
    # Job requisition and approved criteria (PRD 7.4)
    # ------------------------------------------------------------------
    def get_requisition(self) -> dict[str, Any] | None:
        """Return this instance's single requisition, if one has been saved."""
        return dump_row(
            self.db.query_one(
                "SELECT * FROM jobs WHERE instance_id = ? ORDER BY created_at ASC LIMIT 1",
                (self.instance_id,),
            )
        )

    def create_or_update_job(
        self,
        title: str,
        description_text: str,
        source_reference: str | None = None,
        *,
        actor: str = "helper",
        actor_kind: str = "helper",
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Register or replace the single job requisition for this instance.

        Re-running returns the same job row rather than creating a second one; the
        database has exactly one requisition per instance in the initial release.
        """
        from ..models import sha256_hex

        digest = sha256_hex(description_text or "")
        with self.db.write(
            actor=actor,
            actor_kind=actor_kind,
            event="job.upsert",
            entity_type="job",
            request_id=request_id,
            new={
                "title": title,
                "description_sha256": digest,
                "source_reference": source_reference,
            },
        ) as conn:
            if expected_revision is not None:
                revision_row = conn.execute(
                    "SELECT state_revision FROM instances WHERE id = ?",
                    (self.instance_id,),
                ).fetchone()
                current_revision = int(revision_row["state_revision"]) if revision_row else 0
                if current_revision != int(expected_revision):
                    # Local import avoids the mixin/repository composition cycle.
                    from .repository import RevisionConflict

                    raise RevisionConflict(
                        "The requisition changed since this save was prepared.",
                        current_revision=current_revision,
                    )
            existing = conn.execute(
                "SELECT id FROM jobs WHERE instance_id = ? ORDER BY created_at ASC LIMIT 1",
                (self.instance_id,),
            ).fetchone()
            now = now_iso()
            if existing is None:
                job_id = new_id("job")
                conn.execute(
                    "INSERT INTO jobs (id, instance_id, title, description_text, "
                    "description_sha256, source_reference, criteria_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (job_id, self.instance_id, title, description_text, digest, source_reference, now, now),
                )
            else:
                job_id = str(existing["id"])
                conn.execute(
                    "UPDATE jobs SET title = ?, description_text = ?, description_sha256 = ?, "
                    "source_reference = ?, updated_at = ? WHERE id = ?",
                    (title, description_text, digest, source_reference, now, job_id),
                )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dump_row(row) or {}

    def _single_job_id(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT id FROM jobs WHERE instance_id = ? ORDER BY created_at ASC LIMIT 1",
            (self.instance_id,),
        ).fetchone()
        return str(row["id"]) if row else None

    def _pending_or_next_criteria_version(self, conn: sqlite3.Connection) -> int:
        """Version number new proposals should join.

        All proposals made between two activations belong to one version, because a
        criteria version is a *set* the operator approves as a unit (the report
        payload publishes one ``criteria_version``). A pending unapproved version is
        reused; otherwise the next number above the highest known version is used.
        """
        pending = conn.execute(
            "SELECT MAX(version) FROM criteria WHERE instance_id = ? AND approved_at IS NULL",
            (self.instance_id,),
        ).fetchone()[0]
        if pending is not None:
            return int(pending)
        highest = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM criteria WHERE instance_id = ?",
            (self.instance_id,),
        ).fetchone()[0]
        active = conn.execute(_ACTIVE_CRITERIA_SQL, (self.instance_id,)).fetchone()[0]
        return max(int(highest or 0), int(active or 0)) + 1

    def create_criteria_proposal(
        self,
        criterion_id: str,
        definition: str,
        rationale: str = "",
        evidence_rule: str = "",
        label: str | None = None,
        created_by: str = "",
        origin: str = "human",
    ) -> Criterion:
        """Record a proposed criterion. It never reaches analysis until activated.

        A proposal has ``approved_at IS NULL``; the report and the analysis route
        only ever see approved criteria (PRD section 7.4).
        """
        if not definition or not definition.strip():
            raise ValidationFailed(
                "A criterion definition is required.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "definition"},
            )
        if label is not None and label not in _CRITERIA_LABELS:
            raise ValidationFailed(
                "A criterion label must be 'required', 'preferred', or absent.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "label"},
            )
        if origin not in ("human", "agent_proposal"):
            raise ValidationFailed(
                "A criterion origin must be 'human' or 'agent_proposal'.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "origin"},
            )

        with self.db.write(
            actor=created_by or origin,
            actor_kind="human" if origin == "human" else "agent",
            event="criteria.propose",
            entity_type="criteria",
            entity_id=criterion_id,
        ) as conn:
            job_id = self._single_job_id(conn)
            if job_id is None:
                raise ValidationFailed(
                    "A job description must be registered before criteria can be proposed.",
                    code=Code.VALIDATION_FAILED,
                    detail={"field": "job"},
                )
            version = self._pending_or_next_criteria_version(conn)
            conn.execute(
                "INSERT INTO criteria (instance_id, job_id, criterion_id, version, definition, "
                "rationale, evidence_rule, label, created_by, created_at, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.instance_id,
                    job_id,
                    criterion_id,
                    version,
                    definition,
                    rationale,
                    evidence_rule,
                    label,
                    created_by,
                    now_iso(),
                    origin,
                ),
            )
            row = conn.execute(
                "SELECT * FROM criteria WHERE instance_id = ? AND criterion_id = ? AND version = ?",
                (self.instance_id, criterion_id, version),
            ).fetchone()
        return criterion_from_row(row)

    def activate_criteria_version(self, version: int, actor: str) -> list[Criterion]:
        """Approve every proposal at ``version`` and make it the active set.

        Older approved criteria are marked ``superseded_at`` rather than deleted, so
        history is preserved. An approved criterion with no row at the new version is
        carried forward as an approved copy, which is what makes a version a complete
        set: activating a version that only proposes an addition must not silently
        drop the criteria the operator still relies on.
        """
        if int(version) < 1:
            raise ValidationFailed(
                "A criteria version must be at least 1.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "version"},
            )
        with self.db.write(
            actor=actor,
            actor_kind="human",
            event="criteria.activate",
            entity_type="criteria",
            new={"version": int(version)},
        ) as conn:
            present_rows = conn.execute(
                "SELECT * FROM criteria WHERE instance_id = ? AND version = ?",
                (self.instance_id, int(version)),
            ).fetchall()
            if not present_rows:
                raise NotFound(
                    "No criteria proposal exists at that version.",
                    code=Code.NOT_FOUND,
                    detail={"version": int(version)},
                )
            now = now_iso()
            conn.execute(
                "UPDATE criteria SET approved_by = ?, approved_at = ? "
                "WHERE instance_id = ? AND version = ? AND approved_at IS NULL",
                (actor, now, self.instance_id, int(version)),
            )
            conn.execute(
                "UPDATE criteria SET superseded_at = ? "
                "WHERE instance_id = ? AND version < ? AND approved_at IS NOT NULL "
                "AND superseded_at IS NULL",
                (now, self.instance_id, int(version)),
            )
            present = {str(r["criterion_id"]) for r in present_rows}
            prior_ids = [
                str(r["criterion_id"])
                for r in conn.execute(
                    "SELECT DISTINCT criterion_id FROM criteria "
                    "WHERE instance_id = ? AND version < ? AND approved_at IS NOT NULL",
                    (self.instance_id, int(version)),
                ).fetchall()
            ]
            for criterion_id in prior_ids:
                if criterion_id in present:
                    continue
                source = conn.execute(
                    "SELECT * FROM criteria WHERE instance_id = ? AND criterion_id = ? "
                    "AND approved_at IS NOT NULL AND version < ? ORDER BY version DESC LIMIT 1",
                    (self.instance_id, criterion_id, int(version)),
                ).fetchone()
                if source is None:
                    continue
                conn.execute(
                    "INSERT INTO criteria (instance_id, job_id, criterion_id, version, definition, "
                    "rationale, evidence_rule, label, created_by, created_at, approved_by, approved_at, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.instance_id,
                        source["job_id"],
                        criterion_id,
                        int(version),
                        source["definition"],
                        source["rationale"],
                        source["evidence_rule"],
                        source["label"],
                        source["created_by"],
                        now,
                        actor,
                        now,
                        source["origin"],
                    ),
                )
            conn.execute(
                "UPDATE jobs SET criteria_version = ?, updated_at = ? WHERE instance_id = ?",
                (int(version), now, self.instance_id),
            )
            result = conn.execute(
                "SELECT * FROM criteria WHERE instance_id = ? AND version = ? ORDER BY criterion_id",
                (self.instance_id, int(version)),
            ).fetchall()
            return [criterion_from_row(r) for r in result]

    def list_criteria(self, version: int | None = None, approved_only: bool = True) -> list[Criterion]:
        """List criteria for a version, defaulting to the active approved set."""
        params: list[Any] = [self.instance_id]
        clauses = ["instance_id = ?"]
        if version is None:
            active = self.active_criteria_version()
            if active == 0 and approved_only:
                return []
            if active != 0:
                clauses.append("version = ?")
                params.append(active)
        else:
            clauses.append("version = ?")
            params.append(int(version))
        if approved_only:
            clauses.append("approved_at IS NOT NULL")
        sql = (
            "SELECT * FROM criteria WHERE " + " AND ".join(clauses) + " ORDER BY criterion_id"
        )
        return [criterion_from_row(r) for r in self.db.query(sql, tuple(params))]

    def active_criteria_version(self) -> int:
        """Highest approved criteria version, or 0 when no criteria are approved."""
        return int(self.db.scalar(_ACTIVE_CRITERIA_SQL, (self.instance_id,), default=0) or 0)

    # ------------------------------------------------------------------
    # Profiles and evidence (PRD 6.3, 6.4, 7.2, 7.3)
    # ------------------------------------------------------------------
    def insert_profile(
        self,
        result: AnalysisResult,
        run_meta: Mapping[str, Any],
        is_current: bool = True,
        is_fixture: bool = False,
    ) -> ProfileRecord:
        """Commit one validated analysis result as a profile plus its evidence.

        The previous current profile for the document is cleared and the new one is
        inserted in the *same* transaction, so the partial unique index
        ``idx_profiles_current`` can never see two current rows. Suggested tasks are
        committed by the caller through :meth:`upsert_task`; keeping them out of this
        transaction is what lets regeneration avoid reopening human-closed work.
        """
        if not result.document_id:
            raise ValidationFailed(
                "An analysis result must name its document.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "document_id"},
            )
        if int(result.source_revision) < 1:
            raise ValidationFailed(
                "An analysis result must be bound to a source revision of at least 1.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "source_revision"},
            )
        meta = dict(run_meta or {})
        profile_id = new_id("profile")
        now = now_iso()
        with self.db.write(
            actor=str(meta.get("actor") or "helper"),
            actor_kind="helper",
            event="profile.insert",
            entity_type="profile",
            entity_id=profile_id,
            new={
                "document_id": result.document_id,
                "source_revision": int(result.source_revision),
                "criteria_version": int(result.criteria_version),
            },
        ) as conn:
            if is_current:
                conn.execute(
                    "UPDATE profiles SET is_current = 0 WHERE document_id = ? AND is_current = 1",
                    (result.document_id,),
                )
            conn.execute(
                "INSERT INTO profiles (id, instance_id, document_id, source_revision, "
                "criteria_version, prompt_version, schema_version, model_route, model_version, "
                "summary_text, validation_state, validation_detail, is_fixture, is_current, "
                "stale, generated_at, run_request_id, run_started_at, run_ended_at, token_usage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    profile_id,
                    self.instance_id,
                    result.document_id,
                    int(result.source_revision),
                    int(result.criteria_version),
                    str(meta.get("prompt_version") or ""),
                    str(result.schema_version),
                    str(meta.get("model_route") or ModelRoute.UNAVAILABLE.value),
                    meta.get("model_version"),
                    result.summary_text,
                    str(meta.get("validation_state") or "pending"),
                    meta.get("validation_detail"),
                    1 if is_fixture else 0,
                    1 if is_current else 0,
                    now,
                    meta.get("run_request_id"),
                    meta.get("run_started_at"),
                    meta.get("run_ended_at"),
                    meta.get("token_usage"),
                ),
            )
            for item in result.evidence:
                conn.execute(
                    "INSERT INTO evidence (instance_id, profile_id, document_id, evidence_key, "
                    "claim_kind, criterion_id, result, span_id, locator_json, quote, validation, "
                    "validation_detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.instance_id,
                        profile_id,
                        result.document_id,
                        item.id,
                        item.claim_kind,
                        item.criterion_id,
                        item.result.value if item.result is not None else None,
                        item.span_id,
                        encode_json(item.locator or {}),
                        item.quote,
                        item.validation or "unchecked",
                        item.validation_detail,
                        now,
                    ),
                )
            row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            return profile_from_row(row)

    def current_profile(self, document_id: str) -> ProfileRecord | None:
        row = self.db.query_one(
            "SELECT * FROM profiles WHERE document_id = ? AND is_current = 1 LIMIT 1",
            (document_id,),
        )
        return profile_from_row(row) if row else None

    def find_reusable_profile(
        self,
        document_id: str,
        source_revision: int,
        criteria_version: int,
        prompt_version: str,
        schema_version: str,
        model_route: str,
    ) -> ProfileRecord | None:
        """Return a non-stale profile whose exact input configuration still matches.

        A change to any bound input makes the cached assessment unusable, so the
        match is on every field rather than a subset (PRD section 6.3).
        """
        row = self.db.query_one(
            "SELECT * FROM profiles WHERE document_id = ? AND source_revision = ? "
            "AND criteria_version = ? AND prompt_version = ? AND schema_version = ? "
            "AND model_route = ? AND stale = 0 "
            "ORDER BY is_current DESC, generated_at DESC LIMIT 1",
            (
                document_id,
                int(source_revision),
                int(criteria_version),
                prompt_version,
                schema_version,
                model_route,
            ),
        )
        return profile_from_row(row) if row else None

    def evidence_for_profile(self, profile_id: str) -> list[dict[str, Any]]:
        """Evidence rows for one profile, with the stored locator decoded."""
        rows = self.db.query(
            "SELECT * FROM evidence WHERE profile_id = ? ORDER BY id ASC",
            (profile_id,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dump_row(row) or {}
            item["locator"] = json_column(row["locator_json"], default={})
            result.append(item)
        return result

    def mark_profiles_stale(self, document_id: str | None = None, reason: str | None = None) -> int:
        """Flag profiles stale without deleting them or touching any decision.

        Staleness is a visibility state, not a deletion: the previous assessment is
        retained for history and shown as stale (PRD sections 6.3, 7.4).
        """
        where = ["instance_id = ?", "stale = 0"]
        params: list[Any] = [self.instance_id]
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        count_sql = "SELECT COUNT(*) FROM profiles WHERE " + " AND ".join(where)
        pending = int(self.db.scalar(count_sql, tuple(params), default=0) or 0)
        if pending == 0:
            return 0
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="profile.mark_stale",
            entity_type="profile" if document_id else None,
            entity_id=document_id,
            new={"reason": reason} if reason else None,
        ) as conn:
            cur = conn.execute(
                "UPDATE profiles SET stale = 1 WHERE " + " AND ".join(where),
                tuple(params),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # Idempotency (PRD 12.2)
    # ------------------------------------------------------------------
    def idempotency_lookup(self, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        """Return the stored result of a repeat request, or raise on key reuse.

        Same key plus same request hash means the original side effect must not run
        again; same key plus a different hash is a client bug and a conflict.
        """
        row = self.db.query_one(
            "SELECT * FROM idempotency_records WHERE instance_id = ? AND scope = ? AND key = ?",
            (self.instance_id, scope, key),
        )
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise Conflict(
                "This idempotency key was already used with a different request.",
                code=Code.IDEMPOTENCY_KEY_REUSED,
                detail={"scope": scope, "key": key},
            )
        return {
            "scope": scope,
            "key": key,
            "request_hash": str(row["request_hash"]),
            "response": json_column(row["response_json"], default=None),
            "job_id": row["job_id"],
            "state_revision": row["state_revision"],
        }

    def idempotency_store(
        self,
        scope: str,
        key: str,
        request_hash: str,
        response: Any = None,
        job_id: str | None = None,
    ) -> None:
        """Persist the outcome of a retryable request under its key."""
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="idempotency.store",
            entity_type="idempotency",
            entity_id=key,
        ) as conn:
            current = conn.execute(
                "SELECT state_revision FROM instances WHERE id = ?", (self.instance_id,)
            ).fetchone()
            revision = int(current["state_revision"]) if current else None
            now = now_iso()
            conn.execute(
                "INSERT INTO idempotency_records (instance_id, scope, key, request_hash, "
                "response_json, job_id, state_revision, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(scope, key) DO UPDATE SET request_hash = excluded.request_hash, "
                "response_json = excluded.response_json, job_id = excluded.job_id, "
                "state_revision = excluded.state_revision",
                (
                    self.instance_id,
                    scope,
                    key,
                    request_hash,
                    encode_json(response) if response is not None else None,
                    job_id,
                    revision,
                    now,
                ),
            )

    # ------------------------------------------------------------------
    # Conversations (PRD 9.4)
    # ------------------------------------------------------------------
    def get_or_create_conversation(self, reviewer: str, kind: str = ChatKind.CHAT.value) -> dict[str, Any]:
        """Return the opaque conversation bound to this instance and reviewer.

        The id is generated, never derived from a reviewer name or a folder path, so
        it can be handed to the adapter as a session reference without leaking
        applicant-identifying structure (PRD section 9.4).
        """
        kind_value = str(kind)
        if kind_value not in (ChatKind.CHAT.value, ChatKind.ANALYSIS.value):
            raise ValidationFailed(
                "A conversation kind must be 'chat' or 'analysis'.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "kind"},
            )
        row = self.db.query_one(
            "SELECT * FROM conversations WHERE instance_id = ? AND reviewer = ? AND kind = ?",
            (self.instance_id, reviewer, kind_value),
        )
        if row is not None:
            return dump_row(row) or {}
        with self.db.write(
            actor=reviewer,
            actor_kind="human",
            event="conversation.create",
            entity_type="conversation",
        ) as conn:
            again = conn.execute(
                "SELECT * FROM conversations WHERE instance_id = ? AND reviewer = ? AND kind = ?",
                (self.instance_id, reviewer, kind_value),
            ).fetchone()
            if again is not None:
                return dump_row(again) or {}
            conversation_id = new_id("conversation")
            now = now_iso()
            conn.execute(
                "INSERT INTO conversations (id, instance_id, reviewer, kind, criteria_version, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, self.instance_id, reviewer, kind_value, self.active_criteria_version(), now, now),
            )
            created = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return dump_row(created) or {}

    def append_message(
        self,
        conversation_id: str,
        role: str,
        body: str,
        payload: Any = None,
        coverage: Any = None,
        request_id: str | None = None,
    ) -> str:
        """Append one turn to a conversation. History is stored locally."""
        if role not in ("user", "assistant", "system"):
            raise ValidationFailed(
                "A message role must be 'user', 'assistant', or 'system'.",
                code=Code.VALIDATION_FAILED,
                detail={"field": "role"},
            )
        message_id = new_id("message")
        with self.db.write(
            actor="helper",
            actor_kind="helper",
            event="message.append",
            entity_type="conversation",
            entity_id=conversation_id,
            request_id=request_id,
        ) as conn:
            parent = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND instance_id = ?",
                (conversation_id, self.instance_id),
            ).fetchone()
            if parent is None:
                raise NotFound(
                    "That conversation does not exist in this instance.",
                    code=Code.NOT_FOUND,
                    detail={"entity": "conversation"},
                )
            now = now_iso()
            conn.execute(
                "INSERT INTO messages (id, instance_id, conversation_id, role, body, payload_json, "
                "coverage_json, request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    self.instance_id,
                    conversation_id,
                    role,
                    body,
                    encode_json(payload) if payload is not None else None,
                    encode_json(coverage) if coverage is not None else None,
                    request_id,
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
        return message_id

    def list_messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE conversation_id = ? AND instance_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, self.instance_id, int(limit)),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dump_row(row) or {}
            item["payload"] = json_column(row["payload_json"], default=None)
            item["coverage"] = json_column(row["coverage_json"], default=None)
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Audit (PRD 11.3)
    # ------------------------------------------------------------------
    def list_audit(
        self,
        limit: int = 100,
        before_seq: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the append-only audit history, newest first.

        Read-only: audit rows are written by ``Database.write`` in the same
        transaction as the mutation they describe.
        """
        clauses = ["instance_id = ?"]
        params: list[Any] = [self.instance_id]
        if before_seq is not None:
            clauses.append("seq < ?")
            params.append(int(before_seq))
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        params.append(int(limit))
        rows = self.db.query(
            "SELECT * FROM audit_events WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?",
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dump_row(row) or {}
            item["affected_ids"] = json_column(row["affected_ids_json"], default=[])
            item["prior"] = json_column(row["prior_json"], default=None)
            item["new"] = json_column(row["new_json"], default=None)
            out.append(item)
        return out
