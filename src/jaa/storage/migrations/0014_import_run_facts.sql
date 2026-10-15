-- Which facts each import run saw (#73, ADR 0027).

-- `add_imported_facts` skips a candidate whose value is already stored for the same key, so
-- `profile_facts.import_run_id` only names the run that first stored a value. This table links
-- a run to every fact its candidates matched, added or skipped, so the latest LinkedIn import
-- can be compared with confirmed facts even where both sources agree. Runs stored before this
-- migration have no rows here; readers fall back to `profile_facts.import_run_id`.
CREATE TABLE import_run_facts (
    import_run_id INTEGER NOT NULL REFERENCES import_runs (id) ON DELETE CASCADE,
    fact_id       INTEGER NOT NULL REFERENCES profile_facts (id) ON DELETE CASCADE,
    PRIMARY KEY (import_run_id, fact_id)
);
CREATE INDEX import_run_facts_fact ON import_run_facts (fact_id);

CREATE TRIGGER import_run_facts_immutable BEFORE UPDATE ON import_run_facts
BEGIN
    SELECT RAISE(ABORT, 'import run facts are immutable');
END;
