-- Imports pulled from LinkedIn's Member Data Portability API (#72, ADR 0026).

-- SQLite cannot change a CHECK constraint in place, so import_runs is rebuilt with the new
-- source type. `migrate` runs migrations with foreign keys off and checks them before
-- committing, so dropping the old table does not null profile_facts.import_run_id.
CREATE TABLE import_runs_new (
    id                  INTEGER PRIMARY KEY,
    source_type         TEXT NOT NULL CHECK (source_type IN ('pdf', 'docx', 'latex', 'linkedin_archive', 'linkedin_api', 'text')),
    source_path         TEXT,
    source_sha256       TEXT,
    imported_at         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('succeeded', 'partial', 'failed')),
    summary             TEXT NOT NULL DEFAULT '{}',
    errors              TEXT NOT NULL DEFAULT '[]',
    retention           TEXT NOT NULL CHECK (retention IN ('keep', 'delete_after_import')),
    source_deleted_at   TEXT
);
INSERT INTO import_runs_new
    SELECT id, source_type, source_path, source_sha256, imported_at, status, summary, errors,
           retention, source_deleted_at
    FROM import_runs;
DROP TABLE import_runs;
ALTER TABLE import_runs_new RENAME TO import_runs;
