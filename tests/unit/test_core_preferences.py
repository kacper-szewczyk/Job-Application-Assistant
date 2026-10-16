import pytest

from jaa.core.errors import PreferenceError
from jaa.core.models import JsonValue, ReusePolicy
from jaa.core.preferences import (
    QUESTIONS,
    QuestionKind,
    check_value,
    format_value,
    is_sensitive_question,
    parse_answer,
    question,
    reuse_policy,
)
from jaa.core.sensitivity import is_sensitive_key


def test_catalogue_covers_spec_preferences() -> None:
    keys = [q.key for q in QUESTIONS]
    assert len(keys) == len(set(keys))
    assert keys == [
        "search.target_roles",
        "search.seniority",
        "search.locations",
        "search.work_arrangement",
        "salary.range",
        "search.employment_types",
        "search.industries",
        "search.travel",
        "search.relocation",
        "sponsorship.required",
        "search.start_date",
        "answer_reuse.default",
        "answer_reuse.sensitive",
        "documents.paper_size",
    ]
    for q in QUESTIONS:
        has_choices = q.kind in (QuestionKind.CHOICE, QuestionKind.MULTI_CHOICE)
        assert bool(q.choices) == has_choices, q.key


def test_salary_and_sponsorship_are_sensitive() -> None:
    sensitive = {q.key for q in QUESTIONS if is_sensitive_question(q)}
    assert sensitive == {"salary.range", "sponsorship.required"}
    # The catalogue flag and the key-prefix rule agree, so exports and logs treat them alike.
    assert all(is_sensitive_key(k) for k in sensitive)


def test_sensitive_answers_cannot_default_to_silent_reuse() -> None:
    values = {c.value for c in question("answer_reuse.sensitive").choices}
    assert ReusePolicy.REUSE.value not in values


def test_unknown_key() -> None:
    with pytest.raises(PreferenceError, match="unknown preference"):
        question("search.favourite_colour")


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        (
            "search.target_roles",
            "Backend Engineer, platform engineer; Backend engineer",
            ["Backend Engineer", "platform engineer"],
        ),
        ("search.seniority", "senior, 1", ["entry", "senior"]),
        ("search.seniority", "Lead / staff", ["lead"]),
        ("search.work_arrangement", "On-site, remote", ["remote", "on_site"]),
        ("search.employment_types", "full time", ["full_time"]),
        ("search.travel", "2", "occasional"),
        ("search.relocation", "within country", "within_country"),
        ("sponsorship.required", "No", "no"),
        ("answer_reuse.default", "3", "always_ask"),
        (
            "salary.range",
            "70000-90000 EUR per year",
            {"min": 70000, "max": 90000, "currency": "EUR", "period": "year"},
        ),
        (
            "salary.range",
            "70k \N{EN DASH} 92.5k eur/yr",
            {"min": 70000, "max": 92500, "currency": "EUR", "period": "year"},
        ),
        (
            "salary.range",
            "100.000 PLN a month",
            {"min": 100000, "max": None, "currency": "PLN", "period": "month"},
        ),
        (
            "salary.range",
            "85 000 to 95,000 GBP",
            {"min": 85000, "max": 95000, "currency": "GBP", "period": "year"},
        ),
        ("search.start_date", "ASAP", {"type": "immediately"}),
        ("search.start_date", "2026-12-01", {"type": "date", "date": "2026-12-01"}),
        ("search.start_date", "1 month notice", {"type": "notice", "amount": 1, "unit": "months"}),
        ("search.start_date", "4 weeks", {"type": "notice", "amount": 4, "unit": "weeks"}),
    ],
)
def test_parse_answer(key: str, text: str, expected: JsonValue) -> None:
    q = question(key)
    value = parse_answer(q, text)
    assert value == expected
    check_value(q, value)


