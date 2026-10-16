"""Import the user's own LinkedIn data archive (SPEC §5, ADR 0010).

LinkedIn is an import source, not a scraping target: this module only reads files the user
downloaded from LinkedIn and hands to us. It never fetches a profile URL and never asks for
credentials, cookies or tokens.

The archive is a ZIP of CSV files whose set and columns LinkedIn may change at any time, so a
file is used only when its name and required columns are recognized; everything else is listed
as skipped. Unrelated files (messages, connections, ads data, …) are never opened.

Field keys match the CV importer (ADR 0008) so that the same employer imported from a CV and
from LinkedIn lands on the same key and shows up as a duplicate or a conflict in review.
"""

import csv
import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from jaa.core.clock import utcnow
from jaa.core.errors import ImportSourceError
from jaa.core.models import (
    FactCandidate,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JsonObject,
    JsonValue,
    RetentionChoice,
)
from jaa.core.review import same_value
from jaa.importers.archive import MAX_MEMBER_BYTES, list_members, read_member
from jaa.importers.base import ImportIssue, IssueSeverity, resolve_source
from jaa.importers.cv import MAX_EXCERPT, slug

log = logging.getLogger(__name__)

INSTRUCTIONS: Final = """\
How to get your LinkedIn data archive

1. On linkedin.com, open Me → Settings & Privacy → Data privacy → Get a copy of your data.
2. Choose the data you want. Pick at least Profile, Positions, Education, Skills, Languages,
   Certifications, Projects, Honors, Courses, Publications, Patents, Volunteering, Email
   addresses and Phone numbers (or request the larger archive).
3. Select "Request archive". LinkedIn emails you when it is ready (often within minutes for
   selected data, up to a day for the full archive).
4. Download the ZIP file and run:  jaa import linkedin <file.zip>
   You can also pass individual CSV files from the archive.

jaa reads only the file you give it. It never signs in to LinkedIn, never asks for your
password, cookies or tokens, and never opens your profile URL. LinkedIn decides what the
archive contains, so some sections may be missing. If something is missing, import your CV
(`jaa import cv`), a profile PDF saved from LinkedIn (`jaa import cv profile.pdf`), or paste
the sections as text (`jaa import text`).

Help: https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data
"""

# Full archives include messages and media; we only read the small profile CSVs from them.
MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_ROWS: Final = 5_000
CONFIDENCE: Final = 0.9  # structured fields the user entered on LinkedIn
DESCRIPTION_CONFIDENCE: Final = 0.8  # free text whose line splitting is ours

_ZIP_MAGIC: Final = (b"PK\x03\x04", b"PK\x05\x06")

# LinkedIn profile sections that the archive has but we do not import yet.
_NOT_IMPORTED: Final = frozenset(
    {
        "organizations.csv",
        "testscores.csv",
        "recommendationsreceived.csv",
        "causesyoucareabout.csv",
    }
)
# Profile.csv columns we leave out on purpose: they are not used in CVs (data minimization).
_PROFILE_NOT_IMPORTED: Final = (
    "maiden name",
    "address",
    "birth date",
    "zip code",
    "twitter handles",
    "instant messengers",
    "industry",
)

_MONTH_NAMES: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH: Final = re.compile(r"(?i)^([a-z]{3,9})\.?(?:\s+\d{1,2},)?\s+((?:19|20)\d{2})$")
_URL: Final = re.compile(r"(?i)\b(?:https?://|www\.)[^\s,\[\]]+")
_BULLET: Final = re.compile(r"^(?:[•·▪‣◦●○■□➢►✓*]|[-–—](?=\s))\s*")


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", name.lower())


# --- pure parsing ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CsvFile:
    """A CSV file from the archive (or given directly) and its display name."""

    name: str
    data: bytes = field(repr=False)


class _Named(Protocol):
    @property
    def name(self) -> str: ...


type Row = dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class RowTable:
    """Rows of one LinkedIn table that did not come from a CSV file (the portability API).

    `kind` is the archive file the rows correspond to (e.g. `positions.csv`), so they are
    parsed with the same column rules; `name` is what provenance shows.
    """

    name: str
    kind: str
    # Keyed by lower-cased column label, as in the archive CSVs.
    rows: tuple[Row, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedLinkedIn:
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]
    imported_files: tuple[str, ...]
    skipped_files: tuple[str, ...]
    sections: dict[str, int]


