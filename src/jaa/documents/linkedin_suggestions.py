"""Read-only suggestions for the user's LinkedIn profile (#73, ADR 0027).

Compares what the latest LinkedIn import showed with the user's confirmed facts and with what
their target jobs ask for. Pure: callers pass facts and match checks, nothing is stored. The
user edits LinkedIn themselves; jaa has no write access and never automates LinkedIn pages.

A suggestion's `text` is only ever confirmed fact wording, copied or joined, never generated
prose; `check_suggestion` re-checks that with the tailoring claim rules and anything that fails
is withheld rather than shown.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from jaa.core.dedupe import fold
from jaa.core.models import FactState, JsonValue, ProfileFact
from jaa.documents.content import text_of
from jaa.documents.tailoring import fact_text, unsupported
from jaa.matching.match import Outcome, RequirementCheck, tokens
from jaa.matching.requirements import Importance, RequirementKind

METHOD: Final = "linkedin-suggestions-v1"
# Sections a LinkedIn profile has; contact details and other sensitive fields are left out.
SECTIONS: Final = (
    "headline",
    "summary",
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
)
_ENTRY_SECTIONS: Final = {"experience": "employer", "education": "institution"}
_NAMED_SECTIONS: Final = {"skills": None, "certifications": "name", "languages": "language"}


class SuggestionKind(StrEnum):
    # The confirmed item is not on LinkedIn.
    ADD = "add"
    # LinkedIn shows a different value than the confirmed one.
    UPDATE = "update"
    # LinkedIn shows a value the user rejected during review, with nothing confirmed instead.
    CHECK = "check"


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetJob:
    """A job the user is aiming for, with its requirements checked against confirmed facts."""

    job_id: int
    checks: tuple[RequirementCheck, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Suggestion:
    kind: SuggestionKind
    # `headline`, `experience.acme`, `experience.acme.title`, `skills.python`, …
    address: str
    # Confirmed wording to put on LinkedIn; empty for CHECK. Profile content: never log it.
    text: str = field(repr=False)
    # What LinkedIn shows now, if anything.
    current: str | None = field(default=None, repr=False)
    # Confirmed facts the suggestion is based on (empty only for CHECK).
    fact_ids: tuple[int, ...] = ()
    # The facts from the LinkedIn import it was compared with.
    linkedin_fact_ids: tuple[int, ...] = ()
    # Target jobs whose requirements these facts meet.
    job_ids: tuple[int, ...] = ()
    required_by_jobs: bool = False

    @property
    def section(self) -> str:
        return self.address.split(".", 1)[0]


@dataclass(frozen=True, slots=True, kw_only=True)
class Gap:
    """A requirement of target jobs that no confirmed fact meets: nothing to suggest for it."""

    requirement: str = field(repr=False)
    importance: Importance
    job_ids: tuple[int, ...]
    # True when unreviewed facts would back it: review them before anything else.
    unreviewed_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Suggestions:
    suggestions: tuple[Suggestion, ...]
    gaps: tuple[Gap, ...]
    # Facts from the LinkedIn import the user has not reviewed yet: they back nothing.
    unreviewed_linkedin: int = 0
    # Suggestions dropped by `check_suggestion`; should stay 0.
    withheld: int = 0


def _section(fact: ProfileFact) -> str:
    return fact.field_key.split(".", 1)[0]


def _relevant(facts: Iterable[ProfileFact]) -> list[ProfileFact]:
    return [f for f in facts if _section(f) in SECTIONS and not f.is_sensitive]


def _words(text: str) -> str:
    return " ".join(tokens(text))


def _same(a: ProfileFact, b: ProfileFact) -> bool:
    """Whether two values say the same thing, ignoring case, punctuation and missing parts.

    For structured values only the parts both sides have are compared, so a CV certification
    without an issuer matches the LinkedIn one with it.
    """
    x, y = a.value_normalized, b.value_normalized
    if isinstance(x, dict) and isinstance(y, dict):
        shared = [k for k in x if k in y and x[k] is not None and y[k] is not None]
        return all(_words(str(x[k])) == _words(str(y[k])) for k in shared)
    return _words(fact_text_of(a)) == _words(fact_text_of(b))


def fact_text_of(fact: ProfileFact) -> str:
    """The fact's wording as written, for showing and comparing."""
    return text_of(fact.value_normalized) or fact.value_raw


