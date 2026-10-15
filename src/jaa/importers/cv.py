"""Reconstruct profile fact candidates from the text lines of a CV (ADR 0008).

This is heuristic by nature, so it is conservative: every candidate carries a confidence, a
location and the source excerpt it came from, and goes to review as `unreviewed`. When a line
cannot be interpreted it is reported as an issue rather than guessed at or silently dropped.

Field keys are stable per item (ADR 0006): `skills.<slug>`, `experience.<employer>.title`, …,
so re-importing an edited CV yields duplicates or conflicts instead of new facts.

Headings, keywords, month names and "present" words are English and Polish (ADR 0031). They
are matched on folded text (lowercase, no diacritics), so `Doświadczenie` and `DOSWIADCZENIE`
are the same heading, and a CV may mix the two languages.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from jaa.core.languages import language_name
from jaa.core.models import FactCandidate, JsonObject, JsonValue
from jaa.core.review import same_value
from jaa.importers.base import ImportIssue, IssueSeverity, SourceLine

MAX_EXCERPT: Final = 300


class Section(StrEnum):
    HEADER = "header"
    CONTACT = "contact"
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    LANGUAGES = "languages"
    CERTIFICATIONS = "certifications"
    UNSUPPORTED = "unsupported"


_HEADINGS: Final[dict[Section, tuple[str, ...]]] = {
    Section.CONTACT: (
        "contact",
        "contact details",
        "contact information",
        "personal details",
        "personal information",
    ),
    Section.SUMMARY: (
        "summary",
        "profile",
        "professional summary",
        "professional profile",
        "personal profile",
        "career summary",
        "about",
        "about me",
        "objective",
        "career objective",
    ),
    Section.EXPERIENCE: (
        "experience",
        "work experience",
        "professional experience",
        "relevant experience",
        "employment",
        "employment history",
        "work history",
        "career history",
    ),
    Section.EDUCATION: ("education", "education and training", "academic background"),
    Section.SKILLS: (
        "skills",
        "technical skills",
        "key skills",
        "core skills",
        "skills and tools",
        "competencies",
        "core competencies",
        "technologies",
        "tech stack",
    ),
    Section.LANGUAGES: ("languages", "language skills"),
    Section.CERTIFICATIONS: (
        "certifications",
        "certificates",
        "licenses and certifications",
        "licences and certifications",
        "certifications and licenses",
    ),
    # Recognized so their content is reported as not imported instead of being misread.
    Section.UNSUPPORTED: (
        "projects",
        "personal projects",
        "volunteering",
        "volunteer experience",
        "interests",
        "hobbies",
        "publications",
        "awards",
        "honors",
        "honours",
        "achievements",
        "references",
        "courses",
        "training",
        "activities",
        "leadership",
    ),
}
_HEADINGS_PL: Final[dict[Section, tuple[str, ...]]] = {
    Section.CONTACT: (
        "kontakt",
        "dane kontaktowe",
        "informacje kontaktowe",
        "dane osobowe",
        "dane osobowe i kontaktowe",
    ),
    Section.SUMMARY: (
        "podsumowanie",
        "podsumowanie zawodowe",
        "profil",
        "profil zawodowy",
        "o mnie",
        "cel zawodowy",
    ),
    Section.EXPERIENCE: (
        "doświadczenie",
        "doświadczenie zawodowe",
        "przebieg pracy zawodowej",
        "przebieg kariery",
        "historia zatrudnienia",
        "zatrudnienie",
    ),
    Section.EDUCATION: (
        "wykształcenie",
        "edukacja",
        "wykształcenie i szkolenia",
        "edukacja i szkolenia",
    ),
    Section.SKILLS: (
        "umiejętności",
        "umiejętności techniczne",
        "kluczowe umiejętności",
        "umiejętności i narzędzia",
        "kompetencje",
        "kompetencje kluczowe",
        "kluczowe kompetencje",
        "technologie",
        "stos technologiczny",
    ),
    Section.LANGUAGES: (
        "języki",
        "języki obce",
        "znajomość języków",
        "znajomość języków obcych",
        "umiejętności językowe",
    ),
    Section.CERTIFICATIONS: (
        "certyfikaty",
        "certyfikacje",
        "certyfikaty i licencje",
        "licencje i certyfikaty",
        "uprawnienia",
    ),
    Section.UNSUPPORTED: (
        "projekty",
        "projekty osobiste",
        "wolontariat",
        "zainteresowania",
        "hobby",
        "publikacje",
        "nagrody",
        "osiągnięcia",
        "nagrody i osiągnięcia",
        "referencje",
        "kursy",
        "szkolenia",
        "kursy i szkolenia",
        "działalność dodatkowa",
        "aktywności",
    ),
}


def _fold(text: str) -> str:
    """Lowercase without diacritics (`Łódź` → `lodz`), one character for each character.

    Keeping the length lets a match on the folded text be cut out of the original text.
    """
    out: list[str] = []
    for char in text:
        lower = char.lower()
        if len(lower) != 1:
            lower = char
        lower = _FOLD_LETTERS.get(lower, lower)
        decomposed = unicodedata.normalize("NFD", lower)
        if len(decomposed) > 1 and all(unicodedata.combining(c) for c in decomposed[1:]):
            lower = decomposed[0]
        out.append(lower)
    return "".join(out)


# Letters with no Unicode decomposition to a base letter.
_FOLD_LETTERS: Final = {"ł": "l", "ø": "o", "đ": "d"}

_HEADING_LOOKUP: Final = {
    _fold(text): section
    for table in (_HEADINGS, _HEADINGS_PL)
    for section, texts in table.items()
    for text in texts
}

# The data-processing consent clause Polish CVs end with (RODO, "klauzula"). It is not a
# profile fact; jaa adds the user's own clause to generated CVs.
_CONSENT_HEADINGS: Final = frozenset(
    {"klauzula", "klauzula informacyjna", "klauzula rodo", "rodo", "zgoda na przetwarzanie danych"}
)
_CONSENT_LINE: Final = re.compile(
    r"^(?:wyrazam zgode na przetwarzanie|zgodnie z art\. ?6|"
    r"i (?:hereby )?(?:agree|consent) to the processing of (?:my )?personal data)"
)

# --- patterns --------------------------------------------------------------------------------

_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE: Final = re.compile(r"(?<![\w/])\+?\(?\d[\d \-().]{7,}\d(?![\w/])")
_PHONE_MIN_DIGITS: Final = 9
_URL: Final = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s|,;]+|\b(?:linkedin\.com|github\.com|gitlab\.com)/[^\s|,;]+"
)
_BULLET: Final = re.compile(r"^(?:[•·▪‣◦●○■□➢►✓*]|[-–—](?=\s))\s*")
_PART_SEPARATOR: Final = re.compile(r"\s+[|•·]\s+|\s*\|\s*|\s+[-–—]\s+")
_AT: Final = re.compile(r"\s+(?:at|@)\s+", re.IGNORECASE)
# Polish "Programista w Example": only split when the part before "w" names a role.
_AT_PL: Final = re.compile(r"\s+w\s+")
_COMPANY_SUFFIX: Final = re.compile(
    r"(?i)^(?:inc|ltd|llc|llp|plc|gmbh|ag|s\.?a|b\.?v|sp\. z o\.o|co|corp|limited)\.?$"
)
_TITLE_WORDS: Final = re.compile(
    r"(?i)\b(?:engineer|developer|programmer|manager|analyst|designer|consultant|lead|director"
    r"|intern|internship|scientist|architect|specialist|officer|administrator|head|coordinator"
    r"|associate|assistant|technician|executive|founder|co-founder|owner|cto|ceo|cfo|coo|vp"
    r"|president|researcher|teacher|lecturer|professor|tester|devops|sre|accountant|advis[eo]r"
    r"|editor|writer|trainee|apprentice|contractor|freelancer|supervisor|strategist"
    r"|product owner|scrum master|representative"
    # Polish, folded: stems cover feminine and inflected forms (programistka, kierownikiem).
    r"|inzynier\w*|programist\w*|deweloper\w*|kierowni\w*|anality\w*|projektan\w*"
    r"|konsultan\w*|dyrektor\w*|stazyst\w*|praktykan\w*|specjalist\w*|architekt\w*"
    r"|administrator\w*|koordynator\w*|asystent\w*|technik|techniczka|tester\w*|ksiegow\w*"
    r"|doradc\w*|prezes\w*|zalozyciel\w*|wspolzalozyciel\w*|wlasciciel\w*|nauczyciel\w*"
    r"|wykladow\w*|badacz\w*|lider\w*|liderk\w*|menedzer\w*|menadzer\w*|redaktor\w*"
    r"|przedstawiciel\w*|opiekun\w*|zastepc\w*|kontroler\w*|sprzedawc\w*)\b"
)
_DEGREE_WORDS: Final = re.compile(
    r"(?i)\b(?:b\.?sc|m\.?sc|b\.?eng|m\.?eng|b\.?a|m\.?a|ph\.?d|mba|llb|llm|bachelor|master"
    r"|doctorate|diploma|degree|a-levels?|high school"
    r"|licencja\w*|magister\w*|mgr|inz|inzynier\w*|doktor\w*|dr|studia|dyplom\w*|matura"
    r"|absolwent\w*)\b\.?"
)
_INSTITUTION_WORDS: Final = re.compile(
    r"(?i)\b(?:university|college|institute|school|academy|polytechnic|conservatory"
    r"|uniwersytet\w*|politechnik\w*|akademi\w*|szkol\w*|uczelni\w*|instytut\w*|liceum"
    r"|technikum|kolegium)\b"
)
# Polish legal forms written after the name without a comma ("Example sp. z o.o.").
_COMPANY_TAIL_PL: Final = re.compile(
    r"(?i)\s(?:sp\.?\s*z\s*o\.?\s*o|s\.?\s*a|sp\.?\s*[jkp]|s\.?\s*k\.?\s*a)\.?$"
)
_NOT_A_NAME: Final = frozenset({"curriculum vitae", "resume", "cv", "zyciorys"})
_NAME_TOKEN: Final = re.compile(r"^[^\W\d_][^\W\d_'’.-]*(?:['’.-][^\W\d_]+)*\.?$")

_MONTH_NAMES: Final = (
    ("jan", "january"),
    ("feb", "february"),
    ("mar", "march"),
    ("apr", "april"),
    ("may", "may"),
    ("jun", "june"),
    ("jul", "july"),
    ("aug", "august"),
    ("sep", "sept", "september"),
    ("oct", "october"),
    ("nov", "november"),
    ("dec", "december"),
)
# Polish, folded: abbreviation, nominative, genitive ("od stycznia") and locative ("w styczniu").
_MONTH_NAMES_PL: Final = (
    ("sty", "styczen", "stycznia", "styczniu"),
    ("lut", "luty", "lutego", "lutym"),
    ("mar", "marzec", "marca", "marcu"),
    ("kwi", "kwiecien", "kwietnia", "kwietniu"),
    ("maj", "maja", "maju"),
    ("cze", "czerwiec", "czerwca", "czerwcu"),
    ("lip", "lipiec", "lipca", "lipcu"),
    ("sie", "sierpien", "sierpnia", "sierpniu"),
    ("wrz", "wrzesien", "wrzesnia", "wrzesniu"),
    ("paz", "pazdziernik", "pazdziernika", "pazdzierniku"),
    ("lis", "listopad", "listopada", "listopadzie"),
    ("gru", "grudzien", "grudnia", "grudniu"),
)
_MONTHS: Final = {
    name: n
    for table in (_MONTH_NAMES, _MONTH_NAMES_PL)
    for n, names in enumerate(table, start=1)
    for name in names
}
_MONTH_RE: Final = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MM: Final = r"(?:0?[1-9]|1[0-2])"
_YEAR: Final = r"(?:19|20)\d{2}"
_POINT: Final = (
    rf"(?:(?:{_MONTH_RE})\.?\s+{_YEAR}|{_MM}[/.]{_YEAR}|{_YEAR}[-/.]{_MM}(?!\d)|{_YEAR})"
)
_CURRENT: Final = (
    r"(?:present|current|now|today|ongoing"
    r"|obecnie|aktualnie|nadal|teraz|do teraz|do dzisiaj|do dzis|dzisiaj|dzis|do chwili obecnej)"
)
# Date patterns run on folded text (`_fold`), so Polish month names are written without accents.
_DATE_RANGE: Final = re.compile(
    rf"(?i)(?<![\w/.])(?:(?:from|od)\s+)?(?P<start>{_POINT})\s*(?:-|–|—|to|until|do)\s*"
    rf"(?P<end>{_POINT}|{_CURRENT})(?![\w/])"
)
_SINGLE_DATE: Final = re.compile(rf"(?i)(?<![\w/.])(?P<point>{_POINT})(?![\w/])")


# --- small helpers ---------------------------------------------------------------------------


def heading_section(text: str) -> Section | None:
    """The section a heading line starts, or None if the line is not a known heading."""
    return _HEADING_LOOKUP.get(_heading_key(text))


def _heading_key(text: str) -> str:
    return " ".join(_fold(text).replace("&", "and").rstrip(":").split())


def slug(text: str) -> str:
    """A stable, readable field-key segment: `C++` → `c-plus-plus`, `Node.js` → `node-dot-js`."""
    value = re.sub(r"\([^)]*\)", " ", text.lower())
    value = value.replace("+", " plus ").replace("#", " sharp ").replace("&", " and ")
    value = re.sub(r"(?:(?<=\w)|^)\.(?=\w)", " dot ", value.strip())
    value = value.replace("ł", "l").replace("ø", "o").replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:60].rstrip("-") or "item"


def _normalize_point(text: str) -> str:
    text = _fold(text.strip()).rstrip(".")
    if match := re.fullmatch(rf"({_MONTH_RE})\.?\s+(\d{{4}})", text):
        return f"{match.group(2)}-{_MONTHS[match.group(1)]:02d}"
    if match := re.fullmatch(r"(\d{1,2})[/.](\d{4})", text):
        return f"{match.group(2)}-{int(match.group(1)):02d}"
    if match := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text):
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return text


def _date_range(match: re.Match[str]) -> JsonObject:
    end = match.group("end")
    current = re.fullmatch(_CURRENT, end, re.IGNORECASE) is not None
    return {
        "start": _normalize_point(match.group("start")),
        "end": None if current else _normalize_point(end),
        "current": current,
    }


def _strip_bullet(text: str) -> tuple[str, bool]:
    stripped = _BULLET.sub("", text, count=1)
    return stripped.strip(), stripped != text


def _split_items(text: str, separators: str = ",;|•·") -> list[str]:
    """Split on separators that are not inside parentheses."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char in separators and depth <= 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item.strip().rstrip(".").strip() for item in items if item.strip(" .")]


