import logging
import sqlite3
from datetime import timedelta

import pytest

from jaa.answers.classify import AnswerAction, AnswerClass, Note
from jaa.answers.topics import question_key
from jaa.core import models as m
from jaa.core.errors import AnswerError, NotFoundError
from jaa.storage import answers as service
from jaa.storage import preferences as prefs
from jaa.storage.jobs import add_job
from jaa.storage.repository import profile_facts
from tests.conftest import SYNTHETIC_ANSWER, SYNTHETIC_EMAIL, T0, ident

AUTH = "Are you authorized to work in Poland?"
WHY = "Why do you want to build widgets?"


def _job(conn: sqlite3.Connection, questions: list[m.JsonValue] | None = None) -> int:
    job = add_job(
        conn,
        m.Job(
            source="example_board",
            source_job_id="w-1",
            canonical_url="https://boards.example.test/jobs/1",
            employer="Example Co",
            title="Widget Engineer",
            first_seen_at=T0,
            last_seen_at=T0,
            requirements={"questions": questions} if questions else {},
            status_changed_at=T0,
        ),
    )
    return ident(job.id)


def _email_fact(conn: sqlite3.Connection) -> int:
    fact = profile_facts.insert(
        conn,
        m.ProfileFact(
            field_key="contact.email",
            value_raw=SYNTHETIC_EMAIL,
            value_normalized=SYNTHETIC_EMAIL,
            state=m.FactState.CONFIRMED,
            origin=m.FactOrigin.USER,
            is_sensitive=True,
            created_at=T0,
            updated_at=T0,
            confirmed_at=T0,
        ),
    )
    return ident(fact.id)


def test_save_uses_reuse_preferences_and_default_review_dates(conn: sqlite3.Connection) -> None:
    ordinary = service.save_answer(conn, question_text=WHY, value="I like widgets", at=T0)
    assert ordinary.question_key == "custom.why_do_you_want_to_build_widgets"
    assert ordinary.reuse_policy is m.ReusePolicy.REUSE_WITH_REVIEW
    assert ordinary.confirmed_at == T0
    assert ordinary.expires_at == T0 + service.REVIEW_PERIOD
    assert not ordinary.is_sensitive

    sensitive = service.save_answer(conn, question_text=AUTH, value=SYNTHETIC_ANSWER, at=T0)
    assert sensitive.reuse_policy is m.ReusePolicy.ALWAYS_ASK
    assert sensitive.expires_at == T0 + service.EXPLICIT_REVIEW_PERIOD
    assert sensitive.is_sensitive

    prefs.set_preference(conn, "answer_reuse.default", "reuse")
    prefs.set_preference(conn, "answer_reuse.sensitive", "reuse_with_review")
    again = service.save_answer(conn, question_text=WHY, value="Widgets!", at=T0)
    assert again.reuse_policy is m.ReusePolicy.REUSE
    sensitive = service.save_answer(conn, question_text=AUTH, value=SYNTHETIC_ANSWER, at=T0)
    assert sensitive.reuse_policy is m.ReusePolicy.REUSE_WITH_REVIEW


def test_sensitive_answers_always_have_a_future_review_date(conn: sqlite3.Connection) -> None:
    with pytest.raises(AnswerError, match="review date"):
        service.save_answer(conn, question_text=AUTH, value="yes", expires_at=None)
    with pytest.raises(AnswerError, match="future"):
        service.save_answer(conn, question_text=WHY, value="x", expires_at=T0, at=T0)
    never = service.save_answer(conn, question_text=WHY, value="x", expires_at=None)
    assert never.expires_at is None
    with pytest.raises(AnswerError):
        service.save_answer(conn, question_text=WHY, value="  ")


