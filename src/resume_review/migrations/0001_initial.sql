-- Migration 0001: initial schema for the folder-local resume review instance.
--
-- Authority: PRD section 11 (Database and persistence contracts).
--
-- Conventions used throughout:
--   * All timestamps are ISO-8601 UTC strings ('YYYY-MM-DDTHH:MM:SS.ffffff+00:00').
--   * All booleans are INTEGER 0/1 with a CHECK constraint.
--   * All revisions/counters are INTEGER, monotonically increasing, never reused.
--   * Every table that holds applicant-adjacent data is scoped to instance_id.
--     There is exactly one instance per database, enforced by a check on `instances`.
--   * Paths stored in records are RELATIVE to the registered root (PRD section 4).
--     Absolute paths live only in the protected host registry, never in the DB.
--   * Authentication secrets, Gateway tokens and login credentials are NEVER stored
--     here. Only non-secret actor references required to explain history.
--
-- The connection layer sets `PRAGMA foreign_keys = ON` per connection; it is not
-- repeated here because the pragma is a no-op inside a transaction.

-- ---------------------------------------------------------------------------
-- Instance identity and global state revision
-- ---------------------------------------------------------------------------
-- schema_version here is a fast cache of the highest applied migration; the
-- authoritative record is `schema_migrations`.
CREATE TABLE instances (
    id                  TEXT    PRIMARY KEY,
    schema_version      INTEGER NOT NULL,
    app_version         TEXT    NOT NULL,
    -- Bumped by every successful application mutation, in the same transaction
    -- as the mutation and its audit event. Clients use it for optimistic display.
    state_revision      INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    -- Non-secret descriptor of where the workspace lives, for diagnostics only.
    -- Never an authorisation decision: authorization comes from the host registry.
    storage_mode        TEXT    NOT NULL DEFAULT 'local'
                                CHECK (storage_mode IN ('local', 'shared_host_local')),
    host_label          TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    -- One instance per database (PRD section 11.1).
    singleton           INTEGER NOT NULL DEFAULT 1 CHECK (singleton = 1),
    UNIQUE (singleton)
);

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    checksum    TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Job requisition and approved criteria
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    title               TEXT    NOT NULL DEFAULT '',
    description_text    TEXT    NOT NULL DEFAULT '',
    description_sha256  TEXT    NOT NULL DEFAULT '',
    source_reference    TEXT,
    criteria_version    INTEGER NOT NULL DEFAULT 0 CHECK (criteria_version >= 0),
    approved_by         TEXT,
    approved_at         TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_jobs_instance ON jobs(instance_id);

-- A criterion row is either a proposal (approved_at IS NULL) or an approved
-- definition. Approval is a human action recorded through the review interface.
-- (criterion_id, version) is the stable identity; editing a definition produces
-- a new version rather than mutating history.
CREATE TABLE criteria (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    job_id              TEXT    NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    criterion_id        TEXT    NOT NULL,
    version             INTEGER NOT NULL CHECK (version >= 1),
    definition          TEXT    NOT NULL,
    rationale           TEXT    NOT NULL DEFAULT '',
    evidence_rule       TEXT    NOT NULL DEFAULT '',
    -- 'required'/'preferred' are advisory labels. They do NOT authorise
    -- automatic rejection and do not hide unknowns (PRD section 7.4).
    label               TEXT    CHECK (label IS NULL OR label IN ('required', 'preferred')),
    created_by          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    approved_by         TEXT,
    approved_at         TEXT,
    superseded_at       TEXT,
    origin              TEXT    NOT NULL DEFAULT 'human'
                                CHECK (origin IN ('human', 'agent_proposal')),
    UNIQUE (instance_id, criterion_id, version)
);

CREATE INDEX idx_criteria_active ON criteria(instance_id, version, approved_at);

