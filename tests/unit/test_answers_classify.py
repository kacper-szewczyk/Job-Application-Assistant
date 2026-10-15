from dataclasses import replace
from datetime import timedelta

import pytest

from jaa.answers.classify import (
    AnswerAction,
    AnswerClass,
    AnswerContext,
    Confidence,
    Note,
    QuestionInput,
    classify,
)
from jaa.answers.topics import find_topic, is_explicit_key, question_key, topic_for_key
from jaa.core.models import Answer, FactOrigin, FactState, JsonValue, ProfileFact, ReusePolicy
from tests.conftest import SYNTHETIC_EMAIL, T0

NOW = T0 + timedelta(days=10)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Will you now or in the future require visa sponsorship?", "sponsorship"),
        ("Are you legally authorized to work in Poland?", "work_authorization"),
        ("Czy posiadasz pozwolenie na pracę w Polsce?", "work_authorization"),
        ("What are your salary expectations?", "salary"),
        ("Oczekiwania finansowe (widełki wynagrodzenia)", "salary"),
        ("Have you ever been convicted of a criminal offence?", "criminal"),
        ("Do you have a disability?", "medical"),
        ("What is your gender?", "demographic"),
        ("Płeć", "demographic"),
        ("I consent to the processing of my personal data", "consent"),
        ("Wyrażam zgodę na przetwarzanie danych osobowych (RODO)", "consent"),
        ("I certify that the information above is true and complete", "legal"),
        ("Email address", "contact.email"),
        ("Phone number", "contact.phone"),
        ("LinkedIn profile URL", "contact.links.linkedin"),
        ("Full name", "identity.name"),
        ("What is your notice period?", "start_date"),
        ("Okres wypowiedzenia", "start_date"),
        ("Are you open to relocation?", "relocation"),
        ("Salary and start date", "salary"),  # sensitive wins
    ],
)
def test_topics(text: str, key: str) -> None:
    topic = find_topic(text)
    assert topic is not None
    assert topic.key == key


def test_unknown_questions_get_a_custom_key_per_wording() -> None:
    assert find_topic("Why do you want to build widgets?") is None
    key = question_key("Why do you want to build widgets?")
    assert key == "custom.why_do_you_want_to_build_widgets"
    assert question_key("why do you want to build WIDGETS") == key
    long_a = question_key("Describe " + "a very long question " * 10 + "one")
    long_b = question_key("Describe " + "a very long question " * 10 + "two")
    assert long_a != long_b
    assert len(long_a) < 90


def test_explicit_topics_are_keyed_per_wording() -> None:
    poland = question_key("Are you authorized to work in Poland?")
    us = question_key("Are you authorized to work in the US?")
    assert poland != us
    assert poland.startswith("work_authorization.")
    assert is_explicit_key(poland)
    assert topic_for_key(poland) == find_topic("Are you authorized to work in Poland?")
    # Ordinary topics share one key across wordings.
    assert question_key("Notice period?") == question_key("When can you start?") == "start_date"
    assert not is_explicit_key("start_date")


def _answer(
    text: str,
    value: str = "synthetic-answer",
    *,
    policy: ReusePolicy = ReusePolicy.REUSE,
    expires_in: timedelta | None = timedelta(days=100),
    confirmed: bool = True,
    id_: int = 7,
) -> Answer:
    return Answer(
        id=id_,
        question_key=question_key(text),
        question_text=text,
        value=value,
        reuse_policy=policy,
        is_sensitive=is_explicit_key(question_key(text)),
        created_at=T0,
        confirmed_at=T0 if confirmed else None,
        expires_at=T0 + expires_in if expires_in else None,
    )


def _fact(key: str, value: str, *, confirmed: bool = True, id_: int = 3) -> ProfileFact:
    return ProfileFact(
        id=id_,
        field_key=key,
        value_raw=value,
        value_normalized=value,
        state=FactState.CONFIRMED if confirmed else FactState.UNREVIEWED,
        origin=FactOrigin.IMPORT,
        created_at=T0,
        updated_at=T0,
        confirmed_at=T0 if confirmed else None,
    )


