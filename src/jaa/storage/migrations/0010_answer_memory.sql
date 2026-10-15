-- Application question answers (T14, ADR 0017).
--
-- `answers` becomes append-only: the latest row per question_key is the current answer, and
-- every change (a new value, a re-confirmation, a new reuse policy) adds a row. Deleting rows
-- stays possible so the user can forget an answer. `origin` says how the row came about;
-- `source_answer_id`, `source_preference_key` and `answer_sources` say where an accepted value
-- came from, and `job_id` is the job whose question was being answered, if any.
ALTER TABLE answers ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'
    CHECK (origin IN ('user', 'accepted', 'reconfirmed', 'policy_changed'));
ALTER TABLE answers ADD COLUMN source_preference_key TEXT;
ALTER TABLE answers ADD COLUMN source_answer_id INTEGER REFERENCES answers (id) ON DELETE SET NULL;
ALTER TABLE answers ADD COLUMN job_id INTEGER REFERENCES jobs (id) ON DELETE SET NULL;

-- The references are left out so that deleting a job or a forgotten answer can clear them.
CREATE TRIGGER answers_immutable BEFORE UPDATE OF
    question_key, question_text, value, reuse_policy, is_sensitive, created_at, confirmed_at,
    expires_at, origin, source_preference_key
ON answers
BEGIN
    SELECT RAISE(ABORT, 'answers are append-only');
END;

CREATE TRIGGER answer_sources_immutable BEFORE UPDATE ON answer_sources
BEGIN
    SELECT RAISE(ABORT, 'answer sources are append-only');
END;

-- Questions the user copied from an employer's application form, for sources that do not
-- publish them. Listing questions stay in jobs.requirements.
CREATE TABLE job_questions (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE INDEX job_questions_job ON job_questions (job_id);
