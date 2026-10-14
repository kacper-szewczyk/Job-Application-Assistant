"""Filtering stored jobs (T11, ADR 0014). Pure functions over `Job`s.

Text matching folds case, diacritics and punctuation (`jaa.core.dedupe.fold`), so `lodz`
finds `Łódź` and `python` finds `Python/Django`. Every word of `text` must appear somewhere in
the title, employer, location or description as a whole word: `java` does not find
`JavaScript`.
"""

from dataclasses import dataclass
from typing import cast

from jaa.core.dedupe import fold
from jaa.core.errors import SearchError
from jaa.core.models import Job, JobStatus, JsonObject, JsonValue


@dataclass(frozen=True, slots=True, kw_only=True)
class JobQuery:
    """What to look for. Empty fields do not filter."""

    text: str | None = None
    company: str | None = None
    location: str | None = None
    sources: tuple[str, ...] = ()
    statuses: tuple[JobStatus, ...] = ()
    include_removed: bool = False

    def to_json(self) -> JsonObject:
        return {
            "text": self.text,
            "company": self.company,
            "location": self.location,
            "sources": list(self.sources),
            "statuses": [s.value for s in self.statuses],
            "include_removed": self.include_removed,
        }

    @classmethod
    def from_json(cls, data: JsonObject) -> "JobQuery":
        """Read a stored query; raises `SearchError` if it is not one."""
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise SearchError(f"unknown search fields {sorted(unknown)}")
        try:
            return cls(
                text=_optional_text(data.get("text")),
                company=_optional_text(data.get("company")),
                location=_optional_text(data.get("location")),
                sources=tuple(_texts(data.get("sources"))),
                statuses=tuple(JobStatus(s) for s in _texts(data.get("statuses"))),
                include_removed=data.get("include_removed") is True,
            )
        except ValueError as exc:
            raise SearchError(f"unreadable saved search: {exc}") from exc

    def describe(self) -> str:
        """A short, human-readable summary for listings."""
        parts: list[str] = []
        if self.text:
            parts.append(f'"{self.text}"')
        if self.company:
            parts.append(f"company: {self.company}")
        if self.location:
            parts.append(f"location: {self.location}")
        if self.sources:
            parts.append("source: " + ", ".join(self.sources))
        if self.statuses:
            parts.append("status: " + ", ".join(s.value for s in self.statuses))
        if self.include_removed:
            parts.append("including removed")
        return "; ".join(parts) or "all jobs"


def _optional_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value.strip() or None


def _texts(value: JsonValue) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of text")
    return cast(list[str], value)


def matches(job: Job, query: JobQuery) -> bool:
    """Whether `job` passes every filter in `query`."""
    if job.removed_at is not None and not query.include_removed:
        return False
    if query.sources and job.source not in query.sources:
        return False
    if query.statuses and job.status not in query.statuses:
        return False
    if query.company and fold(query.company) not in fold(job.employer):
        return False
    if query.location and fold(query.location) not in fold(job.location or ""):
        return False
    if query.text:
        haystack = " ".join([job.title, job.employer, job.location or "", job.description or ""])
        words = set(fold(haystack).split())
        return all(w in words for w in fold(query.text).split())
    return True
