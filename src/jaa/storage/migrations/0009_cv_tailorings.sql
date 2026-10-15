-- Tailored CVs (T13, ADR 0016).
--
-- A tailoring is the plan of edits for one job's CV, checked against one profile version.
-- Each change (a proposal, the user's edits, an approval) adds a revision; nothing is updated.
-- `plan` holds the edits and cover letter paragraphs (CV text), `checks` the issue codes per
-- edit, and `approvals` fingerprints of the edits the user approved despite their issues.
CREATE TABLE cv_tailorings (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    profile_version_id  INTEGER NOT NULL REFERENCES profile_versions (id) ON DELETE CASCADE,
    job_match_id        INTEGER REFERENCES job_matches (id) ON DELETE SET NULL,
    origin              TEXT NOT NULL CHECK (origin IN ('proposed', 'edited', 'approved')),
    plan                TEXT NOT NULL DEFAULT '{}',
    checks              TEXT NOT NULL DEFAULT '[]',
    approvals           TEXT NOT NULL DEFAULT '[]',
    blocked             INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
    method              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (job_id, revision)
);

CREATE TRIGGER cv_tailorings_immutable BEFORE UPDATE ON cv_tailorings
BEGIN
    SELECT RAISE(ABORT, 'cv tailorings are append-only');
END;
