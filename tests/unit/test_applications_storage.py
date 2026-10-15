import hashlib
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from jaa.core import models as m
from jaa.core.errors import ApplicationError, DuplicateApplicationError
from jaa.storage import answers
from jaa.storage import applications as service
from jaa.storage.applications import HandoffWarning
from jaa.storage.db import open_database
from jaa.storage.duplicates import review_flag
from jaa.storage.jobs import add_job, status_history
from jaa.storage.repository import document_artifacts, job_duplicates, jobs
from tests.conftest import T0, ident

NOW = T0 + timedelta(days=1)
NOTE = "synthetic-note: spoke to recruiter"
NOTICE = "synthetic-notice: one month"
S = m.JobStatus


def _job(
    conn: sqlite3.Connection,
    id_: str = "w-1",
    url: str = "https://boards.example.test/jobs/1",
    title: str = "Widget Engineer",
    location: str | None = None,
) -> int:
    stored = add_job(
        conn,
        m.Job(
            source="example_board",
            source_job_id=id_,
            canonical_url=url,
            source_url=url + "?ref=jaa",
            employer="Example Co",
            title=title,
            location=location,
            first_seen_at=T0,
            last_seen_at=T0,
            status_changed_at=T0,
        ),
    )
    return ident(stored.id)


def _cv(conn: sqlite3.Connection, job_id: int, tmp_path: Path, name: str = "cv.pdf") -> int:
    path = tmp_path / name
    path.write_bytes(b"%PDF synthetic")
    with conn:
        stored = document_artifacts.insert(
            conn,
            m.DocumentArtifact(
                kind=m.ArtifactKind.CV_PDF,
                path=str(path),
                job_id=job_id,
                content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                created_at=T0,
            ),
        )
    return ident(stored.id)


def test_start_moves_to_preparing_and_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "jaa.sqlite3"
    conn = open_database(db_path)
    job_id = _job(conn)
    started = service.start_application(conn, job_id, note=NOTE, at=NOW)
    assert started.created and started.job.status is S.PREPARING
    again = service.start_application(conn, job_id)
    assert not again.created and again.application.id == started.application.id
    conn.close()

    reopened = open_database(db_path)
    record = service.application_record(reopened, job_id)
    assert record.application is not None and record.application.created_at == NOW
    last = record.history[-1]
    assert (last.from_status, last.to_status, last.actor) == (
        S.DISCOVERED,
        S.PREPARING,
        m.StatusActor.USER,
    )
    assert last.note == NOTE
    reopened.close()


def test_duplicates_are_flagged_before_preparation(conn: sqlite3.Connection) -> None:
    first = _job(conn)
    service.start_application(conn, first, at=NOW)
    same_link = _job(conn, "w-2")
    with pytest.raises(DuplicateApplicationError) as err:
        service.start_application(conn, same_link)
    assert err.value.job_ids == (first,)
    assert service.get_application(conn, same_link) is None
    assert jobs.get(conn, same_link).status is S.DISCOVERED

    started = service.start_application(conn, same_link, allow_duplicates=True)
    assert started.created and [c.job.id for c in started.conflicts] == [first]
    assert f"#{first}" in (status_history(conn, same_link)[-1].note or "")


def test_same_role_elsewhere_conflicts_until_judged_different(conn: sqlite3.Connection) -> None:
    first = _job(conn, location="Warsaw")
    service.start_application(conn, first)
    other = _job(conn, "w-2", "https://boards.example.test/jobs/2", location="Warsaw")
    assert [c.job.id for c in service.find_application_conflicts(conn, other)] == [first]
    with conn:
        flag = job_duplicates.insert(
            conn,
            m.JobDuplicate(
                job_id=first,
                other_job_id=other,
                reason=m.DuplicateReason.EMPLOYER_TITLE_LOCATION,
                detected_at=NOW,
            ),
        )
    review_flag(conn, ident(flag.id), m.DuplicateVerdict.DIFFERENT_JOBS)
    assert service.find_application_conflicts(conn, other) == []
    # Jobs nobody started do not conflict.
    unstarted = _job(conn, "w-3", "https://boards.example.test/jobs/3", title="Gadget Engineer")
    assert service.find_application_conflicts(conn, unstarted) == []