def _parts(text: str) -> list[str]:
    """Split an entry heading into parts on `|`, ` - `, `, ` (keeping `Acme, Inc.` whole)."""
    parts: list[str] = []
    for chunk in _PART_SEPARATOR.split(text):
        for piece in (p.strip() for p in chunk.split(",")):
            if not piece:
                continue
            if parts and _COMPANY_SUFFIX.match(piece):
                parts[-1] = f"{parts[-1]}, {piece}"
            else:
                parts.append(piece)
    return [p.strip(" -–—|") for p in parts if p.strip(" -–—|")]


def _is_prose(text: str) -> bool:
    # A full stop that ends a legal form ("Example sp. z o.o.", "Example Ltd.") is not a sentence.
    sentence = text.endswith(".") and not (
        _COMPANY_TAIL_PL.search(text) or _COMPANY_SUFFIX.match(text.split()[-1])
    )
    return len(text) > 100 or (sentence and len(text.split()) >= 6)


# --- result building -------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    section: Section
    title: str  # the heading as written, or "header"
    lines: list[SourceLine]
    heading: SourceLine | None = None
    consent: bool = False


@dataclass(slots=True)
class _Collector:
    candidates: list[FactCandidate] = field(default_factory=list[FactCandidate])
    issues: list[ImportIssue] = field(default_factory=list[ImportIssue])
    per_section: dict[str, int] = field(default_factory=dict[str, int])

    def add(
        self,
        block: _Block,
        line: SourceLine,
        key: str,
        raw: str,
        normalized: JsonValue,
        confidence: float,
        excerpt: str | None = None,
    ) -> None:
        excerpt_text = excerpt if excerpt is not None else line.text
        if len(excerpt_text) > MAX_EXCERPT:
            excerpt_text = excerpt_text[: MAX_EXCERPT - 1] + "…"
        location = (
            line.location
            if block.section is Section.HEADER
            else (f"{line.location} · {block.title}")
        )
        for existing in self.candidates:
            if existing.field_key == key:
                if not same_value(existing.value_normalized, normalized):
                    self.issues.append(
                        ImportIssue(
                            code="duplicate_field",
                            severity=IssueSeverity.WARNING,
                            message=f"A second value for {key} was found; only the first was "
                            f"imported. Second value: “{_short(raw)}”.",
                            location=location,
                        )
                    )
                return
        self.candidates.append(
            FactCandidate(
                field_key=key,
                value_raw=raw,
                value_normalized=normalized,
                confidence=confidence,
                source_location=location,
                source_excerpt=excerpt_text,
            )
        )
        self.per_section[block.section.value] = self.per_section.get(block.section.value, 0) + 1

    def unused(self, block: _Block, lines: Sequence[SourceLine]) -> None:
        if not lines:
            return
        where = (
            "at the top of the CV"
            if block.section is Section.HEADER
            else (f"in the “{block.title}” section")
        )
        count = len(lines)
        self.issues.append(
            ImportIssue(
                code="text_not_imported",
                severity=IssueSeverity.WARNING,
                message=f"{count} line{'s' if count != 1 else ''} {where} could not be "
                f"interpreted and {'were' if count != 1 else 'was'} not imported, e.g. "
                f"“{_short(lines[0].text)}”.",
                location=lines[0].location,
            )
        )


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedCV:
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]
    # Candidate counts per section, for the import summary.
    sections: dict[str, int]
    # Every heading that started a section, as written, with the section it was read as.
    headings: tuple[tuple[str, Section], ...] = ()


