"""A synthetic confirmed profile for CV generation tests. Every value is invented."""

import sqlite3
from datetime import UTC, datetime
from typing import Final

from jaa.core.models import (
    FactCandidate,
    FactOrigin,
    FactState,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JsonValue,
    ProfileFact,
    RetentionChoice,
)
from jaa.storage import profile

T0: Final = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)

PROFILE: Final[dict[str, JsonValue]] = {
    "identity.name": "Avery Żółkiewska-Synthetic",
    "headline": "Senior Widget Engineer & Team Lead",
    "contact.email": "avery.cv@example.test",
    "contact.phone": "+48 600 000 001",
    "contact.location": "Łódź, Poland",
    "contact.links.github": "https://github.com/avery-synthetic",
    "summary": "Builds 100% synthetic widgets with {care} and $budget_limits#.",
    "experience.example-co.employer": "Example Co",
    "experience.example-co.title": "Senior Widget Engineer",
    "experience.example-co.dates": {"start": "2021-01", "end": None, "current": True},
    "experience.example-co.location": "Wrocław",
    "experience.example-co.description": [
        "Led the migration of widget pipelines to event streaming.",
        "Mentored four engineers \\input{/etc/hostname} without escaping issues.",
    ],
    "experience.gadgets.employer": "Gadgets Sp. z o.o.",
    "experience.gadgets.title": "Software Developer",
    "experience.gadgets.dates": {"start": "2017-03", "end": "2020-12", "current": False},
    "experience.gadgets.description": ["Built gadget inventory APIs."],
    "education.example-university.institution": "Example University of Technology",
    "education.example-university.degree": "MSc Computer Science",
    "education.example-university.dates": {"start": "2013", "end": "2016", "current": False},
    "skills.python": "Python",
    "skills.c-plus-plus": "C++",
    "skills.react-native": "React Native",
    "languages.english": {"language": "English", "level": "C1"},
    "languages.polish": {"language": "Polish", "level": "native"},
    "certifications.psm-i": {
        "name": "Professional Scrum Master I",
        "date": {"start": "2020-08", "end": None, "current": False},
        "authority": "Scrum.org",
    },
    "hobbies.skiing": "Skiing",
}


def _raw(value: JsonValue) -> str:
    return value if isinstance(value, str) else str(value)


def confirmed_facts(profile_data: dict[str, JsonValue] | None = None) -> list[ProfileFact]:
    """In-memory confirmed facts, for pure tests."""
    return [
        ProfileFact(
            id=i,
            field_key=key,
            value_raw=_raw(value),
            value_normalized=value,
            state=FactState.CONFIRMED,
            origin=FactOrigin.USER,
            created_at=T0,
            updated_at=T0,
        )
        for i, (key, value) in enumerate((profile_data or PROFILE).items(), start=1)
    ]


def store_profile(
    conn: sqlite3.Connection, *, unreviewed: dict[str, JsonValue] | None = None
) -> None:
    """Import PROFILE, confirm it, and add `unreviewed` facts that stay unreviewed."""
    run = ImportRun(
        source_type=ImportSourceType.TEXT,
        imported_at=T0,
        status=ImportStatus.SUCCEEDED,
        retention=RetentionChoice.KEEP,
    )
    result = profile.add_imported_facts(
        conn,
        run,
        [
            FactCandidate(field_key=k, value_raw=_raw(v), value_normalized=v)
            for k, v in PROFILE.items()
        ],
    )
    profile.confirm_facts(conn, [f.id for f in result.added if f.id is not None])
    if unreviewed:
        profile.add_imported_facts(
            conn,
            run,
            [
                FactCandidate(field_key=k, value_raw=_raw(v), value_normalized=v)
                for k, v in unreviewed.items()
            ],
        )