@dataclass(slots=True)
class _Collector:
    candidates: list[FactCandidate] = field(default_factory=list[FactCandidate])
    issues: list[ImportIssue] = field(default_factory=list[ImportIssue])
    sections: dict[str, int] = field(default_factory=dict[str, int])

    def add(
        self,
        location: str,
        key: str,
        raw: str,
        normalized: JsonValue,
        confidence: float = CONFIDENCE,
        *,
        quote: bool = True,
    ) -> None:
        for existing in self.candidates:
            if existing.field_key == key:
                if not same_value(existing.value_normalized, normalized):
                    second = f" Second value: “{_short(raw)}”." if quote else ""
                    self.warn(
                        "duplicate_field",
                        f"A second value for {key} was found; only the first was imported."
                        + second,
                        location,
                    )
                return
        excerpt = raw if len(raw) <= MAX_EXCERPT else raw[: MAX_EXCERPT - 1] + "…"
        self.candidates.append(
            FactCandidate(
                field_key=key,
                value_raw=raw,
                value_normalized=normalized,
                confidence=confidence,
                source_location=location,
                source_excerpt=excerpt,
            )
        )
        section = key.split(".", 1)[0]
        self.sections[section] = self.sections.get(section, 0) + 1

    def warn(self, code: str, message: str, location: str | None = None) -> None:
        self.issues.append(
            ImportIssue(
                code=code, severity=IssueSeverity.WARNING, message=message, location=location
            )
        )


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_date(text: str) -> str | None:
    """`Jan 2021` → `2021-01`, `2021` → `2021`; None when the format is not recognized."""
    text = text.strip()
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return text
    if match := _MONTH.fullmatch(text):
        word = match.group(1).lower()
        month = next(
            (n for n, name in enumerate(_MONTH_NAMES, 1) if name.startswith(word[:3])), None
        )
        if month is not None and (len(word) == 3 or word in (_MONTH_NAMES[month - 1], "sept")):
            return f"{match.group(2)}-{month:02d}"
    match = re.fullmatch(r"(\d{1,2})/((?:19|20)\d{2})", text)
    if match and 1 <= int(match.group(1)) <= 12:
        return f"{match.group(2)}-{int(match.group(1)):02d}"
    return None


def _rows(file: CsvFile, out: _Collector) -> list[Row] | None:
    """Rows keyed by lower-cased column name, or None (with an issue) if unreadable."""
    try:
        text = file.data.decode("utf-8-sig")
    except UnicodeDecodeError:
        out.issues.append(
            ImportIssue(
                code="unreadable",
                severity=IssueSeverity.ERROR,
                message="The file is not UTF-8 text and was not imported.",
                location=file.name,
            )
        )
        return None
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = [h.strip().lower() for h in next(reader, list[str]())]
        rows: list[Row] = []
        for values in reader:
            if len(rows) >= MAX_ROWS:
                out.warn(
                    "too_many_rows",
                    f"More than {MAX_ROWS} rows; the rest were not imported.",
                    file.name,
                )
                break
            if any(v.strip() for v in values):
                rows.append({h: v.strip() for h, v in zip(header, values, strict=False)})
    except csv.Error as exc:
        out.issues.append(
            ImportIssue(
                code="unreadable",
                severity=IssueSeverity.ERROR,
                message=f"The file is not valid CSV ({exc}) and was not imported.",
                location=file.name,
            )
        )
        return None
    return rows


def _where(file: _Named, index: int) -> str:
    return f"{file.name} row {index}"


def _dates(
    file: _Named,
    index: int,
    start_raw: str,
    end_raw: str,
    out: _Collector,
    *,
    open_end_is_current: bool,
) -> tuple[JsonObject, str] | None:
    """Normalized dates in the CV importer's shape, plus the raw text, if any date is given."""
    if not start_raw and not end_raw:
        return None
    start = parse_date(start_raw) if start_raw else None
    end = parse_date(end_raw) if end_raw else None
    if (start_raw and start is None) or (end_raw and end is None):
        out.warn(
            "date_not_recognized",
            f"The dates “{_short(start_raw)}” – “{_short(end_raw)}” were not recognized and "
            "were not imported.",
            _where(file, index),
        )
        return None
    current = open_end_is_current and not end_raw
    raw = f"{start_raw} – {end_raw or ('Present' if current else '')}".strip(" –")
    return {"start": start, "end": end, "current": current}, raw


