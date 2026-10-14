import io
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from jaa.core import models as m
from jaa.core.errors import NotFoundError, StorageError
from jaa.core.sensitivity import is_sensitive_key
from jaa.logging import setup_logging
from jaa.storage import repository as repo
from jaa.storage.jobs import set_job_status, status_history
from tests.conftest import (
    SYNTHETIC_ANSWER,
    SYNTHETIC_EMAIL,
    SYNTHETIC_TITLE,
    T0,
    Sample,
    ident,
    populate,
)


def test_round_trip_every_model(conn: sqlite3.Connection, sample: Sample) -> None:
    assert repo.import_runs.get(conn, ident(sample.run.id)) == sample.run
    assert repo.profile_facts.get(conn, ident(sample.fact.id)) == sample.fact
    assert repo.profile_facts.get(conn, ident(sample.email.id)) == sample.email
    assert repo.profile_versions.get(conn, ident(sample.version.id)) == sample.version
    assert repo.answers.get(conn, ident(sample.answer.id)) == sample.answer
    assert repo.jobs.get(conn, ident(sample.job.id)) == sample.job
    assert repo.applications.get(conn, ident(sample.application.id)) == sample.application
    assert repo.document_artifacts.get(conn, ident(sample.artifact.id)) == sample.artifact


def test_insert_assigns_id_and_requires_none(conn: sqlite3.Connection, sample: Sample) -> None:
    assert sample.version.id is not None
    with pytest.raises(StorageError, match="id=None"):
        repo.profile_versions.insert(conn, sample.version)


def test_json_null_value_round_trips(conn: sqlite3.Connection) -> None:
    pref = repo.preferences.insert(conn, m.Preference(key="start_date", value=None, updated_at=T0))
    assert repo.preferences.get(conn, ident(pref.id)).value is None


def test_naive_datetime_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(StorageError, match="naive"):
        repo.profile_versions.insert(conn, m.ProfileVersion(created_at=datetime(2026, 1, 1)))  # noqa: DTZ001


def test_get_missing_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(NotFoundError):
        repo.jobs.get(conn, 404)


def test_list_filters(conn: sqlite3.Connection, sample: Sample) -> None:
    confirmed = repo.profile_facts.list(conn, state=m.FactState.CONFIRMED)
    assert [f.id for f in confirmed] == [sample.email.id]
    with pytest.raises(StorageError, match="unknown filter"):
        repo.profile_facts.list(conn, nope=1)


def test_fact_review_state_can_change(conn: sqlite3.Connection, sample: Sample) -> None:
    later = T0 + timedelta(hours=1)
    updated = repo.profile_facts.update(
        conn,
        ident(sample.fact.id),
        state=m.FactState.CONFIRMED,
        confirmed_at=later,
        updated_at=later,
    )
    assert updated.state is m.FactState.CONFIRMED
    assert updated.value_raw == SYNTHETIC_TITLE


@pytest.mark.parametrize("column", ["value_raw", "field_key", "source_excerpt", "origin"])
def test_fact_values_are_immutable(conn: sqlite3.Connection, sample: Sample, column: str) -> None:
    value: object = m.FactOrigin.USER if column == "origin" else "changed"
    with pytest.raises(StorageError, match="immutable"):
        repo.profile_facts.update(conn, ident(sample.fact.id), **{column: value})


def test_deleting_import_run_keeps_facts(conn: sqlite3.Connection, sample: Sample) -> None:
    conn.execute("DELETE FROM import_runs WHERE id = ?", (sample.run.id,))
    assert repo.profile_facts.get(conn, ident(sample.fact.id)).import_run_id is None


def test_profile_versions_are_immutable(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE profile_versions SET note = 'x' WHERE id = ?", (sample.version.id,))


def test_single_profile_row(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(StorageError):
        repo.profiles.insert(conn, m.Profile(created_at=T0, updated_at=T0))


def test_submission_requires_confirmation_source(conn: sqlite3.Connection, sample: Sample) -> None:
    job = repo.jobs.insert(conn, replace(sample.job, id=None, source_job_id="456"))
    with pytest.raises(StorageError):
        repo.applications.insert(
            conn, m.Application(job_id=ident(job.id), created_at=T0, submitted_at=T0)
        )


def test_one_application_per_job(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(StorageError):
        repo.applications.insert(conn, m.Application(job_id=ident(sample.job.id), created_at=T0))


def test_duplicate_source_job_rejected(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(StorageError):
        repo.jobs.insert(conn, replace(sample.job, id=None))


def test_job_status_history(conn: sqlite3.Connection, sample: Sample) -> None:
    job_id = ident(sample.job.id)
    t1 = T0 + timedelta(days=1)
    job = set_job_status(
        conn, job_id, m.JobStatus.SAVED, m.StatusActor.USER, note="looks good", at=t1
    )
    assert job.status is m.JobStatus.SAVED
    assert repo.jobs.get(conn, job_id).status_changed_at == t1
    # Setting the same status again records nothing.
    set_job_status(conn, job_id, m.JobStatus.SAVED, m.StatusActor.USER)

    history = status_history(conn, job_id)
    assert [(e.from_status, e.to_status) for e in history] == [
        (None, m.JobStatus.DISCOVERED),
        (m.JobStatus.DISCOVERED, m.JobStatus.SAVED),
    ]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE job_status_events SET note = 'x'")


def test_reprs_hide_personal_values(sample: Sample) -> None:
    text = " ".join(
        repr(x) for x in (sample.fact, sample.email, sample.answer, sample.application, sample.job)
    )
    for secret in (
        SYNTHETIC_TITLE,
        SYNTHETIC_EMAIL,
        SYNTHETIC_ANSWER,
        "Build widgets.",
        "applied via",
    ):
        assert secret not in text


def test_storage_never_logs_values(tmp_path: object, conn: sqlite3.Connection) -> None:
    stream = io.StringIO()
    setup_logging("DEBUG", stream=stream)
    logging.getLogger("jaa").addHandler(logging.StreamHandler(raw := io.StringIO()))
    sample = populate(conn)
    repo.profile_facts.update(conn, ident(sample.fact.id), state=m.FactState.REJECTED)
    repo.answers.list(conn, question_key="sponsorship.required")
    raw_log = raw.getvalue()  # unscrubbed: proves values are never passed to the logger
    assert "inserted profile_facts" in raw_log
    for secret in (SYNTHETIC_TITLE, SYNTHETIC_EMAIL, SYNTHETIC_ANSWER):
        assert secret not in raw_log


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("contact.email", True),
        ("work_authorization.eu", True),
        ("salary.min", True),
        ("Demographics.gender", True),
        ("experience.0.title", False),
        ("skills", False),
    ],
)
def test_sensitive_keys(key: str, sensitive: bool) -> None:
    assert is_sensitive_key(key) is sensitive