-- ---------------------------------------------------------------------------
-- Documents: stable identity across renames and managed moves
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id                  TEXT    PRIMARY KEY,          -- UUIDv4, stable for life
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    -- Original filename as first observed. Display only; never used as identity.
    original_filename   TEXT    NOT NULL,
    -- Human/derived display name when confidently extracted; NULL means "unknown",
    -- which the UI must render distinctly from an empty string.
    display_name        TEXT,
    current_rel_path    TEXT    NOT NULL,             -- root-relative, forward slashes
    first_seen_rel_path TEXT    NOT NULL,
    media_type          TEXT    NOT NULL DEFAULT 'unknown'
                                CHECK (media_type IN ('pdf', 'docx', 'txt', 'unsupported', 'unknown')),
    size_bytes          INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    content_sha256      TEXT,
    current_revision    INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    -- Filesystem identity where available (volume serial + file index on Windows,
    -- st_dev + st_ino on POSIX). Diagnostics only, never an authorization input.
    fs_identity         TEXT,
    processing_state    TEXT    NOT NULL DEFAULT 'discovered'
                                CHECK (processing_state IN
                                    ('discovered','extracting','analyzing','ready',
                                     'manual_review','error','stale')),
    processing_detail   TEXT,
    -- Verified filesystem reconciliation result, not a human intent (PRD section 10).
    location            TEXT    NOT NULL DEFAULT 'active'
                                CHECK (location IN ('active','rejected','trash','missing','conflict')),
    location_version    INTEGER NOT NULL DEFAULT 0 CHECK (location_version >= 0),
    -- Set when material input changes while a human decision exists. Flags the
    -- decision for reconsideration; never overwrites it automatically.
    decision_needs_recheck INTEGER NOT NULL DEFAULT 0 CHECK (decision_needs_recheck IN (0,1)),
    recheck_reason      TEXT,
    -- Same bytes at two paths create two submissions plus this flag. No automatic
    -- merge, delete, or decision transfer (PRD section 6.2 / AT-09).
    duplicate_content   INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_content IN (0,1)),
    duplicate_of        TEXT    REFERENCES documents(id) ON DELETE SET NULL,
    -- Unknown submitted dates remain unknown (PRD section 6.2).
    submitted_at        TEXT,
    ingested_at         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    archived_at         TEXT,
    UNIQUE (instance_id, current_rel_path)
);

CREATE INDEX idx_documents_processing  ON documents(instance_id, processing_state);
CREATE INDEX idx_documents_location    ON documents(instance_id, location);
CREATE INDEX idx_documents_ingested    ON documents(instance_id, ingested_at, id);
CREATE INDEX idx_documents_sha         ON documents(instance_id, content_sha256);
CREATE INDEX idx_documents_path        ON documents(instance_id, current_rel_path);

-- Immutable record of each parsed byte-sequence. A content change produces a new
-- revision; the prior revision row is never edited.
CREATE TABLE document_revisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    content_sha256      TEXT    NOT NULL,
    size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
    rel_path            TEXT    NOT NULL,
    parser_name         TEXT,
    parser_version      TEXT,
    extraction_state    TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (extraction_state IN ('pending','ok','partial','failed','unsupported')),
    extraction_detail   TEXT,
    -- Bundle-relative reference to .review/extracted/<document_id>/<revision>.json
    span_ref            TEXT,
    span_count          INTEGER NOT NULL DEFAULT 0 CHECK (span_count >= 0),
    char_count          INTEGER NOT NULL DEFAULT 0 CHECK (char_count >= 0),
    page_count          INTEGER,
    captured_at         TEXT    NOT NULL,
    UNIQUE (document_id, revision)
);

CREATE INDEX idx_revisions_sha ON document_revisions(content_sha256, parser_name, parser_version);

-- ---------------------------------------------------------------------------
-- Analysis profiles and evidence
-- ---------------------------------------------------------------------------
CREATE TABLE profiles (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- The exact input configuration this assessment is valid for. A change to any
    -- of these makes the assessment stale (PRD section 6.3 caching rule).
    source_revision     INTEGER NOT NULL CHECK (source_revision >= 1),
    criteria_version    INTEGER NOT NULL CHECK (criteria_version >= 0),
    prompt_version      TEXT    NOT NULL,
    schema_version      TEXT    NOT NULL,
    model_route         TEXT    NOT NULL,
    model_version       TEXT,
    summary_text        TEXT    NOT NULL DEFAULT '',
    -- Trusted run metadata is attached by the helper, never reported by the model.
    validation_state    TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (validation_state IN ('pending','valid','invalid','partial')),
    validation_detail   TEXT,
    is_fixture          INTEGER NOT NULL DEFAULT 0 CHECK (is_fixture IN (0,1)),
    is_current          INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0,1)),
    stale               INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
    generated_at        TEXT    NOT NULL,
    run_request_id      TEXT,
    run_started_at      TEXT,
    run_ended_at        TEXT,
    token_usage         INTEGER,
    UNIQUE (id, instance_id)
);