# --- sections --------------------------------------------------------------------------------


def _split_sections(lines: Iterable[SourceLine]) -> list[_Block]:
    blocks = [_Block(Section.HEADER, "header", [])]
    for line in lines:
        if not blocks[-1].consent and _is_consent(line.text):
            # The clause may come with or without a heading of its own.
            heading = _heading_key(line.text) in _CONSENT_HEADINGS
            blocks.append(
                _Block(
                    Section.UNSUPPORTED,
                    "consent clause",
                    [] if heading else [line],
                    heading=line,
                    consent=True,
                )
            )
            continue
        section = heading_section(line.text)
        if section is None and line.is_heading and len(blocks) > 1:
            # A styled heading we do not know: keep its content out of the previous section.
            section = Section.UNSUPPORTED
        if section is None:
            blocks[-1].lines.append(line)
        else:
            blocks.append(_Block(section, line.text.rstrip(": "), [], heading=line))
    return blocks


def _is_consent(text: str) -> bool:
    folded = _fold(text).strip()
    return _heading_key(text) in _CONSENT_HEADINGS or _CONSENT_LINE.match(folded) is not None


def _contact(block: _Block, out: _Collector, *, allow_name: bool) -> None:
    unused: list[SourceLine] = []
    name_done = not allow_name
    for line in block.lines:
        text, _ = _strip_bullet(line.text)
        rest = text
        found = False
        for match in _EMAIL.finditer(text):
            out.add(block, line, "contact.email", match.group(), match.group().lower(), 0.95)
            found = True
        rest = _EMAIL.sub(" ", rest)
        for match in _URL.finditer(rest):
            url = match.group().rstrip(".)")
            normalized = url if url.lower().startswith("http") else f"https://{url}"
            host = re.sub(r"(?i)^(?:https?://)?(?:www\.)?", "", url).split("/")[0].lower()
            site = host.split(".")[-2] if host.count(".") >= 1 else host
            known = site in {"linkedin", "github", "gitlab"}
            out.add(
                block, line, f"contact.links.{site if known else 'website'}", url, normalized, 0.9
            )
            found = True
        rest = _URL.sub(" ", rest)
        for match in _PHONE.finditer(rest):
            digits = re.sub(r"\D", "", match.group())
            if len(digits) < _PHONE_MIN_DIGITS or _DATE_RANGE.search(match.group()):
                continue
            plus = "+" if match.group().lstrip("(").startswith("+") else ""
            out.add(block, line, "contact.phone", match.group().strip(), plus + digits, 0.8)
            found = True
        if not found and " ".join(_fold(text).split()) in _NOT_A_NAME:
            # A document title, not content: the name may still follow it.
            continue
        if not found and not name_done:
            name_done = True
            tokens = text.split()
            if 2 <= len(tokens) <= 5 and all(_NAME_TOKEN.match(t) for t in tokens):
                out.add(block, line, "identity.name", text, text, 0.6)
                continue
        if not found:
            # Labels such as "Email:" next to a value on the same line are fine; a line with no
            # recognizable value (an address, a tagline) is reported.
            unused.append(line)
    out.unused(block, unused)