def _lines(text: str) -> list[str]:
    items = [_BULLET.sub("", line, count=1).strip() for line in text.splitlines()]
    return [item for item in items if item]


def _unique(base: str, start: JsonValue, used: set[str]) -> str:
    """Same rule as the CV importer: a repeated employer gets its start date, then a counter."""
    candidate = base
    if candidate in used and isinstance(start, str):
        candidate = f"{base}-{slug(start)}"
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _profile(file: _Named, rows: list[Row], out: _Collector) -> None:
    if len(rows) > 1:
        out.warn("extra_rows", f"{len(rows)} rows; only the first was used.", file.name)
    row = rows[0]
    where = _where(file, 1)
    name = " ".join(p for p in (row.get("first name", ""), row.get("last name", "")) if p)
    if name:
        out.add(where, "identity.name", name, name)
    if headline := row.get("headline", ""):
        out.add(where, "headline", headline, headline)
    if summary := row.get("summary", ""):
        out.add(where, "summary", summary, " ".join(summary.split()))
    if location := row.get("geo location", ""):
        out.add(where, "contact.location", location, location)
    for match in _URL.finditer(row.get("websites", "")):
        url = match.group().rstrip(".)")
        normalized = url if url.lower().startswith("http") else f"https://{url}"
        host = re.sub(r"(?i)^(?:https?://)?(?:www\.)?", "", url).split("/")[0].lower()
        site = host.split(".")[-2] if host.count(".") >= 1 else host
        kind = site if site in {"linkedin", "github", "gitlab"} else "website"
        out.add(where, f"contact.links.{kind}", url, normalized)
    left_out = [c for c in _PROFILE_NOT_IMPORTED if row.get(c)]
    if left_out:
        out.warn(
            "fields_not_imported",
            f"Not imported because CVs do not use them: {', '.join(left_out)}.",
            where,
        )


def _emails(file: _Named, rows: list[Row], out: _Collector) -> None:
    usable = [(i, r) for i, r in enumerate(rows, 1) if "@" in r.get("email address", "")]
    if not usable:
        return
    index, row = next(
        ((i, r) for i, r in usable if r.get("primary", "").lower() == "yes"), usable[0]
    )
    email = row["email address"]
    out.add(_where(file, index), "contact.email", email, email.lower(), 0.95)
    if len(usable) > 1:
        out.warn(
            "other_values",
            f"{len(usable) - 1} other email address{'es' if len(usable) > 2 else ''} "
            f"{'were' if len(usable) > 2 else 'was'} not imported; only the "
            "primary one was. Add another during review if you use it on CVs.",
            file.name,
        )


def _phones(file: _Named, rows: list[Row], out: _Collector) -> None:
    usable = [
        (i, r) for i, r in enumerate(rows, 1) if len(re.sub(r"\D", "", r.get("number", ""))) >= 6
    ]
    if not usable:
        return
    index, row = usable[0]
    number = row["number"]
    plus = "+" if number.lstrip().startswith("+") else ""
    out.add(_where(file, index), "contact.phone", number, plus + re.sub(r"\D", "", number), 0.9)
    if len(usable) > 1:
        out.warn(
            "other_values",
            f"{len(usable) - 1} other phone number{'s' if len(usable) > 2 else ''} "
            f"{'were' if len(usable) > 2 else 'was'} not imported; only the first "
            "one was.",
            file.name,
        )


def _positions(file: _Named, rows: list[Row], out: _Collector) -> None:
    used: set[str] = set()
    for index, row in enumerate(rows, 1):
        where = _where(file, index)
        employer = row.get("company name", "")
        if not employer:
            out.warn("missing_value", "A position without a company name was not imported.", where)
            continue
        dates = _dates(
            file,
            index,
            row.get("started on", ""),
            row.get("finished on", ""),
            out,
            open_end_is_current=True,
        )
        entry = _unique(slug(employer), dates[0].get("start") if dates else None, used)
        prefix = f"experience.{entry}"
        out.add(where, f"{prefix}.employer", employer, employer)
        if title := row.get("title", ""):
            out.add(where, f"{prefix}.title", title, title)
        if dates:
            out.add(where, f"{prefix}.dates", dates[1], dates[0])
        if location := row.get("location", ""):
            out.add(where, f"{prefix}.location", location, location)
        if description := row.get("description", ""):
            out.add(
                where,
                f"{prefix}.description",
                description,
                list[JsonValue](_lines(description)),
                DESCRIPTION_CONFIDENCE,
            )


