from dataclasses import replace

import pytest

from jaa.core.models import FactState, JsonValue
from jaa.documents.content import CvLanguage, build_content, format_period, lines_of, text_of
from tests.fixtures.cv_profile import confirmed_facts


def test_build_content_arranges_confirmed_facts() -> None:
    result = build_content(confirmed_facts())
    content = result.content
    assert content.name == "Avery Żółkiewska-Synthetic"
    assert content.phone == "+48 600 000 001"
    assert content.links == (("github", "https://github.com/avery-synthetic"),)
    # Current job first, then newest first.
    assert [e.organization for e in content.experience] == ["Example Co", "Gadgets Sp. z o.o."]
    assert content.experience[0].items[0].startswith("Led the migration")
    assert content.education[0].role == "MSc Computer Science"
    assert content.skills == ("Python", "C++", "React Native")
    assert [(x.name, x.level) for x in content.languages] == [
        ("English", "C1"),
        ("Polish", "native"),
    ]
    assert content.certifications[0].authority == "Scrum.org"
    assert [f.field_key for f in result.unused] == ["hobbies.skiing"]
    assert len(content.fact_ids) == len(confirmed_facts()) - 1


def test_only_confirmed_facts_are_used() -> None:
    facts = confirmed_facts()
    facts = [
        replace(f, state=FactState.UNREVIEWED) if f.field_key == "headline" else f for f in facts
    ]
    facts = [
        replace(f, state=FactState.REJECTED) if f.field_key.startswith("skills.") else f
        for f in facts
    ]
    content = build_content(facts).content
    assert content.headline is None
    assert content.skills == ()


def test_entries_without_dates_go_last_and_heading_is_the_role() -> None:
    facts = confirmed_facts(
        {
            "experience.a.employer": "Undated Co",
            "experience.b.heading": "Engineer, Dated Co",
            "experience.b.dates": {"start": "2019-05", "end": "2020-01", "current": False},
            "experience.c.employer": "Older Co",
            "experience.c.dates": {"start": "2010", "end": "2012", "current": False},
        }
    )
    entries = build_content(facts).content.experience
    assert [(e.role, e.organization) for e in entries] == [
        ("Engineer, Dated Co", None),
        (None, "Older Co"),
        (None, "Undated Co"),
    ]


@pytest.mark.parametrize(
    ("dates", "language", "expected"),
    [
        ({"start": "2021-01", "end": None, "current": True}, CvLanguage.EN, "01.2021-Present"),
        ({"start": "2021-01", "end": None, "current": True}, CvLanguage.PL, "01.2021-obecnie"),
        ({"start": "2013", "end": "2016", "current": False}, CvLanguage.EN, "2013-2016"),
        ({"start": "2020-08", "end": None, "current": False}, CvLanguage.EN, "08.2020"),
        ("summer 2019", CvLanguage.EN, "summer 2019"),
        (None, CvLanguage.EN, None),
    ],
)
def test_format_period(dates: JsonValue, language: CvLanguage, expected: str | None) -> None:
    assert format_period(dates, language, dash="-") == expected


def test_text_helpers() -> None:
    assert text_of({"name": "PSM I", "date": None}) == "PSM I"
    assert text_of(["a", "", "b"]) == "a; b"
    assert text_of(True) is None
    assert lines_of(["one", " ", "two"]) == ("one", "two")
    assert lines_of("single") == ("single",)


def test_repr_hides_cv_contents() -> None:
    content = build_content(confirmed_facts()).content
    assert "Avery" not in repr(content)
    assert "Example Co" not in repr(content.experience[0])