def _summary(block: _Block, out: _Collector) -> None:
    if not block.lines:
        return
    text = " ".join(_strip_bullet(line.text)[0] for line in block.lines)
    out.add(block, block.lines[0], "summary", text, text, 0.6, excerpt=text)


@dataclass(slots=True)
class _Entry:
    header: list[SourceLine] = field(default_factory=list[SourceLine])
    details: list[SourceLine] = field(default_factory=list[SourceLine])
    dates: JsonObject | None = None
    dates_raw: str = ""
    date_line: SourceLine | None = None


def _find_dates(text: str, *, allow_single: bool) -> tuple[JsonObject, str] | None:
    """The dates in `text` and the text they were written as."""
    folded = _fold(text)
    if match := _DATE_RANGE.search(folded):
        return _date_range(match), text[match.start() : match.end()]
    if allow_single and (match := _SINGLE_DATE.search(folded)):
        point = _normalize_point(match.group("point"))
        return {"start": None, "end": point, "current": False}, text[match.start() : match.end()]
    return None


def _segment(lines: Sequence[SourceLine], *, allow_single_date: bool) -> list[_Entry]:
    """Group a section's lines into entries: heading lines (one with dates) then details."""
    entries: list[_Entry] = []
    for line in lines:
        text, bulleted = _strip_bullet(line.text)
        if line.is_bullet or bulleted or _is_prose(text):
            if not entries:
                entries.append(_Entry())
            entries[-1].details.append(line)
            continue
        if line.entry is not None:
            # The markup delimits entries itself, and names the dates part.
            current = _Entry(header=[line])
            entries.append(current)
            if line.entry.dates and (found := _find_dates(line.entry.dates, allow_single=True)):
                current.dates, current.dates_raw = found
                current.date_line = line
            continue
        found = _find_dates(text, allow_single=allow_single_date)
        current = entries[-1] if entries else None
        if current is None or current.details:
            current = _Entry()
            entries.append(current)
        elif found and current.dates is not None and current.date_line is not None:
            # Another dated heading without details in between: the lines after the previous
            # entry's date line belong to the new entry (stacked "Employer / Title / Dates").
            cut = current.header.index(current.date_line) + 1
            moved = current.header[cut:]
            del current.header[cut:]
            current = _Entry(header=moved)
            entries.append(current)
        current.header.append(line)
        if found and current.dates is None:
            current.dates, current.dates_raw = found
            current.date_line = line
    return entries


