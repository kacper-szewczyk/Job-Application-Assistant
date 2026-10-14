-- Canonical URLs, duplicate flags and saved searches (T11, ADR 0014).

-- `source_url` is the link to the listing exactly as the source gave it; it is what the user
-- opens. `canonical_url` becomes the normalized form used to find the same job elsewhere.
-- Existing rows keep their URL in both until their next fetch normalizes `canonical_url`.
-- `dedupe_key` is the folded "employer | title | location" fallback, NULL when a part is missing.
ALTER TABLE jobs ADD COLUMN source_url TEXT;
UPDATE jobs SET source_url = canonical_url;
ALTER TABLE jobs ADD COLUMN dedupe_key TEXT;
CREATE INDEX jobs_dedupe_key ON jobs (dedupe_key);

-- A pair of jobs that may be the same job. Flags are never merged automatically; the user
-- decides in job_duplicate_reviews. One row per pair, lower job id first. Append-only.
CREATE TABLE job_duplicates (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    other_job_id    INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    reason          TEXT NOT NULL CHECK (reason IN ('canonical_url', 'employer_title_location')),
    detected_at     TEXT NOT NULL,
    CHECK (job_id < other_job_id),
    UNIQUE (job_id, other_job_id)
);
CREATE INDEX job_duplicates_other ON job_duplicates (other_job_id);

CREATE TRIGGER job_duplicates_immutable BEFORE UPDATE ON job_duplicates
BEGIN
    SELECT RAISE(ABORT, 'job duplicate flags are append-only');
END;

-- The user's verdicts on a flag. The latest one counts. Append-only.
CREATE TABLE job_duplicate_reviews (
    id              INTEGER PRIMARY KEY,
    duplicate_id    INTEGER NOT NULL REFERENCES job_duplicates (id) ON DELETE CASCADE,
    verdict         TEXT NOT NULL CHECK (verdict IN ('same_job', 'different_jobs')),
    reviewed_at     TEXT NOT NULL
);
CREATE INDEX job_duplicate_reviews_duplicate ON job_duplicate_reviews (duplicate_id, id);

CREATE TRIGGER job_duplicate_reviews_immutable BEFORE UPDATE ON job_duplicate_reviews
BEGIN
    SELECT RAISE(ABORT, 'duplicate reviews are append-only');
END;

-- A named job search. `query` is a `jaa.core.search.JobQuery` as JSON. `last_refreshed_at` is
-- when its results were last shown, so the next run can point out jobs first seen since.
CREATE TABLE saved_searches (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    query               TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_refreshed_at   TEXT,
    last_result_count   INTEGER
);