def test_answers_are_append_only_and_versions_keep_sources(conn: sqlite3.Connection) -> None:
    fact = _email_fact(conn)
    first = service.save_answer(
        conn,
        question_text="Email",
        value=SYNTHETIC_EMAIL,
        origin=m.AnswerOrigin.ACCEPTED,
        fact_ids=[fact],
        at=T0,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE answers SET value = '\"x\"' WHERE id = ?", (first.id,))
    later = T0 + timedelta(days=400)
    reconfirmed = service.reconfirm_answer(conn, "contact.email", at=later)
    assert reconfirmed.origin is m.AnswerOrigin.RECONFIRMED
    assert reconfirmed.confirmed_at == later
    assert reconfirmed.expires_at == later + service.REVIEW_PERIOD
    changed = service.set_reuse_policy(conn, "contact.email", m.ReusePolicy.ALWAYS_ASK, at=later)
    assert changed.origin is m.AnswerOrigin.POLICY_CHANGED
    assert changed.expires_at == reconfirmed.expires_at
    history = service.answer_history(conn, "contact.email")
    assert [a.origin for a in history] == [
        m.AnswerOrigin.ACCEPTED,
        m.AnswerOrigin.RECONFIRMED,
        m.AnswerOrigin.POLICY_CHANGED,
    ]
    assert all(service.answer_fact_ids(conn, ident(a.id)) == (fact,) for a in history)
    assert service.current_answers(conn)["contact.email"] == changed


def test_forget_deletes_every_version(conn: sqlite3.Connection) -> None:
    service.save_answer(conn, question_text=WHY, value="a")
    service.save_answer(conn, question_text=WHY, value="b")
    assert service.forget_answer(conn, question_key(WHY)) == 2
    assert service.current_answers(conn) == {}
    with pytest.raises(NotFoundError):
        service.forget_answer(conn, question_key(WHY))
    with pytest.raises(NotFoundError):
        service.reconfirm_answer(conn, question_key(WHY))


def test_unknown_job_or_fact_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(NotFoundError):
        service.save_answer(conn, question_text=WHY, value="a", job_id=99)
    with pytest.raises(NotFoundError):
        service.save_answer(conn, question_text=WHY, value="a", fact_ids=[99])
    assert service.current_answers(conn) == {}


def test_job_questions_from_listing_and_user(conn: sqlite3.Connection) -> None:
    job = _job(
        conn,
        [
            {
                "key": "q1",
                "label": "Are you open to relocation?",
                "kind": "single_select",
                "required": True,
                "options": ["Yes", "No"],
            },
            {"key": "q2", "label": "  ", "kind": "text", "required": False, "options": []},
        ],
    )
    added = service.add_job_question(conn, job, "  Email   address ", required=True)
    assert added.label == "Email address"
    found = service.job_question_inputs(conn, job)
    assert [(q.text, q.origin, q.required, q.options) for q in found] == [
        ("Are you open to relocation?", "listing", True, ("Yes", "No")),
        ("Email address", "user", True, ()),
    ]
    service.remove_job_question(conn, job, ident(added.id))
    with pytest.raises(NotFoundError):
        service.remove_job_question(conn, job, ident(added.id))
    with pytest.raises(NotFoundError):
        service.add_job_question(conn, 99, "Email")


def test_prepare_answers_end_to_end(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    job = _job(conn)
    fact = _email_fact(conn)
    for text in ("Email", AUTH, WHY, "What are your salary expectations?"):
        service.add_job_question(conn, job, text)
    service.save_answer(conn, question_text=AUTH, value=SYNTHETIC_ANSWER, at=T0)
    prefs.set_preference(conn, "answer_reuse.default", "reuse")

    with caplog.at_level(logging.DEBUG):
        email, auth, why, salary = service.prepare_answers(conn, job, now=T0 + timedelta(days=1))
    assert (email.answer_class, email.action) == (AnswerClass.PROFILE_BACKED, AnswerAction.FILL)
    assert email.provenance.fact_ids == (fact,)
    assert (auth.answer_class, auth.action) == (AnswerClass.REUSABLE_ANSWER, AnswerAction.ASK)
    assert Note.SENSITIVE in auth.notes
    assert (why.answer_class, why.action) == (AnswerClass.UNANSWERED, AnswerAction.ASK)
    assert (salary.answer_class, salary.action) == (AnswerClass.UNANSWERED, AnswerAction.ASK)

    expired = service.prepare_answers(conn, job, now=T0 + timedelta(days=200))[1]
    assert Note.EXPIRED in expired.notes
    for text in (SYNTHETIC_ANSWER, SYNTHETIC_EMAIL, "authorized", "salary"):
        assert text not in caplog.text


def test_deleting_a_job_or_a_source_answer_keeps_answers(conn: sqlite3.Connection) -> None:
    job = _job(conn)
    poland = service.save_answer(conn, question_text=AUTH, value="yes", job_id=job)
    us = service.save_answer(
        conn,
        question_text="Are you authorized to work in the US?",
        value="yes",
        origin=m.AnswerOrigin.ACCEPTED,
        source_answer_id=poland.id,
    )
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job,))
    service.forget_answer(conn, poland.question_key)
    (kept,) = service.current_answers(conn).values()
    assert kept.id == us.id
    assert kept.source_answer_id is None
    with pytest.raises(NotFoundError):
        service.save_answer(conn, question_text=WHY, value="a", source_answer_id=999)
