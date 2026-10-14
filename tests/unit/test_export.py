import json
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jaa.core.errors import ExportFormatError, StorageError
from jaa.storage.db import connect, open_database
from jaa.storage.export import (
    EXPORT_TABLES,
    backup_database,
    export_data,
    export_json,
    import_data,
    import_json,
)
from tests.conftest import Sample


def _tables(data: dict[str, object]) -> object:
    return data["tables"]


def test_export_covers_every_table(conn: sqlite3.Connection, sample: Sample) -> None:
    data = export_data(conn)
    tables: dict[str, list[object]] = data["tables"]  # type: ignore[assignment]
    assert set(tables) == set(EXPORT_TABLES)
    assert all(tables[t] for t in EXPORT_TABLES), "sample should populate every table"
    user_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert user_tables - set(EXPORT_TABLES) == {"schema_migrations"}


def test_round_trip(conn: sqlite3.Connection, sample: Sample, tmp_path: Path) -> None:
    dest = export_json(conn, tmp_path / "export.json")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600

    restored = open_database(tmp_path / "restored.sqlite3")
    inserted = import_json(restored, dest)
    assert inserted == sum(len(rows) for rows in json.loads(dest.read_text())["tables"].values())
    assert _tables(export_data(restored)) == _tables(export_data(conn))
    restored.close()


def test_export_refuses_overwrite(conn: sqlite3.Connection, tmp_path: Path) -> None:
    dest = tmp_path / "export.json"
    export_json(conn, dest)
    with pytest.raises(StorageError, match="already exists"):
        export_json(conn, dest)
    export_json(conn, dest, overwrite=True)


def test_import_requires_empty_database(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(StorageError, match="empty database"):
        import_data(conn, export_data(conn))


# Exports are untyped JSON documents, so mutators work on Any.
type Mutator = Callable[[Any], None]

BAD_EXPORTS: list[tuple[Mutator, str]] = [
    (lambda d: d.update(format="other"), "not a jaa export"),
    (lambda d: d.update(format_version=99), "format_version"),
    (lambda d: d.update(schema_version=99), "schema_version"),
    (lambda d: d["tables"].update(evil=[]), "unknown tables"),
    (lambda d: d["tables"]["jobs"][0].update({"id) VALUES (1); --": 1}), "unknown columns"),
    (lambda d: d["tables"].update(jobs="nope"), "list of objects"),
]


@pytest.mark.parametrize(("mutate", "message"), BAD_EXPORTS)
def test_import_rejects_bad_exports(
    conn: sqlite3.Connection, sample: Sample, tmp_path: Path, mutate: Mutator, message: str
) -> None:
    data: Any = json.loads(json.dumps(export_data(conn)))
    mutate(data)
    target = open_database(tmp_path / "target.sqlite3")
    with pytest.raises(ExportFormatError, match=message):
        import_data(target, data)
    target.close()


def test_import_is_atomic(conn: sqlite3.Connection, sample: Sample, tmp_path: Path) -> None:
    data: Any = json.loads(json.dumps(export_data(conn)))
    data["tables"]["job_status_events"][0]["job_id"] = 999  # dangling foreign key
    target = open_database(tmp_path / "target.sqlite3")
    with pytest.raises(ExportFormatError):
        import_data(target, data)
    assert target.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0] == 0
    target.close()


def test_import_rejects_unreadable_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ExportFormatError):
        import_json(conn, bad)


def test_backup_is_complete_and_private(
    conn: sqlite3.Connection, sample: Sample, tmp_path: Path
) -> None:
    dest = backup_database(conn, tmp_path / "backup.sqlite3")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    copy = connect(dest)
    assert _tables(export_data(copy)) == _tables(export_data(conn))
    copy.close()
    with pytest.raises(StorageError, match="already exists"):
        backup_database(conn, dest)
