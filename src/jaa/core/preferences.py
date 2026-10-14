"""Onboarding questions for search and application preferences (SPEC §4 step 5, ADR 0007).

Pure: the question catalogue, parsing user text into normalized values, checking values that
arrive already structured (e.g. over MCP), and formatting values for display. Error messages
never echo the user's input, so they are safe to log.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final, cast

from jaa.core.errors import PreferenceError
from jaa.core.models import JsonValue, ReusePolicy
from jaa.core.sensitivity import is_sensitive_key


class QuestionKind(StrEnum):
    TEXT_LIST = "text_list"
    CHOICE = "choice"
    MULTI_CHOICE = "multi_choice"
    SALARY_RANGE = "salary_range"
    START_DATE = "start_date"


@dataclass(frozen=True, slots=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Question:
    key: str
    prompt: str
    kind: QuestionKind
    hint: str
    sensitive: bool = False
    choices: tuple[Choice, ...] = ()

    def choice(self, value: str) -> Choice | None:
        return next((c for c in self.choices if c.value == value), None)


SALARY_PERIODS: Final = ("year", "month", "day", "hour")
NOTICE_UNITS: Final = ("weeks", "months")

QUESTIONS: Final[tuple[Question, ...]] = (
    Question(
        key="search.target_roles",
        prompt="Which roles are you looking for?",
        kind=QuestionKind.TEXT_LIST,
        hint="comma-separated, e.g. Backend Engineer, Platform Engineer",
    ),
    Question(
        key="search.seniority",
        prompt="Which seniority levels should jobs have?",
        kind=QuestionKind.MULTI_CHOICE,
        hint="one or more numbers or names, comma-separated",
        choices=(
            Choice("entry", "Entry / junior"),
            Choice("mid", "Mid-level"),
            Choice("senior", "Senior"),
            Choice("lead", "Lead / staff"),
            Choice("principal", "Principal"),
            Choice("manager", "Manager"),
            Choice("director", "Director"),
            Choice("executive", "Executive"),
        ),
    ),
    Question(
        key="search.locations",
        prompt="Where do you want to work?",
        kind=QuestionKind.TEXT_LIST,
        hint="cities, countries or regions, comma-separated, e.g. Berlin, Netherlands, EU",
    ),
    Question(
        key="search.work_arrangement",
        prompt="Which work arrangements are acceptable?",
        kind=QuestionKind.MULTI_CHOICE,
        hint="one or more numbers or names, comma-separated",
        choices=(
            Choice("remote", "Remote"),
            Choice("hybrid", "Hybrid"),
            Choice("on_site", "On-site"),
        ),
    ),
    Question(
        key="salary.range",
        prompt="What salary range are you looking for?",
        kind=QuestionKind.SALARY_RANGE,
        hint="MIN[-MAX] CURRENCY [per year|month|day|hour], e.g. 70000-90000 EUR per year",
        sensitive=True,
    ),
    Question(
        key="search.employment_types",
        prompt="Which employment types are acceptable?",
        kind=QuestionKind.MULTI_CHOICE,
        hint="one or more numbers or names, comma-separated",
        choices=(
            Choice("full_time", "Full-time"),
            Choice("part_time", "Part-time"),
            Choice("contract", "Contract"),
            Choice("freelance", "Freelance"),
            Choice("temporary", "Temporary"),
            Choice("internship", "Internship"),
        ),
    ),
    Question(
        key="search.industries",
        prompt="Which industries interest you?",
        kind=QuestionKind.TEXT_LIST,
        hint="comma-separated, e.g. healthcare, climate, developer tools",
    ),
    Question(
        key="search.travel",
        prompt="How much work travel is acceptable?",
        kind=QuestionKind.CHOICE,
        hint="a number or name",
        choices=(
            Choice("none", "None"),
            Choice("occasional", "Occasional (up to about 25%)"),
            Choice("frequent", "Frequent (more than 25%)"),
        ),
    ),
    Question(
        key="search.relocation",
        prompt="Would you relocate for a job?",
        kind=QuestionKind.CHOICE,
        hint="a number or name",
        choices=(
            Choice("no", "No"),
            Choice("within_country", "Within my country"),
            Choice("international", "Internationally"),
        ),
    ),
    Question(
        key="sponsorship.required",
        prompt="Do you need visa sponsorship to work in your target locations?",
        kind=QuestionKind.CHOICE,
        hint="a number or name",
        sensitive=True,
        choices=(
            Choice("no", "No"),
            Choice("yes", "Yes"),
            Choice("depends", "Depends on the country (ask me per application)"),
        ),
    ),
    Question(
        key="search.start_date",
        prompt="When could you start?",
        kind=QuestionKind.START_DATE,
        hint="immediately, a date (YYYY-MM-DD), or a notice period such as 4 weeks or 3 months",
    ),
    Question(
        key="answer_reuse.default",
        prompt="How should saved answers to ordinary application questions be reused?",
        kind=QuestionKind.CHOICE,
        hint="a number or name",
        choices=(
            Choice(
                ReusePolicy.REUSE_WITH_REVIEW.value, "Fill them in, and show them to me to review"
            ),
            Choice(ReusePolicy.REUSE.value, "Fill them in without asking"),
            Choice(ReusePolicy.ALWAYS_ASK.value, "Ask me every time"),
        ),
    ),
    Question(
        key="answer_reuse.sensitive",
        prompt=(
            "How should saved answers to sensitive questions (work authorization, salary, "
            "demographics, legal attestations) be reused?"
        ),
        kind=QuestionKind.CHOICE,
        hint="a number or name",
        # Never "reuse without asking" for sensitive answers as a blanket rule (SPEC §4):
        # a safe reusable answer has to be configured per answer.
        choices=(
            Choice(ReusePolicy.ALWAYS_ASK.value, "Ask me every time"),
            Choice(
                ReusePolicy.REUSE_WITH_REVIEW.value, "Suggest my saved answer, and I confirm it"
            ),
        ),
    ),
)

_BY_KEY: Final = {q.key: q for q in QUESTIONS}

# Used when the user skipped the reuse questions.
DEFAULT_REUSE_POLICY: Final = ReusePolicy.REUSE_WITH_REVIEW
DEFAULT_SENSITIVE_REUSE_POLICY: Final = ReusePolicy.ALWAYS_ASK


def question(key: str) -> Question:
    """The question for a preference key; raises `PreferenceError` for unknown keys."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise PreferenceError(f"unknown preference {key!r}") from None


