-- Onboarding preferences (T04, ADR 0007).

-- A skipped question is stored, so onboarding can tell "skipped" from "never asked".
-- Skipped rows hold the JSON value 'null'.
ALTER TABLE preferences
    ADD COLUMN status TEXT NOT NULL DEFAULT 'answered' CHECK (status IN ('answered', 'skipped'));

-- Append-only history: every answer, change or skip, with the value it set.
CREATE TABLE preference_events (
    id              INTEGER PRIMARY KEY,
    preference_key  TEXT NOT NULL REFERENCES preferences (key) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('answered', 'skipped')),
    value           TEXT NOT NULL,
    is_sensitive    INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    occurred_at     TEXT NOT NULL
);
CREATE INDEX preference_events_key ON preference_events (preference_key, occurred_at);

CREATE TRIGGER preference_events_immutable BEFORE UPDATE ON preference_events
BEGIN
    SELECT RAISE(ABORT, 'preference history is append-only');
END;
