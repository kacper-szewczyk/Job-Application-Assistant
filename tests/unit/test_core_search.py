from datetime import UTC, datetime

import pytest

from jaa.core.errors import SearchError
from jaa.core.models import Job, JobStatus
from jaa.core.search import JobQuery, matches

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _job(**changes: object) -> Job:
    fields: dict[str, object] = {
        "source": "solid_jobs",
        "source_job_id": "1",
        "canonical_url": "https://solid.jobs/o/1",
        "employer": "Przykład Sp. z o.o.",
        "title": "Senior Python Developer",
        "location": "Łódź (hybrid)",
        "description": "Django, PostgreSQL and JavaScript.",
        "first_seen_at": T0,
        "last_seen_at": T0,
        "status_changed_at": T0,
    }
    return Job(**(fields | changes))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (JobQuery(), True),
        (JobQuery(text="python"), True),
        (JobQuery(text="POSTGRESQL django"), True),
        (JobQuery(text="przyklad lodz"), True),
        (JobQuery(text="python rust"), False),
        # Whole words only.
        (JobQuery(text="java"), False),
        (JobQuery(text="pyth"), False),
        (JobQuery(company="przykład"), True),
        (JobQuery(company="other"), False),
        (JobQuery(location="lodz"), True),
        (JobQuery(location="hybrid"), True),
        (JobQuery(location="remote"), False),
        (JobQuery(sources=("solid_jobs", "other")), True),
        (JobQuery(sources=("other",)), False),
        (JobQuery(statuses=(JobStatus.DISCOVERED,)), True),
        (JobQuery(statuses=(JobStatus.SAVED, JobStatus.PREPARING)), False),
    ],
)
def test_matches(query: JobQuery, expected: bool) -> None:
    assert matches(_job(), query) is expected


def test_removed_jobs_only_when_asked() -> None:
    removed = _job(removed_at=T0)
    assert not matches(removed, JobQuery())
    assert matches(removed, JobQuery(include_removed=True))


def test_location_filter_needs_a_location() -> None:
    assert not matches(_job(location=None), JobQuery(location="lodz"))


def test_query_json_round_trip() -> None:
    query = JobQuery(
        text="python",
        company="Example",
        location="Remote",
        sources=("solid_jobs",),
        statuses=(JobStatus.SAVED,),
        include_removed=True,
    )
    assert JobQuery.from_json(query.to_json()) == query
    assert JobQuery.from_json({}) == JobQuery()
    assert query.describe() == (
        '"python"; company: Example; location: Remote; source: solid_jobs; status: saved; '
        "including removed"
    )
    assert JobQuery().describe() == "all jobs"


@pytest.mark.parametrize(
    "data",
    [
        {"colour": "blue"},
        {"text": 3},
        {"sources": "solid_jobs"},
        {"sources": [1]},
        {"statuses": ["hired"]},
    ],
)
def test_unreadable_query(data: dict[str, object]) -> None:
    with pytest.raises(SearchError):
        JobQuery.from_json(data)  # type: ignore[arg-type]
