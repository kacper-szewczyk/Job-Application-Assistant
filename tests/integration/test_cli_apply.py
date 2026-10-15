import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.cli import apply as apply_cli
from jaa.core import models as m
from jaa.storage import applications as service
from jaa.storage.db import open_database
from jaa.storage.jobs import add_job
from tests.conftest import T0, ident

runner = CliRunner()
URL = "https://boards.example.test/jobs/1"
NOTE = "synthetic-note: applied after call"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "220"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


def _job(db: sqlite3.Connection, source_id: str = "w-1") -> int:
    stored = add_job(
        db,
        m.Job(
            source="example_board",
            source_job_id=source_id,
            canonical_url=URL,
            source_url=URL,
            employer="Example Co",
            title="Widget Engineer",
            first_seen_at=T0,
            last_seen_at=T0,
            status_changed_at=T0,
        ),
    )
    return ident(stored.id)


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    return result.output


def test_start_handoff_submit_and_track(
    env: dict[str, str], db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(db)
    opened: list[str] = []
    monkeypatch.setattr(apply_cli, "open_url", opened.append)

    out = _run(env, "apply", "start", str(job))
    assert "preparing" in out

    out = _run(env, "apply", "handoff", str(job), "--open")
    assert URL in out and opened == [URL]
    assert "No CV built" in out
    assert f"jaa apply submitted {job}" in out

    # Declining, or no answer at all, records nothing.
    out = _run(env, "apply", "submitted", str(job), input="n\n", code=1)
    assert "Nothing recorded" in out
    _run(env, "apply", "submitted", str(job), input="", code=1)
    assert service.get_application(db, job) is not None
    assert not service.is_submitted(service.get_application(db, job))

    out = _run(
        env,
        "apply",
        "submitted",
        str(job),
        "--at",
        "2026-10-14T10:30",
        "--reference",
        "REF-7",
        "--note",
        NOTE,
        input="y\n",
    )
    assert "Recorded" in out and "2026-10-14 10:30 UTC" in out
    _run(env, "apply", "submitted", str(job), input="y\n", code=1)

    _run(env, "apply", "status", str(job), "interviewing", "--note", "first round")
    out = _run(env, "apply", "status", str(job), "saved", code=1)
    assert "already submitted" in out

    out = _run(env, "apply", "show", str(job))
    for expected in ("interviewing", "REF-7", NOTE, "first round", "confirmed by user", URL):
        assert expected in out
    out = _run(env, "apply", "list", "--status", "interviewing")
    assert "Widget Engineer" in out
    assert "Application: submitted" in _run(env, "jobs", "show", str(job))


def test_duplicate_is_flagged_before_preparation(
    env: dict[str, str], db: sqlite3.Connection
) -> None:
    first, second = _job(db), _job(db, "w-2")
    _run(env, "apply", "start", str(first))
    result = runner.invoke(app, ["apply", "start", str(second)], env=env)
    assert result.exit_code == 1
    assert f"#{first}" in result.output and "--allow-duplicate" in result.output
    assert service.get_application(db, second) is None
    assert "Possible duplicate" in _run(env, "apply", "start", str(second), "--allow-duplicate")


def test_submitted_cannot_be_set_as_a_status(env: dict[str, str], db: sqlite3.Connection) -> None:
    job = _job(db)
    out = _run(env, "apply", "status", str(job), "submitted", code=1)
    assert "confirm" in out
    assert _run(env, "apply", "list").startswith("No applications yet")
