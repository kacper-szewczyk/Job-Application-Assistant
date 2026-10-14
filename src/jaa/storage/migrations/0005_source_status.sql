-- Pause/resume history for job source connectors (T09, ADR 0011).

-- Append-only: the latest row per source is its status; no row means active. `source` is the
-- connector key (`jaa.connectors.base.SourcePolicy.key`), not a foreign key, because
-- connectors are defined in code, not in the database.
CREATE TABLE source_status_events (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active', 'paused')),
    actor       TEXT NOT NULL CHECK (actor IN ('user', 'system', 'integration')),
    reason      TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX source_status_events_source ON source_status_events (source, id);

CREATE TRIGGER source_status_events_immutable BEFORE UPDATE ON source_status_events
BEGIN
    SELECT RAISE(ABORT, 'source status history is append-only');
END;
