-- Match explanations (T12, ADR 0015).
--
-- A match is computed for one job against one profile version and never changed afterwards:
-- matching again adds a row. `evidence` lists requirements backed by facts (unreviewed facts
-- are labeled and not counted), `gaps` those that are not, `uncertainty` why the score may be
-- off, and `reasons` how the score was reached. `job_content_sha256` is the job's content hash
-- at the time, so a later change to the listing shows the match as outdated. `method` names the
-- matcher version that produced the row.
ALTER TABLE job_matches ADD COLUMN uncertainty TEXT NOT NULL DEFAULT '[]';
ALTER TABLE job_matches ADD COLUMN reasons TEXT NOT NULL DEFAULT '[]';
ALTER TABLE job_matches ADD COLUMN job_content_sha256 TEXT;
ALTER TABLE job_matches ADD COLUMN method TEXT NOT NULL DEFAULT '';
CREATE INDEX job_matches_job_created ON job_matches (job_id, created_at);

CREATE TRIGGER job_matches_immutable BEFORE UPDATE ON job_matches
BEGIN
    SELECT RAISE(ABORT, 'job matches are append-only');
END;
