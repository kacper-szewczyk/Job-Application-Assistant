import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime

import pytest

from jaa.connectors.base import ApplicationQuestion, JobListing, QuestionKind
from jaa.core.errors import MatchError
from jaa.core.job_details import JobDetails, ListedSkill, SalaryRange, WorkArrangement
from jaa.core.models import JsonValue
from jaa.storage import listings, matches
from jaa.storage.preferences import set_preference
from jaa.storage.profile import active_version_id, reject_facts
from jaa.storage.repository import jobs, profile_facts
from tests.fixtures.cv_profile import store_profile

T1 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
DESCRIPTION = "Requirements:\n- Python and Django\n- Kubernetes\nNice to have:\n- Docker"


def _listing(description: str = DESCRIPTION) -> JobListing:
    return JobListing(
        source_job_id="job-1",
        url="https://jobs.example.test/1",
        title="Senior Widget Engineer",
        company="Example Widgets",
        location="Wrocław",
        description=description,
        details=JobDetails(
            skills=(ListedSkill(name="C++", level="Advanced"),),
            languages=(ListedSkill(name="Angielski", level="B2"),),
            experience_level="Senior",
            work_arrangement=WorkArrangement.HYBRID,
            salaries=(SalaryRange(min=15000, max=21000, currency="PLN", period="month"),),
        ),
        questions=(ApplicationQuestion(key="q1", label="Why widgets?", kind=QuestionKind.TEXT),),
    )


def _store(conn: sqlite3.Connection, listing: JobListing | None = None) -> int:
    changes = listings.store_listings(conn, "test", "all", [listing or _listing()], at=T1)
    [job_id] = changes.new or changes.changed or changes.unchanged
    return job_id


def test_listing_details_and_questions_are_stored(conn: sqlite3.Connection) -> None:
    job = jobs.get(conn, _store(conn))
    assert job.requirements["skills"] == [{"name": "C++", "level": "Advanced"}]
    assert job.requirements["work_arrangement"] == "hybrid"
    questions = job.requirements["questions"]
    assert isinstance(questions, list) and len(questions) == 1


def test_hash_of_listings_without_details_is_unchanged() -> None:
    plain = JobListing(source_job_id="2", url="https://jobs.example.test/2", title="T", company="C")
    old_payload: list[JsonValue] = [plain.url, "T", "C", None, None, None, None, []]
    expected = hashlib.sha256(json.dumps(old_payload, ensure_ascii=False).encode()).hexdigest()
    assert listings.content_sha256(plain) == expected


def test_matching_needs_a_confirmed_profile(conn: sqlite3.Connection) -> None:
    job_id = _store(conn)
    with pytest.raises(MatchError, match="no confirmed profile"):
        matches.match_jobs(conn, [job_id])


def test_match_is_stored_with_profile_version_evidence_and_gaps(conn: sqlite3.Connection) -> None:
    store_profile(conn, unreviewed={"skills.docker": "Docker"})
    set_preference(conn, "search.seniority", ["senior"])
    job_id = _store(conn)

    [stored] = matches.match_jobs(conn, [job_id], at=T1)
    row = stored.match
    assert row.profile_version_id == active_version_id(conn)
    assert row.job_content_sha256 == jobs.get(conn, job_id).content_sha256
    assert row.method == "wording-v1"
    outcomes = {
        e["requirement"] if e.get("type") == "requirement" else e["preference"]: e["outcome"]
        for e in [*row.evidence, *row.gaps]
        if isinstance(e, dict)
    }
    assert outcomes == {
        "C++": "met",
        "Angielski": "met",
        "Python and Django": "met",
        "Kubernetes": "missing",
        "Docker": "unreviewed_only",
        "search.seniority": "fits",
    }
    # (2 + 2 + 2 + 1 seniority) of (2 * 4 + 1 + 1).
    assert row.score == 0.7
    assert any(isinstance(u, dict) and u["code"] == "unreviewed_evidence" for u in row.uncertainty)
    assert matches.latest_match(conn, job_id) == row


def test_matches_are_append_only_and_go_out_of_date(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    job_id = _store(conn)
    [first] = matches.match_jobs(conn, [job_id])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE job_matches SET score = 1 WHERE id = ?", (first.match.id,))

    job = jobs.get(conn, job_id)
    assert matches.outdated_reasons(first.match, job, active_version_id(conn)) == []

    [python] = profile_facts.list(conn, field_key="skills.python")
    reject_facts(conn, [python.id or 0])
    _store(conn, _listing(description=DESCRIPTION + "\n- Rust"))
    job = jobs.get(conn, job_id)
    assert matches.outdated_reasons(first.match, job, active_version_id(conn)) == [
        "profile changed",
        "listing changed",
    ]

    [second] = matches.match_jobs(conn, [job_id])
    assert second.match.score < first.match.score
    assert matches.latest_matches(conn) == {job_id: second.match}
    assert len(conn.execute("SELECT id FROM job_matches").fetchall()) == 2


def test_logs_carry_ids_and_counts_only(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    store_profile(conn)
    job_id = _store(conn)
    caplog.set_level(logging.DEBUG, logger="jaa")
    matches.match_jobs(conn, [job_id])
    assert "matched jobs=1" in caplog.text
    for text in ("Python", "Kubernetes", "Widget", "C++", "Angielski"):
        assert text not in caplog.text
