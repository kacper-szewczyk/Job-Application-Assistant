import logging
import sqlite3
from datetime import timedelta

import pytest

from jaa.core.errors import MatchError
from jaa.core.models import (
    FactCandidate,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JobStatus,
    JsonValue,
    RetentionChoice,
    StatusActor,
)
from jaa.documents.linkedin_suggestions import SuggestionKind
from jaa.storage import profile
from jaa.storage.jobs import set_job_status
from jaa.storage.linkedin_profile import latest_linkedin_run, linkedin_suggestions
from jaa.storage.manual_jobs import add_manual_job
from jaa.storage.repository import profile_facts
from tests.fixtures.cv_profile import PROFILE, T0, store_profile

# What the synthetic LinkedIn profile shows: some values equal to the confirmed profile.
LINKEDIN: dict[str, JsonValue] = {
    "headline": "Widget Engineer",
    "experience.example-co.employer": "Example Co",
    "experience.example-co.title": "Senior Widget Engineer",
    "skills.python": "Python",
}
DESCRIPTION = "Requirements:\n- React Native\n- Kubernetes\n"


def _run(
    source: ImportSourceType, offset: int = 1, status: ImportStatus = ImportStatus.SUCCEEDED
) -> ImportRun:
    return ImportRun(
        source_type=source,
        imported_at=T0 + timedelta(hours=offset),
        status=status,
        retention=RetentionChoice.KEEP,
    )


def _import(conn: sqlite3.Connection, values: dict[str, JsonValue], offset: int = 1) -> int:
    run = _run(ImportSourceType.LINKEDIN_ARCHIVE, offset)
    candidates = [
        FactCandidate(field_key=k, value_raw=str(v), value_normalized=v) for k, v in values.items()
    ]
    stored = profile.add_imported_facts(conn, run, candidates).run
    assert stored.id is not None
    return stored.id


def _job(conn: sqlite3.Connection, status: JobStatus = JobStatus.SAVED) -> int:
    added = add_manual_job(
        conn,
        url="https://example.test/jobs/1",
        title="Widget Engineer",
        company="Example Widgets",
        description=DESCRIPTION,
        at=T0,
    )
    job_id = added.job.id
    assert job_id is not None
    if status is not JobStatus.DISCOVERED:
        set_job_status(conn, job_id, status, StatusActor.USER, at=T0)
    return job_id


def test_an_import_links_every_fact_it_saw_including_known_values(
    conn: sqlite3.Connection,
) -> None:
    store_profile(conn)
    run_id = _import(conn, LINKEDIN)
    seen = {profile_facts.get(conn, i).field_key for i in profile.run_fact_ids(conn, run_id)}
    assert seen == set(LINKEDIN)
    # Only the differing headline was stored as a new fact.
    assert [f.field_key for f in profile_facts.list(conn, import_run_id=run_id)] == ["headline"]


def test_runs_without_links_fall_back_to_the_facts_they_added(conn: sqlite3.Connection) -> None:
    run_id = _import(conn, {"skills.go": "Go"})
    conn.execute("DELETE FROM import_run_facts")
    assert [profile_facts.get(conn, i).field_key for i in profile.run_fact_ids(conn, run_id)] == [
        "skills.go"
    ]


def test_deleting_an_import_removes_its_links(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    run_id = _import(conn, LINKEDIN)
    profile.delete_import(conn, run_id)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM import_run_facts WHERE import_run_id = ?", (run_id,)
        ).fetchone()[0]
        == 0
    )


def test_no_linkedin_import_means_no_report(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    assert linkedin_suggestions(conn) is None


def test_nothing_confirmed_is_an_error(conn: sqlite3.Connection) -> None:
    _import(conn, LINKEDIN)
    with pytest.raises(MatchError):
        linkedin_suggestions(conn)


def test_the_latest_successful_linkedin_import_is_used(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    older = _import(conn, LINKEDIN, offset=1)
    profile.add_imported_facts(
        conn, _run(ImportSourceType.LINKEDIN_API, 5, ImportStatus.FAILED), []
    )
    profile.add_imported_facts(conn, _run(ImportSourceType.PDF, 6), [])
    run = latest_linkedin_run(conn)
    assert run is not None and run.id == older


def test_suggestions_compare_the_import_with_confirmed_facts_and_saved_jobs(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    store_profile(conn)
    _import(conn, LINKEDIN)
    saved = _job(conn)
    caplog.set_level(logging.DEBUG)

    report = linkedin_suggestions(conn)

    assert report is not None
    assert [j.id for j in report.jobs] == [saved]
    found = {s.address: s for s in report.result.suggestions}
    assert found["headline"].kind is SuggestionKind.UPDATE
    assert found["headline"].text == PROFILE["headline"]
    assert found["headline"].current == "Widget Engineer"
    assert "skills.python" not in found
    assert "experience.example-co.title" not in found
    # The saved job asks for React Native, which is confirmed: that comes first.
    first = report.result.suggestions[0]
    assert first.address == "skills.react-native"
    assert first.job_ids == (saved,)
    assert [g.requirement for g in report.result.gaps] == ["Kubernetes"]
    assert report.result.unreviewed_linkedin == 1  # the differing headline

    logged = caplog.text
    for value in (PROFILE["headline"], "Widget Engineer", "React Native", "Kubernetes"):
        assert str(value) not in logged
    assert "suggestions=" in logged


def test_discovered_jobs_are_only_used_when_named(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    _import(conn, LINKEDIN)
    job_id = _job(conn, JobStatus.DISCOVERED)
    report = linkedin_suggestions(conn)
    assert report is not None and report.jobs == ()
    named = linkedin_suggestions(conn, [job_id, job_id])
    assert named is not None and [j.id for j in named.jobs] == [job_id]


def test_suggesting_writes_nothing(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    _import(conn, LINKEDIN)
    _job(conn)
    before = conn.total_changes
    linkedin_suggestions(conn)
    assert conn.total_changes == before
