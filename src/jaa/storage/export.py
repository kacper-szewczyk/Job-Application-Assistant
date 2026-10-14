"""Backup (SQLite file copy) and JSON export/import. Format: docs/export-format.md."""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Final, cast

from jaa import __version__
from jaa.core.clock import utcnow
from jaa.core.errors import ExportFormatError, StorageError
from jaa.storage.db import DATABASE_FILE_MODE, connect, schema_version

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
    "answers",
    "answer_sources",
    "jobs",
    "job_status_events",
    "job_matches",
    "applications",
    "application_answers",
    "document_artifacts",
    "template_candidates",
    "source_status_events",
)

type Row = dict[str, object]


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
