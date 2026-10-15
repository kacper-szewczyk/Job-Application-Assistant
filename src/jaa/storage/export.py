"""Backup (SQLite file copy) and JSON export/import. Format: docs/export-format.md."""

import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from jaa import __version__
from jaa.core.clock import utcnow
from jaa.core.errors import (
    DatabaseNotEmptyError,
    ExportFormatError,
    RestoreError,
    StorageError,
)
from jaa.storage.db import DATABASE_FILE_MODE, connect, open_database, schema_version

log = logging.getLogger(__name__)

EXPORT_FORMAT: Final = "jaa-export"
EXPORT_FORMAT_VERSION: Final = 1

# Parent tables before children so a restore satisfies foreign keys.
EXPORT_TABLES: Final = (
    "profile_versions",
    "profile",
    "import_runs",
    "profile_facts",
    "profile_version_facts",
    "profile_fact_events",
    "preferences",
    "preference_events",
    "jobs",
    "job_questions",
    "answers",
    "answer_sources",
    "job_status_events",
    "job_duplicates",
    "job_duplicate_reviews",
    "job_matches",
    "cv_tailorings",
    "applications",
    "application_answers",
    "document_artifacts",
    "template_candidates",
    "source_status_events",
    "source_fetch_runs",
    "saved_searches",
)

type Row = dict[str, object]

SQLITE_HEADER: Final = b"SQLite format 3\x00"
# Files SQLite keeps next to a database; writing over one corrupts it.
_SIDECARS: Final = ("", "-wal", "-shm", "-journal")


def _database_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(str(row["file"])).resolve()
    return None


def _protected(database: Path) -> set[Path]:
    return {database.with_name(database.name + suffix) for suffix in _SIDECARS}


def _check_output(conn: sqlite3.Connection, dest: Path) -> Path:
    """Resolve where a backup or export goes, refusing the live database and directories."""
    dest = dest.expanduser().resolve()
    database = _database_file(conn)
    if database is not None and dest in _protected(database):
        raise StorageError(f"{dest} is jaa's own database; choose another file")
    if dest.is_dir():
        raise StorageError(f"{dest} is a directory; give a file name")
    if not dest.parent.is_dir():
        raise StorageError(f"folder {dest.parent} does not exist")
    return dest


def _write_private(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write a file readable only by the owner. Exports contain personal data."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, DATABASE_FILE_MODE)
    except FileExistsError as exc:
        raise StorageError(f"{path} already exists; pass overwrite to replace it") from exc
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    path.chmod(DATABASE_FILE_MODE)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")]


def export_data(conn: sqlite3.Connection) -> dict[str, object]:
    tables: dict[str, list[Row]] = {}
    for table in EXPORT_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608 - fixed table list
        tables[table] = [dict(r) for r in rows]
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "schema_version": schema_version(conn),
        "app_version": __version__,
        "exported_at": utcnow().isoformat(),
        "tables": tables,
    }


def export_json(conn: sqlite3.Connection, dest: Path, *, overwrite: bool = False) -> Path:
    dest = _check_output(conn, dest)
    data = export_data(conn)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode()
    _write_private(dest, payload, overwrite=overwrite)
    counts = {
        t: len(cast(list[Row], rows)) for t, rows in cast(dict[str, object], data["tables"]).items()
    }
    log.info("exported %d rows", sum(counts.values()))
    return dest


def _validate(data: object, conn: sqlite3.Connection) -> dict[str, list[Row]]:
    if not isinstance(data, dict):
        raise ExportFormatError("export must be a JSON object")
    doc = cast(dict[str, object], data)
    if doc.get("format") != EXPORT_FORMAT:
        raise ExportFormatError("not a jaa export file")
    if doc.get("format_version") != EXPORT_FORMAT_VERSION:
        raise ExportFormatError(f"unsupported export format_version {doc.get('format_version')!r}")
    if doc.get("schema_version") != schema_version(conn):
        raise ExportFormatError(
            f"export schema_version {doc.get('schema_version')!r} does not match database "
            f"schema_version {schema_version(conn)}"
        )
    tables = doc.get("tables")
    if not isinstance(tables, dict):
        raise ExportFormatError("export is missing 'tables'")
    tables = cast(dict[str, object], tables)
    unknown = set(tables) - set(EXPORT_TABLES)
    if unknown:
        raise ExportFormatError(f"export has unknown tables {sorted(unknown)}")
    result: dict[str, list[Row]] = {}
    for table in EXPORT_TABLES:
        rows = tables.get(table, [])
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) for r in cast(list[object], rows)
        ):
            raise ExportFormatError(f"table {table!r} must be a list of objects")
        allowed = set(_columns(conn, table))
        typed_rows = cast(list[Row], rows)
        for row in typed_rows:
            bad = set(row) - allowed
            if bad:
                raise ExportFormatError(f"table {table!r} has unknown columns {sorted(bad)}")
        result[table] = typed_rows
    return result