@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("search.target_roles", " , ; "),
        ("search.seniority", "senior, wizard"),
        ("search.travel", "4"),
        ("search.travel", "0"),
        ("salary.range", "lots"),
        ("salary.range", "90000-70000 EUR"),
        ("salary.range", "70000 EURO"),
        ("salary.range", "70000 EUR per fortnight"),
        ("search.start_date", "2026-02-30"),
        ("search.start_date", "soonish"),
        ("search.relocation", ""),
    ],
)
def test_parse_answer_rejects_bad_input(key: str, text: str) -> None:
    with pytest.raises(PreferenceError):
        parse_answer(question(key), text)


SECRET = "synthetic-secret-salary-text 123456"  # noqa: S105 - synthetic test value


def test_errors_do_not_echo_input() -> None:
    with pytest.raises(PreferenceError) as exc:
        parse_answer(question("salary.range"), SECRET)
    assert SECRET not in str(exc.value)
    assert "123456" not in str(exc.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("search.target_roles", []),
        ("search.target_roles", ["ok", 3]),
        ("search.target_roles", "Backend Engineer"),
        ("search.seniority", ["senior", "senior"]),
        ("search.seniority", ["wizard"]),
        ("search.travel", "sometimes"),
        ("search.travel", None),
        ("salary.range", {"min": 1, "max": None, "currency": "EUR"}),
        ("salary.range", {"min": -1, "max": None, "currency": "EUR", "period": "year"}),
        ("salary.range", {"min": 1.5, "max": None, "currency": "EUR", "period": "year"}),
        ("salary.range", {"min": True, "max": None, "currency": "EUR", "period": "year"}),
        ("salary.range", {"min": 10, "max": 5, "currency": "EUR", "period": "year"}),
        ("salary.range", {"min": 10, "max": None, "currency": "eur", "period": "year"}),
        ("search.start_date", {"type": "date", "date": "tomorrow"}),
        ("search.start_date", {"type": "notice", "amount": 2, "unit": "days"}),
        ("search.start_date", {"type": "immediately", "extra": 1}),
        ("search.start_date", {"type": "later"}),
    ],
)
def test_check_value_rejects_malformed(key: str, value: JsonValue) -> None:
    with pytest.raises(PreferenceError):
        check_value(question(key), value)


@pytest.mark.parametrize(
    ("key", "value", "shown"),
    [
        ("search.target_roles", ["A", "B"], "A, B"),
        ("search.seniority", ["entry", "lead"], "Entry / junior, Lead / staff"),
        ("search.travel", "none", "None"),
        (
            "salary.range",
            {"min": 70000, "max": 90000, "currency": "EUR", "period": "year"},
            "70,000\N{EN DASH}90,000 EUR per year",
        ),
        (
            "salary.range",
            {"min": 50, "max": None, "currency": "USD", "period": "hour"},
            "50+ USD per hour",
        ),
        ("search.start_date", {"type": "immediately"}, "Immediately"),
        ("search.start_date", {"type": "date", "date": "2026-12-01"}, "From 2026-12-01"),
        ("search.start_date", {"type": "notice", "amount": 1, "unit": "months"}, "1 month notice"),
        ("search.start_date", {"type": "notice", "amount": 4, "unit": "weeks"}, "4 weeks notice"),
    ],
)
def test_format_value(key: str, value: JsonValue, shown: str) -> None:
    assert format_value(question(key), value) == shown


def test_reuse_policy_defaults_are_conservative() -> None:
    assert reuse_policy({}, sensitive=False) is ReusePolicy.REUSE_WITH_REVIEW
    assert reuse_policy({}, sensitive=True) is ReusePolicy.ALWAYS_ASK
    answers: dict[str, JsonValue] = {
        "answer_reuse.default": "reuse",
        "answer_reuse.sensitive": "reuse_with_review",
    }
    assert reuse_policy(answers, sensitive=False) is ReusePolicy.REUSE
    assert reuse_policy(answers, sensitive=True) is ReusePolicy.REUSE_WITH_REVIEW
    # A value outside the allowed choices (e.g. hand-edited data) falls back to asking.
    assert reuse_policy({"answer_reuse.sensitive": "reuse"}, sensitive=True) is (
        ReusePolicy.ALWAYS_ASK
    )
