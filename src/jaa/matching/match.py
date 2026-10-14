"""Advisory match of a job against the profile and preferences (T12, ADR 0015). Pure functions.

Every requirement is checked against profile facts by wording:

* a listed skill matches a skill or certification fact with the same words, or a mention of it
  in a headline, summary, job title, job description or degree;
* a listed language matches a language fact for the same language (English and Polish names
  are understood; levels are shown, not compared);
* a description line matches when it names one of the profile's skills, certifications or
  languages.

Only confirmed facts of the profile version count. Unreviewed facts that would match are shown,
labeled, and do not raise the score. Preferences (seniority, work arrangement, salary, target
roles, locations) add fits and mismatches where both sides are known, and uncertainty where
they are not.

The score is the share of weighted points met: required requirements count 2, preferred ones
and each comparable preference 1. Nothing here invents a fact or a preference: a requirement
without evidence is a gap, never assumed.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

from jaa.core.dedupe import fold, plain
from jaa.core.job_details import SalaryRange, WorkArrangement
from jaa.core.models import FactState, JsonObject, JsonValue, ProfileFact
from jaa.documents.content import text_of
from jaa.matching.requirements import (
    Importance,
    JobRequirements,
    Requirement,
    RequirementKind,
)

# Stored with each match; bump when the rules change so older matches can be told apart.
METHOD: Final = "wording-v1"
WEIGHTS: Final = {Importance.REQUIRED: 2.0, Importance.PREFERRED: 1.0}
PREFERENCE_WEIGHT: Final = 1.0


class Outcome(StrEnum):
    MET = "met"
    # Only unreviewed facts would back it: shown, not counted.
    UNREVIEWED_ONLY = "unreviewed_only"
    MISSING = "missing"


class PreferenceOutcome(StrEnum):
    FITS = "fits"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence:
    fact_id: int
    field_key: str
    state: FactState
    # The words of the fact that matched, e.g. "Python". Profile content: never log it.
    term: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequirementCheck:
    requirement: Requirement
    outcome: Outcome
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PreferenceCheck:
    key: str
    outcome: PreferenceOutcome
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Uncertainty:
    code: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchResult:
    score: float
    checks: tuple[RequirementCheck, ...]
    preferences: tuple[PreferenceCheck, ...]
    uncertainty: tuple[Uncertainty, ...]
    reasons: tuple[str, ...]
    # False when there was nothing to compare: the score then means nothing.
    scored: bool = True

    def evidence_json(self) -> list[JsonValue]:
        found: list[JsonValue] = [
            _check_json(c) for c in self.checks if c.outcome is not Outcome.MISSING
        ]
        found += [
            _preference_json(p) for p in self.preferences if p.outcome is PreferenceOutcome.FITS
        ]
        return found

    def gaps_json(self) -> list[JsonValue]:
        found: list[JsonValue] = [
            _check_json(c) for c in self.checks if c.outcome is Outcome.MISSING
        ]
        found += [
            _preference_json(p) for p in self.preferences if p.outcome is PreferenceOutcome.MISMATCH
        ]
        return found

    def uncertainty_json(self) -> list[JsonValue]:
        return [{"code": u.code, "message": u.message} for u in self.uncertainty]


def _check_json(check: RequirementCheck) -> JsonObject:
    r = check.requirement
    return {
        "type": "requirement",
        "requirement": r.text,
        "level": r.level,
        "kind": r.kind.value,
        "importance": r.importance.value,
        "origin": r.origin.value,
        "outcome": check.outcome.value,
        "facts": [
            {"fact_id": e.fact_id, "field_key": e.field_key, "state": e.state.value, "term": e.term}
            for e in check.evidence
        ],
    }


def _preference_json(check: PreferenceCheck) -> JsonObject:
    return {
        "type": "preference",
        "preference": check.key,
        "outcome": check.outcome.value,
        "reason": check.reason,
    }


# Tokens keep `+`, `#` and inner dots so that `C++`, `C#` and `Node.js` are not `C` or `Node`.
_TOKEN: Final = re.compile(r"[0-9a-z+#]+(?:\.[0-9a-z+#]+)*")


def tokens(text: str) -> tuple[str, ...]:
    """Lowercase words without diacritics, keeping the symbols that name technologies."""
    return tuple(_TOKEN.findall(plain(text)))


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    return any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


# Language names as English words; Polish names and the stems of their inflected forms
# ("języka angielskiego") map to the same name.
_LANGUAGES: Final[dict[str, tuple[str, ...]]] = {
    "english": ("english", "angielsk"),
    "polish": ("polish", "polsk"),
    "german": ("german", "niemieck"),
    "french": ("french", "francusk"),
    "spanish": ("spanish", "hiszpansk"),
    "italian": ("italian", "wlosk"),
    "russian": ("russian", "rosyjsk"),
    "ukrainian": ("ukrainian", "ukrainsk"),
    "czech": ("czech", "czesk"),
    "slovak": ("slovak", "slowack"),
    "dutch": ("dutch", "niderlandzk", "holendersk"),
    "swedish": ("swedish", "szwedzk"),
    "norwegian": ("norwegian", "norwesk"),
    "danish": ("danish", "dunsk"),
    "finnish": ("finnish", "finsk"),
    "portuguese": ("portuguese", "portugalsk"),
    "japanese": ("japanese", "japonsk"),
    "chinese": ("chinese", "chinsk"),
    "hungarian": ("hungarian", "wegiersk"),
    "romanian": ("romanian", "rumunsk"),
}


def language_name(text: str) -> str | None:
    """The English name of the language `text` names, or None."""
    words = fold(text).split()
    for name, stems in _LANGUAGES.items():
        if any(w.startswith(stem) for w in words for stem in stems):
            return name
    return None


@dataclass(frozen=True, slots=True)
class _Term:
    fact: ProfileFact
    words: tuple[str, ...]
    display: str


# Facts whose text may mention a skill (by key prefix and last key part).
_MENTION_KEYS: Final = ("headline", "summary")
_MENTION_PARTS: Final = {"title", "description", "heading", "degree", "details"}


@dataclass(frozen=True, slots=True)
class _Index:
    skills: tuple[_Term, ...]
    languages: tuple[tuple[ProfileFact, str], ...]
    mentions: tuple[_Term, ...]


def _index(facts: Sequence[ProfileFact]) -> _Index:
    skills: list[_Term] = []
    languages: list[tuple[ProfileFact, str]] = []
    mentions: list[_Term] = []
    for fact in facts:
        key = fact.field_key
        value = fact.value_normalized
        if key.startswith("skills.") or key.startswith("certifications."):
            text = text_of(value)
            if text and (words := tokens(text)):
                skills.append(_Term(fact, words, text))
        elif key.startswith("languages."):
            text = text_of(value)
            name = language_name(text) if text else None
            if text and name:
                languages.append((fact, name))
        elif key in _MENTION_KEYS or (
            key.startswith(("experience.", "education.")) and key.split(".")[-1] in _MENTION_PARTS
        ):
            for line in _lines(value):
                if words := tokens(line):
                    mentions.append(_Term(fact, words, line))
    return _Index(tuple(skills), tuple(languages), tuple(mentions))


def _lines(value: JsonValue) -> list[str]:
    if isinstance(value, list):
        return [t for t in (text_of(v) for v in cast(list[JsonValue], value)) if t]
    text = text_of(value)
    return [text] if text else []


def _evidence(fact: ProfileFact, term: str) -> Evidence:
    return Evidence(fact_id=fact.id or 0, field_key=fact.field_key, state=fact.state, term=term)


def _find(requirement: Requirement, index: _Index) -> list[Evidence]:
    """Facts in `index` that back `requirement`."""
    wanted = tokens(requirement.text)
    if requirement.kind is RequirementKind.LANGUAGE:
        name = language_name(requirement.text)
        return [_evidence(f, name) for f, n in index.languages if name is not None and n == name]
    if requirement.kind is RequirementKind.SKILL:
        direct = [
            _evidence(t.fact, t.display)
            for t in index.skills
            if _contains(t.words, wanted) or _contains(wanted, t.words)
        ]
        if direct:
            return direct
        mentioned = {t.fact.id: t for t in index.mentions if _contains(t.words, wanted)}
        return [_evidence(t.fact, requirement.text) for t in mentioned.values()]
    found = [_evidence(t.fact, t.display) for t in index.skills if _contains(wanted, t.words)]
    words = fold(requirement.text).split()
    found += [
        _evidence(f, n)
        for f, n in index.languages
        if any(w.startswith(stem) for w in words for stem in _LANGUAGES[n])
    ]
    return found


def _check(requirement: Requirement, confirmed: _Index, unreviewed: _Index) -> RequirementCheck:
    if evidence := _find(requirement, confirmed):
        return RequirementCheck(
            requirement=requirement, outcome=Outcome.MET, evidence=tuple(evidence)
        )
    if evidence := _find(requirement, unreviewed):
        return RequirementCheck(
            requirement=requirement, outcome=Outcome.UNREVIEWED_ONLY, evidence=tuple(evidence)
        )
    return RequirementCheck(requirement=requirement, outcome=Outcome.MISSING)


# Source seniority labels, folded, to `search.seniority` choices.
SENIORITY: Final = {
    "intern": "entry",
    "internship": "entry",
    "trainee": "entry",
    "junior": "entry",
    "entry": "entry",
    "regular": "mid",
    "mid": "mid",
    "medium": "mid",
    "senior": "senior",
    "expert": "lead",
    "lead": "lead",
    "staff": "lead",
    "principal": "principal",
    "manager": "manager",
    "director": "director",
}


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[JsonValue], value) if isinstance(v, str) and v.strip()]


@dataclass(slots=True)
class _Preferences:
    checks: list[PreferenceCheck] = field(default_factory=list[PreferenceCheck])
    uncertainty: list[Uncertainty] = field(default_factory=list[Uncertainty])

    def fits(self, key: str, reason: str) -> None:
        self.checks.append(PreferenceCheck(key=key, outcome=PreferenceOutcome.FITS, reason=reason))

    def mismatch(self, key: str, reason: str) -> None:
        self.checks.append(
            PreferenceCheck(key=key, outcome=PreferenceOutcome.MISMATCH, reason=reason)
        )

    def unsure(self, code: str, message: str) -> None:
        self.uncertainty.append(Uncertainty(code=code, message=message))


def _seniority(out: _Preferences, wanted: list[str], level: str | None) -> None:
    if not wanted or level is None:
        return
    mapped = SENIORITY.get(fold(level))
    if mapped is None:
        out.unsure(
            "seniority_unknown", f"The listed experience level {level!r} is not one jaa knows."
        )
    elif mapped in wanted:
        out.fits("search.seniority", f"Experience level {level} is one you are looking for.")
    else:
        out.mismatch("search.seniority", f"Experience level {level} is not one you chose.")


def _arrangement(out: _Preferences, wanted: list[str], listed: WorkArrangement | None) -> None:
    if not wanted:
        return
    if listed is None:
        out.unsure(
            "arrangement_unknown",
            "The listing does not say whether it is remote, hybrid or on-site.",
        )
    elif listed.value in wanted:
        out.fits("search.work_arrangement", f"{_ARRANGEMENT[listed]} work is acceptable to you.")
    else:
        out.mismatch(
            "search.work_arrangement", f"{_ARRANGEMENT[listed]} work is not one you chose."
        )


_ARRANGEMENT: Final = {
    WorkArrangement.REMOTE: "Remote",
    WorkArrangement.HYBRID: "Hybrid",
    WorkArrangement.ON_SITE: "On-site",
}


def _salary(out: _Preferences, wanted: JsonValue, listed: tuple[SalaryRange, ...]) -> None:
    # Messages hold no amounts: the salary preference is sensitive.
    if not isinstance(wanted, dict):
        return
    minimum, currency, period = wanted.get("min"), wanted.get("currency"), wanted.get("period")
    if not isinstance(minimum, int) or not isinstance(currency, str) or not isinstance(period, str):
        return
    if not listed:
        out.unsure("no_salary", "The listing gives no salary.")
        return
    comparable = [s for s in listed if s.currency == currency and s.period == period]
    if not comparable:
        out.unsure(
            "salary_not_comparable",
            "The listed salary is in another currency or period than your range.",
        )
        return
    tops = [top for s in comparable if (top := s.max if s.max is not None else s.min) is not None]
    if any(top >= minimum for top in tops):
        out.fits("salary.range", "The listed salary reaches your minimum.")
    else:
        out.mismatch("salary.range", "The listed salary is below your minimum.")


def _roles(out: _Preferences, wanted: list[str], title: str) -> None:
    if not wanted:
        return
    title_words = fold(title).split()
    if any(_contains(title_words, fold(role).split()) for role in wanted):
        out.fits("search.target_roles", "The title names one of your target roles.")
    else:
        out.unsure("title_not_target", "The title does not name any of your target roles.")


def _locations(
    out: _Preferences, wanted: list[str], location: str | None, arrangement: WorkArrangement | None
) -> None:
    if not wanted or arrangement is WorkArrangement.REMOTE:
        return
    if location is None:
        out.unsure("location_unknown", "The listing gives no location.")
        return
    place = fold(location).split()
    if any(_contains(place, fold(w).split()) for w in wanted):
        out.fits("search.locations", "The location is one of yours.")
    else:
        # Not a mismatch: "Poland" in the preferences does cover "Warszawa".
        out.unsure("location_unclear", "The location is not one you listed; check it.")


def _preferences(
    requirements: JobRequirements,
    title: str,
    location: str | None,
    preferences: Mapping[str, JsonValue],
) -> _Preferences:
    out = _Preferences()
    _roles(out, _strings(preferences.get("search.target_roles")), title)
    _seniority(out, _strings(preferences.get("search.seniority")), requirements.experience_level)
    _arrangement(
        out, _strings(preferences.get("search.work_arrangement")), requirements.work_arrangement
    )
    _locations(
        out, _strings(preferences.get("search.locations")), location, requirements.work_arrangement
    )
    _salary(out, preferences.get("salary.range"), requirements.salaries)
    return out


def _points(weight: float) -> str:
    return f"{weight:g} point" + ("" if weight == 1 else "s")


def _count(checks: Sequence[RequirementCheck], importance: Importance) -> tuple[int, int]:
    mine = [c for c in checks if c.requirement.importance is importance]
    return sum(c.outcome is Outcome.MET for c in mine), len(mine)


def match_job(
    requirements: JobRequirements,
    *,
    title: str,
    location: str | None,
    confirmed: Sequence[ProfileFact],
    unreviewed: Sequence[ProfileFact] = (),
    preferences: Mapping[str, JsonValue] | None = None,
) -> MatchResult:
    """Compare a job's requirements with facts and preferences.

    `confirmed` must be the facts of one profile version; facts in another state are ignored
    there. `unreviewed` facts are only used to label requirements they would back.
    """
    good = _index([f for f in confirmed if f.state is FactState.CONFIRMED])
    pending = _index([f for f in unreviewed if f.state is FactState.UNREVIEWED])
    checks = tuple(_check(r, good, pending) for r in requirements.requirements)
    prefs = _preferences(requirements, title, location, preferences or {})
    uncertainty = _uncertainty(requirements, checks, preferences or {}) + prefs.uncertainty

    points = sum(WEIGHTS[c.requirement.importance] for c in checks if c.outcome is Outcome.MET)
    points += sum(PREFERENCE_WEIGHT for p in prefs.checks if p.outcome is PreferenceOutcome.FITS)
    total = sum(WEIGHTS[c.requirement.importance] for c in checks)
    total += PREFERENCE_WEIGHT * len(prefs.checks)
    scored = total > 0
    score = round(points / total, 4) if scored else 0.0

    reasons: list[str] = []
    for importance, label in (
        (Importance.REQUIRED, "Required"),
        (Importance.PREFERRED, "Nice to have"),
    ):
        met, of = _count(checks, importance)
        if of:
            each = _points(WEIGHTS[importance])
            reasons.append(f"{label}: {met} of {of} backed by confirmed facts ({each} each).")
    if prefs.checks:
        fits = sum(p.outcome is PreferenceOutcome.FITS for p in prefs.checks)
        reasons.append(
            f"Preferences: {fits} of {len(prefs.checks)} fit ({PREFERENCE_WEIGHT:g} point each)."
        )
    if scored:
        reasons.append(f"Score {score:.0%}: {points:g} of {total:g} points.")
    else:
        uncertainty.append(
            Uncertainty(
                code="nothing_to_compare",
                message="Nothing in the listing could be compared, so there is no score.",
            )
        )
    return MatchResult(
        score=score,
        checks=checks,
        preferences=tuple(prefs.checks),
        uncertainty=tuple(uncertainty),
        reasons=tuple(reasons),
        scored=scored,
    )


def _uncertainty(
    requirements: JobRequirements,
    checks: Sequence[RequirementCheck],
    preferences: Mapping[str, JsonValue],
) -> list[Uncertainty]:
    found: list[Uncertainty] = []
    if not requirements.has_description:
        found.append(Uncertainty(code="no_description", message="The job has no description."))
    elif not requirements.sections_found:
        found.append(
            Uncertainty(
                code="no_sections",
                message=(
                    "No requirements heading was recognized in the description, so only the "
                    "listed skills and languages were compared. Read the description yourself."
                ),
            )
        )
    text = [c for c in checks if c.requirement.kind is RequirementKind.TEXT]
    if text:
        found.append(
            Uncertainty(
                code="compared_by_wording",
                message=(
                    f"{len(text)} requirement(s) from the description were compared by wording "
                    "only. A gap may be something you have that your profile does not name."
                ),
            )
        )
    pending = [c for c in checks if c.outcome is Outcome.UNREVIEWED_ONLY]
    if pending:
        found.append(
            Uncertainty(
                code="unreviewed_evidence",
                message=(
                    f"{len(pending)} requirement(s) are backed only by unreviewed facts and do "
                    "not count. Review those facts with `jaa profile facts` to count them."
                ),
            )
        )
    if any(c.requirement.kind is RequirementKind.LANGUAGE and c.requirement.level for c in checks):
        found.append(
            Uncertainty(code="language_levels", message="Language levels were not compared.")
        )
    if not preferences:
        found.append(
            Uncertainty(
                code="no_preferences",
                message=(
                    "No preferences are set, so salary, location and seniority were not checked."
                ),
            )
        )
    return found