def _education(file: _Named, rows: list[Row], out: _Collector) -> None:
    used: set[str] = set()
    for index, row in enumerate(rows, 1):
        where = _where(file, index)
        school = row.get("school name", "")
        if not school:
            out.warn(
                "missing_value", "An education entry without a school was not imported.", where
            )
            continue
        dates = _dates(
            file,
            index,
            row.get("start date", ""),
            row.get("end date", ""),
            out,
            open_end_is_current=False,
        )
        entry = _unique(slug(school), dates[0].get("start") if dates else None, used)
        prefix = f"education.{entry}"
        out.add(where, f"{prefix}.institution", school, school)
        if degree := row.get("degree name", ""):
            out.add(where, f"{prefix}.degree", degree, degree)
        if dates:
            out.add(where, f"{prefix}.dates", dates[1], dates[0])
        for column, key in (("notes", "details"), ("activities", "activities")):
            if text := row.get(column, ""):
                out.add(
                    where,
                    f"{prefix}.{key}",
                    text,
                    list[JsonValue](_lines(text)),
                    DESCRIPTION_CONFIDENCE,
                )


def _skills(file: _Named, rows: list[Row], out: _Collector) -> None:
    for index, row in enumerate(rows, 1):
        if name := row.get("name", ""):
            out.add(_where(file, index), f"skills.{slug(name)}", name, name)


def _languages(file: _Named, rows: list[Row], out: _Collector) -> None:
    for index, row in enumerate(rows, 1):
        if name := row.get("name", ""):
            level = row.get("proficiency", "") or None
            raw = f"{name} ({level})" if level else name
            out.add(
                _where(file, index),
                f"languages.{slug(name)}",
                raw,
                {"language": name, "level": level},
            )


def _certifications(file: _Named, rows: list[Row], out: _Collector) -> None:
    for index, row in enumerate(rows, 1):
        name = row.get("name", "")
        if not name:
            continue
        dates = _dates(
            file,
            index,
            row.get("started on", ""),
            row.get("finished on", ""),
            out,
            open_end_is_current=False,
        )
        authority = row.get("authority", "") or None
        raw = " · ".join(p for p in (name, authority, dates[1] if dates else None) if p)
        out.add(
            _where(file, index),
            f"certifications.{slug(name)}",
            raw,
            {"name": name, "date": dates[0] if dates else None, "authority": authority},
        )


def _point(file: _Named, index: int, raw: str, out: _Collector) -> str | None:
    """A single date (`Issued On`, `Published On`) as `YYYY-MM` or `YYYY`, warning if unknown."""
    date = parse_date(raw)
    if date is None:
        out.warn(
            "date_not_recognized",
            f"The date “{_short(raw)}” was not recognized and was not imported.",
            _where(file, index),
        )
    return date


@dataclass(frozen=True, slots=True, kw_only=True)
class _EntrySection:
    """A section whose rows are entries like positions: one fact per field (ADR 0032).

    Each field reads the first non-empty of its columns, so a column LinkedIn renamed can be
    listed beside the old one.
    """

    section: str
    # The entry's naming field and its columns; the entry key is its slug.
    name: tuple[str, tuple[str, ...]]
    texts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Start and end columns of a period, stored as `dates` in the CV importer's shape.
    period: tuple[str, str] | None = None
    open_end_is_current: bool = False
    # A single date column, stored as `date`.
    date: str | None = None
    url: str | None = None
    description: str | None = None
    # Columns left out on purpose; their names (never values) are reported once per file.
    left_out: tuple[str, ...] = ()


def _first(row: Row, columns: Sequence[str]) -> str:
    return next((value for column in columns if (value := row.get(column, ""))), "")