@dataclass(frozen=True, slots=True)
class _EntryKind:
    prefix: str
    primary: str  # e.g. "employer" — names the entry key
    secondary: str  # e.g. "title"
    secondary_words: re.Pattern[str]
    primary_words: re.Pattern[str] | None
    details_key: str
    allow_single_date: bool


_EXPERIENCE: Final = _EntryKind(
    "experience", "employer", "title", _TITLE_WORDS, None, "description", False
)
_EDUCATION: Final = _EntryKind(
    "education", "institution", "degree", _DEGREE_WORDS, _INSTITUTION_WORDS, "details", True
)


def _classify(
    kind: _EntryKind, parts: list[str]
) -> tuple[str | None, str | None, float, list[str]]:
    """Pick (primary, secondary, confidence, unused parts) from an entry heading's parts."""
    if kind.primary_words is not None:
        primaries = [p for p in parts if kind.primary_words.search(_fold(p))]
        secondaries = [
            p for p in parts if p not in primaries and kind.secondary_words.search(_fold(p))
        ]
        primary = primaries[0] if primaries else None
        secondary = secondaries[0] if secondaries else None
        if primary is None and secondary is not None and len(parts) == 2:
            primary = next(p for p in parts if p != secondary)
    else:
        with_suffix = [
            p
            for p in parts
            if _COMPANY_SUFFIX.search(p.split(",")[-1].strip()) or _COMPANY_TAIL_PL.search(p)
        ]
        titled = [
            p for p in parts if kind.secondary_words.search(_fold(p)) and p not in with_suffix
        ]
        others = [p for p in parts if p not in titled]
        secondary = titled[0] if len(titled) == 1 else None
        primary = others[0] if secondary is not None and others else None
        if secondary is None and len(with_suffix) == 1 and len(parts) == 2:
            primary = with_suffix[0]
            secondary = next(p for p in parts if p != primary)
    used = {primary, secondary}
    return primary, secondary, 0.6, [p for p in parts if p not in used]