def _name(fact: ProfileFact, part: str | None) -> str:
    value = fact.value_normalized
    if part is not None and isinstance(value, dict):
        found = value.get(part)
        if isinstance(found, str):
            return found
    return fact.value_raw if part is not None else fact_text_of(fact)


def _entries(facts: Iterable[ProfileFact], section: str) -> dict[str, dict[str, ProfileFact]]:
    grouped: dict[str, dict[str, ProfileFact]] = {}
    for fact in facts:
        parts = fact.field_key.split(".")
        if parts[0] == section and len(parts) == 3:
            grouped.setdefault(parts[1], {})[parts[2]] = fact
    return grouped


@dataclass(slots=True)
class _Builder:
    jobs_by_fact: Mapping[int, tuple[set[int], bool]]
    found: list[Suggestion] = field(default_factory=list[Suggestion])

    def add(
        self,
        kind: SuggestionKind,
        address: str,
        text: str,
        cited: Sequence[ProfileFact],
        linkedin: Sequence[ProfileFact] = (),
        current: str | None = None,
    ) -> None:
        job_ids: set[int] = set()
        required = False
        for fact in cited:
            jobs, req = self.jobs_by_fact.get(fact.id or 0, (set[int](), False))
            job_ids |= jobs
            required = required or req
        self.found.append(
            Suggestion(
                kind=kind,
                address=address,
                text=text,
                current=current,
                fact_ids=tuple(sorted(f.id for f in cited if f.id is not None)),
                linkedin_fact_ids=tuple(sorted(f.id for f in linkedin if f.id is not None)),
                job_ids=tuple(sorted(job_ids)),
                required_by_jobs=required,
            )
        )


def _singles(
    confirmed: Sequence[ProfileFact], linkedin: Sequence[ProfileFact], out: _Builder
) -> None:
    for key in ("headline", "summary"):
        mine = next((f for f in confirmed if f.field_key == key), None)
        if mine is None:
            continue
        theirs = next((f for f in linkedin if f.field_key == key), None)
        if theirs is None:
            out.add(SuggestionKind.ADD, key, fact_text_of(mine), [mine])
        elif not _same(mine, theirs):
            out.add(
                SuggestionKind.UPDATE,
                key,
                fact_text_of(mine),
                [mine],
                [theirs],
                fact_text_of(theirs),
            )


def _entry_text(fields: Mapping[str, ProfileFact], order: Sequence[str]) -> str:
    return " · ".join(fields[f].value_raw for f in order if f in fields)


def _entry_sections(
    confirmed: Sequence[ProfileFact], linkedin: Sequence[ProfileFact], out: _Builder
) -> None:
    orders = {
        "experience": ("title", "employer", "dates", "location"),
        "education": ("degree", "institution", "dates"),
    }
    for section, name_field in _ENTRY_SECTIONS.items():
        theirs = _entries(linkedin, section)
        by_name = {
            fold(fields[name_field].value_raw): entry
            for entry, fields in theirs.items()
            if name_field in fields
        }
        for entry, fields in _entries(confirmed, section).items():
            name = fields.get(name_field)
            match = theirs.get(entry)
            if match is None and name is not None:
                match = theirs.get(by_name.get(fold(name.value_raw), ""))
            if match is None:
                if name is None:
                    continue
                shown = [fields[f] for f in orders[section] if f in fields]
                out.add(
                    SuggestionKind.ADD,
                    f"{section}.{entry}",
                    _entry_text(fields, orders[section]),
                    shown,
                )
                continue
            for part in (*orders[section][:1], "dates", "description", "details"):
                mine = fields.get(part)
                if mine is None or part == name_field:
                    continue
                other = match.get(part)
                if other is None:
                    out.add(SuggestionKind.ADD, f"{section}.{entry}.{part}", _lines(mine), [mine])
                elif not _same(mine, other) and part not in ("description", "details"):
                    # Descriptions are free text that rarely match word for word; only a
                    # missing one is worth pointing out.
                    out.add(
                        SuggestionKind.UPDATE,
                        f"{section}.{entry}.{part}",
                        mine.value_raw,
                        [mine],
                        [other],
                        other.value_raw,
                    )


def _lines(fact: ProfileFact) -> str:
    value = fact.value_normalized
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if isinstance(v, str))
    return fact.value_raw


