# Export format (`jaa-export`, format version 1)

`jaa export <file>` writes every record in the local database as one JSON document. It is meant for moving data between machines, inspecting what is stored, and as a portable backup. `jaa backup <file>` instead writes a byte-for-byte consistent SQLite copy.

Both files contain your personal data. They are created with owner-only permissions (`0600`) and are never overwritten unless you pass `--force`.

## Structure

```json
{
  "format": "jaa-export",
  "format_version": 1,
  "schema_version": 5,
  "app_version": "0.1.0",
  "exported_at": "2026-10-14T09:00:00+00:00",
  "tables": {
    "profile_versions": [ { "id": 1, "created_at": "...", "note": "initial" } ],
    "profile": [ ... ],
    "...": [ ... ]
  }
}
```

- `format` / `format_version`: identify this document type. The version changes only when the envelope changes.
- `schema_version`: the database migration version the rows conform to. An export can only be loaded into a database at the same schema version.
- `tables`: one array per table, in foreign-key order (parents first). Rows are objects keyed by column name, with values exactly as stored:
  - timestamps: ISO-8601 text with UTC offset
  - enums: their string value (see `jaa.core.models`)
  - booleans: `0` / `1`
  - JSON columns (`summary`, `errors`, `value_normalized`, `value`, `requirements`, `evidence`, `gaps`, `uncertainty`, `reasons`, `plan`, `checks`, `approvals`, `manifest`, `files`, `packages`, `issues`, `query`): JSON **text**, not nested JSON

Tables, in order: `profile_versions`, `profile`, `import_runs`, `profile_facts`, `profile_version_facts`, `profile_fact_events`, `preferences`, `preference_events`, `jobs`, `job_questions`, `answers`, `answer_sources`, `job_status_events`, `job_duplicates`, `job_duplicate_reviews`, `job_matches`, `cv_tailorings`, `applications`, `application_answers`, `document_artifacts`, `template_candidates`, `source_status_events`, `source_fetch_runs`, `saved_searches`. `schema_migrations` is not exported; the target database is migrated before import.

Rows with `is_sensitive = 1` (profile facts, preferences and their history, answers) hold contact, eligibility, compensation or other sensitive data. `template_candidates.files` holds the full LaTeX source of imported CVs, including contact details. `job_matches.evidence` quotes the words of profile facts that matched a job's requirements. `cv_tailorings.plan` holds reworded CV text and cover letter paragraphs. `answers.value` holds your answers to application questions (all versions), and `job_questions.label` the questions you copied from application forms. `applications.handoff` lists the question keys (which quote questions), how each was answered and the ids of the answers, facts and documents used for a submission; `job_status_events.note` holds your notes.

## Loading an export

`jaa.storage.export.import_json` loads an export into an empty, fully migrated database in a single transaction. If anything fails (unknown table or column, schema mismatch, broken foreign key), nothing is written.

`jaa restore FILE` loads an export, or a SQLite backup written by `jaa backup`, through `jaa.storage.export.restore`:

- The file type is detected from its first bytes (`SQLite format 3`) rather than its name.
- The restored database is built in a temporary file next to the live one: an export is loaded into a freshly migrated database; a backup is copied read-only, integrity-checked, migrated forward if it comes from an older jaa, and checked for broken foreign keys. A backup from a newer jaa, or one whose applied migrations were changed, is refused.
- Only a complete, checked database replaces the live one, with an atomic rename. A failed restore leaves the live database untouched.
- A live database that has records is replaced only with `--force`, and is first copied to `backups/before-restore-<UTC timestamp>.sqlite3` in the data directory. Restore stops if another process (such as `jaa mcp serve`) still has the database open.

## Compatibility

When a migration changes the schema, older exports need to be loaded into a database at their schema version and migrated forward. A converter may be added if this becomes a common need.
