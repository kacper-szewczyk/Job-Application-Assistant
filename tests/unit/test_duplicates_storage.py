import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jaa.connectors.base import JobListing
from jaa.core.errors import NotFoundError
from jaa.core.models import DuplicateReason, DuplicateVerdict, JobStatus, StatusActor
from jaa.storage import duplicates as dupes
from jaa.storage.jobs import set_job_status
from jaa.storage.listings import store_listings
from jaa.storage.repository import jobs

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
TITLE = "Synthetic Widget Wrangler"


def _listing(job_id: str, **changes: object) -> JobListing:
    fields: dict[str, object] = {
        "source_job_id": job_id,
        "url": f"https://board-a.example.test/jobs/{job_id}?utm_source=jaa",
        "title": TITLE,
        "company": "Example Widgets Sp. z o.o.",
        "location": "Kraków",
    }
    return JobListing(**(fields | changes))  # type: ignore[arg-type]


def _id(conn: sqlite3.Connection, source: str, source_job_id: str) -> int:
    [job] = [j for j in jobs.list(conn, source=source) if j.source_job_id == source_job_id]
    assert job.id is not None
    return job.id


def test_repeated_fetches_store_a_job_once(conn: sqlite3.Connection) -> None:
    for i in range(3):
        changes = store_listings(conn, "board_a", "all", [_listing("1")], at=T0 + timedelta(i))
        assert changes.flagged == ()
    [job] = jobs.list(conn)
    assert job.canonical_url == "https://board-a.example.test/jobs/1"
    assert job.source_url == "https://board-a.example.test/jobs/1?utm_source=jaa"
    assert job.last_seen_at == T0 + timedelta(2)
    assert dupes.list_flags(conn, include_reviewed=True) == []


def test_same_link_on_two_sources_is_flagged_not_merged(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    changes = store_listings(
        conn,
        "board_b",
        "it",
        [
            _listing(
                "b-9",
                url="https://board-b.example.test/9",
                canonical_url="https://WWW.board-a.example.test/jobs/1/",
                title="Something else entirely",
            )
        ],
        at=T0,
    )
    assert len(jobs.list(conn)) == 2
    [flag] = dupes.list_flags(conn)
    assert changes.flagged == (flag.id,)
    assert flag.flag.reason is DuplicateReason.CANONICAL_URL
    assert (flag.job.source, flag.other.source) == ("board_a", "board_b")
    assert flag.verdict is None


def test_same_employer_title_and_location_is_flagged(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(
        conn,
        "board_b",
        "it",
        [
            _listing(
                "b-1",
                url="https://board-b.example.test/1",
                title=f"{TITLE} (K/M)",
                company="EXAMPLE WIDGETS sp. z o.o.",
                location="krakow",
            ),
            # Same employer and title elsewhere: not flagged.
            _listing("b-2", url="https://board-b.example.test/2", location="Gdańsk"),
            # No location: never flagged by the fallback.
            _listing("b-3", url="https://board-b.example.test/3", location=None),
        ],
        at=T0,
    )
    [flag] = dupes.list_flags(conn)
    assert flag.flag.reason is DuplicateReason.EMPLOYER_TITLE_LOCATION
    assert flag.other.source_job_id == "b-1"


def test_flags_are_not_repeated_on_refetch(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(conn, "board_b", "all", [_listing("2")], at=T0)
    again = store_listings(conn, "board_b", "all", [_listing("2", description="new")], at=T0)
    assert again.changed and again.flagged == ()
    assert len(dupes.list_flags(conn)) == 1


def test_changed_title_is_checked_again(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(
        conn,
        "board_b",
        "all",
        [_listing("2", url="https://b.example.test/2", title="Other")],
        at=T0,
    )
    assert dupes.list_flags(conn) == []
    changes = store_listings(
        conn, "board_b", "all", [_listing("2", url="https://b.example.test/2")], at=T0
    )
    assert len(changes.flagged) == 1


def test_jobs_stored_before_keys_existed_get_them(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    job_id = _id(conn, "board_a", "1")
    # As migration 0007 leaves rows fetched before it.
    url = "https://board-a.example.test/jobs/1?utm_source=jaa"
    jobs.update(conn, job_id, canonical_url=url, source_url=url, dedupe_key=None)
    changes = store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    assert changes.unchanged == (job_id,)
    job = jobs.get(conn, job_id)
    assert job.canonical_url == "https://board-a.example.test/jobs/1"
    assert job.dedupe_key == "example widgets | synthetic widget wrangler | krakow"


def test_verdicts_are_recorded_and_the_latest_counts(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(conn, "board_b", "all", [_listing("2")], at=T0)
    [flag] = dupes.list_flags(conn)
    a, b = flag.job.id, flag.other.id
    assert dupes.flagged_job_ids(conn) == {a: [b], b: [a]}

    dupes.review_flag(conn, flag.id, DuplicateVerdict.DIFFERENT_JOBS, at=T0)
    assert dupes.list_flags(conn) == []
    assert dupes.flagged_job_ids(conn) == {}
    dupes.review_flag(conn, flag.id, DuplicateVerdict.SAME_JOB, at=T0 + timedelta(1))
    [reviewed] = dupes.list_flags(conn, include_reviewed=True)
    assert reviewed.verdict is DuplicateVerdict.SAME_JOB
    assert dupes.flagged_job_ids(conn) == {a: [b], b: [a]}
    assert [f.id for f in dupes.flags_for_job(conn, b or 0)] == [flag.id]
    # Nothing about the jobs changed.
    assert {j.status for j in jobs.list(conn)} == {JobStatus.DISCOVERED}

    with pytest.raises(NotFoundError):
        dupes.review_flag(conn, 999, DuplicateVerdict.SAME_JOB)
    with pytest.raises(NotFoundError):
        dupes.get_flag(conn, 999)


def test_flags_and_reviews_are_append_only(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(conn, "board_b", "all", [_listing("2")], at=T0)
    [flag] = dupes.list_flags(conn)
    dupes.review_flag(conn, flag.id, DuplicateVerdict.SAME_JOB)
    for sql in (
        "UPDATE job_duplicates SET reason = 'canonical_url'",
        "UPDATE job_duplicate_reviews SET verdict = 'different_jobs'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


def test_duplicate_of_a_job_in_progress_is_visible(conn: sqlite3.Connection) -> None:
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    set_job_status(conn, _id(conn, "board_a", "1"), JobStatus.SUBMITTED, StatusActor.USER)
    store_listings(conn, "board_b", "all", [_listing("2")], at=T0)
    [flag] = dupes.list_flags(conn)
    assert flag.job.status in dupes.IN_PROGRESS


def test_logs_hold_no_listing_text(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jaa")
    store_listings(conn, "board_a", "all", [_listing("1")], at=T0)
    store_listings(conn, "board_b", "all", [_listing("2")], at=T0)
    [flag] = dupes.list_flags(conn)
    dupes.review_flag(conn, flag.id, DuplicateVerdict.SAME_JOB)
    assert "flagged 1 possible duplicate" in caplog.text
    for value in (TITLE, "Example Widgets", "Kraków", "board-a.example.test"):
        assert value not in caplog.text
