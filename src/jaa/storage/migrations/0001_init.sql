-- Initial schema (T02, SPEC §8).
--
-- Conventions:
--   * Timestamps are ISO-8601 UTC text with offset, e.g. 2026-10-14T08:51:08.123456+00:00.
--   * JSON columns hold JSON text.
--   * Enum columns are constrained with CHECK (... IN (...)); values must match jaa.core.models.
--   * is_sensitive marks values that need extra care (contact details, eligibility, salary, ...).

-- Single-user profile. Contact/identity details are profile facts, not columns here.
CREATE TABLE profile (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    active_version_id   INTEGER REFERENCES profile_versions (id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE import_runs (
    id                  INTEGER PRIMARY KEY,
    source_type         TEXT NOT NULL CHECK (source_type IN ('pdf', 'docx', 'latex', 'linkedin_archive', 'text')),
    source_path         TEXT,
    source_sha256       TEXT,
    imported_at         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('succeeded', 'partial', 'failed')),
    summary             TEXT NOT NULL DEFAULT '{}',
    errors              TEXT NOT NULL DEFAULT '[]',
    retention           TEXT NOT NULL CHECK (retention IN ('keep', 'delete_after_import')),
    source_deleted_at   TEXT
);

-- A fact's value and provenance are immutable once written; a user correction is a new fact
-- pointing at the one it corrects. Only review state and timestamps may change.
-- import_run_id and corrected_from_fact_id are excluded from the trigger so that deleting an
-- import run or an old fact (T19) can null the reference.
CREATE TABLE profile_facts (
    id                      INTEGER PRIMARY KEY,
    field_key               TEXT NOT NULL,
    value_raw               TEXT NOT NULL,
    value_normalized        TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'unreviewed' CHECK (state IN ('unreviewed', 'confirmed', 'rejected')),
    origin                  TEXT NOT NULL CHECK (origin IN ('import', 'user', 'correction')),
    confidence              REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    is_sensitive            INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    import_run_id           INTEGER REFERENCES import_runs (id) ON DELETE SET NULL,
    source_location         TEXT,
    source_excerpt          TEXT,
    corrected_from_fact_id  INTEGER REFERENCES profile_facts (id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    confirmed_at            TEXT,
    rejected_at             TEXT
);
CREATE INDEX profile_facts_field_key ON profile_facts (field_key, state);
CREATE INDEX profile_facts_import_run ON profile_facts (import_run_id);

CREATE TRIGGER profile_facts_value_immutable
BEFORE UPDATE OF field_key, value_raw, value_normalized, origin,
                 source_location, source_excerpt, created_at
ON profile_facts
BEGIN
    SELECT RAISE(ABORT, 'profile fact values and provenance are immutable');
END;

-- Immutable snapshot: the set of confirmed facts at a point in time.
CREATE TABLE profile_versions (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE profile_version_facts (
    profile_version_id  INTEGER NOT NULL REFERENCES profile_versions (id) ON DELETE CASCADE,
    fact_id             INTEGER NOT NULL REFERENCES profile_facts (id) ON DELETE CASCADE,
    PRIMARY KEY (profile_version_id, fact_id)
);

CREATE TRIGGER profile_versions_immutable BEFORE UPDATE ON profile_versions
BEGIN
    SELECT RAISE(ABORT, 'profile versions are immutable');
END;

CREATE TRIGGER profile_version_facts_immutable BEFORE UPDATE ON profile_version_facts
BEGIN
    SELECT RAISE(ABORT, 'profile versions are immutable');
END;

CREATE TABLE preferences (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    value           TEXT NOT NULL,
    is_sensitive    INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    updated_at      TEXT NOT NULL
);

CREATE TABLE answers (
    id              INTEGER PRIMARY KEY,
    question_key    TEXT NOT NULL,
    question_text   TEXT NOT NULL,
    value           TEXT NOT NULL,
    reuse_policy    TEXT NOT NULL DEFAULT 'always_ask' CHECK (reuse_policy IN ('always_ask', 'reuse_with_review', 'reuse')),
    is_sensitive    INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    expires_at      TEXT
);
CREATE INDEX answers_question_key ON answers (question_key);

CREATE TABLE answer_sources (
    answer_id   INTEGER NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    fact_id     INTEGER NOT NULL REFERENCES profile_facts (id) ON DELETE CASCADE,
    PRIMARY KEY (answer_id, fact_id)
);

-- Job status is the single application lifecycle (ADR 0005); history is in job_status_events.
CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    source_job_id       TEXT NOT NULL,
    canonical_url       TEXT NOT NULL,
    employer            TEXT NOT NULL,
    title               TEXT NOT NULL,
    location            TEXT,
    description         TEXT,
    posted_at           TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    requirements        TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'discovered' CHECK (status IN (
                            'discovered', 'saved', 'preparing', 'ready_for_review', 'submitted',
                            'interviewing', 'rejected', 'withdrawn', 'closed')),
    status_changed_at   TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);
CREATE INDEX jobs_canonical_url ON jobs (canonical_url);
CREATE INDEX jobs_status ON jobs (status);

CREATE TABLE job_status_events (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    from_status     TEXT CHECK (from_status IS NULL OR from_status IN (
                        'discovered', 'saved', 'preparing', 'ready_for_review', 'submitted',
                        'interviewing', 'rejected', 'withdrawn', 'closed')),
    to_status       TEXT NOT NULL CHECK (to_status IN (
                        'discovered', 'saved', 'preparing', 'ready_for_review', 'submitted',
                        'interviewing', 'rejected', 'withdrawn', 'closed')),
    actor           TEXT NOT NULL CHECK (actor IN ('user', 'system', 'integration')),
    occurred_at     TEXT NOT NULL,
    note            TEXT
);
CREATE INDEX job_status_events_job ON job_status_events (job_id, occurred_at);

CREATE TRIGGER job_status_events_immutable BEFORE UPDATE ON job_status_events
BEGIN
    SELECT RAISE(ABORT, 'job status history is append-only');
END;

CREATE TABLE job_matches (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    profile_version_id  INTEGER NOT NULL REFERENCES profile_versions (id) ON DELETE CASCADE,
    score               REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    evidence            TEXT NOT NULL DEFAULT '[]',
    gaps                TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL
);
CREATE INDEX job_matches_job ON job_matches (job_id);

-- Submission details for a job. A submission must say who confirmed it (SPEC §6 Human control).
CREATE TABLE applications (
    id                      INTEGER PRIMARY KEY,
    job_id                  INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    created_at              TEXT NOT NULL,
    submitted_at            TEXT,
    submission_confirmed_by TEXT CHECK (submission_confirmed_by IS NULL OR submission_confirmed_by IN ('user', 'integration_receipt')),
    external_reference      TEXT,
    notes                   TEXT,
    CHECK ((submitted_at IS NULL) = (submission_confirmed_by IS NULL))
);

CREATE TABLE application_answers (
    application_id  INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    answer_id       INTEGER NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    PRIMARY KEY (application_id, answer_id)
);

CREATE TABLE document_artifacts (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('cv_pdf', 'cv_source', 'cover_letter_pdf', 'cover_letter_source')),
    path                TEXT NOT NULL,
    job_id              INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    application_id      INTEGER REFERENCES applications (id) ON DELETE SET NULL,
    profile_version_id  INTEGER REFERENCES profile_versions (id) ON DELETE SET NULL,
    template_version    TEXT,
    content_sha256      TEXT NOT NULL,
    manifest            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);
CREATE INDEX document_artifacts_job ON document_artifacts (job_id);
