import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jaa.connectors.base import JobListing
from jaa.core.errors import SearchError
from jaa.core.models import FetchRunOutcome, JobStatus, SourceFetchRun, StatusActor
from jaa.core.search import JobQuery
from jaa.storage import searches
from jaa.storage.jobs import set_job_status
from jaa.storage.listings import store_listings
from jaa.storage.repository import jobs, source_fetch_runs

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
QUERY_TEXT = "synthetic-query-word"


def _listing(job_id: str, title: str, location: str = "Remote") -> JobListing:
    return JobListing(
        source_job_id=job_id,
        url=f"https://board.example.test/{job_id}",
        title=title,
        company=f"Company {job_id}",
        location=location,
    )


def _seed(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board", "all", [_listing("1", "Python Developer")], at=T0)
    store_listings(
        conn,
        "board",
        "all",
        [_listing("1", "Python Developer"), _listing("2", "Python Engineer", "Łódź")],
        at=T0 + timedelta(hours=1),
    )
    store_listings(conn, "other", "all", [_listing("3", "Java Developer")], at=T0)


def test_search_jobs_filters_and_orders_newest_first(conn: sqlite3.Connection) -> None:
    _seed(conn)
    found = searches.search_jobs(conn, JobQuery(text="python"))
    assert [j.source_job_id for j in found] == ["2", "1"]
    assert [j.source_job_id for j in searches.search_jobs(conn, JobQuery(location="lodz"))] == ["2"]
    assert len(searches.search_jobs(conn, JobQuery(sources=("other",)))) == 1


def test_save_replace_and_delete(conn: sqlite3.Connection) -> None:
    saved = searches.save_search(conn, " python ", JobQuery(text="python"), at=T0)
    assert saved.name == "python"
    assert (saved.created_at, saved.last_refreshed_at) == (T0, None)
    with pytest.raises(SearchError, match="exists"):
        searches.save_search(conn, "python", JobQuery(text="django"))
    replaced = searches.save_search(
        conn, "python", JobQuery(text="django"), replace=True, at=T0 + timedelta(1)
    )
    assert replaced.id == saved.id
    assert JobQuery.from_json(replaced.query).text == "django"
    assert (replaced.created_at, replaced.updated_at) == (T0, T0 + timedelta(1))
    assert [s.name for s in searches.list_searches(conn)] == ["python"]
    searches.delete_search(conn, "python")
    assert searches.list_searches(conn) == []
    with pytest.raises(SearchError, match="no saved search"):
        searches.delete_search(conn, "python")


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "line\nbreak"])
def test_bad_names(conn: sqlite3.Connection, name: str) -> None:
    with pytest.raises(SearchError):
        searches.save_search(conn, name, JobQuery())


def test_runs_mark_jobs_new_since_the_last_refresh(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board", "all", [_listing("1", "Python Developer")], at=T0)
    searches.save_search(conn, "py", JobQuery(text="python", sources=("board",)))
    first = searches.run_search(conn, "py", at=T0 + timedelta(minutes=5))
    assert first.previous_refresh is None
    assert len(first.new_since_last) == 1
    assert first.search.last_refreshed_at == T0 + timedelta(minutes=5)
    assert first.search.last_result_count == 1
    assert first.data_as_of is None

    store_listings(
        conn,
        "board",
        "all",
        [_listing("1", "Python Developer"), _listing("2", "Python Engineer")],
        at=T0 + timedelta(hours=1),
    )
    source_fetch_runs.insert(
        conn,
        SourceFetchRun(
            source="board",
            target="all",
            started_at=T0,
            finished_at=T0 + timedelta(hours=1),
            outcome=FetchRunOutcome.OK,
        ),
    )
    second = searches.run_search(conn, "py", at=T0 + timedelta(hours=2))
    assert second.previous_refresh == T0 + timedelta(minutes=5)
    [new] = second.new_since_last
    assert jobs.get(conn, new).source_job_id == "2"
    assert second.search.last_result_count == 2
    assert second.data_as_of == T0 + timedelta(hours=1)
    assert searches.last_fetched(conn, ("other",)) is None


def test_status_filters_follow_the_lifecycle(conn: sqlite3.Connection) -> None:
    _seed(conn)
    [job] = searches.search_jobs(conn, JobQuery(text="java"))
    set_job_status(conn, job.id or 0, JobStatus.SAVED, StatusActor.USER)
    found = searches.search_jobs(conn, JobQuery(statuses=(JobStatus.SAVED,)))
    assert [j.id for j in found] == [job.id]


def test_unknown_search(conn: sqlite3.Connection) -> None:
    with pytest.raises(SearchError, match="no saved search"):
        searches.run_search(conn, "missing")


def test_logs_hold_no_query_text(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jaa")
    searches.save_search(conn, "secret-name", JobQuery(text=QUERY_TEXT))
    searches.run_search(conn, "secret-name")
    assert "ran saved search" in caplog.text
    assert QUERY_TEXT not in caplog.text
    assert "secret-name" not in caplog.text