def _entries(block: _Block, out: _Collector, kind: _EntryKind) -> None:
    unused: list[SourceLine] = []
    used_ids: set[str] = set()
    for entry in _segment(block.lines, allow_single_date=kind.allow_single_date):
        if not entry.header:
            unused.extend(entry.details)
            continue
        first = entry.header[0]
        primary: str | None = None
        secondary: str | None = None
        confidence = 0.6
        leftovers: list[str] = []
        texts = [
            line.text.replace(entry.dates_raw, " ") if line is entry.date_line else line.text
            for line in entry.header
        ]
        heading = " | ".join(
            " ".join(t.split()).strip(" ,|-–—") for t in texts if t.strip(" ,|-–—")
        )
        at_split = _AT.split(heading, maxsplit=1) if kind is _EXPERIENCE else []
        if kind is _EXPERIENCE and len(at_split) != 2:
            at_split = _AT_PL.split(heading, maxsplit=1)
            if len(at_split) != 2 or not _TITLE_WORDS.search(_fold(at_split[0])):
                at_split = []
        if first.entry is not None:
            named = first.entry
            heading = first.text
            primary, secondary, confidence = named.organization, named.role, 0.8
            leftovers = [*named.extra, *(line.text for line in entry.header[1:])]
            if named.dates:
                # Whatever the date pattern did not cover ("Summer", "part-time") is reported.
                rest = named.dates.replace(entry.dates_raw, " ") if entry.dates else named.dates
                if rest := " ".join(rest.split()).strip(" ,|-–—()"):
                    leftovers.append(rest)
        elif len(at_split) == 2 and " | " not in at_split[0]:
            secondary = at_split[0].strip()
            rest = _parts(at_split[1])
            primary, leftovers, confidence = rest[0] if rest else None, rest[1:], 0.7
        else:
            primary, secondary, confidence, leftovers = _classify(kind, _parts(heading))

        name = primary or secondary
        if name is None:
            if heading:
                entry_id = _unique(slug(heading.split(" | ")[0]), entry, used_ids)
                out.add(
                    block,
                    first,
                    f"{kind.prefix}.{entry_id}.heading",
                    heading,
                    heading,
                    0.3,
                    excerpt=" / ".join(line.text for line in entry.header),
                )
            else:
                entry_id = _unique("entry", entry, used_ids)
        else:
            entry_id = _unique(slug(name), entry, used_ids)
            excerpt = " / ".join(line.text for line in entry.header)
            if primary is not None:
                out.add(
                    block,
                    first,
                    f"{kind.prefix}.{entry_id}.{kind.primary}",
                    primary,
                    primary,
                    confidence,
                    excerpt=excerpt,
                )
            if secondary is not None:
                out.add(
                    block,
                    first,
                    f"{kind.prefix}.{entry_id}.{kind.secondary}",
                    secondary,
                    secondary,
                    confidence,
                    excerpt=excerpt,
                )
            if leftovers:
                unused.extend(SourceLine(text=p, location=first.location) for p in leftovers)
        if entry.dates is not None and entry.date_line is not None:
            out.add(
                block,
                entry.date_line,
                f"{kind.prefix}.{entry_id}.dates",
                entry.dates_raw,
                entry.dates,
                0.8,
            )
        if entry.details:
            items = [_strip_bullet(line.text)[0] for line in entry.details]
            out.add(
                block,
                entry.details[0],
                f"{kind.prefix}.{entry_id}.{kind.details_key}",
                "\n".join(items),
                list[JsonValue](items),
                0.6,
                excerpt="\n".join(items),
            )
    out.unused(block, unused)


