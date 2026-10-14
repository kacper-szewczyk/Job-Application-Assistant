"""What a job asks for, read from a stored job (T12, ADR 0015). Pure functions.

Two kinds of input, both from the source, never from the candidate:

* structured details the source lists (`jaa.core.job_details`): skills, languages, experience
  level, work arrangement and salary, plus the application questions;
* the description, split into sections by their headings. Headings are recognized in English
  and Polish; lines under a requirements heading become required items, under a nice-to-have
  heading preferred items, and under a responsibilities heading responsibilities.

A description without recognizable headings yields no free-text requirements: guessing which
lines are requirements would make gaps up.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

from jaa.core.dedupe import fold
from jaa.core.job_details import JobDetails, SalaryRange, WorkArrangement
from jaa.core.models import Job, JsonObject, JsonValue


class Importance(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"


class RequirementKind(StrEnum):
    SKILL = "skill"
    LANGUAGE = "language"
    # A line of the description, compared by wording.
    TEXT = "text"


class RequirementOrigin(StrEnum):
    # The source's structured fields.
    LISTED = "listed"
    DESCRIPTION = "description"


@dataclass(frozen=True, slots=True, kw_only=True)
class Requirement:
    text: str
    kind: RequirementKind
    importance: Importance
    origin: RequirementOrigin
    level: str | None = None

    def label(self) -> str:
        return f"{self.text} ({self.level})" if self.level else self.text


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedQuestion:
    """An application question the source published with the job."""

    label: str
    required: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRequirements:
    requirements: tuple[Requirement, ...] = ()
    responsibilities: tuple[str, ...] = field(default=(), repr=False)
    questions: tuple[ListedQuestion, ...] = ()
    experience_level: str | None = None
    work_arrangement: WorkArrangement | None = None
    salaries: tuple[SalaryRange, ...] = ()
    has_description: bool = False
    # Whether the description had a recognized requirements or nice-to-have heading.
    sections_found: bool = False

    @property
    def required(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.importance is Importance.REQUIRED)

    @property
    def preferred(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.importance is Importance.PREFERRED)


class _Section(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITIES = "responsibilities"
    # Anything else with a heading (benefits, about us, process): its lines are ignored.
    OTHER = "other"


# Folded heading phrases. The longest phrase found in a heading line decides its section, so
# "preferred qualifications" is preferred although "qualifications" alone is required.
HEADINGS: Final[dict[_Section, tuple[str, ...]]] = {
    _Section.REQUIRED: (
        "requirements", "required", "required skills", "must have", "must haves",
        "qualifications", "minimum qualifications", "basic qualifications", "what we expect",
        "what you need", "what you bring", "what you will bring", "what we are looking for",
        "what we re looking for", "who you are", "your profile", "your skills", "about you",
        "you have", "skills",
        "wymagania", "wymagane", "nasze wymagania", "wymagania konieczne", "oczekujemy",
        "czego oczekujemy", "czego od ciebie oczekujemy", "nasze oczekiwania", "oczekiwania",
        "twoj profil", "kogo szukamy", "kogo poszukujemy", "czego wymagamy",
        "wymagane umiejetnosci", "umiejetnosci", "musisz miec",
    ),
    _Section.PREFERRED: (
        "nice to have", "nice to haves", "preferred", "preferred qualifications",
        "preferred skills", "bonus", "bonus points",
        "mile widziane", "dodatkowym atutem", "dodatkowe atuty", "atutem bedzie",
        "atutem beda", "dodatkowym atutem bedzie", "plusem bedzie", "bedzie plusem",
        "dodatkowo docenimy", "docenimy",
    ),
    _Section.RESPONSIBILITIES: (
        "responsibilities", "your responsibilities", "key responsibilities", "duties",
        "what you will do", "what you ll do", "what you will be doing", "your role",
        "the role", "your tasks", "tasks", "your job", "day to day",
        "obowiazki", "zakres obowiazkow", "twoje obowiazki", "twoje zadania", "zadania",
        "czym bedziesz sie zajmowac", "co bedziesz robic", "zakres zadan", "opis stanowiska",
    ),
    _Section.OTHER: (
        "we offer", "what we offer", "benefits", "perks", "about us", "about the company",
        "about the team", "company", "recruitment process", "hiring process", "how to apply",
        "why join us", "why us", "salary", "compensation",
        "oferujemy", "co oferujemy", "zapewniamy", "benefity", "o nas", "o firmie",
        "proces rekrutacji", "etapy rekrutacji", "dlaczego warto", "wynagrodzenie",
    ),
}  # fmt: skip

# A heading is a short line; anything longer is a sentence that happens to contain the words.
MAX_HEADING_WORDS: Final = 8
MAX_ITEM_LENGTH: Final = 300
MAX_ITEMS_PER_SECTION: Final = 40
_BULLET: Final = re.compile(r"^\s*(?:[-*\u2022\u00b7\u2013]|\d{1,2}[.)])\s*")


def _heading(line: str) -> _Section | None:
    """The section a heading line starts, or None when the line is not a heading.

    A heading is a short, unbulleted line that is a known phrase, ends with `:` or `?` and
    contains one, or adds at most two words to a phrase of two or more words. This keeps lines
    such as "Strong communication skills" or "You have 3+ years of Python" as items.
    """
    if _BULLET.match(line):
        return None
    folded = fold(line)
    words = folded.split()
    if not words or len(words) > MAX_HEADING_WORDS:
        return None
    marked = line.rstrip().endswith((":", "?"))
    padded = f" {folded} "
    best: tuple[int, _Section] | None = None
    for section, phrases in HEADINGS.items():
        for phrase in phrases:
            if f" {phrase} " not in padded:
                continue
            size = len(phrase.split())
            fits = folded == phrase or marked or (size >= 2 and len(words) - size <= 2)
            if fits and (best is None or len(phrase) > best[0]):
                best = (len(phrase), section)
    return best[1] if best else None


def _item(line: str) -> str | None:
    text = " ".join(_BULLET.sub("", line, count=1).split())
    return text[:MAX_ITEM_LENGTH] or None


def description_sections(description: str) -> dict[_Section, list[str]]:
    """Lines of the description grouped under the section heading above them.

    `Requirements: Python, SQL` counts as a heading followed by one item. Lines before the
    first heading belong to no section.
    """
    sections: dict[_Section, list[str]] = {}
    current: _Section | None = None
    for line in description.splitlines():
        if not line.strip():
            continue
        head, sep, rest = line.partition(":")
        inline = _item(rest) if sep else None
        section = _heading(head + sep) if inline else _heading(line)
        if section is not None:
            current = section
            items = sections.setdefault(section, [])
            if inline:
                items.append(inline)
            continue
        if current is not None and (item := _item(line)):
            items = sections[current]
            if len(items) < MAX_ITEMS_PER_SECTION:
                items.append(item)
    return sections


def _questions(value: JsonValue) -> tuple[ListedQuestion, ...]:
    if not isinstance(value, list):
        return ()
    found: list[ListedQuestion] = []
    for item in cast(list[JsonValue], value):
        if isinstance(item, dict):
            label = item.get("label")
            if isinstance(label, str) and label.strip():
                found.append(
                    ListedQuestion(label=label.strip(), required=item.get("required") is True)
                )
    return tuple(found)


def _unique(requirements: Iterable[Requirement]) -> tuple[Requirement, ...]:
    """Drop repeats (same folded text): a listed skill also written in the description."""
    seen: set[str] = set()
    kept: list[Requirement] = []
    for requirement in requirements:
        key = fold(requirement.text)
        if key and key not in seen:
            seen.add(key)
            kept.append(requirement)
    return tuple(kept)


def extract_requirements(job: Job) -> JobRequirements:
    """Requirements, responsibilities and questions of a stored job."""
    stored: JsonObject = job.requirements
    details = JobDetails.from_json(stored)
    listed = [
        Requirement(
            text=s.name,
            kind=RequirementKind.SKILL,
            importance=Importance.REQUIRED,
            origin=RequirementOrigin.LISTED,
            level=s.level,
        )
        for s in details.skills
    ] + [
        Requirement(
            text=s.name,
            kind=RequirementKind.LANGUAGE,
            importance=Importance.REQUIRED,
            origin=RequirementOrigin.LISTED,
            level=s.level,
        )
        for s in details.languages
    ]
    sections = description_sections(job.description or "")
    written = [
        Requirement(
            text=text,
            kind=RequirementKind.TEXT,
            importance=importance,
            origin=RequirementOrigin.DESCRIPTION,
        )
        for section, importance in (
            (_Section.REQUIRED, Importance.REQUIRED),
            (_Section.PREFERRED, Importance.PREFERRED),
        )
        for text in sections.get(section, [])
    ]
    return JobRequirements(
        requirements=_unique([*listed, *written]),
        responsibilities=tuple(sections.get(_Section.RESPONSIBILITIES, [])),
        questions=_questions(stored.get("questions")),
        experience_level=details.experience_level,
        work_arrangement=details.work_arrangement,
        salaries=details.salaries,
        has_description=bool((job.description or "").strip()),
        sections_found=bool(sections.keys() & {_Section.REQUIRED, _Section.PREFERRED}),
    )