def _named_sections(
    confirmed: Sequence[ProfileFact], linkedin: Sequence[ProfileFact], out: _Builder
) -> set[str]:
    """Add and update named items; returns the folded names LinkedIn already lists as skills."""
    listed_skills: set[str] = set()
    for section, part in _NAMED_SECTIONS.items():
        theirs = {
            _words(_name(f, part)): f
            for f in linkedin
            if _section(f) == section and f.field_key.count(".") == 1
        }
        if section == "skills":
            listed_skills = set(theirs)
        for mine in confirmed:
            if _section(mine) != section or mine.field_key.count(".") != 1:
                continue
            other = theirs.get(_words(_name(mine, part)))
            if other is None:
                out.add(SuggestionKind.ADD, mine.field_key, mine.value_raw, [mine])
            elif section != "skills" and not _same(mine, other):
                out.add(
                    SuggestionKind.UPDATE,
                    mine.field_key,
                    mine.value_raw,
                    [mine],
                    [other],
                    other.value_raw,
                )
    return listed_skills


def _skill_key(term: str) -> str:
    return "skills." + "-".join(tokens(term))


def _evidence_skills(
    targets: Sequence[TargetJob],
    confirmed: Mapping[int, ProfileFact],
    listed: set[str],
    out: _Builder,
) -> None:
    """Skills target jobs ask for that confirmed facts mention but LinkedIn does not list.

    The skill is named with the fact's own words (`Evidence.term`), never the job's.
    """
    known = {s.address for s in out.found} | {f.field_key for f in confirmed.values()}
    named = listed | {
        _words(fact_text_of(f)) for f in confirmed.values() if _section(f) == "skills"
    }
    for target in targets:
        for check in target.checks:
            if (
                check.outcome is not Outcome.MET
                or check.requirement.kind is not RequirementKind.SKILL
            ):
                continue
            for evidence in check.evidence:
                fact = confirmed.get(evidence.fact_id)
                if fact is None or _section(fact) not in _ENTRY_SECTIONS:
                    continue
                address = _skill_key(evidence.term)
                if address in known or _words(evidence.term) in named:
                    continue
                known.add(address)
                out.add(SuggestionKind.ADD, address, evidence.term, [fact])


def _covered(fact: ProfileFact, confirmed: Sequence[ProfileFact]) -> bool:
    """Whether a confirmed fact speaks for the same item as the LinkedIn `fact`."""
    section = _section(fact)
    parts = fact.field_key.split(".")
    for mine in confirmed:
        if mine.field_key == fact.field_key:
            return True
        if _section(mine) != section:
            continue
        if section in _NAMED_SECTIONS and mine.field_key.count(".") == 1:
            part = _NAMED_SECTIONS[section]
            if _words(_name(mine, part)) == _words(_name(fact, part)):
                return True
        mine_parts = mine.field_key.split(".")
        if section in _ENTRY_SECTIONS and len(parts) == 3 and len(mine_parts) == 3:
            name_field = _ENTRY_SECTIONS[section]
            if parts[2] == name_field == mine_parts[2] and fold(mine.value_raw) == fold(
                fact.value_raw
            ):
                return True
    return False


def _rejected(
    confirmed: Sequence[ProfileFact], linkedin: Sequence[ProfileFact], out: _Builder
) -> None:
    for fact in linkedin:
        parts = fact.field_key.split(".")
        # One check per item: the whole value, or an entry's employer or school.
        whole = len(parts) <= 2 or parts[2] == _ENTRY_SECTIONS.get(parts[0])
        if fact.state is FactState.REJECTED and whole and not _covered(fact, confirmed):
            out.add(
                SuggestionKind.CHECK,
                fact.field_key,
                "",
                [],
                [fact],
                fact.value_raw,
            )


def _jobs_by_fact(targets: Sequence[TargetJob]) -> dict[int, tuple[set[int], bool]]:
    found: dict[int, tuple[set[int], bool]] = {}
    for target in targets:
        for check in target.checks:
            if check.outcome is not Outcome.MET:
                continue
            required = check.requirement.importance is Importance.REQUIRED
            for evidence in check.evidence:
                jobs, req = found.get(evidence.fact_id, (set[int](), False))
                jobs.add(target.job_id)
                found[evidence.fact_id] = (jobs, req or required)
    return found


