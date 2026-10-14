-- Storing connector listings as jobs, with change detection and fetch history (T10, ADR 0013).

-- `source_target` is the connector target the listing was last seen in (e.g. a solid.jobs
-- division); removal is only judged within a target. `content_sha256` hashes the listing's
-- content, so a re-fetch can tell changed listings from unchanged ones. `removed_at` is set when
-- a complete fetch of the target no longer lists the job, and cleared if it comes back. It is an
-- observation about the source, not a status: `jobs.status` stays the user's lifecycle (ADR 0005).
ALTER TABLE jobs ADD COLUMN source_target TEXT;
ALTER TABLE jobs ADD COLUMN content_sha256 TEXT;
ALTER TABLE jobs ADD COLUMN source_updated_at TEXT;
ALTER TABLE jobs ADD COLUMN removed_at TEXT;
CREATE INDEX jobs_source_target ON jobs (source, source_target);

-- One row per fetch of one target. Append-only.
CREATE TABLE source_fetch_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'denied', 'unavailable', 'failed')),
    requests    INTEGER NOT NULL DEFAULT 0,
    listings    INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    reappeared  INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    issues      TEXT NOT NULL DEFAULT '[]',
    error       TEXT
);
CREATE INDEX source_fetch_runs_source ON source_fetch_runs (source, target, id);

CREATE TRIGGER source_fetch_runs_immutable BEFORE UPDATE ON source_fetch_runs
BEGIN
    SELECT RAISE(ABORT, 'source fetch history is append-only');
END;