def _unique(base: str, entry: _Entry, used: set[str]) -> str:
    """Disambiguate two entries for the same employer, preferring the start date over a counter."""
    candidate = base
    if candidate in used and entry.dates is not None:
        start = entry.dates.get("start") or entry.dates.get("end")
        if isinstance(start, str):
            candidate = f"{base}-{slug(start)}"
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


_MAX_SKILL_WORDS: Final = 6


def _skills(block: _Block, out: _Collector) -> None:
    unused: list[SourceLine] = []
    for line in block.lines:
        text, _ = _strip_bullet(line.text)
        label, colon, rest = text.partition(":")
        if colon and len(label.split()) <= 4:
            text = rest
        items = _split_items(text)
        kept = [i for i in items if len(i.split()) <= _MAX_SKILL_WORDS and len(i) <= 60]
        for item in kept:
            out.add(block, line, f"skills.{slug(item)}", item, item, 0.7)
        if len(kept) < len(items) or not items:
            unused.append(line)
    out.unused(block, unused)


_LANGUAGE_ITEM: Final = re.compile(
    r"^(?P<language>[^\W\d_](?:[^\W\d_]|['’ -])*?)\s*"
    r"(?:\((?P<paren>[^)]*)\)|[:–—-]\s*(?P<level>.+))?$"
)


