"""`jaa restore`, the delete commands and archived jobs, end to end (T19, ADR 0021)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.config import load_settings
from jaa.core import models as m
from jaa.storage.db import open_database
from jaa.storage.jobs import add_job
from tests.conftest import SYNTHETIC_ANSWER, SYNTHETIC_EMAIL, SYNTHETIC_TITLE, T0, Sample, populate
from tests.integration.test_mcp import call

runner = CliRunner()
VALUES = (SYNTHETIC_EMAIL, SYNTHETIC_TITLE, SYNTHETIC_ANSWER)


def _env(data_dir: Path) -> dict[str, str]:
    return {"JAA_DATA_DIR": str(data_dir), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "220"}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = _env(tmp_path / "data")
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def sample(db: sqlite3.Connection) -> Sample:
    return populate(db)


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    # Debug logging is on: none of these commands may log or print a personal value.
    for value in VALUES:
        assert value not in result.output
    return result.output


def _count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - test


def _closed_job(db: sqlite3.Connection) -> int:
    job = add_job(
        db,
        m.Job(
            source="example_board",
            source_job_id="closed-1",
            canonical_url="https://boards.example.test/jobs/closed-1",
            employer="Closed Co",
            title="Archived Engineer",
            first_seen_at=T0,
            last_seen_at=T0,
            status=m.JobStatus.CLOSED,
            status_changed_at=T0,
        ),
    )
    db.commit()
    assert job.id is not None
    return job.id


def test_finished_jobs_are_listed_as_archived(env: dict[str, str], db: sqlite3.Connection) -> None:
    job_id = _closed_job(db)
    assert "closed (archived)" in _run(env, "jobs", "list")
    settings = load_settings(env)
    found = call(settings, "search_jobs", {"text": "archived"}).data["jobs"]
    assert [(j["id"], j["archived"]) for j in found] == [(job_id, True)]


def test_profile_delete_asks_first(
    env: dict[str, str], db: sqlite3.Connection, sample: Sample
) -> None:
    fact = str(sample.email.id)
    assert "Nothing deleted" in _run(env, "profile", "delete", fact, input="n\n", code=1)
    assert _count(db, "profile_facts") == 2
    out = _run(env, "profile", "delete", fact, input="y\n")
    assert "Deleted 1 fact(s)" in out and "Profile version" in out
    assert _count(db, "profile_facts") == 1


def test_import_delete_and_delete_source(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    cv = tmp_path / "cv.txt"
    cv.write_text(f"{SYNTHETIC_TITLE}\n\nSkills\nPython\n")
    _run(env, "import", "text", str(cv))
    assert _count(db, "profile_facts") > 0

    _run(env, "import", "delete-source", "1", input="n\n", code=1)
    assert cv.exists()
    assert "Deleted" in _run(env, "import", "delete-source", "1", "--yes")
    assert not cv.exists()
    assert "Source deleted" in _run(env, "import", "show", "1")
    assert "already deleted" in _run(env, "import", "delete-source", "1", "--yes", code=1)

    assert "Deleted import #1" in _run(env, "import", "delete", "1", "--yes")
    assert _count(db, "profile_facts") == 0
    assert "not found" in _run(env, "import", "delete", "1", "--yes", code=1).lower()


def test_jobs_delete(env: dict[str, str], db: sqlite3.Connection, sample: Sample) -> None:
    job = str(sample.job.id)
    _run(env, "jobs", "delete", job, input="n\n", code=1)
    assert _count(db, "applications") == 1
    out = _run(env, "jobs", "delete", job, "--yes")
    assert "Deleted 1 job(s)" in out
    # The sample's document path is outside the data directory, so the file is left alone.
    assert "Kept /synthetic/out/cv.pdf" not in out
    assert _count(db, "applications") == 0
    assert _count(db, "answers") == 1


def test_restore_from_backup_and_export(
    env: dict[str, str], db: sqlite3.Connection, sample: Sample, tmp_path: Path
) -> None:
    backup = tmp_path / "backup.sqlite3"
    export = tmp_path / "export.json"
    _run(env, "backup", str(backup))
    _run(env, "export", str(export))

    for source in (backup, export):
        fresh = _env(tmp_path / f"fresh-{source.suffix[1:]}")
        out = _run(fresh, "restore", str(source))
        assert "Restored" in out
        assert "1" in _run(fresh, "apply", "list")

    out = _run(env, "restore", str(backup), code=1)
    assert "already has records" in out and "--force" in out
    out = _run(env, "restore", str(backup), "--force", input="n\n", code=1)
    assert "Nothing restored" in out
    # Another connection, like a running `jaa mcp serve`, keeps the database in use.
    assert "another jaa process" in _run(env, "restore", str(backup), "--force", "--yes", code=1)
    db.close()
    out = _run(env, "restore", str(backup), "--force", "--yes")
    assert "previous database was saved" in out
    assert len(list((Path(env["JAA_DATA_DIR"]) / "backups").iterdir())) == 1


def test_backup_refuses_the_live_database(env: dict[str, str]) -> None:
    database = Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3"
    out = _run(env, "backup", str(database), "--force", code=1)
    assert "own database" in out
    assert "schema version" in _run(env, "info")