def _ctx(
    answers: list[Answer] = [],  # noqa: B006 - never mutated
    facts: list[ProfileFact] = [],  # noqa: B006 - never mutated
    preferences: dict[str, JsonValue] | None = None,
    default_policy: ReusePolicy = ReusePolicy.REUSE,
) -> AnswerContext:
    return AnswerContext(
        answers={a.question_key: a for a in answers},
        facts={f.field_key: f for f in facts},
        preferences=preferences or {},
        default_policy=default_policy,
        now=NOW,
    )


def _q(text: str, *options: str) -> QuestionInput:
    return QuestionInput(text=text, options=options)


def test_unknown_question_is_unanswered() -> None:
    r = classify(_q("Why widgets?"), _ctx())
    assert r.answer_class is AnswerClass.UNANSWERED
    assert r.action is AnswerAction.ASK
    assert r.value is None
    assert r.notes == (Note.UNKNOWN_QUESTION,)


def test_sensitive_question_without_answer_is_asked_even_with_a_preference() -> None:
    salary: JsonValue = {"min": 1000, "max": None, "currency": "EUR", "period": "month"}
    r = classify(
        _q("What are your salary expectations?"), _ctx(preferences={"salary.range": salary})
    )
    assert r.answer_class is AnswerClass.INFERRED_SUGGESTION
    assert r.action is AnswerAction.ASK
    assert r.explicit and r.sensitive
    assert r.value == "1,000+ EUR per month"
    assert r.provenance.preference_key == "salary.range"
    assert r.notes == (Note.SENSITIVE, Note.FROM_PREFERENCE)


def test_sponsorship_depends_gives_no_suggestion() -> None:
    r = classify(
        _q("Do you need visa sponsorship?"), _ctx(preferences={"sponsorship.required": "depends"})
    )
    assert r.answer_class is AnswerClass.UNANSWERED
    assert r.action is AnswerAction.ASK


def test_confirmed_fact_backs_the_answer_with_the_default_policy() -> None:
    fact = _fact("contact.email", SYNTHETIC_EMAIL)
    r = classify(_q("Email"), _ctx(facts=[fact]))
    assert r.answer_class is AnswerClass.PROFILE_BACKED
    assert r.action is AnswerAction.FILL
    assert r.confidence is Confidence.HIGH
    assert r.provenance.fact_ids == (3,)
    assert r.sensitive  # contact details are masked, but not explicit
    review = classify(_q("Email"), _ctx(facts=[fact], default_policy=ReusePolicy.REUSE_WITH_REVIEW))
    assert review.action is AnswerAction.REVIEW


def test_unreviewed_fact_is_only_a_suggestion() -> None:
    r = classify(_q("Phone"), _ctx(facts=[_fact("contact.phone", "+1 555", confirmed=False)]))
    assert r.answer_class is AnswerClass.INFERRED_SUGGESTION
    assert r.action is AnswerAction.ASK
    assert r.confidence is Confidence.LOW
    assert Note.UNREVIEWED_FACT in r.notes


@pytest.mark.parametrize(
    ("policy", "action"),
    [
        (ReusePolicy.REUSE, AnswerAction.FILL),
        (ReusePolicy.REUSE_WITH_REVIEW, AnswerAction.REVIEW),
        (ReusePolicy.ALWAYS_ASK, AnswerAction.ASK),
    ],
)
def test_saved_answer_follows_its_policy(policy: ReusePolicy, action: AnswerAction) -> None:
    text = "Why do you want to work here?"
    r = classify(_q(text), _ctx(answers=[_answer(text, policy=policy)]))
    assert r.answer_class is AnswerClass.REUSABLE_ANSWER
    assert r.action is action
    assert r.confidence is Confidence.HIGH
    assert r.provenance.answer_id == 7


