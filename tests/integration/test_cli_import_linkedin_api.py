import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app, imports
from jaa.connectors.http import HttpResponse
from jaa.core import models as m
from jaa.storage.db import open_database
from jaa.storage.repository import import_runs, profile_facts
from tests.fixtures.linkedin_api import TOKEN, FakeSnapshotApi

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSnapshotApi:
    fake = FakeSnapshotApi()
    monkeypatch.setattr(imports, "_transport", lambda: fake)
    monkeypatch.setattr(imports, "_is_terminal", lambda: True)
    return fake


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


def _import(env: dict[str, str], answers: str = "y\n", *args: str) -> str:
    result = runner.invoke(
        app, ["import", "linkedin-api", *args], env=env, input=f"{TOKEN}\n{answers}"
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_how_explains_getting_a_token_without_asking_for_one(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["import", "linkedin-api", "--how"], env=env)
    assert result.exit_code == 0
    assert "OAuth Token Generator" in result.output
    assert "never stores or logs it" in result.output


def test_refuses_without_a_terminal(env: dict[str, str]) -> None:
    # CliRunner's stdin is not a terminal: a token can never be piped in.
    result = runner.invoke(app, ["import", "linkedin-api"], env=env, input=f"{TOKEN}\n")
    assert result.exit_code == 2
    assert "hidden prompt" in result.output


def test_import_stores_unreviewed_facts_and_adds_saved_jobs(
    env: dict[str, str], fake: FakeSnapshotApi, db: sqlite3.Connection
) -> None:
    output = _import(env)
    assert "linkedin_api (LinkedIn API)" in output
    assert "new facts to review" in output
    assert "LinkedIn had no data for: phone numbers, education, certifications" in output
    assert "Added 1 job (#1); 1 without a link skipped." in output
    assert "Data Engineer — Fictional Freight (9/20/26, 4:00 PM) — may be job #1" in output
    assert "jaa did not record these" in output
    assert "Job titles: Backend Engineer" in output
    assert "These were not saved" in output

    facts = profile_facts.list(db)
    assert facts and all(f.state is m.FactState.UNREVIEWED for f in facts)
    (run,) = import_runs.list(db)
    assert run.source_type is m.ImportSourceType.LINKEDIN_API
    job = db.execute("SELECT source, source_url, description, status FROM jobs").fetchone()
    assert tuple(job) == (
        "manual",
        "https://www.linkedin.com/jobs/view/1000000001",
        None,
        "discovered",
    )
    assert db.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM preferences").fetchone()[0] == 0


def test_reimport_skips_known_values_and_saved_jobs(
    env: dict[str, str], fake: FakeSnapshotApi
) -> None:
    _import(env)
    output = _import(env)
    assert "This source was imported before (#1)" in output
    assert "0 new facts to review" in output
    assert "Added 0 jobs; 1 already in jaa; 1 without a link skipped." in output


def test_saved_jobs_can_be_left_out(
    env: dict[str, str], fake: FakeSnapshotApi, db: sqlite3.Connection
) -> None:
    _import(env, "", "--no-saved-jobs")
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_expired_token_imports_nothing(
    env: dict[str, str], fake: FakeSnapshotApi, db: sqlite3.Connection
) -> None:
    fake.overrides[("PROFILE", 0)] = HttpResponse(
        status=401, url="https://api.linkedin.com/rest/memberSnapshotData"
    )
    result = runner.invoke(app, ["import", "linkedin-api"], env=env, input=f"{TOKEN}\n")
    assert result.exit_code == 2
    assert "expired" in result.output
    assert TOKEN not in result.output
    assert import_runs.list(db) == []


def test_token_never_reaches_output_logs_database_or_export(
    env: dict[str, str], fake: FakeSnapshotApi, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    result = runner.invoke(app, ["import", "linkedin-api"], env=env, input=f"{TOKEN}\ny\n")
    assert result.exit_code == 0, result.output
    assert fake.sent  # it was used
    assert TOKEN not in result.output
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err
    export = tmp_path / "export.json"
    assert runner.invoke(app, ["export", str(export)], env=env).exit_code == 0
    assert TOKEN not in export.read_text(encoding="utf-8")
    for path in Path(env["JAA_DATA_DIR"]).rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
