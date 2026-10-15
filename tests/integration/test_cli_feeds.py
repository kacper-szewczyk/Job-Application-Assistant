"""`jaa jobs fetch` against the recorded remote job feeds (#74, ADR 0028)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.cli import jobs as jobs_cli
from tests.fixtures import feeds
from tests.fixtures.fake_connector import ReplayTransport

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


def test_remotive_daily_allowance_stops_further_fetches(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = feeds.remotive_feed()
    monkeypatch.setattr(jobs_cli, "make_transport", lambda: transport)
    for _ in range(4):
        result = runner.invoke(app, ["jobs", "fetch", "remotive", "all"], env=env)
        assert result.exit_code == 0, result.output
    assert len(transport.sent) == 4

    result = runner.invoke(app, ["jobs", "fetch", "remotive", "all", "software-dev"], env=env)
    assert result.exit_code == 1
    assert "allows 4 requests a day" in result.output
    assert len(transport.sent) == 4

    shown = runner.invoke(app, ["jobs", "show", "1"], env=env).output
    assert "remotive" in shown and "remotive.com/remote-jobs/" in shown


def test_we_work_remotely_is_paused_by_policy(env: dict[str, str]) -> None:
    sources = runner.invoke(app, ["sources", "show", "weworkremotely"], env=env).output
    assert "terms not confirmed" in sources
    result = runner.invoke(app, ["jobs", "fetch", "weworkremotely", "all"], env=env)
    assert result.exit_code != 0
    assert "paused" in result.output


def test_arbeitnow_fetch_reports_new_jobs(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport: ReplayTransport = feeds.arbeitnow_feed()
    monkeypatch.setattr(jobs_cli, "make_transport", lambda: transport)
    result = runner.invoke(app, ["jobs", "fetch", "arbeitnow", "remote"], env=env)
    assert result.exit_code == 0, result.output
    listed = runner.invoke(app, ["jobs", "list"], env=env).output
    assert "Platform Engineer" in listed and "Werkstudent" not in listed
