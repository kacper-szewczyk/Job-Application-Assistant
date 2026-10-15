"""Answer memory: saved answers to application questions and their use per job (ADR 0017).

`answers` is append-only (migration 0010): the newest row per question key is the current
answer, and saving, re-confirming or changing the reuse policy adds a row. `forget_answer`
deletes every row for a key, because the user must be able to remove their answers (SPEC §8).

Logging records ids and counts only, never question keys (they quote questions) or values.
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Final, cast

from jaa.answers.classify import (
    AnswerAction,
    AnswerContext,
    QuestionInput,
    Resolution,
    classify_all,
)
from jaa.answers.topics import is_explicit_key, is_sensitive_question_key, question_key
from jaa.core.clock import utcnow
from jaa.core.errors import AnswerError, NotFoundError
from jaa.core.models import (
    Answer,
    AnswerOrigin,
    FactState,
    JobQuestion,
    JsonValue,
    ProfileFact,
    ReusePolicy,
)
from jaa.core.preferences import reuse_policy
from jaa.storage.preferences import answered_values
from jaa.storage.repository import answers, job_questions, jobs, profile_facts

log = logging.getLogger(__name__)

# How long a saved answer is trusted before it has to be confirmed again. Sensitive answers
# (eligibility, salary, consent…) go stale sooner and must always have a review date.
REVIEW_PERIOD: Final = timedelta(days=365)
EXPLICIT_REVIEW_PERIOD: Final = timedelta(days=180)
MAX_ANSWER_CHARS: Final = 10_000
MAX_QUESTION_CHARS: Final = 2_000


class _Default(Enum):
    DEFAULT = "default"


DEFAULT: Final = _Default.DEFAULT
type Expiry = datetime | _Default | None


def default_expiry(key: str, at: datetime) -> datetime:
    return at + (EXPLICIT_REVIEW_PERIOD if is_explicit_key(key) else REVIEW_PERIOD)


def _resolve_expiry(key: str, expires_at: Expiry, at: datetime) -> datetime | None:
    if isinstance(expires_at, _Default):
        return default_expiry(key, at)
    if expires_at is None and is_explicit_key(key):
        raise AnswerError("answers to sensitive questions need a review date")
    if expires_at is not None and expires_at <= at:
        raise AnswerError("the review date must be in the future")
    return expires_at


def current_answers(conn: sqlite3.Connection) -> dict[str, Answer]:
    """The current (newest) answer per question key."""
    current: dict[str, Answer] = {}
    for answer in answers.list(conn):
        current[answer.question_key] = answer  # ordered by id, so the newest wins
    return current


def current_answer(conn: sqlite3.Connection, key: str) -> Answer:
    found = answers.list(conn, question_key=key)
    if not found:
        raise NotFoundError(f"no saved answer for {key!r}")
    return found[-1]


def answer_history(conn: sqlite3.Connection, key: str) -> list[Answer]:
    return answers.list(conn, question_key=key)


def answer_fact_ids(conn: sqlite3.Connection, answer_id: int) -> tuple[int, ...]:
    rows = conn.execute(
        "SELECT fact_id FROM answer_sources WHERE answer_id = ? ORDER BY fact_id", (answer_id,)
    ).fetchall()
    return tuple(int(r[0]) for r in rows)


def _insert(conn: sqlite3.Connection, answer: Answer, fact_ids: Sequence[int]) -> Answer:
    stored = answers.insert(conn, answer)
    conn.executemany(
        "INSERT INTO answer_sources (answer_id, fact_id) VALUES (?, ?)",
        [(stored.id, f) for f in sorted(set(fact_ids))],
    )
    return stored


def save_answer(
    conn: sqlite3.Connection,
    *,
    question_text: str,
    value: str,
    key: str | None = None,
    policy: ReusePolicy | None = None,
    expires_at: Expiry = DEFAULT,
    origin: AnswerOrigin = AnswerOrigin.USER,
    fact_ids: Sequence[int] = (),
    preference_key: str | None = None,
    source_answer_id: int | None = None,
    job_id: int | None = None,
    at: datetime | None = None,
) -> Answer:
    """Save the user's answer to a question as its new current answer.

    The value is the user's own (typed, or a suggestion the user accepted); saving it confirms
    it. Without `policy` the answer-reuse preferences decide (ADR 0007), so a sensitive answer
    is reused without asking only when the caller passes `ReusePolicy.REUSE` on the user's
    explicit request. `fact_ids`, `preference_key` and `source_answer_id` record where an
    accepted value came from.
    """
    at = at or utcnow()
    question_text = question_text.strip()
    value = value.strip()
    if not question_text or len(question_text) > MAX_QUESTION_CHARS:
        raise AnswerError(f"the question must be 1-{MAX_QUESTION_CHARS} characters")
    if not value or len(value) > MAX_ANSWER_CHARS:
        raise AnswerError(f"the answer must be 1-{MAX_ANSWER_CHARS} characters")
    key = key or question_key(question_text)
    with conn:
        if job_id is not None:
            jobs.get(conn, job_id)
        for fact_id in fact_ids:
            profile_facts.get(conn, fact_id)
        if source_answer_id is not None:
            answers.get(conn, source_answer_id)
        explicit = is_explicit_key(key)
        stored = _insert(
            conn,
            Answer(
                question_key=key,
                question_text=question_text,
                value=value,
                reuse_policy=policy or reuse_policy(answered_values(conn), sensitive=explicit),
                is_sensitive=is_sensitive_question_key(key),
                created_at=at,
                confirmed_at=at,
                expires_at=_resolve_expiry(key, expires_at, at),
                origin=origin,
                source_preference_key=preference_key,
                source_answer_id=source_answer_id,
                job_id=job_id,
            ),
            fact_ids,
        )
    log.info("saved answer id=%s origin=%s", stored.id, origin)
    return stored


def _revise(conn: sqlite3.Connection, key: str, change: Callable[[Answer], Answer]) -> Answer:
    """Add a row that copies the current answer (and its sources) with `change` applied."""
    with conn:
        current = current_answer(conn, key)
        if current.id is None:
            raise NotFoundError("answer has no id")
        stored = _insert(conn, replace(change(current), id=None), answer_fact_ids(conn, current.id))
    log.info("revised answer id=%s origin=%s", stored.id, stored.origin)
    return stored


def reconfirm_answer(
    conn: sqlite3.Connection, key: str, *, expires_at: Expiry = DEFAULT, at: datetime | None = None
) -> Answer:
    """Confirm the current value again, which starts a new review period."""
    when = at or utcnow()
    expiry = _resolve_expiry(key, expires_at, when)
    return _revise(
        conn,
        key,
        lambda a: replace(
            a,
            origin=AnswerOrigin.RECONFIRMED,
            created_at=when,
            confirmed_at=when,
            expires_at=expiry,
        ),
    )


def set_reuse_policy(
    conn: sqlite3.Connection, key: str, policy: ReusePolicy, *, at: datetime | None = None
) -> Answer:
    """Change how the current answer is reused. The value and review date stay the same."""
    when = at or utcnow()
    return _revise(
        conn,
        key,
        lambda a: replace(
            a, origin=AnswerOrigin.POLICY_CHANGED, created_at=when, reuse_policy=policy
        ),
    )


def forget_answer(conn: sqlite3.Connection, key: str) -> int:
    """Delete every saved version of an answer. Returns the number of rows removed."""
    with conn:
        removed = conn.execute("DELETE FROM answers WHERE question_key = ?", (key,)).rowcount
    if removed == 0:
        raise NotFoundError(f"no saved answer for {key!r}")
    log.info("forgot answer versions=%s", removed)
    return removed


def add_job_question(
    conn: sqlite3.Connection,
    job_id: int,
    label: str,
    *,
    required: bool = False,
    at: datetime | None = None,
) -> JobQuestion:
    """Record a question from the employer's form, for sources that do not publish them."""
    label = " ".join(label.split())
    if not label or len(label) > MAX_QUESTION_CHARS:
        raise AnswerError(f"the question must be 1-{MAX_QUESTION_CHARS} characters")
    with conn:
        jobs.get(conn, job_id)
        stored = job_questions.insert(
            conn,
            JobQuestion(job_id=job_id, label=label, required=required, created_at=at or utcnow()),
        )
    log.info("added question id=%s job=%s", stored.id, job_id)
    return stored


