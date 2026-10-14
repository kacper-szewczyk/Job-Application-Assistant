import sqlite3

import pytest

from jaa.core.models import SourceStatus, StatusActor
from jaa.storage import sources


def test_pause_resume_history(conn: sqlite3.Connection) -> None:
    assert sources.latest_status_event(conn, "fake_board") is None
    # Resuming a source that was never paused records nothing.
    assert (
        sources.set_source_status(conn, "fake_board", SourceStatus.ACTIVE, StatusActor.USER) is None
    )

    paused = sources.set_source_status(
        conn, "fake_board", SourceStatus.PAUSED, StatusActor.SYSTEM, "HTTP 403"
    )
    assert paused is not None
    assert (
        sources.set_source_status(conn, "fake_board", SourceStatus.PAUSED, StatusActor.USER) is None
    )
    sources.set_source_status(conn, "fake_board", SourceStatus.ACTIVE, StatusActor.USER)
    sources.set_source_status(conn, "other_board", SourceStatus.PAUSED, StatusActor.USER)

    history = sources.status_history(conn, "fake_board")
    assert [(e.status, e.actor, e.reason) for e in history] == [
        (SourceStatus.PAUSED, StatusActor.SYSTEM, "HTTP 403"),
        (SourceStatus.ACTIVE, StatusActor.USER, None),
    ]
    latest = sources.latest_status_event(conn, "fake_board")
    assert latest is not None
    assert latest.status is SourceStatus.ACTIVE


def test_history_is_append_only(conn: sqlite3.Connection) -> None:
    sources.set_source_status(conn, "fake_board", SourceStatus.PAUSED, StatusActor.USER)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE source_status_events SET status = 'active'")