-- At most one current profile per document. Superseded profiles are retained for
-- history but must never become the current assessment (PRD section 6.4).
CREATE UNIQUE INDEX idx_profiles_current
    ON profiles(document_id) WHERE is_current = 1;
CREATE INDEX idx_profiles_cache
    ON profiles(document_id, source_revision, criteria_version, prompt_version,
                schema_version, model_route);

CREATE TABLE evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    profile_id          TEXT    NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    evidence_key        TEXT    NOT NULL,       -- model-supplied id, unique per profile
    claim_kind          TEXT    NOT NULL CHECK (claim_kind IN ('summary','criterion')),
    criterion_id        TEXT,
    result              TEXT    CHECK (result IS NULL OR result IN
                                    ('supported','not_found','unclear','needs_manual_review')),
    span_id             TEXT    NOT NULL,
    locator_json        TEXT    NOT NULL DEFAULT '{}',
    quote               TEXT    NOT NULL,
    -- Validation outcome for this specific piece of evidence. A quote that does
    -- not occur in the identified span is rejected and never stored as valid.
    validation          TEXT    NOT NULL DEFAULT 'unchecked'
                                CHECK (validation IN ('unchecked','verified','quote_missing','span_missing','malformed')),
    validation_detail   TEXT,
    created_at          TEXT    NOT NULL,
    UNIQUE (profile_id, evidence_key)
);

CREATE INDEX idx_evidence_profile ON evidence(profile_id, claim_kind);
CREATE INDEX idx_evidence_doc     ON evidence(document_id, criterion_id);