def import_data(conn: sqlite3.Connection, data: object) -> int:
    """Load an export into an empty, fully migrated database. Returns rows inserted."""
    tables = _validate(data, conn)
    for table in EXPORT_TABLES:
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():  # noqa: S608 - fixed table list
            raise StorageError(f"import requires an empty database; {table} has rows")
    inserted = 0
    try:
        with conn:
            for table in EXPORT_TABLES:
                for row in tables[table]:
                    cols = ", ".join(row)
                    params = ", ".join(f":{c}" for c in row)
                    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({params})", row)  # noqa: S608 - table fixed, columns validated against schema
                    inserted += 1
    except sqlite3.Error as exc:
        raise ExportFormatError(f"export could not be loaded: {exc}") from exc
    log.info("imported %d rows", inserted)
    return inserted


def import_json(conn: sqlite3.Connection, src: Path) -> int:
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportFormatError(f"cannot read export file: {exc}") from exc
    return import_data(conn, data)


def backup_database(conn: sqlite3.Connection, dest: Path, *, overwrite: bool = False) -> Path:
    """Consistent copy of the live database using SQLite's online backup API."""
    dest = _check_output(conn, dest)
    if dest.exists():
        if not overwrite:
            raise StorageError(f"{dest} already exists; pass overwrite to replace it")
        dest.unlink()
    target = connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    # WAL mode on the copy is fine, but a single self-contained file is easier to move around.
    with sqlite3.connect(dest) as plain:
        plain.execute("PRAGMA journal_mode = DELETE")
    plain.close()
    log.info("database backup written")
    return dest


class RestoreKind(StrEnum):
    BACKUP = "backup"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class RestoreResult:
    kind: RestoreKind
    rows: int
    # Copy of the database that was replaced, when it held any records.
    previous: Path | None


def has_records(conn: sqlite3.Connection) -> bool:
    return any(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # noqa: S608 - fixed table list
        for table in EXPORT_TABLES
    )


def _row_count(conn: sqlite3.Connection) -> int:
    return sum(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - fixed table list
        for table in EXPORT_TABLES
    )


def _source(src: Path, database: Path) -> tuple[Path, RestoreKind]:
    try:
        resolved = src.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RestoreError(f"file not found: {src}") from exc
    if not resolved.is_file():
        raise RestoreError(f"not a regular file: {resolved}")
    if resolved in _protected(database.resolve()):
        raise RestoreError(f"{resolved} is jaa's own database, not a backup")
    with resolved.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    return resolved, RestoreKind.BACKUP if header == SQLITE_HEADER else RestoreKind.EXPORT


def _load_backup(src: Path, staged: Path) -> sqlite3.Connection:
    """Copy a backup into `staged` without writing to it, then check and migrate the copy."""
    try:
        source = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RestoreError(f"cannot open backup: {exc}") from exc
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RestoreError("the backup is damaged (SQLite integrity check failed)")
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master")}
        if "schema_migrations" not in tables:
            raise RestoreError("not a jaa backup: it has no schema_migrations table")
        target = connect(staged)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise RestoreError(f"cannot read backup: {exc}") from exc
    finally:
        source.close()
    # Brings a backup from an older jaa up to date; refuses one from a newer jaa.
    conn = open_database(staged)
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        conn.close()
        raise RestoreError("the backup has records that point at missing records")
    return conn


def _settle(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log into the file and close, leaving one self-contained file."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()


_IN_USE: Final = (
    "the database is open in another jaa process (for example `jaa mcp serve`); "
    "stop it and try again"
)


def _release(conn: sqlite3.Connection) -> None:
    """Fold the log into the live database, failing if another connection still uses it."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    except sqlite3.OperationalError as exc:
        raise RestoreError(_IN_USE) from exc
    if str(mode).lower() != "delete":
        raise RestoreError(_IN_USE)


def _discard(path: Path) -> None:
    for candidate in _protected(path):
        candidate.unlink(missing_ok=True)


def restore(
    database: Path, src: Path, *, replace: bool = False, keep_dir: Path | None = None
) -> RestoreResult:
    """Replace the database with a backup (SQLite file) or export (JSON).

    The new database is built and checked next to the old one and swapped in only when complete,
    so a failed restore leaves the current data as it was. A database that holds records is
    replaced only with `replace`, and is first copied to `keep_dir` (default: a `backups`
    folder beside it), because jaa never deletes the user's data on its own.
    """
    resolved, kind = _source(src, database)
    staged = database.with_name(f".restore-{secrets.token_hex(4)}{database.suffix}")
    try:
        if kind is RestoreKind.BACKUP:
            conn = _load_backup(resolved, staged)
        else:
            conn = open_database(staged)
            try:
                import_json(conn, resolved)
            except BaseException:
                conn.close()
                raise
        rows = _row_count(conn)
        _settle(conn)

        previous: Path | None = None
        if database.exists():
            current = connect(database)
            try:
                filled = has_records(current)
                if filled and not replace:
                    raise DatabaseNotEmptyError(
                        "the database already has records; restoring replaces them "
                        "(a copy is kept first)"
                    )
                _release(current)
                if filled:
                    folder = keep_dir or database.parent / "backups"
                    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
                    previous = backup_database(current, folder / f"before-restore-{stamp}.sqlite3")
            finally:
                current.close()
        # The old database's log must not be replayed onto the restored file.
        for suffix in _SIDECARS[1:]:
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        staged.replace(database)
        database.chmod(DATABASE_FILE_MODE)
    except BaseException:
        _discard(staged)
        raise
    log.info("restored %s rows=%d", kind, rows)
    return RestoreResult(kind, rows, previous)