def _gaps(targets: Sequence[TargetJob]) -> tuple[Gap, ...]:
    grouped: dict[tuple[str, bool], tuple[str, Importance, set[int]]] = {}
    for target in targets:
        for check in target.checks:
            if check.outcome is Outcome.MET:
                continue
            text = check.requirement.text
            only = check.outcome is Outcome.UNREVIEWED_ONLY
            key = (_words(text), only)
            _, importance, jobs = grouped.get(key, (text, check.requirement.importance, set()))
            if check.requirement.importance is Importance.REQUIRED:
                importance = Importance.REQUIRED
            jobs.add(target.job_id)
            grouped[key] = (text, importance, jobs)
    gaps = [
        Gap(requirement=text, importance=imp, job_ids=tuple(sorted(jobs)), unreviewed_only=only)
        for (_, only), (text, imp, jobs) in grouped.items()
    ]
    return tuple(
        sorted(gaps, key=lambda g: (g.importance is not Importance.REQUIRED, -len(g.job_ids)))
    )


def check_suggestion(suggestion: Suggestion, facts: Mapping[int, ProfileFact]) -> list[str]:
    """Words of the suggested text that its cited confirmed facts do not contain.

    Uses the T13 claim rules (numbers, names, acronyms, technology words). Empty means the
    suggestion only says what the cited facts say.
    """
    if suggestion.kind is SuggestionKind.CHECK:
        return []
    cited = [facts[i] for i in suggestion.fact_ids if i in facts]
    if not cited or len(cited) != len(suggestion.fact_ids):
        return ["<no confirmed evidence>"]
    if any(f.state is not FactState.CONFIRMED for f in cited):
        return ["<unconfirmed evidence>"]
    terms = frozenset(tokens(suggestion.text))
    return unsupported(suggestion.text, [fact_text(f) for f in cited], terms)


_ORDER: Final = {name: index for index, name in enumerate(SECTIONS)}


def _rank(s: Suggestion) -> tuple[int, int, int, int, str]:
    return (
        0 if s.required_by_jobs else 1 if s.job_ids else 2,
        -len(s.job_ids),
        _ORDER.get(s.section, len(_ORDER)),
        list(SuggestionKind).index(s.kind),
        s.address,
    )


def suggest(
    linkedin: Sequence[ProfileFact],
    confirmed: Sequence[ProfileFact],
    targets: Sequence[TargetJob] = (),
) -> Suggestions:
    """Suggest LinkedIn changes backed by confirmed facts, most useful for target jobs first.

    `linkedin` are the facts the latest LinkedIn import saw, in any review state: they are what
    LinkedIn shows, true or not. Only `confirmed` facts (the active profile version) back a
    suggestion; a LinkedIn value the user rejected with nothing confirmed for it is a CHECK.
    """
    mine = [f for f in _relevant(confirmed) if f.state is FactState.CONFIRMED]
    # Everything the import saw is on LinkedIn, whatever the user decided about it.
    on_linkedin = _relevant(linkedin)
    by_id = {f.id: f for f in mine if f.id is not None}

    out = _Builder(_jobs_by_fact(targets))
    _singles(mine, on_linkedin, out)
    _entry_sections(mine, on_linkedin, out)
    listed = _named_sections(mine, on_linkedin, out)
    _evidence_skills(targets, by_id, listed, out)
    _rejected(mine, on_linkedin, out)

    kept = [s for s in out.found if not check_suggestion(s, by_id)]
    return Suggestions(
        suggestions=tuple(sorted(kept, key=_rank)),
        gaps=_gaps(targets),
        unreviewed_linkedin=sum(f.state is FactState.UNREVIEWED for f in on_linkedin),
        withheld=len(out.found) - len(kept),
    )


def to_json(suggestion: Suggestion) -> dict[str, JsonValue]:
    return {
        "kind": suggestion.kind.value,
        "section": suggestion.section,
        "address": suggestion.address,
        "text": suggestion.text,
        "current": suggestion.current,
        "fact_ids": list(suggestion.fact_ids),
        "linkedin_fact_ids": list(suggestion.linkedin_fact_ids),
        "job_ids": list(suggestion.job_ids),
        "required_by_jobs": suggestion.required_by_jobs,
    }
