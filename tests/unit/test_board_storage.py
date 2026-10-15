"""Following boards and storing single offers fetched by link (#71, ADR 0025)."""

import logging
import sqlite3

import pytest

from jaa.connectors.base import OfferConnector
from jaa.core.errors import SourcePausedError
from jaa.core.models import FetchRunOutcome, SourceStatus, StatusActor
from jaa.storage import follows, listings
from jaa.storage.repository import jobs, source_follows
from jaa.storage.sources import set_source_status
from tests.fixtures import boards
from tests.fixtures.fake_connector import FakeClock


def _greenhouse() -> OfferConnector:
    connector = boards.connectors()["greenhouse"]
    assert isinstance(connector, OfferConnector)
    return connector


def _one(conn: sqlite3.Connection, job_id: str = boards.GREENHOUSE_JOB) -> listings.FetchReport:
    clock = FakeClock()
    return listings.fetch_and_store_job(
        conn,
        _greenhouse(),
        "ExampleWidgets",
        job_id,
        transport=boards.greenhouse(),
        clock=clock,
        sleep=clock.sleep,
    )


def test_follow_and_unfollow_are_recorded_once(conn: sqlite3.Connection) -> None:
    assert follows.follow(conn, "lever", "acme")
    assert not follows.follow(conn, "lever", "acme")
    assert follows.follow(conn, "greenhouse", "eu:acme")
    assert [(f.source, f.target) for f in follows.followed(conn)] == [
        ("greenhouse", "eu:acme"),
        ("lever", "acme"),
    ]
    assert follows.unfollow(conn, "lever", "acme")
    assert not follows.unfollow(conn, "lever", "acme")
    assert not follows.unfollow(conn, "lever", "never")
    assert [f.target for f in follows.followed(conn)] == ["eu:acme"]
    assert len(source_follows.list(conn)) == 3


def test_follow_history_is_append_only(conn: sqlite3.Connection) -> None:
    follows.follow(conn, "lever", "acme")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE source_follows SET action = 'unfollow'")


def test_one_offer_is_stored_without_judging_the_rest(conn: sqlite3.Connection) -> None:
    whole = listings.fetch_and_store(
        conn, boards.connectors()["greenhouse"], "examplewidgets", transport=boards.greenhouse()
    )
    assert len(whole.changes.new) == 2

    report = _one(conn)
    assert report.error is None
    assert report.run.target == "examplewidgets"
    assert report.run.outcome is FetchRunOutcome.OK
    assert report.changes.removed == ()
    assert len(report.changes.changed) == 1  # now with its questions and first publication date
    stored = [j for j in jobs.list(conn, source="greenhouse") if j.removed_at is None]
    assert len(stored) == 2
    job = next(j for j in stored if j.source_job_id == boards.GREENHOUSE_JOB)
    assert job.source_target == "examplewidgets"
    assert job.requirements["questions"]


def test_an_offer_that_is_gone_stores_nothing(conn: sqlite3.Connection) -> None:
    report = _one(conn, "1")
    assert report.error is None
    assert report.run.listings == 0
    assert jobs.list(conn) == []


def test_paused_source_is_refused_before_any_request(conn: sqlite3.Connection) -> None:
    set_source_status(conn, "greenhouse", SourceStatus.PAUSED, StatusActor.USER)
    with pytest.raises(SourcePausedError):
        _one(conn)


def test_offer_text_stays_out_of_the_logs(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _one(conn)
    assert "Build widgets" not in caplog.text
    assert "visa sponsorship" not in caplog.text
