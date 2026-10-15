"""The CV content model: confirmed profile facts arranged into CV sections (SPEC §7).

`build_content` is pure. It only rearranges facts the caller passes in; it never adds text that
is not in a fact (CLAUDE.md: never invent candidate facts). Facts whose keys no section uses are
returned in `ContentResult.unused`, so the CLI can say what was left out instead of dropping it
silently. Fields hold CV contents, so they use `repr=False`.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

from jaa.core.models import FactState, JsonObject, JsonValue, ProfileFact


class CvLanguage(StrEnum):
    EN = "en"
    PL = "pl"


@dataclass(frozen=True, slots=True, kw_only=True)
class Labels:
    """Fixed words the template prints. Everything else comes from facts."""

    babel: str
    summary: str
    experience: str
    education: str
    skills: str
    languages: str
    certifications: str
    consent: str
    present: str


LABELS: Final[dict[CvLanguage, Labels]] = {
    CvLanguage.EN: Labels(
        babel="english",
        summary="Summary",
        experience="Experience",
        education="Education",
        skills="Skills",
        languages="Languages",
        certifications="Certificates",
        consent="Consent",
        present="Present",
    ),
    CvLanguage.PL: Labels(
        babel="polish",
        summary="Podsumowanie",
        experience="Doświadczenie zawodowe",
        education="Wykształcenie",
        skills="Umiejętności",
        languages="Języki",
        certifications="Certyfikaty",
        consent="Oświadczenie",
        present="obecnie",
    ),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class CvEntry:
    """One experience or education entry. `entry_id` is the middle part of its field keys."""

    entry_id: str
    role: str | None = field(default=None, repr=False)  # job title or degree
    organization: str | None = field(default=None, repr=False)  # employer or institution
    dates: JsonObject | None = field(default=None, repr=False)
    location: str | None = field(default=None, repr=False)
    items: tuple[str, ...] = field(default=(), repr=False)  # bullets or education details
    # Indices into `items` a tailored CV sets in bold (ADR 0016).
    emphasized: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CvLanguageSkill:
    name: str = field(repr=False)
    level: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class CvCertification:
    name: str = field(repr=False)
    date: JsonValue = field(default=None, repr=False)
    authority: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class CvContent:
    name: str | None = field(default=None, repr=False)
    headline: str | None = field(default=None, repr=False)
    email: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    location: str | None = field(default=None, repr=False)
    links: tuple[tuple[str, str], ...] = field(default=(), repr=False)  # (kind, url)
    summary: str | None = field(default=None, repr=False)
    experience: tuple[CvEntry, ...] = field(default=(), repr=False)
    education: tuple[CvEntry, ...] = field(default=(), repr=False)
    skills: tuple[str, ...] = field(default=(), repr=False)
    # The field key of each skill, in the same order (`skills.python`).
    skill_keys: tuple[str, ...] = ()
    # Indices into `skills` a tailored CV sets in bold (ADR 0016).
    emphasized_skills: tuple[int, ...] = ()
    languages: tuple[CvLanguageSkill, ...] = field(default=(), repr=False)
    certifications: tuple[CvCertification, ...] = field(default=(), repr=False)
    # The field key of each certification, in the same order.
    certification_keys: tuple[str, ...] = ()
    # Ids of every fact the content uses, for the manifest.
    fact_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ContentResult:
    content: CvContent
    # Confirmed facts no section uses (unknown keys, or kinds the template cannot show).
    unused: tuple[ProfileFact, ...] = ()


def text_of(value: JsonValue) -> str | None:
    """A fact value as display text: strings as-is, lists joined, `{"name": …}` by name."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        parts = [t for t in (text_of(v) for v in value) if t]
        return "; ".join(parts) or None
    for key in ("name", "language", "value"):
        if key in value:
            return text_of(value[key])
    return None


def lines_of(value: JsonValue) -> tuple[str, ...]:
    """A description value as separate lines (bullets)."""
    if isinstance(value, list):
        return tuple(t for t in (text_of(v) for v in value) if t)
    text = text_of(value)
    return (text,) if text else ()


_YEAR_MONTH: Final = re.compile(r"(\d{4})-(\d{2})")