def remove_job_question(conn: sqlite3.Connection, job_id: int, question_id: int) -> None:
    with conn:
        removed = conn.execute(
            "DELETE FROM job_questions WHERE id = ? AND job_id = ?", (question_id, job_id)
        ).rowcount
    if removed == 0:
        raise NotFoundError(f"job #{job_id} has no question #{question_id}")


def _listing_questions(requirements: JsonValue) -> list[QuestionInput]:
    if not isinstance(requirements, dict):
        return []
    found: list[QuestionInput] = []
    for item in cast(list[JsonValue], requirements.get("questions") or []):
        if not isinstance(item, dict):
            continue
        label, key, options = item.get("label"), item.get("key"), item.get("options")
        if not isinstance(label, str) or not label.strip():
            continue
        found.append(
            QuestionInput(
                text=label.strip(),
                required=item.get("required") is True,
                options=tuple(o for o in cast(list[JsonValue], options or []) if isinstance(o, str))
                if isinstance(options, list)
                else (),
                origin="listing",
                origin_id=key if isinstance(key, str) else None,
            )
        )
    return found


def job_question_inputs(conn: sqlite3.Connection, job_id: int) -> list[QuestionInput]:
    """The job's questions: those its listing published, then those the user added."""
    job = jobs.get(conn, job_id)
    return _listing_questions(job.requirements) + [
        QuestionInput(text=q.label, required=q.required, origin="user", origin_id=str(q.id))
        for q in job_questions.list(conn, job_id=job_id)
    ]


def _facts_by_key(conn: sqlite3.Connection) -> dict[str, ProfileFact]:
    """The confirmed fact per key, else the newest unreviewed one."""
    found: dict[str, ProfileFact] = {}
    for fact in profile_facts.list(conn, state=FactState.UNREVIEWED):
        found[fact.field_key] = fact
    for fact in profile_facts.list(conn, state=FactState.CONFIRMED):
        found[fact.field_key] = fact
    return found


def answer_context(conn: sqlite3.Connection, now: datetime | None = None) -> AnswerContext:
    preferences = answered_values(conn)
    return AnswerContext(
        answers=current_answers(conn),
        facts=_facts_by_key(conn),
        preferences=preferences,
        default_policy=reuse_policy(preferences, sensitive=False),
        now=now or utcnow(),
    )


def prepare_answers(
    conn: sqlite3.Connection, job_id: int, *, now: datetime | None = None
) -> list[Resolution]:
    """Classify every question of a job and say which answers may be used."""
    questions = job_question_inputs(conn, job_id)
    resolutions = classify_all(questions, answer_context(conn, now))
    log.info(
        "prepared answers job=%s questions=%s ask=%s",
        job_id,
        len(resolutions),
        sum(r.action is AnswerAction.ASK for r in resolutions),
    )
    return resolutions
