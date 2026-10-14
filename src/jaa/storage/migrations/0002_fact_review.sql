-- Fact review workflow (T03, ADR 0006).

-- A field key holds at most one confirmed value. Replacing it means rejecting the old fact
-- in the same transaction (jaa.storage.profile), so a profile version is never ambiguous.
CREATE UNIQUE INDEX profile_facts_one_confirmed_per_key
    ON profile_facts (field_key) WHERE state = 'confirmed';

-- Append-only history of review decisions. profile_version_id is the version created by the
-- decision, or NULL when the confirmed set did not change (e.g. rejecting a candidate).
CREATE TABLE profile_fact_events (
    id                  INTEGER PRIMARY KEY,
    fact_id             INTEGER NOT NULL REFERENCES profile_facts (id) ON DELETE CASCADE,
    from_state          TEXT CHECK (from_state IS NULL OR from_state IN ('unreviewed', 'confirmed', 'rejected')),
    to_state            TEXT NOT NULL CHECK (to_state IN ('unreviewed', 'confirmed', 'rejected')),
    occurred_at         TEXT NOT NULL,
    profile_version_id  INTEGER REFERENCES profile_versions (id) ON DELETE SET NULL
);
CREATE INDEX profile_fact_events_fact ON profile_fact_events (fact_id, occurred_at);

CREATE TRIGGER profile_fact_events_immutable
BEFORE UPDATE OF fact_id, from_state, to_state, occurred_at ON profile_fact_events
BEGIN
    SELECT RAISE(ABORT, 'profile fact history is append-only');
END;