def _point(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if match := _YEAR_MONTH.fullmatch(value):
        return f"{match.group(2)}.{match.group(1)}"
    return value


EN_DASH: Final = "\u2013"


def format_period(dates: JsonValue, language: CvLanguage, *, dash: str = EN_DASH) -> str | None:
    """`{"start": "2021-01", "end": null, "current": true}` → `01.2021`, en dash, `Present`."""
    if not isinstance(dates, dict):
        return text_of(dates)
    start = _point(dates.get("start"))
    end = _point(dates.get("end"))
    if dates.get("current") is True and end is None:
        end = LABELS[language].present
    if start and end:
        return f"{start}{dash}{end}"
    return start or end


def _sort_key(entry: CvEntry) -> tuple[int, str]:
    """Current entries first, then by start date, newest first; undated entries last."""
    dates = entry.dates or {}
    start = dates.get("start")
    current = dates.get("current") is True
    return (0 if current else 1 if isinstance(start, str) else 2, _inverted(start))


def _inverted(start: JsonValue) -> str:
    # Sort descending on the date text without parsing it: invert each digit.
    if not isinstance(start, str):
        return ""
    return "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in start)


_EXPERIENCE_FIELDS: Final = {"employer", "title", "dates", "location", "description", "heading"}
_EDUCATION_FIELDS: Final = {"institution", "degree", "dates", "details", "activities"}


def _entries(
    facts: Iterable[ProfileFact], prefix: str, allowed: set[str], used: list[ProfileFact]
) -> tuple[CvEntry, ...]:
    grouped: dict[str, dict[str, ProfileFact]] = {}
    for fact in facts:
        parts = fact.field_key.split(".")
        if len(parts) == 3 and parts[0] == prefix and parts[2] in allowed:
            grouped.setdefault(parts[1], {})[parts[2]] = fact
            used.append(fact)

    def value(group: dict[str, ProfileFact], name: str) -> JsonValue:
        fact = group.get(name)
        return None if fact is None else fact.value_normalized

    entries: list[CvEntry] = []
    for entry_id, group in grouped.items():
        dates = value(group, "dates")
        if prefix == "experience":
            # An ambiguous heading (ADR 0008) is shown whole as the role.
            role = text_of(value(group, "title")) or text_of(value(group, "heading"))
            entry = CvEntry(
                entry_id=entry_id,
                role=role,
                organization=text_of(value(group, "employer")),
                dates=cast(JsonObject, dates) if isinstance(dates, dict) else None,
                location=text_of(value(group, "location")),
                items=lines_of(value(group, "description")),
            )
        else:
            entry = CvEntry(
                entry_id=entry_id,
                role=text_of(value(group, "degree")),
                organization=text_of(value(group, "institution")),
                dates=cast(JsonObject, dates) if isinstance(dates, dict) else None,
                items=lines_of(value(group, "details")) + lines_of(value(group, "activities")),
            )
        entries.append(entry)
    return tuple(sorted(entries, key=_sort_key))


def build_content(facts: Sequence[ProfileFact]) -> ContentResult:
    """Arrange confirmed facts into CV sections. Facts in any other state are ignored."""
    confirmed = [f for f in facts if f.state is FactState.CONFIRMED]
    by_key = {f.field_key: f for f in confirmed}
    used: list[ProfileFact] = []

    def single(key: str) -> str | None:
        fact = by_key.get(key)
        if fact is None:
            return None
        used.append(fact)
        return text_of(fact.value_normalized)

    def phone() -> str | None:
        # The raw text keeps the user's own spacing ("+48 600 000 000"), unlike the normalized one.
        fact = by_key.get("contact.phone")
        if fact is None:
            return None
        used.append(fact)
        return fact.value_raw.strip() or text_of(fact.value_normalized)

    def group(prefix: str) -> list[ProfileFact]:
        found = [f for f in confirmed if f.field_key.startswith(prefix)]
        used.extend(found)
        return found

    links = tuple(
        (f.field_key.removeprefix("contact.links."), url)
        for f in group("contact.links.")
        if (url := text_of(f.value_normalized))
    )
    languages: list[CvLanguageSkill] = []
    for fact in group("languages."):
        value = fact.value_normalized
        if isinstance(value, dict):
            name = text_of(value.get("language"))
            level = text_of(value.get("level"))
        else:
            name, level = text_of(value), None
        if name:
            languages.append(CvLanguageSkill(name=name, level=level))
    certifications: list[CvCertification] = []
    certification_keys: list[str] = []
    for fact in group("certifications."):
        value = fact.value_normalized
        name = text_of(value)
        if not name:
            continue
        certification_keys.append(fact.field_key)
        if isinstance(value, dict):
            certifications.append(
                CvCertification(
                    name=name,
                    date=value.get("date"),
                    authority=text_of(value.get("authority")),
                )
            )
        else:
            certifications.append(CvCertification(name=name))

    skills = [(f.field_key, t) for f in group("skills.") if (t := text_of(f.value_normalized))]
    content = CvContent(
        name=single("identity.name"),
        headline=single("headline"),
        email=single("contact.email"),
        phone=phone(),
        location=single("contact.location"),
        links=links,
        summary=single("summary"),
        experience=_entries(confirmed, "experience", _EXPERIENCE_FIELDS, used),
        education=_entries(confirmed, "education", _EDUCATION_FIELDS, used),
        skills=tuple(t for _, t in skills),
        skill_keys=tuple(k for k, _ in skills),
        languages=tuple(languages),
        certifications=tuple(certifications),
        certification_keys=tuple(certification_keys),
        fact_ids=tuple(sorted(f.id for f in used if f.id is not None)),
    )
    used_ids = {id(f) for f in used}
    unused = tuple(f for f in confirmed if id(f) not in used_ids)
    return ContentResult(content=content, unused=unused)