-- ---------------------------------------------------------------------------
-- Human state: decisions, notes, tasks
-- ---------------------------------------------------------------------------
-- Exactly one decision row per document; disposition changes bump decision_revision.
-- Decision revision is deliberately separate from note revision so that editing a
-- note does not invalidate an otherwise identical move plan (PRD section 11.2).
CREATE TABLE decisions (
    document_id         TEXT    PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    disposition         TEXT    NOT NULL DEFAULT 'unreviewed'
                                CHECK (disposition IN ('unreviewed','keep','reject','hold')),
    decision_revision   INTEGER NOT NULL DEFAULT 0 CHECK (decision_revision >= 0),
    actor               TEXT    NOT NULL DEFAULT '',
    decided_at          TEXT,
    needs_recheck       INTEGER NOT NULL DEFAULT 0 CHECK (needs_recheck IN (0,1)),
    -- Set while a Trash request is pending or the file is in Trash: the UI freezes
    -- disposition controls until cancel or restore (PRD section 8.3).
    disposition_frozen  INTEGER NOT NULL DEFAULT 0 CHECK (disposition_frozen IN (0,1)),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_decisions_disposition ON decisions(instance_id, disposition);

CREATE TABLE notes (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    body                TEXT    NOT NULL DEFAULT '',
    author              TEXT    NOT NULL,
    note_revision       INTEGER NOT NULL DEFAULT 1 CHECK (note_revision >= 1),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    deleted_at          TEXT
);

CREATE INDEX idx_notes_document ON notes(document_id, created_at);

-- Tasks live in their own table so regeneration cannot overwrite notes or
-- automatically reopen completed work (PRD section 8.4).
CREATE TABLE review_tasks (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    task_type           TEXT    NOT NULL DEFAULT 'general',
    criterion_id        TEXT,
    source_revision     INTEGER,
    -- Dedupe key: task type + document + criterion + relevant source revision.
    dedupe_key          TEXT    NOT NULL,
    -- Agent-suggested and human-created tasks are distinguished, never merged.
    origin              TEXT    NOT NULL DEFAULT 'human'
                                CHECK (origin IN ('human','agent','system')),
    title               TEXT    NOT NULL,
    detail              TEXT    NOT NULL DEFAULT '',
    state               TEXT    NOT NULL DEFAULT 'open'
                                CHECK (state IN ('open','closed','dismissed')),
    severity            TEXT    NOT NULL DEFAULT 'normal'
                                CHECK (severity IN ('info','normal','attention')),
    resolution          TEXT,
    resolution_note     TEXT,
    closed_by           TEXT,
    closed_at           TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (instance_id, dedupe_key)
);

CREATE INDEX idx_tasks_open     ON review_tasks(instance_id, state, document_id);
CREATE INDEX idx_tasks_document ON review_tasks(document_id, state);

-- ---------------------------------------------------------------------------
-- File actions: pending intent, immutable plan, approval, execution
-- ---------------------------------------------------------------------------
-- Pending intent is a human request or an unapproved agent proposal. It never
-- moves anything by itself.
CREATE TABLE action_intents (
    document_id         TEXT    PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    intent              TEXT    NOT NULL DEFAULT 'none'
                                CHECK (intent IN ('none','move_rejected','restore_active',
                                                  'move_trash','restore_previous')),
    intent_revision     INTEGER NOT NULL DEFAULT 0 CHECK (intent_revision >= 0),
    state               TEXT    NOT NULL DEFAULT 'saved'
                                CHECK (state IN ('saved','planned','cancelled','applied','blocked')),
    requester           TEXT    NOT NULL DEFAULT '',
    origin_batch_id     TEXT,
    note                TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE action_batches (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    plan_json           TEXT    NOT NULL,
    plan_hash           TEXT    NOT NULL,
    criteria_version    INTEGER NOT NULL DEFAULT 0,
    -- The approval record: authenticated reviewer, plan hash, time, expiry.
    approval_actor      TEXT,
    approval_time       TEXT,
    approval_expires_at TEXT,
    execution_state     TEXT    NOT NULL DEFAULT 'planned'
                                CHECK (execution_state IN ('planned','approved','applying',
                                                           'completed','partial','blocked','canceled')),
    -- Bumped on every executor transition so a replay cannot repeat committed work.
    execution_revision  INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    error_code          TEXT,
    error_detail        TEXT,
    -- For restore plans: the batch whose effect this plan inverts.
    inverse_of_batch_id TEXT
);

CREATE INDEX idx_batches_state ON action_batches(instance_id, execution_state);
CREATE INDEX idx_batches_hash  ON action_batches(plan_hash);

-- Durable per-operation record. Intent is written here BEFORE the filesystem is
-- touched, so a crash is always recoverable by inspecting the journal plus the
-- actual source/destination state (PRD section 13.3).
CREATE TABLE file_operations (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    batch_id            TEXT    NOT NULL REFERENCES action_batches(id) ON DELETE CASCADE,
    document_id         TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    sequence            INTEGER NOT NULL CHECK (sequence >= 0),
    kind                TEXT    NOT NULL CHECK (kind IN ('move_rejected','restore_active',
                                                         'move_trash','restore_previous')),
    source_rel_path     TEXT    NOT NULL,
    destination_rel_path TEXT   NOT NULL,
    expected_sha256     TEXT    NOT NULL,
    expected_size       INTEGER,
    source_revision     INTEGER NOT NULL,
    decision_revision   INTEGER NOT NULL,
    intent_revision     INTEGER NOT NULL,
    location_version    INTEGER NOT NULL,
    -- Durable step progression: planned -> intent_recorded -> file_moved -> committed.
    -- 'needs_reconciliation' is the terminal state when reality is ambiguous.
    state               TEXT    NOT NULL DEFAULT 'planned'
                                CHECK (state IN ('planned','intent_recorded','file_moved',
                                                 'committed','needs_reconciliation','failed','skipped')),
    observed_source_state      TEXT,
    observed_destination_state TEXT,
    error_code          TEXT,
    error_detail        TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (batch_id, sequence)
);

CREATE INDEX idx_fileops_state   ON file_operations(instance_id, state);
CREATE INDEX idx_fileops_document ON file_operations(document_id);

-- ---------------------------------------------------------------------------
-- Durable work queue
-- ---------------------------------------------------------------------------
CREATE TABLE processing_jobs (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    -- Stable job key so a retry or replay does not create duplicate profiles/tasks.
    job_key             TEXT    NOT NULL,
    kind                TEXT    NOT NULL CHECK (kind IN ('scan','extraction','analysis','snapshot','chat')),
    document_id         TEXT    REFERENCES documents(id) ON DELETE CASCADE,
    input_versions      TEXT    NOT NULL DEFAULT '{}',
    state               TEXT    NOT NULL DEFAULT 'queued'
                                CHECK (state IN ('queued','leased','running','succeeded',
                                                 'failed','canceled','superseded')),
    lease_token         TEXT,
    lease_expires_at    TEXT,
    lease_owner         TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts        INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    repair_attempts     INTEGER NOT NULL DEFAULT 0 CHECK (repair_attempts >= 0),
    cancel_requested    INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    result_ref          TEXT,
    error_code          TEXT,
    error_detail        TEXT,
    progress_done       INTEGER NOT NULL DEFAULT 0,
    progress_total      INTEGER NOT NULL DEFAULT 0,
    priority            INTEGER NOT NULL DEFAULT 100,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    finished_at         TEXT,
    UNIQUE (instance_id, job_key)
);

CREATE INDEX idx_jobs_claim  ON processing_jobs(instance_id, state, priority, created_at);
CREATE INDEX idx_jobs_lease  ON processing_jobs(lease_expires_at);
CREATE INDEX idx_jobs_doc    ON processing_jobs(document_id);

-- ---------------------------------------------------------------------------
-- Folder-scoped conversation
-- ---------------------------------------------------------------------------
-- Opaque conversation identity bound server-side to (instance, reviewer). Candidate
-- names and full folder paths are never provider-visible session identifiers.
CREATE TABLE conversations (
    id                  TEXT    PRIMARY KEY,        -- opaque
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    reviewer            TEXT    NOT NULL,
    kind                TEXT    NOT NULL DEFAULT 'chat'
                                CHECK (kind IN ('chat','analysis')),
    -- Reference to the adapter-side session. Analysis sessions are distinct from a
    -- reviewer's freeform chat thread (PRD section 9.4).
    adapter_session_ref TEXT,
    criteria_version    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (instance_id, reviewer, kind)
);

CREATE TABLE messages (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    conversation_id     TEXT    NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                TEXT    NOT NULL CHECK (role IN ('user','assistant','system')),
    body                TEXT    NOT NULL,
    -- Validated structured payload attached to an assistant turn (e.g. a filter
    -- proposal). Stored as data; never executed as SQL or code.
    payload_json        TEXT,
    coverage_json       TEXT,
    request_id          TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

CREATE TABLE saved_filters (
    id                  TEXT    PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    definition_json     TEXT    NOT NULL,
    criteria_version    INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (instance_id, name)
);

-- ---------------------------------------------------------------------------
-- Audit and idempotency
-- ---------------------------------------------------------------------------
-- Append-only through the application API. A filesystem administrator can still
-- modify local data; this is not advertised as cryptographic tamper-proofing
-- (PRD section 11.3).
CREATE TABLE audit_events (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    actor               TEXT    NOT NULL DEFAULT '',
    actor_kind          TEXT    NOT NULL DEFAULT 'human'
                                CHECK (actor_kind IN ('human','helper','agent','system')),
    event               TEXT    NOT NULL,
    entity_type         TEXT,
    entity_id           TEXT,
    affected_ids_json   TEXT    NOT NULL DEFAULT '[]',
    prior_json          TEXT,
    new_json            TEXT,
    outcome             TEXT    NOT NULL DEFAULT 'ok'
                                CHECK (outcome IN ('ok','denied','conflict','error')),
    request_id          TEXT,
    code                TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX idx_audit_instance ON audit_events(instance_id, seq);
CREATE INDEX idx_audit_entity   ON audit_events(entity_type, entity_id, seq);

CREATE TABLE idempotency_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    scope               TEXT    NOT NULL,     -- principal + route, canonicalised
    key                 TEXT    NOT NULL,
    request_hash        TEXT    NOT NULL,
    response_json       TEXT,
    job_id              TEXT,
    state_revision      INTEGER,
    created_at          TEXT    NOT NULL,
    expires_at          TEXT,
    UNIQUE (scope, key)
);

CREATE INDEX idx_idem_created ON idempotency_records(created_at);

-- ---------------------------------------------------------------------------
-- Extraction cache
-- ---------------------------------------------------------------------------
-- Keyed by content hash + parser version so unchanged bytes are never reparsed and
-- a retry never creates duplicate spans (PRD section 6.3).
CREATE TABLE extraction_cache (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id         TEXT    NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    content_sha256      TEXT    NOT NULL,
    parser_name         TEXT    NOT NULL,
    parser_version      TEXT    NOT NULL,
    payload_json        TEXT    NOT NULL,
    span_count          INTEGER NOT NULL DEFAULT 0,
    char_count          INTEGER NOT NULL DEFAULT 0,
    page_count          INTEGER,
    created_at          TEXT    NOT NULL,
    last_used_at        TEXT    NOT NULL,
    UNIQUE (instance_id, content_sha256, parser_name, parser_version)
);