def _entry_rows(spec: _EntrySection, file: _Named, rows: list[Row], out: _Collector) -> None:
    used: set[str] = set()
    name_field, name_columns = spec.name
    for index, row in enumerate(rows, 1):
        where = _where(file, index)
        name = _first(row, name_columns)
        if not name:
            out.warn(
                "missing_value",
                f"An entry without a {name_columns[0]} was not imported.",
                where,
            )
            continue
        dates = (
            _dates(
                file,
                index,
                row.get(spec.period[0], ""),
                row.get(spec.period[1], ""),
                out,
                open_end_is_current=spec.open_end_is_current,
            )
            if spec.period
            else None
        )
        date_raw = row.get(spec.date, "") if spec.date else ""
        date = _point(file, index, date_raw, out) if date_raw else None
        start = dates[0].get("start") if dates else date
        prefix = f"{spec.section}.{_unique(slug(name), start, used)}"
        out.add(where, f"{prefix}.{name_field}", name, name)
        for field_name, columns in spec.texts:
            if text := _first(row, columns):
                out.add(where, f"{prefix}.{field_name}", text, text)
        if dates:
            out.add(where, f"{prefix}.dates", dates[1], dates[0])
        if date:
            out.add(where, f"{prefix}.date", date_raw, date)
        if spec.url and (url := row.get(spec.url, "")):
            normalized = url if url.lower().startswith("http") else f"https://{url}"
            out.add(where, f"{prefix}.url", url, normalized)
        if spec.description and (text := row.get(spec.description, "")):
            out.add(
                where,
                f"{prefix}.description",
                text,
                list[JsonValue](_lines(text)),
                DESCRIPTION_CONFIDENCE,
            )
    left_out = [c for c in spec.left_out if any(r.get(c) for r in rows)]
    if left_out:
        out.warn(
            "fields_not_imported",
            f"Not imported because CVs do not use them: {', '.join(left_out)}.",
            file.name,
        )


_PROJECTS: Final = _EntrySection(
    section="projects",
    name=("title", ("title",)),
    period=("started on", "finished on"),
    url="url",
    description="description",
)
_VOLUNTEERING: Final = _EntrySection(
    section="volunteering",
    name=("organization", ("company name", "organization")),
    texts=(("role", ("role",)),),
    period=("started on", "finished on"),
    open_end_is_current=True,
    description="description",
    # A cause ("Politics", "Human Rights", …) can reveal beliefs; CVs do not need it.
    left_out=("cause",),
)
_PUBLICATIONS: Final = _EntrySection(
    section="publications",
    name=("title", ("name", "title")),
    texts=(("publisher", ("publisher",)),),
    date="published on",
    url="url",
    description="description",
)
_PATENTS: Final = _EntrySection(
    section="patents",
    name=("title", ("title",)),
    texts=(
        ("number", ("patent number", "number")),
        ("application_number", ("application number",)),
    ),
    date="issued on",
    url="url",
    description="description",
)
_HONORS: Final = _EntrySection(
    section="honors",
    name=("title", ("title",)),
    texts=(("issuer", ("issuer",)),),
    date="issued on",
    description="description",
)


type _Handler = Callable[[_Named, list[Row], _Collector], None]


def _entry_handler(spec: _EntrySection) -> _Handler:
    def handle(file: _Named, rows: list[Row], out: _Collector) -> None:
        _entry_rows(spec, file, rows, out)

    return handle


def _courses(file: _Named, rows: list[Row], out: _Collector) -> None:
    for index, row in enumerate(rows, 1):
        if name := row.get("name", ""):
            number = row.get("number", "") or None
            raw = f"{name} ({number})" if number else name
            out.add(
                _where(file, index),
                f"courses.{slug(name)}",
                raw,
                {"name": name, "number": number},
            )


@dataclass(frozen=True, slots=True)
class _Known:
    handler: _Handler
    required: tuple[str, ...]


# Keyed by the normalized file name (lower case, no spaces or underscores).
_KNOWN: Final[dict[str, _Known]] = {
    "profile.csv": _Known(_profile, ("first name", "last name")),
    "emailaddresses.csv": _Known(_emails, ("email address",)),
    "phonenumbers.csv": _Known(_phones, ("number",)),
    "positions.csv": _Known(_positions, ("company name", "title")),
    "education.csv": _Known(_education, ("school name",)),
    "skills.csv": _Known(_skills, ("name",)),
    "languages.csv": _Known(_languages, ("name",)),
    "certifications.csv": _Known(_certifications, ("name",)),
    "projects.csv": _Known(_entry_handler(_PROJECTS), ("title",)),
    "volunteering.csv": _Known(_entry_handler(_VOLUNTEERING), ("company name",)),
    "publications.csv": _Known(_entry_handler(_PUBLICATIONS), ("name",)),
    "patents.csv": _Known(_entry_handler(_PATENTS), ("title",)),
    "honors.csv": _Known(_entry_handler(_HONORS), ("title",)),
    "courses.csv": _Known(_courses, ("name",)),
}