def test_saved_answer_beats_profile_fact() -> None:
    answer = _answer("Email", "other@example.test")
    r = classify(_q("Email"), _ctx(answers=[answer], facts=[_fact("contact.email", "x@y.test")]))
    assert r.answer_class is AnswerClass.REUSABLE_ANSWER
    assert r.value == "other@example.test"


def test_different_wording_on_an_ordinary_topic_is_reviewed() -> None:
    r = classify(_q("When could you start?"), _ctx(answers=[_answer("Notice period?", "1 month")]))
    assert r.answer_class is AnswerClass.REUSABLE_ANSWER
    assert r.action is AnswerAction.REVIEW
    assert r.confidence is Confidence.MEDIUM
    assert Note.DIFFERENT_WORDING in r.notes


def test_expired_and_unconfirmed_answers_must_be_confirmed_again() -> None:
    text = "Why widgets?"
    expired = classify(_q(text), _ctx(answers=[_answer(text, expires_in=timedelta(days=5))]))
    assert expired.action is AnswerAction.ASK
    assert Note.EXPIRED in expired.notes
    assert expired.value == "synthetic-answer"  # shown so the user can confirm it
    unconfirmed = classify(_q(text), _ctx(answers=[_answer(text, confirmed=False)]))
    assert unconfirmed.action is AnswerAction.ASK
    assert Note.UNCONFIRMED in unconfirmed.notes
    never = classify(_q(text), _ctx(answers=[_answer(text, expires_in=None)]))
    assert never.action is AnswerAction.FILL


def test_value_outside_the_options_is_asked() -> None:
    text = "Are you open to relocation?"
    r = classify(_q(text, "Yes", "No"), _ctx(answers=[_answer(text, "maybe")]))
    assert r.action is AnswerAction.ASK
    assert Note.NOT_AN_OPTION in r.notes
    ok = classify(_q(text, "Yes", "No"), _ctx(answers=[_answer(text, "yes")]))
    assert ok.action is AnswerAction.FILL


def test_sensitive_saved_answer_is_asked_unless_configured_for_safe_reuse() -> None:
    text = "Are you authorized to work in Poland?"
    for policy in (ReusePolicy.ALWAYS_ASK, ReusePolicy.REUSE_WITH_REVIEW):
        r = classify(_q(text), _ctx(answers=[_answer(text, policy=policy)]))
        assert r.action is AnswerAction.ASK
        assert r.answer_class is AnswerClass.REUSABLE_ANSWER
        assert r.notes[0] is Note.SENSITIVE
    safe = classify(_q(text), _ctx(answers=[_answer(text, policy=ReusePolicy.REUSE)]))
    assert safe.action is AnswerAction.FILL
    assert Note.SAFE_REUSE in safe.notes
    expired = _answer(text, policy=ReusePolicy.REUSE, expires_in=timedelta(days=1))
    assert classify(_q(text), _ctx(answers=[expired])).action is AnswerAction.ASK


def test_sensitive_answer_to_another_wording_is_only_a_suggestion() -> None:
    saved = _answer("Are you authorized to work in Poland?", "yes", policy=ReusePolicy.REUSE)
    r = classify(_q("Are you authorized to work in the US?"), _ctx(answers=[saved]))
    assert r.answer_class is AnswerClass.INFERRED_SUGGESTION
    assert r.action is AnswerAction.ASK
    assert r.provenance.answer_id == 7
    assert Note.SAFE_REUSE not in r.notes
    assert {Note.SENSITIVE, Note.DIFFERENT_WORDING} <= set(r.notes)


def test_resolution_repr_hides_values() -> None:
    text = "Why widgets?"
    r = classify(_q(text), _ctx(answers=[replace(_answer(text), value="hidden-synthetic")]))
    assert "hidden-synthetic" not in repr(r)
