from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.cli import jobs as jobs_cli
from jaa.connectors import registry, solid_jobs
from jaa.connectors.base import Connector
from jaa.connectors.http import HttpResponse, Transport
from tests.fixtures import solid_jobs as rec

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    # The recorded pages use a page size of 2; everything else is the shipped connector.
    connectors: dict[str, Connector] = {"solid_jobs": rec.connector()}
    monkeypatch.setattr(registry, "available_connectors", lambda: connectors)
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


def _serve(monkeypatch: pytest.MonkeyPatch, transport: Transport) -> None:
    monkeypatch.setattr(jobs_cli, "make_transport", lambda: transport)


def _run(env: dict[str, str], *args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env)
    assert result.exit_code == code, result.output
    return result.output


def test_fetch_list_and_refetch(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, rec.first_fetch())
    out = _run(env, "jobs", "fetch", "solid_jobs", "it")
    assert "solid_jobs IT: 3 listing(s); 3 new, 0 changed" in out
    assert "(2 request(s))" in out
    assert "2 issue(s): duplicate_listing x1, unreadable_listing x1" in out

    listed = _run(env, "jobs", "list")
    for text in (
        "Senior Widget Engineer",
        "Example Widgets Sp. z o.o.",
        "Kraków, Wrocław",
        "Remote",
    ):
        assert text in listed

    _serve(monkeypatch, rec.later_fetch())
    out = _run(env, "jobs", "fetch", "solid_jobs", "IT")
    assert "1 new, 1 changed, 1 unchanged, 0 back again, 1 removed" in out
    assert "Data Engineer" not in _run(env, "jobs", "list")
    assert "discovered (removed)" in _run(env, "jobs", "list", "--include-removed")
    assert "Recent fetches:" in _run(env, "sources", "show", "solid_jobs")


def test_unknown_source_and_division(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, rec.first_fetch())
    assert "unknown source 'nope'" in _run(env, "jobs", "fetch", "nope", "IT", code=1)
    out = _run(env, "jobs", "fetch", "solid_jobs", "Astrology", code=1)
    assert "unknown division 'Astrology'" in out
    assert ", ".join(solid_jobs.DIVISIONS) in out


def test_denied_source_is_paused(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, rec.answering(HttpResponse(status=403, url=rec.url(0))))
    out = _run(env, "jobs", "fetch", "solid_jobs", "IT", "HR", code=1)
    assert "refused access (HTTP 403)" in out
    assert "solid_jobs is now paused" in out
    assert "jaa sources resume solid_jobs" in out
    assert "solid_jobs HR" not in out  # stopped after the pause

    again = _run(env, "jobs", "fetch", "solid_jobs", "IT", code=1)
    assert "solid_jobs is paused" in again
    assert "paused: solid_jobs: solid.jobs refused access" in _run(env, "sources", "list")


def test_list_when_empty(env: dict[str, str]) -> None:
    assert "No jobs stored yet." in _run(env, "jobs", "list")
