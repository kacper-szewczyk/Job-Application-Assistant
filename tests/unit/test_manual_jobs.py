import logging
import sqlite3

import pytest

from jaa.connectors.base import JobListing
from jaa.core.errors import JobEntryError
from jaa.core.models import JobStatus, StatusActor
from jaa.matching.requirements import extract_requirements
from jaa.storage import duplicates
from jaa.storage.jobs import status_history
from jaa.storage.listings import store_listings
from jaa.storage.manual_jobs import MANUAL_SOURCE, add_manual_job
from tests.conftest import T0

URL = "https://justjoin.example/offers/widget-engineer?utm_source=newsletter"
DESCRIPTION = """Synthetic widget team offer.

Requirements:
- Python
- SQL
"""


def _add(conn: sqlite3.Connection, **overrides: str | None) -> int:
    fields: dict[str, str | None] = {
        "url": URL,
        "title": "Widget Engineer",
        "company": "Example Widgets Sp. z o.o.",
        "location": "Kraków",
        "description": DESCRIPTION,
    } | overrides
    added = add_manual_job(
        conn,
        url=fields["url"] or "",
        title=fields["title"] or "",
        company=fields["company"] or "",
        location=fields["location"],
        description=fields["description"],
        at=T0,
    )
    assert added.job.id is not None
    return added.job.id


def test_stores_a_manual_job_like_a_fetched_one(conn: sqlite3.Connection) -> None:
    added = add_manual_job(
        conn,
        url=URL,
        title="  Widget   Engineer ",
        company="Example Widgets Sp. z o.o.",
        location="Kraków",
        description=DESCRIPTION,
        at=T0,
    )
    job = added.job
    assert job.source == MANUAL_SOURCE
    assert job.title == "Widget Engineer"
    assert job.source_url == URL
    assert job.canonical_url == "https://justjoin.example/offers/widget-engineer"
    assert job.dedupe_key == "example widgets | widget engineer | krakow"
    assert job.first_seen_at == job.last_seen_at == job.status_changed_at == T0
    assert job.source_target is None and job.removed_at is None
    assert job.status is JobStatus.DISCOVERED
    assert added.flagged == ()
    [event] = status_history(conn, job.id or 0)
    assert event.actor is StatusActor.USER
    assert [r.text for r in extract_requirements(job).required] == ["Python", "SQL"]


def test_a_link_added_twice_is_refused(conn: sqlite3.Connection) -> None:
    job_id = _add(conn)
    with pytest.raises(JobEntryError, match=f"already added as job {job_id}"):
        _add(conn, url="https://www.justjoin.example/offers/widget-engineer/")


def test_a_fetched_job_with_the_same_link_is_flagged(conn: sqlite3.Connection) -> None:
    store_listings(
        conn,
        "solid_jobs",
        "IT",
        [
            JobListing(
                source_job_id="s1",
                url="https://justjoin.example/offers/widget-engineer",
                title="Something else",
                company="Other",
            )
        ],
        at=T0,
    )
    added = add_manual_job(conn, url=URL, title="Widget Engineer", company="Example", at=T0)
    [flag] = duplicates.list_flags(conn)
    assert added.flagged == (flag.id,)
    assert added.job.id in (flag.job.id, flag.other.id)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"url": "justjoin.example/offers/1"}, "must be a web address"),
        ({"url": "ftp://justjoin.example/offers/1"}, "must be a web address"),
        ({"url": "https://"}, "must be a web address"),
        ({"title": "   "}, "title is empty"),
        ({"company": ""}, "company is empty"),
        ({"title": "x" * 301}, "longer than 300"),
        ({"description": "x" * 100_001}, "description is longer"),
    ],
)
def test_incomplete_entries_are_refused(
    conn: sqlite3.Connection, overrides: dict[str, str | None], message: str
) -> None:
    with pytest.raises(JobEntryError, match=message):
        _add(conn, **overrides)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_optional_fields_may_be_left_out(conn: sqlite3.Connection) -> None:
    job_id = _add(conn, location="  ", description="\n")
    row = conn.execute("SELECT location, description, dedupe_key FROM jobs WHERE id = ?", (job_id,))
    assert tuple(row.fetchone()) == (None, None, None)


def test_logs_hold_no_job_text(conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _add(conn)
    assert "added manual job" in caplog.text
    for value in ("Widget Engineer", "Example Widgets", "justjoin", "Synthetic widget"):
        assert value not in caplog.text
