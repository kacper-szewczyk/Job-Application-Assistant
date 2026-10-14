"""Structured job details a source publishes besides the description (T12, ADR 0015).

Connectors fill `JobDetails` when the source has structured fields (SOLID.Jobs lists skills,
languages, an experience level and salary ranges). Storage keeps them as JSON in
`jobs.requirements`, next to the job's application questions, and matching reads them back
with `JobDetails.from_json`. Values are the source's own words; nothing here is about the
candidate.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from jaa.core.models import JsonObject, JsonValue


class WorkArrangement(StrEnum):
    """Same values as the `search.work_arrangement` preference choices."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedSkill:
    """A skill or language the listing asks for, e.g. `Python` at `Advanced`."""

    name: str
    level: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SalaryRange:
    """One listed pay range. `period` uses the salary preference's periods (year, month, …)."""

    min: int | None = None
    max: int | None = None
    currency: str
    period: str
    # The kind of contract the range is for, as the source names it (e.g. `B2B`, `UoP`).
    contract: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobDetails:
    skills: tuple[ListedSkill, ...] = ()
    languages: tuple[ListedSkill, ...] = ()
    # The source's seniority label, e.g. `Junior`, `Regular`, `Senior`.
    experience_level: str | None = None
    work_arrangement: WorkArrangement | None = None
    salaries: tuple[SalaryRange, ...] = ()

    def is_empty(self) -> bool:
        return self == JobDetails()

    def to_json(self) -> JsonObject:
        """Only the fields that are set, so an empty value stays `{}`."""
        data: JsonObject = {}
        if self.skills:
            data["skills"] = [_skill_json(s) for s in self.skills]
        if self.languages:
            data["languages"] = [_skill_json(s) for s in self.languages]
        if self.experience_level:
            data["experience_level"] = self.experience_level
        if self.work_arrangement:
            data["work_arrangement"] = self.work_arrangement.value
        if self.salaries:
            data["salaries"] = [
                {
                    "min": s.min,
                    "max": s.max,
                    "currency": s.currency,
                    "period": s.period,
                    "contract": s.contract,
                }
                for s in self.salaries
            ]
        return data

    @classmethod
    def from_json(cls, data: JsonObject) -> "JobDetails":
        """Read stored details, skipping anything malformed rather than failing."""
        arrangement = data.get("work_arrangement")
        return cls(
            skills=_skills(data.get("skills")),
            languages=_skills(data.get("languages")),
            experience_level=_text(data.get("experience_level")),
            work_arrangement=(
                WorkArrangement(arrangement)
                if arrangement in {w.value for w in WorkArrangement}
                else None
            ),
            salaries=_salaries(data.get("salaries")),
        )


def _skill_json(skill: ListedSkill) -> JsonObject:
    return {"name": skill.name, "level": skill.level}


def _text(value: JsonValue) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [cast(JsonObject, v) for v in value if isinstance(v, dict)]


def _skills(value: JsonValue) -> tuple[ListedSkill, ...]:
    return tuple(
        ListedSkill(name=name, level=_text(item.get("level")))
        for item in _objects(value)
        if (name := _text(item.get("name")))
    )


def _amount(value: JsonValue) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return round(value)


def _salaries(value: JsonValue) -> tuple[SalaryRange, ...]:
    found: list[SalaryRange] = []
    for item in _objects(value):
        currency, period = _text(item.get("currency")), _text(item.get("period"))
        low, high = _amount(item.get("min")), _amount(item.get("max"))
        if currency is None or period is None or (low is None and high is None):
            continue
        found.append(
            SalaryRange(
                min=low,
                max=high,
                currency=currency.upper(),
                period=period,
                contract=_text(item.get("contract")),
            )
        )
    return tuple(found)
