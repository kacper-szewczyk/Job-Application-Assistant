-- LaTeX CVs kept as template candidates for CV generation (T06, ADR 0009).

-- files is JSON text {relative path: LaTeX source}; it contains the CV's own content.
-- A candidate is a snapshot, so only import_run_id may change (set to NULL when T19 deletes
-- the import run).
CREATE TABLE template_candidates (
    id              INTEGER PRIMARY KEY,
    import_run_id   INTEGER REFERENCES import_runs (id) ON DELETE SET NULL,
    main_file       TEXT NOT NULL,
    files           TEXT NOT NULL,
    document_class  TEXT,
    packages        TEXT NOT NULL DEFAULT '[]',
    content_sha256  TEXT NOT NULL UNIQUE,
    registered_at   TEXT NOT NULL
);

CREATE TRIGGER template_candidates_immutable
BEFORE UPDATE OF main_file, files, document_class, packages, content_sha256, registered_at
ON template_candidates
BEGIN
    SELECT RAISE(ABORT, 'template candidates are immutable');
END;