def is_recognized(name: str) -> bool:
    """Whether a file (by its base name) is one the importer reads or knows about."""
    key = _normalize_name(Path(name).name)
    return key in _KNOWN or key in _NOT_IMPORTED


def parse_linkedin_files(files: Sequence[CsvFile]) -> ParsedLinkedIn:
    """Turn LinkedIn export CSVs into fact candidates. Pure; never raises on content.

    Files are processed in a fixed order (profile first) so keys do not depend on the order
    they appear in the archive.
    """
    out = _Collector()
    imported: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    order = list(_KNOWN)
    ranked = sorted(
        files,
        key=lambda f: (
            order.index(k) if (k := _normalize_name(Path(f.name).name)) in order else len(order),
            f.name,
        ),
    )
    for file in ranked:
        key = _normalize_name(Path(file.name).name)
        known = _KNOWN.get(key)
        if key in _NOT_IMPORTED:
            out.warn(
                "section_not_imported",
                "This section is not imported yet. Add anything important from it during review.",
                file.name,
            )
            continue
        if known is None:
            skipped.append(file.name)
            continue
        if key in seen:
            out.warn("duplicate_file", f"{file.name} appears twice; only the first was read.")
            continue
        seen.add(key)
        rows = _rows(file, out)
        if rows is None:
            continue
        if rows and (missing := [c for c in known.required if c not in rows[0]]):
            out.warn(
                "unexpected_format",
                f"The expected columns are missing ({', '.join(missing)}); "
                "LinkedIn may have changed the format. It was not imported.",
                file.name,
            )
            continue
        imported.append(file.name)
        if rows:
            known.handler(file, rows, out)
    return ParsedLinkedIn(
        candidates=tuple(out.candidates),
        issues=tuple(out.issues),
        imported_files=tuple(imported),
        skipped_files=tuple(skipped),
        sections=dict(out.sections),
    )


def known_kinds() -> tuple[str, ...]:
    """The archive files (normalized names) whose rows become facts, in processing order."""
    return tuple(_KNOWN)


def parse_linkedin_tables(tables: Sequence[RowTable]) -> ParsedLinkedIn:
    """Turn LinkedIn rows that did not come from CSV files into fact candidates. Pure.

    The rows follow the archive's column labels (ADR 0026), so they meet the same column
    checks and field keys as an archive import. Tables of unknown kinds are listed as skipped.
    """
    out = _Collector()
    imported: list[str] = []
    skipped: list[str] = []
    order = list(_KNOWN)
    for table in sorted(
        tables,
        key=lambda t: (order.index(t.kind) if t.kind in order else len(order), t.name),
    ):
        known = _KNOWN.get(table.kind)
        if known is None:
            skipped.append(table.name)
            continue
        rows = list(table.rows[:MAX_ROWS])
        if len(table.rows) > MAX_ROWS:
            out.warn(
                "too_many_rows",
                f"More than {MAX_ROWS} rows; the rest were not imported.",
                table.name,
            )
        if rows and (missing := [c for c in known.required if c not in rows[0]]):
            out.warn(
                "unexpected_format",
                f"The expected fields are missing ({', '.join(missing)}); "
                "LinkedIn may have changed the format. It was not imported.",
                table.name,
            )
            continue
        imported.append(table.name)
        if rows:
            known.handler(table, rows, out)
    return ParsedLinkedIn(
        candidates=tuple(out.candidates),
        issues=tuple(out.issues),
        imported_files=tuple(imported),
        skipped_files=tuple(skipped),
        sections=dict(out.sections),
    )


