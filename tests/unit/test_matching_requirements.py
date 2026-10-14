from datetime import UTC, datetime

import pytest

from jaa.core.job_details import JobDetails, ListedSkill, WorkArrangement
from jaa.core.models import Job, JsonObject, JsonValue
from jaa.matching.requirements import (
    Importance,
    ListedQuestion,
    Requirement,
    RequirementKind,
    RequirementOrigin,
    description_sections,
    extract_requirements,
)

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _job(description: str | None = None, requirements: JsonObject | None = None) -> Job:
    return Job(
        source="test",
        source_job_id="1",
        canonical_url="https://jobs.example.test/1",
        employer="Example Widgets",
        title="Widget Engineer",
        description=description,
        requirements=requirements or {},
        first_seen_at=T0,
        last_seen_at=T0,
        status_changed_at=T0,
    )


def _texts(description: str) -> dict[str, list[str]]:
    return {k.value: v for k, v in description_sections(description).items()}


def test_english_sections() -> None:
    description = """We build widgets.

Your responsibilities:
- Build widget APIs
- Review code

Requirements
- 3+ years of Python
- Strong communication skills
You have experience with Docker

Nice to have:
- Kubernetes

What we offer
- Free fruit
"""
    assert _texts(description) == {
        "responsibilities": ["Build widget APIs", "Review code"],
        "required": [
            "3+ years of Python",
            "Strong communication skills",
            "You have experience with Docker",
        ],
        "preferred": ["Kubernetes"],
        "other": ["Free fruit"],
    }


def test_polish_sections() -> None:
    description = """Czym będziesz się zajmować?

Budowa syntetycznych widgetów.
Czego od Ciebie oczekujemy?
- Znajomość Pythona
- Bardzo dobra znajomość języka angielskiego
Mile widziane:
- Docker
Oferujemy:
- Pakiet medyczny
"""
    assert _texts(description) == {
        "responsibilities": ["Budowa syntetycznych widgetów."],
        "required": ["Znajomość Pythona", "Bardzo dobra znajomość języka angielskiego"],
        "preferred": ["Docker"],
        "other": ["Pakiet medyczny"],
    }


def test_inline_heading_and_numbered_items() -> None:
    assert _texts("Requirements: Python, SQL\n1. Git\n2) Linux") == {
        "required": ["Python, SQL", "Git", "Linux"]
    }


@pytest.mark.parametrize(
    "line",
    [
        "- Requirements",  # a bullet is never a heading
        "Docker is a plus",
        "We expect you to know our product requirements by heart and more",
        "Experience: 3 years",
    ],
)
def test_lines_that_are_not_headings(line: str) -> None:
    assert _texts(f"Requirements\n{line}") == {"required": [line.removeprefix("- ")]}


def test_text_before_any_heading_is_not_a_requirement() -> None:
    requirements = extract_requirements(_job("Python, Docker and good vibes."))
    assert requirements.requirements == ()
    assert requirements.has_description
    assert not requirements.sections_found


def test_listed_details_questions_and_dedupe() -> None:
    details = JobDetails(
        skills=(ListedSkill(name="Python", level="Advanced"),),
        languages=(ListedSkill(name="Angielski", level="B2"),),
        experience_level="Senior",
        work_arrangement=WorkArrangement.REMOTE,
    )
    questions: list[JsonValue] = [
        {"key": "q1", "label": "Why widgets?", "kind": "text", "required": True},
        {"key": "q2", "label": " ", "kind": "text"},
    ]
    stored = details.to_json()
    stored["questions"] = questions
    found = extract_requirements(_job("Requirements:\n- python\n- SQL", stored))
    assert found.requirements == (
        Requirement(
            text="Python",
            kind=RequirementKind.SKILL,
            importance=Importance.REQUIRED,
            origin=RequirementOrigin.LISTED,
            level="Advanced",
        ),
        Requirement(
            text="Angielski",
            kind=RequirementKind.LANGUAGE,
            importance=Importance.REQUIRED,
            origin=RequirementOrigin.LISTED,
            level="B2",
        ),
        Requirement(
            text="SQL",
            kind=RequirementKind.TEXT,
            importance=Importance.REQUIRED,
            origin=RequirementOrigin.DESCRIPTION,
        ),
    )
    assert found.questions == (ListedQuestion(label="Why widgets?", required=True),)
    assert found.experience_level == "Senior"
    assert found.work_arrangement is WorkArrangement.REMOTE


def test_long_sections_are_capped() -> None:
    lines = "\n".join(f"- item {i}" for i in range(100))
    assert len(_texts(f"Requirements\n{lines}")["required"]) == 40
