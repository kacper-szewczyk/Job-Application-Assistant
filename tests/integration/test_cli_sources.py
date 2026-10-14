import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.connectors import registry
from jaa.connectors.base import Connector, SourceState, effective_state
from jaa.connectors.run import run_connector
from jaa.core.errors import SourceAccessDeniedError, SourcePausedError
from jaa.core.models import SourceStatus, StatusActor
from jaa.storage import sources
from jaa.storage.db import open_database
from tests.fixtures.fake_connector import (
    FakeBoard,
    FakeClock,
    ReplayTransport,
    board_url,
    json_response,
    make_policy,
    recorded_board,
)

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch) -> dict[str, Connector]:
    available: dict[str, Connector] = {
        "fake_board": FakeBoard(),
        "unreviewed_board": FakeBoard(
            policy=make_policy(key="unreviewed_board", name="Unreviewed", terms_reviewed_on=None)
        ),
    }
    monkeypatch.setattr(registry, "available_connectors", lambda: available)
    return available


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


def _run(env: dict[str, str], *args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env)
    assert result.exit_code == code, result.output
    return result.output


def test_list_without_connectors(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "available_connectors", dict[str, Connector])
    assert "No job sources are available yet." in _run(env, "sources", "list")


def test_shipped_sources_are_listed(env: dict[str, str]) -> None:
    out = _run(env, "sources", "list")
    assert "solid_jobs" in out
    assert "SOLID.Jobs" in out
    shown = _run(env, "sources", "show", "solid_jobs")
    assert "https://solid.jobs/api-ofert-pracy" in shown
    assert "2026-10-14" in shown


@pytest.mark.usefixtures("boards")
def test_list_and_show(env: dict[str, str]) -> None:
    out = _run(env, "sources", "list")
    assert "fake_board" in out
    assert "active" in out
    assert "paused by policy: source terms have not been reviewed" in out

    out = _run(env, "sources", "show", "fake_board")
    assert "boards.example.test" in out
    assert "2 per 1s, at most 10 per run" in out
    assert "https://boards.example.test/terms" in out


@pytest.mark.usefixtures("boards")
def test_pause_and_resume(env: dict[str, str], db: sqlite3.Connection) -> None:
    assert "fake_board paused." in _run(
        env, "sources", "pause", "fake_board", "--reason", "terms changed"
    )
    assert "already paused" in _run(env, "sources", "pause", "fake_board")
    assert "paused: terms changed" in _run(env, "sources", "list")
    assert "paused by user: terms changed" in _run(env, "sources", "show", "fake_board")

    assert "fake_board resumed." in _run(env, "sources", "resume", "fake_board")
    assert "was not paused" in _run(env, "sources", "resume", "fake_board")
    history = sources.status_history(db, "fake_board")
    assert [e.status for e in history] == [SourceStatus.PAUSED, SourceStatus.ACTIVE]


@pytest.mark.usefixtures("boards")
def test_policy_pause_cannot_be_resumed(env: dict[str, str]) -> None:
    out = _run(env, "sources", "resume", "unreviewed_board", code=1)
    assert "paused by its source policy" in out


@pytest.mark.usefixtures("boards")
def test_unknown_source(env: dict[str, str]) -> None:
    assert "unknown source" in _run(env, "sources", "pause", "nope", code=1)


def test_access_denied_pauses_until_user_resumes(
    boards: dict[str, Connector], db: sqlite3.Connection
) -> None:
    """The flow T10 wires into `jaa jobs fetch`: a denial pauses the source; no more requests."""
    board = boards["fake_board"]
    url = board_url("example")
    denied = ReplayTransport({url: [json_response(url, b"{}", status=403)]})
    clock = FakeClock()

    def state() -> SourceState:
        return effective_state(board.policy, sources.latest_status_event(db, "fake_board"))

    with pytest.raises(SourceAccessDeniedError) as exc_info:
        run_connector(board, "example", state=state(), transport=denied, sleep=clock.sleep)
    sources.set_source_status(
        db, "fake_board", SourceStatus.PAUSED, StatusActor.SYSTEM, str(exc_info.value)
    )

    with pytest.raises(SourcePausedError, match="HTTP 403"):
        run_connector(board, "example", state=state(), transport=denied, sleep=clock.sleep)
    assert len(denied.sent) == 1

    sources.set_source_status(db, "fake_board", SourceStatus.ACTIVE, StatusActor.USER)
    outcome = run_connector(
        board, "example", state=state(), transport=recorded_board(), sleep=clock.sleep
    )
    assert len(outcome.listings) == 2