def _languages(block: _Block, out: _Collector) -> None:
    unused: list[SourceLine] = []
    for line in block.lines:
        text, _ = _strip_bullet(line.text)
        items = _split_items(text)
        matched = 0
        for item in items:
            match = _LANGUAGE_ITEM.match(item)
            if match is None or len(match.group("language").split()) > 3:
                continue
            language = match.group("language").strip()
            level = (match.group("paren") or match.group("level") or "").strip() or None
            out.add(
                block,
                line,
                f"languages.{_language_key(language)}",
                item,
                {"language": language, "level": level},
                0.7,
            )
            matched += 1
        if matched < len(items) or not items:
            unused.append(line)
    out.unused(block, unused)


def _language_key(language: str) -> str:
    """`english` for "English", "angielski" and "Język angielski", so keys match across CVs."""
    words = _fold(language).split()
    if len(words) == 2 and words[0] in {"jezyk", "j."}:
        words = words[1:]
    name = language_name(words[0]) if len(words) == 1 else None
    return name or slug(language)


def _certifications(block: _Block, out: _Collector) -> None:
    for line in block.lines:
        text, _ = _strip_bullet(line.text)
        found = _find_dates(text, allow_single=True)
        name = text
        date: JsonValue = None
        if found is not None:
            dates, raw = found
            name = " ".join(text.replace(raw, " ").split()).strip(" ,|-–—()")
            date = dates
        if name:
            out.add(
                block,
                line,
                f"certifications.{slug(name)}",
                text,
                {"name": name, "date": date},
                0.6,
            )


def parse_cv(lines: Sequence[SourceLine]) -> ParsedCV:
    """Turn located CV lines into fact candidates and issues. Pure; never raises on content."""
    out = _Collector()
    blocks = _split_sections(lines)
    for block in blocks:
        match block.section:
            case Section.HEADER:
                _contact(block, out, allow_name=True)
            case Section.CONTACT:
                _contact(block, out, allow_name=False)
            case Section.SUMMARY:
                _summary(block, out)
            case Section.EXPERIENCE:
                _entries(block, out, _EXPERIENCE)
            case Section.EDUCATION:
                _entries(block, out, _EDUCATION)
            case Section.SKILLS:
                _skills(block, out)
            case Section.LANGUAGES:
                _languages(block, out)
            case Section.CERTIFICATIONS:
                _certifications(block, out)
            case Section.UNSUPPORTED if block.consent:
                out.issues.append(
                    ImportIssue(
                        code="consent_clause_not_imported",
                        severity=IssueSeverity.WARNING,
                        message="The data-processing consent clause was not imported: it is "
                        "not a profile fact. Generated CVs use the clause you give jaa "
                        "(--consent-file).",
                        location=(block.heading or block.lines[0]).location,
                    )
                )
            case Section.UNSUPPORTED:
                if block.lines:
                    count = len(block.lines)
                    out.issues.append(
                        ImportIssue(
                            code="section_not_imported",
                            severity=IssueSeverity.WARNING,
                            message=f"The “{block.title}” section ({count} "
                            f"line{'s' if count != 1 else ''}) is not imported yet. Add anything "
                            "important from it during review.",
                            location=(block.heading or block.lines[0]).location,
                        )
                    )
    sections = [block for block in blocks[1:] if not block.consent]
    if not sections and lines:
        out.issues.append(
            ImportIssue(
                code="no_sections",
                severity=IssueSeverity.WARNING,
                message="No known CV section headings (Experience, Education, Skills, …) were "
                "found, so only contact details could be extracted.",
            )
        )
    return ParsedCV(
        candidates=tuple(out.candidates),
        issues=tuple(out.issues),
        sections=dict(out.per_section),
        headings=tuple((block.title, block.section) for block in sections),
    )
