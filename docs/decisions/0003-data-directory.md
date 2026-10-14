# 0003 — Default data directory

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #1

## Context
SPEC §11 asked whether generated documents live in the app data directory or a user-selected folder.

## Decision
Store the database, imported sources and generated documents in the platform user data directory (via platformdirs, e.g. `~/Library/Application Support/jaa` on macOS). Overridable with the `JAA_DATA_DIR` environment variable or config. Never inside the repository.

## Consequences
- Predictable location for backup/export/delete (T19).
- Users who want documents elsewhere set the override; per-document export can be added later.
