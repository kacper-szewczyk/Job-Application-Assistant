"""SQLite connection handling and versioned SQL migrations."""

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jaa.core.clock import utcnow
from jaa.core.errors import MigrationError, StorageError

log = logging.getLogger(__name__)

DATABASE_FILE_MODE = 0o600
_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations() -> list[Migration]:
    """Read packaged migrations in version order; versions must be contiguous from 1."""
    migrations: list[Migration] = []
    for entry in files("jaa.storage.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            continue
        migrations.append(
            Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8"))
        )
    migrations.sort(key=lambda m: m.version)
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise MigrationError("migration versions must be contiguous starting at 0001")
    return migrations


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a database with foreign keys enforced. Creates the file owner-only if missing."""
    if str(path) != ":memory:":
        db_path = Path(path)
        if not db_path.exists():
            # Create with restrictive permissions before SQLite opens it with the umask.
            fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, DATABASE_FILE_MODE)
            os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    """Map of applied migration version to checksum."""
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def schema_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions, default=0)


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations, each in its own transaction. Returns versions applied."""
    migrations = load_migrations() if migrations is None else migrations
    known = {m.version: m for m in migrations}
    applied = applied_versions(conn)

    for version, checksum in applied.items():
        migration = known.get(version)
        if migration is None:
            raise MigrationError(
                f"database has migration {version:04d} which this version of jaa does not know; "
                "upgrade jaa or restore a backup"
            )
        if migration.checksum != checksum:
            raise MigrationError(f"applied migration {version:04d} was modified after it ran")

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        return []
    # SQLite's procedure for rebuilding a table (to change a CHECK, say): foreign keys off, so
    # dropping the old table does not fire ON DELETE actions, then a foreign key check before
    # the commit. The pragma cannot change inside a transaction, so it is set around them.
    enforced = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        return [_apply(conn, migration) for migration in pending]
    finally:
        if enforced:
            conn.execute("PRAGMA foreign_keys = ON")


def _apply(conn: sqlite3.Connection, migration: Migration) -> int:
    # executescript commits any open transaction and runs the script as written, so the
    # explicit BEGIN makes the migration and its bookkeeping row one transaction, committed
    # below once the foreign keys check out.
    script = (
        "BEGIN;\n"
        f"{migration.sql}\n;\n"
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
        f"VALUES ({migration.version}, '{migration.name}', '{migration.checksum}', "
        f"'{utcnow().isoformat()}');\n"
    )
    try:
        conn.executescript(script)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationError(
                f"migration {migration.version:04d} failed: it broke foreign key references"
            )
        conn.commit()
    except (sqlite3.Error, MigrationError) as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"migration {migration.version:04d} failed: {exc}") from exc
    log.info("applied migration %04d_%s", migration.version, migration.name)
    return migration.version


def open_database(path: Path) -> sqlite3.Connection:
    """Open the app database and bring its schema up to date."""
    try:
        conn = connect(path)
    except (OSError, sqlite3.Error) as exc:
        raise StorageError(f"cannot open database at {path}: {exc}") from exc
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn
