import re
import sqlite3
import stat
from enum import StrEnum
from pathlib import Path

import pytest

from jaa.core import models as m
from jaa.core.errors import MigrationError
from jaa.storage.db import Migration, connect, load_migrations, migrate, schema_version

EXPECTED_TABLES = {
    "schema_migrations",
    "profile",
    "import_runs",
    "profile_facts",
    "profile_versions",
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
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_migrate_empty_database(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    applied = migrate(conn)
    assert applied == [m.version for m in load_migrations()]
    assert _tables(conn) == EXPECTED_TABLES
    assert schema_version(conn) == len(applied)
    assert migrate(conn) == []


def test_database_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    connect(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO answer_sources VALUES (999, 999)")


def test_modified_migration_detected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    migrate(conn)
    edited = [Migration(mg.version, mg.name, mg.sql + "\n-- edited") for mg in load_migrations()]
    with pytest.raises(MigrationError, match="modified"):
        migrate(conn, edited)


def test_unknown_applied_migration_detected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    migrate(conn)
    with pytest.raises(MigrationError, match="does not know"):
        migrate(conn, [])


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    bad = Migration(1, "bad", "CREATE TABLE half_done (id INTEGER);\nTHIS IS NOT SQL;")
    with pytest.raises(MigrationError):
        migrate(conn, [bad])
    assert "half_done" not in _tables(conn)
    assert schema_version(conn) == 0


ENUM_COLUMNS: list[tuple[str, str, type[StrEnum]]] = [
    ("import_runs", "source_type", m.ImportSourceType),
    ("import_runs", "status", m.ImportStatus),
    ("import_runs", "retention", m.RetentionChoice),
    ("profile_facts", "state", m.FactState),
    ("profile_facts", "origin", m.FactOrigin),
    ("profile_fact_events", "from_state", m.FactState),
    ("profile_fact_events", "to_state", m.FactState),
    ("preferences", "status", m.PreferenceStatus),
    ("preference_events", "status", m.PreferenceStatus),
    ("answers", "reuse_policy", m.ReusePolicy),
    ("jobs", "status", m.JobStatus),
    ("job_status_events", "from_status", m.JobStatus),
    ("job_status_events", "to_status", m.JobStatus),
    ("job_status_events", "actor", m.StatusActor),
    ("applications", "submission_confirmed_by", m.SubmissionConfirmation),
    ("document_artifacts", "kind", m.ArtifactKind),
]


@pytest.mark.parametrize(("table", "column", "enum"), ENUM_COLUMNS)
def test_check_constraints_match_enums(
    conn: sqlite3.Connection, table: str, column: str, enum: type[StrEnum]
) -> None:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    match = re.search(rf"\b{column}\b[^,]*?\bIN \(([^)]*)\)", sql, re.DOTALL)
    assert match, f"no CHECK ... IN (...) for {table}.{column}"
    allowed = set(re.findall(r"'([^']*)'", match.group(1)))
    assert allowed == {e.value for e in enum}


def test_job_statuses_match_spec() -> None:
    assert [s.value for s in m.JobStatus] == [
        "discovered",
        "saved",
        "preparing",
        "ready_for_review",
        "submitted",
        "interviewing",
        "rejected",
        "withdrawn",
        "closed",
    ]