def test_status_rules(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    with pytest.raises(ApplicationError, match="confirm"):
        service.set_application_status(conn, job_id, S.SUBMITTED)
    with pytest.raises(ApplicationError, match="apply start"):
        service.set_application_status(conn, job_id, S.READY_FOR_REVIEW)
    with pytest.raises(ApplicationError, match="submission"):
        service.set_application_status(conn, job_id, S.INTERVIEWING)
    assert service.set_application_status(conn, job_id, S.SAVED, note=NOTE).status is S.SAVED
    service.start_application(conn, job_id)
    assert (
        service.set_application_status(conn, job_id, S.READY_FOR_REVIEW).status
        is S.READY_FOR_REVIEW
    )


def test_only_the_users_confirmation_records_a_submission(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    with pytest.raises(ApplicationError, match="integration"):
        service.record_submission(
            conn, job_id, confirmation=m.SubmissionConfirmation.INTEGRATION_RECEIPT
        )
    with pytest.raises(ApplicationError, match="future"):
        service.record_submission(
            conn,
            job_id,
            confirmation=m.SubmissionConfirmation.USER,
            submitted_at=NOW + timedelta(hours=1),
            now=NOW,
        )
    assert service.get_application(conn, job_id) is None
    assert jobs.get(conn, job_id).status is S.DISCOVERED


def test_submission_stores_link_documents_and_answers(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job_id = _job(conn)
    service.start_application(conn, job_id, at=NOW)
    cv = _cv(conn, job_id, tmp_path)
    answers.add_job_question(conn, job_id, "What is your notice period?", required=True)
    saved = answers.save_answer(
        conn,
        question_text="What is your notice period?",
        value=NOTICE,
        policy=m.ReusePolicy.REUSE,
        at=T0,
    )
    answers.add_job_question(conn, job_id, "Do you require visa sponsorship?")

    found = service.handoff(conn, job_id, now=NOW)
    assert found.url == "https://boards.example.test/jobs/1?ref=jaa"
    assert [d.id for d in found.documents] == [cv]
    # The sponsorship question still needs the user's input.
    assert found.warnings == (HandoffWarning.QUESTIONS_NEED_INPUT,)

    stored = service.record_submission(
        conn,
        job_id,
        confirmation=m.SubmissionConfirmation.USER,
        reference="REF-1",
        note=NOTE,
        now=NOW,
    )
    assert stored.submitted_at == NOW
    assert stored.submission_confirmed_by is m.SubmissionConfirmation.USER
    assert stored.source_url == found.url
    assert stored.handoff["documents"] == [cv]
    questions = stored.handoff["questions"]
    assert isinstance(questions, list) and len(questions) == 2

    record = service.application_record(conn, job_id)
    assert record.job.status is S.SUBMITTED
    assert [d.id for d in record.documents] == [cv]
    assert record.answer_ids == (ident(saved.id),)
    assert record.history[-1].actor is m.StatusActor.USER

    with pytest.raises(ApplicationError, match="already"):
        service.record_submission(conn, job_id, confirmation=m.SubmissionConfirmation.USER)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be changed"):
        conn.execute(
            "UPDATE applications SET submitted_at = ? WHERE job_id = ?", (T0.isoformat(), job_id)
        )
    # Notes and status changes after submission still work.
    assert service.set_application_status(conn, job_id, S.INTERVIEWING).status is S.INTERVIEWING
    with pytest.raises(ApplicationError, match="already submitted"):
        service.set_application_status(conn, job_id, S.PREPARING)


def test_submission_refuses_duplicates_and_foreign_documents(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first = _job(conn)
    service.record_submission(conn, first, confirmation=m.SubmissionConfirmation.USER)
    second = _job(conn, "w-2")
    with pytest.raises(DuplicateApplicationError):
        service.record_submission(conn, second, confirmation=m.SubmissionConfirmation.USER)
    foreign = _cv(conn, first, tmp_path)
    with pytest.raises(ApplicationError, match="not built for"):
        service.record_submission(
            conn,
            second,
            confirmation=m.SubmissionConfirmation.USER,
            document_ids=[foreign],
            allow_duplicates=True,
        )
    assert service.get_application(conn, second) is None


def test_handoff_warns_about_missing_and_changed_documents(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job_id = _job(conn)
    assert service.handoff(conn, job_id).warnings == (
        HandoffWarning.NOT_STARTED,
        HandoffWarning.NO_CV,
    )
    _cv(conn, job_id, tmp_path)
    (tmp_path / "cv.pdf").write_bytes(b"edited")
    assert HandoffWarning.DOCUMENT_CHANGED in service.handoff(conn, job_id).warnings
    (tmp_path / "cv.pdf").unlink()
    assert HandoffWarning.DOCUMENT_MISSING in service.handoff(conn, job_id).warnings


def test_logs_carry_no_notes_answers_or_links(
    conn: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    job_id = _job(conn)
    answers.add_job_question(conn, job_id, "What is your notice period?")
    answers.save_answer(
        conn, question_text="What is your notice period?", value=NOTICE, policy=m.ReusePolicy.REUSE
    )
    service.start_application(conn, job_id, note=NOTE)
    service.handoff(conn, job_id)
    service.record_submission(
        conn, job_id, confirmation=m.SubmissionConfirmation.USER, note=NOTE, reference="REF-SECRET"
    )
    for secret in (NOTE, NOTICE, "REF-SECRET", "boards.example.test"):
        assert secret not in caplog.text
