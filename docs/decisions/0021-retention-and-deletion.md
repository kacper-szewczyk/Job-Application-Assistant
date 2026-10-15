# 0021 — Retention, deletion and restore

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #20 (decision recorded in #1)

## Context
SPEC §13 asks for export, backup and delete controls, retention defaults, hardened path and archive handling, and verified log scrubbing (T19). Backup and JSON export existed since T01, and answers could already be forgotten (ADR 0017), but nothing restored a backup and the user could not delete facts, imports, jobs or applications. The retention decision in #1 was open: should finished applications or old answer versions expire?

Separately, `jaa backup --force` and `jaa export --force` accepted the live database as their destination and would have destroyed it.

## Decision
- **Nothing is deleted automatically.** Rejected, withdrawn and closed jobs stay with their full history, marked *archived* (`jaa.core.applications.ARCHIVED`): the CLI lists show `(archived)` and the MCP job and application outputs carry `archived: true`. Every saved answer version is kept until the user runs `jaa answers forget`. There are no expiry jobs or background clean-ups. This was the user's decision in #1.
- **Deletion is explicit, asks first, and is final.** New commands delete what the user names, each in one transaction, after a yes/no prompt (`--yes` skips it):
  - `jaa profile delete FACT…`: facts, their review events and their rows in every profile version. Deleting a confirmed fact snapshots a new profile version so matches and tailored CVs see the change. Corrections the user typed are kept. `jaa profile reject` stays the way to mark a fact wrong while keeping a record.
  - `jaa import delete RUN`: the import run, every fact it added and any template registered from it.
  - `jaa import delete-source RUN`: the user's own file(s) the import read, only if their combined SHA-256 still matches the import. All files are checked before any is deleted, and a path that is now a symlink is refused. The run records `source_deleted_at`.
  - `jaa jobs delete JOB…`: the job with its application, status history, matches, tailored CVs, questions, duplicate flags and document records. Generated files are deleted only when they are inside the data directory and unchanged since jaa wrote them. Other files are reported as kept. Saved answers stay because they are reusable across jobs.
- **Deleting really deletes.** Deletion removes rows, including from append-only history. The append-only triggers block only `UPDATE`, so they keep history honest without making the user's data impossible to remove.
- **Restore is safe by construction.** `jaa restore FILE` accepts a backup (detected by the SQLite header) or an export. It builds the new database in a temporary file beside the live one: it copies a backup read-only, integrity-checks it, migrates it forward and checks its foreign keys, or it loads an export into a fresh database. Only then does an atomic rename swap it in. If the live database has records, restore requires `--force` and first copies them to `backups/before-restore-<timestamp>.sqlite3`. A database another process still holds open is not replaced.
- **Output paths are checked.** Backup and export resolve their destination and refuse the live database, its `-wal`/`-shm`/`-journal` files, directories and missing folders.
- **The user runs these, not Claude.** The delete commands and `jaa restore` are user-only in the skill and denied in `.claude/settings.json` (ADR 0020). No MCP tool deletes or restores.

## Consequences
- The database grows with every archived job and answer version. That is the user's choice; `jaa jobs delete` and `jaa answers forget` are the way to shrink it.
- A deleted job that its source still lists comes back as a new `discovered` job on the next fetch.
- Deleting confirmed facts rewrites the membership of earlier profile versions. Records derived from a fact are not rewritten: match evidence keeps its fact ids and matched terms, tailored CV plans keep their rewritten text, and generated PDFs keep what they printed. To remove those too, the user deletes the jobs they belong to. General (not job-specific) CV files have no delete command yet; the user deletes them from the `documents` folder.
- Archive and path hardening (zip-slip, symlinks, encrypted members, member count, size and compression limits, non-regular files) and log scrubbing are covered by tests. Tests run the delete and restore paths with debug logging and assert that no synthetic personal value appears in the output.