# --- reading sources -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceFile:
    """A file the user gave us and its hash at import time (checked before deleting it)."""

    path: Path
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedLinkedInImport:
    run: ImportRun
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]
    sources: tuple[SourceFile, ...]
    skipped_files: tuple[str, ...]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_zip(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(4) in _ZIP_MAGIC


def _read_archive(path: Path) -> tuple[list[CsvFile], list[str], int]:
    """Recognized CSVs, the names of members that were not read, and the member count."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = list_members(zf)
            files: list[CsvFile] = []
            unread: list[str] = []
            for member in members:
                if member.name.suffix.lower() == ".csv" and is_recognized(member.name.name):
                    files.append(CsvFile(name=str(member.name), data=read_member(zf, member)))
                else:
                    unread.append(str(member.name))
    except zipfile.BadZipFile as exc:
        raise ImportSourceError(f"not a readable ZIP archive: {path}") from exc
    return files, unread, len(members)


def _read_csv(path: Path) -> CsvFile:
    resolve_source(path, max_bytes=MAX_MEMBER_BYTES)
    return CsvFile(name=path.name, data=path.read_bytes())


def prepare_linkedin_import(
    paths: Sequence[Path],
    *,
    retention: RetentionChoice,
    at: datetime | None = None,
) -> PreparedLinkedInImport:
    """Read a LinkedIn archive (one ZIP) or CSV files from one, and build the import run.

    Raises `ImportSourceError` when nothing can be recorded: unusable paths, an unsupported
    file type, or an unsafe archive (`UnsafeArchiveError`, which refuses the whole archive).
    """
    if not paths:
        raise ImportSourceError("no files given")
    at = at or utcnow()
    resolved = list(dict.fromkeys(resolve_source(p, max_bytes=MAX_ARCHIVE_BYTES) for p in paths))
    zips = [p for p in resolved if _is_zip(p)]
    if zips and len(resolved) > 1:
        raise ImportSourceError("import one LinkedIn archive at a time, without other files")
    for path in resolved:
        if not zips and path.suffix.lower() != ".csv":
            raise ImportSourceError(
                f"unsupported file: {path.name}; import the LinkedIn ZIP archive or CSV files "
                "from it"
            )

    sources = tuple(SourceFile(path=p, sha256=_file_sha256(p)) for p in resolved)
    stats: dict[str, JsonValue] = {}
    if zips:
        files, unread, members = _read_archive(zips[0])
        stats["archive_members"] = members
        source_path = str(zips[0])
    else:
        files = [_read_csv(path) for path in resolved]
        unread = []
        # Several CSVs: the run points at their folder; `source_files` lists each one.
        source_path = str(resolved[0] if len(resolved) == 1 else resolved[0].parent)

    parsed = parse_linkedin_files(files)
    skipped = tuple(sorted((*unread, *parsed.skipped_files), key=str.lower))
    issues = parsed.issues
    if not parsed.candidates:
        hint = (
            "No profile files were found. Is this a LinkedIn data archive?"
            if not parsed.imported_files
            else "The profile files contained no values to import."
        )
        issues += (ImportIssue(code="no_facts", severity=IssueSeverity.ERROR, message=hint),)

    if len(sources) == 1:
        combined = sources[0].sha256
    else:
        combined = hashlib.sha256("\n".join(sorted(s.sha256 for s in sources)).encode()).hexdigest()
    status = (
        ImportStatus.FAILED
        if not parsed.candidates
        else (ImportStatus.PARTIAL if issues else ImportStatus.SUCCEEDED)
    )
    run = ImportRun(
        source_type=ImportSourceType.LINKEDIN_ARCHIVE,
        source_path=source_path,
        source_sha256=combined,
        imported_at=at,
        status=status,
        summary={
            **stats,
            "files": list[JsonValue](parsed.imported_files),
            "skipped_files": list[JsonValue](skipped),
            "source_files": [str(s.path) for s in sources],
            "sections": dict[str, JsonValue](parsed.sections),
            "candidates": len(parsed.candidates),
            "issues": len(issues),
        },
        errors=[issue.to_json() for issue in issues],
        retention=retention,
    )
    log.info(
        "prepared linkedin import status=%s files=%d skipped=%d candidates=%d issues=%d",
        status,
        len(parsed.imported_files),
        len(skipped),
        len(parsed.candidates),
        len(issues),
    )
    return PreparedLinkedInImport(
        run=run,
        candidates=parsed.candidates,
        issues=issues,
        sources=sources,
        skipped_files=skipped,
    )


def delete_sources(sources: Sequence[SourceFile]) -> None:
    """Delete the imported files, but only if each is unchanged since it was imported.

    All files are checked before any is deleted, so a mismatch deletes nothing.
    """
    for source in sources:
        try:
            current = _file_sha256(source.path)
        except OSError as exc:
            raise ImportSourceError(f"cannot read {source.path} to delete it: {exc}") from exc
        if current != source.sha256:
            raise ImportSourceError(f"{source.path} changed since it was imported; not deleted")
    for source in sources:
        try:
            source.path.unlink()
        except OSError as exc:
            raise ImportSourceError(f"cannot delete {source.path}: {exc}") from exc
    log.info("deleted imported source files=%d", len(sources))
