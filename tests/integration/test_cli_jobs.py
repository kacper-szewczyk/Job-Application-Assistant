from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.cli import jobs as jobs_cli
from jaa.connectors import registry, solid_jobs
from jaa.connectors.base import Connector, JobListing
from jaa.connectors.http import HttpResponse, Transport
from jaa.core.models import JobStatus, StatusActor
from jaa.storage.db import open_database
from jaa.storage.jobs import set_job_status
from jaa.storage.listings import store_listings
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


def _store_elsewhere(env: dict[str, str], *listings: JobListing) -> None:
    """Listings from a second, synthetic source, stored straight into the CLI's database."""
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        store_listings(conn, "board_b", "all", listings, at=datetime(2026, 10, 14, tzinfo=UTC))
    finally:
        conn.close()


def test_list_filters(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, rec.first_fetch())
    _run(env, "jobs", "fetch", "solid_jobs", "IT")
    out = _run(env, "jobs", "list", "-q", "widget engineer")
    assert "Senior Widget Engineer" in out
    assert "Data Engineer" not in out
    out = _run(env, "jobs", "list", "--location", "krakow")
    assert "Kraków, Wrocław" in out
    assert "Senior Widget Engineer" not in out
    out = _run(env, "jobs", "list", "--status", "saved")
    assert "No jobs match status: saved." in out
    assert "No jobs match" in _run(env, "jobs", "list", "--source", "other")
    _run(env, "jobs", "list", "--status", "hired", code=2)


def test_saved_searches(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert "No saved searches" in _run(env, "jobs", "searches", "list")
    _serve(monkeypatch, rec.first_fetch())
    _run(env, "jobs", "fetch", "solid_jobs", "IT")
    out = _run(env, "jobs", "list", "-q", "engineer", "--save", "eng")
    assert "Saved search 'eng': \"engineer\"." in out
    _run(env, "jobs", "list", "-q", "qa", "--save", "eng", code=1)
    _run(env, "jobs", "list", "-q", "engineer", "--remote", code=2)

    listed = _run(env, "jobs", "searches", "list")
    assert "eng" in listed and "never" in listed

    out = _run(env, "jobs", "searches", "run", "eng")
    assert "2 job(s), 2 new since never" in out
    assert out.count(" new") >= 2

    _serve(monkeypatch, rec.later_fetch())
    _run(env, "jobs", "fetch", "solid_jobs", "IT")
    out = _run(env, "jobs", "searches", "run", "eng")
    # The data engineer listing is gone and offer D, a platform engineer, is new.
    assert "2 job(s), 1 new since 2026-" in out
    assert "Platform Engineer new" in out
    assert "UTC" in _run(env, "jobs", "searches", "list")

    _run(env, "jobs", "list", "-q", "qa", "--save", "eng", "--replace")
    assert "QA" in _run(env, "jobs", "searches", "run", "eng")
    assert "Deleted saved search 'eng'." in _run(env, "jobs", "searches", "delete", "eng")
    _run(env, "jobs", "searches", "run", "eng", code=1)


def test_show_and_duplicates(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, rec.first_fetch())
    _run(env, "jobs", "fetch", "solid_jobs", "IT")
    assert "No possible duplicates" in _run(env, "jobs", "duplicates", "list")
    _store_elsewhere(
        env,
        JobListing(
            source_job_id="b-1",
            url="https://board-b.example.test/1",
            title="Senior Widget Engineer (K/M)",
            company="EXAMPLE WIDGETS sp. z o.o.",
            location="warszawa, hybrid",
        ),
    )
    out = _run(env, "jobs", "list")
    assert "duplicate? #4" in out and "duplicate? #1" in out

    out = _run(env, "jobs", "duplicates", "list")
    assert "same employer, title and location" in out
    assert "#1 Senior Widget Engineer" in out and "#4 Senior Widget Engineer (K/M)" in out

    shown = _run(env, "jobs", "show", "1")
    assert "solid_jobs: https://solid.jobs/o/syntha01/jaa" in shown
    assert "Possible duplicates (unreviewed):" in shown
    assert "flag 1: #4" in shown
    assert "Budowa syntetycznych widgetów." in shown

    _run(env, "jobs", "duplicates", "mark", "1", "maybe", code=2)
    _run(env, "jobs", "duplicates", "mark", "9", "same", code=1)
    assert "as the same job" in _run(env, "jobs", "duplicates", "mark", "1", "same")
    shown = _run(env, "jobs", "show", "1")
    assert "board_b: https://board-b.example.test/1" in shown
    assert "Possible duplicates" not in shown
    assert "No possible duplicates" in _run(env, "jobs", "duplicates", "list")
    assert "same job" in _run(env, "jobs", "duplicates", "list", "--all")

    _run(env, "jobs", "duplicates", "mark", "1", "different")
    assert "duplicate?" not in _run(env, "jobs", "list")
    assert "board_b:" not in _run(env, "jobs", "show", "1")
    _run(env, "jobs", "show", "99", code=1)


def test_duplicate_of_a_job_in_progress_is_called_out(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, rec.first_fetch())
    _run(env, "jobs", "fetch", "solid_jobs", "IT")
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        set_job_status(conn, 1, JobStatus.SUBMITTED, StatusActor.USER)
    finally:
        conn.close()
    _store_elsewhere(
        env,
        JobListing(
            source_job_id="b-1",
            url="https://board-b.example.test/1",
            canonical_url="https://solid.jobs/o/syntha01",
            title="Widget person",
            company="Someone",
        ),
    )
    out = _run(env, "jobs", "duplicates", "list")
    assert "same link" in out
    assert "1 flag(s) involve a job you are already working on or applied to" in out