def is_sensitive_question(q: Question) -> bool:
    return q.sensitive or is_sensitive_key(q.key)


def _token(text: str) -> str:
    return re.sub(r"[\s\-/]+", "_", text.strip().casefold())


def _split(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]", text) if part.strip()]


def _parse_choice(q: Question, text: str) -> str:
    token = _token(text)
    if token.isdigit() and 1 <= int(token) <= len(q.choices):
        return q.choices[int(token) - 1].value
    for c in q.choices:
        if token in (c.value, _token(c.label)):
            return c.value
    options = ", ".join(c.value for c in q.choices)
    raise PreferenceError(f"{q.key}: expected one of {options} (or its number)")


def _parse_text_list(q: Question, text: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for item in _split(text):
        if item.casefold() not in seen:
            seen.add(item.casefold())
            items.append(item)
    if not items:
        raise PreferenceError(f"{q.key}: enter at least one item")
    return items


def _parse_multi_choice(q: Question, text: str) -> list[str]:
    picked = {_parse_choice(q, part) for part in _split(text)}
    if not picked:
        raise PreferenceError(f"{q.key}: choose at least one option")
    return [c.value for c in q.choices if c.value in picked]


_AMOUNT = r"\d+(?:\.\d+)?k|\d{1,3}(?:[,.' _]\d{3})+|\d+"
_SALARY = re.compile(
    rf"(?P<min>{_AMOUNT})(?:\s*(?:-|\u2013|to)\s*(?P<max>{_AMOUNT}))?\s*(?P<currency>[a-z]{{3}})"
    r"(?:\s*(?:/|per|a)\s*(?P<period>[a-z]+))?",
    re.IGNORECASE,
)
_PERIOD_ALIASES: Final = {
    "year": "year",
    "yr": "year",
    "y": "year",
    "annum": "year",
    "annual": "year",
    "yearly": "year",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "day": "day",
    "daily": "day",
    "hour": "hour",
    "hr": "hour",
    "h": "hour",
    "hourly": "hour",
}


def _amount(text: str) -> int:
    if text.casefold().endswith("k"):
        return round(float(text[:-1]) * 1000)
    return int(re.sub(r"[,.' _]", "", text))


def _parse_salary(q: Question, text: str) -> dict[str, JsonValue]:
    match = _SALARY.fullmatch(text.strip())
    if match is None:
        raise PreferenceError(f"{q.key}: expected {q.hint}")
    period = _PERIOD_ALIASES.get((match["period"] or "year").casefold())
    if period is None:
        raise PreferenceError(f"{q.key}: period must be one of {', '.join(SALARY_PERIODS)}")
    value: dict[str, JsonValue] = {
        "min": _amount(match["min"]),
        "max": _amount(match["max"]) if match["max"] else None,
        "currency": match["currency"].upper(),
        "period": period,
    }
    _check_salary(q, value)
    return value


_NOTICE = re.compile(r"(?P<amount>\d+)\s*(?P<unit>weeks?|months?)(?:\s+notice)?", re.IGNORECASE)


def _parse_start_date(q: Question, text: str) -> dict[str, JsonValue]:
    token = text.strip().casefold()
    if token in ("immediately", "now", "asap"):
        return {"type": "immediately"}
    if notice := _NOTICE.fullmatch(token):
        unit = notice["unit"] if notice["unit"].endswith("s") else notice["unit"] + "s"
        value: dict[str, JsonValue] = {
            "type": "notice",
            "amount": int(notice["amount"]),
            "unit": unit,
        }
        _check_start_date(q, value)
        return value
    try:
        return {"type": "date", "date": date.fromisoformat(token).isoformat()}
    except ValueError:
        raise PreferenceError(f"{q.key}: expected {q.hint}") from None


def parse_answer(q: Question, text: str) -> JsonValue:
    """Turn the user's typed answer into the normalized value stored for the question."""
    if not text.strip():
        raise PreferenceError(f"{q.key}: answer is empty")
    match q.kind:
        case QuestionKind.TEXT_LIST:
            return list[JsonValue](_parse_text_list(q, text))
        case QuestionKind.CHOICE:
            return _parse_choice(q, text)
        case QuestionKind.MULTI_CHOICE:
            return list[JsonValue](_parse_multi_choice(q, text))
        case QuestionKind.SALARY_RANGE:
            return _parse_salary(q, text)
        case QuestionKind.START_DATE:
            return _parse_start_date(q, text)


def _fields(q: Question, value: JsonValue, required: set[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != required:
        raise PreferenceError(f"{q.key}: expected an object with keys {sorted(required)}")
    return value


def _check_salary(q: Question, value: JsonValue) -> None:
    v = _fields(q, value, {"min", "max", "currency", "period"})
    low, high = v["min"], v["max"]
    if type(low) is not int or low < 0 or (high is not None and type(high) is not int):
        raise PreferenceError(f"{q.key}: amounts must be non-negative whole numbers")
    if high is not None and high < low:
        raise PreferenceError(f"{q.key}: maximum is below minimum")
    currency = v["currency"]
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise PreferenceError(f"{q.key}: currency must be a 3-letter code such as EUR")
    if v["period"] not in SALARY_PERIODS:
        raise PreferenceError(f"{q.key}: period must be one of {', '.join(SALARY_PERIODS)}")


def _check_start_date(q: Question, value: JsonValue) -> None:
    if not isinstance(value, dict):
        raise PreferenceError(f"{q.key}: expected an object")
    match value.get("type"):
        case "immediately":
            _fields(q, value, {"type"})
        case "date":
            day = _fields(q, value, {"type", "date"})["date"]
            try:
                date.fromisoformat(cast(str, day))
            except (TypeError, ValueError):
                raise PreferenceError(f"{q.key}: date must be YYYY-MM-DD") from None
        case "notice":
            v = _fields(q, value, {"type", "amount", "unit"})
            amount = v["amount"]
            if type(amount) is not int or amount < 0 or v["unit"] not in NOTICE_UNITS:
                raise PreferenceError(f"{q.key}: notice must be a whole number of weeks or months")
        case _:
            raise PreferenceError(f"{q.key}: type must be immediately, date or notice")


def check_value(q: Question, value: JsonValue) -> None:
    """Raise `PreferenceError` unless `value` has the normalized shape for the question."""
    match q.kind:
        case QuestionKind.TEXT_LIST:
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(v, str) and v.strip() for v in value)
            ):
                raise PreferenceError(f"{q.key}: expected a non-empty list of text")
        case QuestionKind.CHOICE:
            if not isinstance(value, str) or q.choice(value) is None:
                options = ", ".join(c.value for c in q.choices)
                raise PreferenceError(f"{q.key}: expected one of {options}")
        case QuestionKind.MULTI_CHOICE:
            if (
                not isinstance(value, list)
                or not value
                or len(set(map(str, value))) != len(value)
                or not all(isinstance(v, str) and q.choice(v) for v in value)
            ):
                options = ", ".join(c.value for c in q.choices)
                raise PreferenceError(f"{q.key}: expected distinct values from {options}")
        case QuestionKind.SALARY_RANGE:
            _check_salary(q, value)
        case QuestionKind.START_DATE:
            _check_start_date(q, value)


def format_value(q: Question, value: JsonValue) -> str:
    """Human-readable form of a stored value, for terminal display (never for logs)."""
    match q.kind:
        case QuestionKind.TEXT_LIST:
            return ", ".join(map(str, cast(list[JsonValue], value)))
        case QuestionKind.CHOICE:
            c = q.choice(cast(str, value))
            return c.label if c else str(value)
        case QuestionKind.MULTI_CHOICE:
            labels = [
                (c.label if (c := q.choice(str(v))) else str(v)) for v in cast(list[str], value)
            ]
            return ", ".join(labels)
        case QuestionKind.SALARY_RANGE:
            v = cast(dict[str, JsonValue], value)
            amount = f"{v['min']:,}" + (f"\u2013{v['max']:,}" if v["max"] is not None else "+")
            return f"{amount} {v['currency']} per {v['period']}"
        case QuestionKind.START_DATE:
            v = cast(dict[str, JsonValue], value)
            if v["type"] == "immediately":
                return "Immediately"
            if v["type"] == "date":
                return f"From {v['date']}"
            unit = str(v["unit"]).removesuffix("s") if v["amount"] == 1 else v["unit"]
            return f"{v['amount']} {unit} notice"


def reuse_policy(answers: Mapping[str, JsonValue], *, sensitive: bool) -> ReusePolicy:
    """Default reuse policy for a newly saved answer, from the answer-reuse preferences.

    `answers` maps preference keys to answered values; skipped or missing keys fall back to
    the conservative defaults. A sensitive answer is never reused without asking by default.
    """
    key = "answer_reuse.sensitive" if sensitive else "answer_reuse.default"
    fallback = DEFAULT_SENSITIVE_REUSE_POLICY if sensitive else DEFAULT_REUSE_POLICY
    value = answers.get(key)
    q = question(key)
    if isinstance(value, str) and q.choice(value) is not None:
        return ReusePolicy(value)
    return fallback
