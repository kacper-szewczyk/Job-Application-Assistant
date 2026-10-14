import logging
import sqlite3

import pytest

from jaa.connectors.http import HttpResponse, Transport, TransportError
from jaa.core.errors import ConnectorTargetError, SourcePausedError
from jaa.core.models import FetchRunOutcome, Job, JobStatus, SourceStatus, StatusActor
from jaa.storage import listings
from jaa.storage.jobs import set_job_status, status_history
from jaa.storage.repository import jobs, source_fetch_runs
from jaa.storage.sources import latest_status_event, set_source_status
from tests.fixtures import solid_jobs as rec
from tests.fixtures.fake_connector import FakeClock


def _fetch(
    conn: sqlite3.Connection, transport: Transport, target: str = "IT"
) -> listings.FetchReport:
    clock = FakeClock()
    return listings.fetch_and_store(
        conn, rec.connector(), target, transport=transport, clock=clock, sleep=clock.sleep
    )


def _job(conn: sqlite3.Connection, key: str) -> Job:
    [job] = [j for j in jobs.list(conn, source="solid_jobs") if j.source_job_id == key]
    return job


def test_first_fetch_stores_new_jobs_and_the_run(conn: sqlite3.Connection) -> None:
    report = _fetch(conn, rec.first_fetch())
    assert report.error is None
    assert len(report.changes.new) == 3
    run = report.run
    assert (run.outcome, run.requests, run.listings, run.new, run.removed) == (
        FetchRunOutcome.OK,
        2,
        3,
        3,
        0,
    )
    assert {str(i["code"]) for i in run.issues if isinstance(i, dict)} == {
        "unreadable_listing",
        "duplicate_listing",
    }
    assert source_fetch_runs.list(conn) == [run]

    a = _job(conn, rec.KEY_A)
    assert (a.source, a.source_target, a.status) == ("solid_jobs", "IT", JobStatus.DISCOVERED)
    assert a.canonical_url == "https://solid.jobs/o/syntha01"
    assert a.source_url == "https://solid.jobs/o/syntha01/jaa"
    assert a.dedupe_key == "example widgets | senior widget engineer | warszawa hybrid"
    assert a.employer == "Example Widgets Sp. z o.o."
    assert a.first_seen_at == a.last_seen_at == run.finished_at
    assert a.removed_at is None
    assert a.content_sha256 is not None
    assert [e.to_status for e in status_history(conn, a.id or 0)] == [JobStatus.DISCOVERED]


def test_later_fetches_detect_changes_removals_and_returns(conn: sqlite3.Connection) -> None:
    first = _fetch(conn, rec.first_fetch()).run
    later = _fetch(conn, rec.later_fetch())
    run = later.run
    assert (run.new, run.changed, run.unchanged, run.removed, run.reappeared) == (1, 1, 1, 1, 0)

    a, b = _job(conn, rec.KEY_A), _job(conn, rec.KEY_B)
    assert a.title == "Senior Widget Engineer (Python)"
    assert (a.first_seen_at, a.last_seen_at) == (first.finished_at, run.finished_at)
    assert b.removed_at == run.finished_at
    assert b.status is JobStatus.DISCOVERED  # removal is an observation, not a status change
    assert b.last_seen_at == first.finished_at

    again = _fetch(conn, rec.first_fetch()).run
    assert (again.new, again.reappeared, again.removed, again.changed) == (0, 1, 1, 1)
    assert _job(conn, rec.KEY_B).removed_at is None
    assert _job(conn, rec.KEY_D).removed_at == again.finished_at


def test_refetching_keeps_the_users_status(conn: sqlite3.Connection) -> None:
    _fetch(conn, rec.first_fetch())
    job_id = _job(conn, rec.KEY_A).id or 0
    set_job_status(conn, job_id, JobStatus.SAVED, StatusActor.USER)
    _fetch(conn, rec.later_fetch())
    assert _job(conn, rec.KEY_A).status is JobStatus.SAVED
    assert len(status_history(conn, job_id)) == 2


def test_unavailable_source_records_the_run_and_removes_nothing(conn: sqlite3.Connection) -> None:
    _fetch(conn, rec.first_fetch())
    failing = rec.answering(HttpResponse(status=503, url=rec.url(0)), TransportError("down"))
    report = _fetch(conn, failing)
    assert report.run.outcome is FetchRunOutcome.UNAVAILABLE
    assert report.run.error is not None and "attempts" in report.run.error
    assert not report.paused
    assert all(j.removed_at is None for j in jobs.list(conn))
    assert latest_status_event(conn, "solid_jobs") is None


def test_access_denied_pauses_the_source(conn: sqlite3.Connection) -> None:
    report = _fetch(conn, rec.answering(HttpResponse(status=403, url=rec.url(0))))
    assert report.run.outcome is FetchRunOutcome.DENIED
    assert report.paused
    event = latest_status_event(conn, "solid_jobs")
    assert event is not None
    assert (event.status, event.actor) == (SourceStatus.PAUSED, StatusActor.SYSTEM)
    assert "refused access (HTTP 403)" in (event.reason or "")

    transport = rec.first_fetch()
    with pytest.raises(SourcePausedError):
        _fetch(conn, transport)
    assert transport.sent == []
    assert len(source_fetch_runs.list(conn)) == 1

    set_source_status(conn, "solid_jobs", SourceStatus.ACTIVE, StatusActor.USER)
    assert _fetch(conn, rec.first_fetch()).run.outcome is FetchRunOutcome.OK


def test_unknown_target_records_nothing(conn: sqlite3.Connection) -> None:
    with pytest.raises(ConnectorTargetError):
        _fetch(conn, rec.first_fetch(), "Astrology")
    assert source_fetch_runs.list(conn) == []


def test_fetch_history_is_append_only(conn: sqlite3.Connection) -> None:
    _fetch(conn, rec.first_fetch())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE source_fetch_runs SET listings = 0")


def test_logs_carry_counts_not_listing_text(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jaa")
    _fetch(conn, rec.first_fetch())
    _fetch(conn, rec.later_fetch())
    assert "new=1 changed=1" in caplog.text
    for text in ("Senior Widget", "Example Widgets", "syntetycznych", "Warszawa"):
        assert text not in caplog.text
