from collections.abc import Mapping, Sequence

from jaa.core.job_details import SalaryRange, WorkArrangement
from jaa.core.models import FactOrigin, FactState, JsonValue, ProfileFact
from jaa.matching.match import (
    MatchResult,
    Outcome,
    PreferenceOutcome,
    language_name,
    match_job,
    tokens,
)
from jaa.matching.requirements import (
    Importance,
    JobRequirements,
    Requirement,
    RequirementKind,
    RequirementOrigin,
)
from tests.fixtures.cv_profile import T0, confirmed_facts


def _req(
    text: str,
    kind: RequirementKind = RequirementKind.TEXT,
    importance: Importance = Importance.REQUIRED,
    level: str | None = None,
) -> Requirement:
    origin = (
        RequirementOrigin.DESCRIPTION if kind is RequirementKind.TEXT else RequirementOrigin.LISTED
    )
    return Requirement(text=text, kind=kind, importance=importance, origin=origin, level=level)


def _reqs(
    *items: Requirement,
    experience_level: str | None = None,
    work_arrangement: WorkArrangement | None = None,
    salaries: tuple[SalaryRange, ...] = (),
) -> JobRequirements:
    return JobRequirements(
        requirements=items,
        experience_level=experience_level,
        work_arrangement=work_arrangement,
        salaries=salaries,
        has_description=True,
        sections_found=True,
    )


def _unreviewed(fact_id: int, key: str, value: JsonValue) -> ProfileFact:
    return ProfileFact(
        id=fact_id,
        field_key=key,
        value_raw=str(value),
        value_normalized=value,
        state=FactState.UNREVIEWED,
        origin=FactOrigin.IMPORT,
        created_at=T0,
        updated_at=T0,
    )


FACTS = confirmed_facts()
BY_KEY = {f.field_key: f for f in FACTS}


def _match(
    requirements: JobRequirements,
    *,
    title: str = "Widget Engineer",
    location: str | None = None,
    unreviewed: Sequence[ProfileFact] = (),
    preferences: Mapping[str, JsonValue] | None = None,
) -> MatchResult:
    return match_job(
        requirements,
        title=title,
        location=location,
        confirmed=FACTS,
        unreviewed=unreviewed,
        preferences=preferences,
    )


def test_tokens_keep_technology_symbols() -> None:
    assert tokens("C++, C#, Node.js and Łódź.") == ("c++", "c#", "node.js", "and", "lodz")


def test_language_names_in_english_and_polish() -> None:
    assert language_name("Angielski") == "english"
    assert language_name("bardzo dobra znajomość języka angielskiego") == "english"
    assert language_name("Polish") == "polish"
    assert language_name("Klingon") is None


def test_listed_skill_matches_a_skill_fact() -> None:
    result = _match(_reqs(_req("Python", RequirementKind.SKILL, level="Advanced")))
    [check] = result.checks
    assert check.outcome is Outcome.MET
    assert [(e.fact_id, e.term) for e in check.evidence] == [(BY_KEY["skills.python"].id, "Python")]
    assert result.score == 1.0


def test_c_plus_plus_is_not_c() -> None:
    result = _match(_reqs(_req("C", RequirementKind.SKILL), _req("C++", RequirementKind.SKILL)))
    assert [c.outcome for c in result.checks] == [Outcome.MISSING, Outcome.MET]


def test_listed_skill_can_be_backed_by_a_mention_in_experience() -> None:
    result = _match(_reqs(_req("Event streaming", RequirementKind.SKILL)))
    [check] = result.checks
    assert check.outcome is Outcome.MET
    assert check.evidence[0].field_key == "experience.example-co.description"


def test_languages_match_across_english_and_polish_names() -> None:
    result = _match(
        _reqs(
            _req("Angielski", RequirementKind.LANGUAGE, level="Advanced"),
            _req("Niemiecki", RequirementKind.LANGUAGE),
        )
    )
    assert [c.outcome for c in result.checks] == [Outcome.MET, Outcome.MISSING]
    assert "language_levels" in {u.code for u in result.uncertainty}


def test_description_lines_match_by_named_skills_and_languages() -> None:
    result = _match(
        _reqs(
            _req("3+ years of Python and Django"),
            _req("Bardzo dobra znajomość języka angielskiego"),
            _req("Experience with Kubernetes"),
        )
    )
    assert [c.outcome for c in result.checks] == [Outcome.MET, Outcome.MET, Outcome.MISSING]
    assert "compared_by_wording" in {u.code for u in result.uncertainty}


def test_unreviewed_facts_are_labeled_and_not_counted() -> None:
    docker = _unreviewed(900, "skills.docker", "Docker")
    result = _match(
        _reqs(_req("Docker", RequirementKind.SKILL), _req("Python", RequirementKind.SKILL)),
        unreviewed=[docker],
    )
    first, second = result.checks
    assert first.outcome is Outcome.UNREVIEWED_ONLY
    assert [(e.fact_id, e.state) for e in first.evidence] == [(900, FactState.UNREVIEWED)]
    assert second.outcome is Outcome.MET
    assert result.score == 0.5
    [stored] = [
        e for e in result.evidence_json() if isinstance(e, dict) and e["requirement"] == "Docker"
    ]
    assert stored["outcome"] == "unreviewed_only"
    assert stored["facts"] == [
        {"fact_id": 900, "field_key": "skills.docker", "state": "unreviewed", "term": "Docker"}
    ]
    assert "unreviewed_evidence" in {u.code for u in result.uncertainty}


def test_unconfirmed_facts_passed_as_confirmed_are_ignored() -> None:
    docker = _unreviewed(901, "skills.docker", "Docker")
    result = match_job(
        _reqs(_req("Docker", RequirementKind.SKILL)),
        title="Widget Engineer",
        location=None,
        confirmed=[docker],
    )
    assert result.checks[0].outcome is Outcome.MISSING


def test_score_weights_and_reasons() -> None:
    result = _match(
        _reqs(
            _req("Python", RequirementKind.SKILL),
            _req("Kubernetes", RequirementKind.SKILL),
            _req("React Native", RequirementKind.SKILL, Importance.PREFERRED),
        )
    )
    # Python 2 + React Native 1 out of 2 + 2 + 1.
    assert result.score == 0.6
    assert result.reasons == (
        "Required: 1 of 2 backed by confirmed facts (2 points each).",
        "Nice to have: 1 of 1 backed by confirmed facts (1 point each).",
        "Score 60%: 3 of 5 points.",
    )
    gaps = result.gaps_json()
    assert [g["requirement"] for g in gaps if isinstance(g, dict)] == ["Kubernetes"]


def test_nothing_to_compare_has_no_score() -> None:
    result = _match(JobRequirements(has_description=True))
    assert not result.scored
    assert result.score == 0.0
    codes = {u.code for u in result.uncertainty}
    assert {"no_sections", "nothing_to_compare", "no_preferences"} <= codes


def test_preferences_fit_and_mismatch() -> None:
    requirements = _reqs(
        experience_level="Junior",
        work_arrangement=WorkArrangement.HYBRID,
        salaries=(SalaryRange(min=10000, max=12000, currency="PLN", period="month"),),
    )
    result = _match(
        requirements,
        title="Senior Widget Engineer",
        location="Wrocław (hybrid)",
        preferences={
            "search.target_roles": ["Widget Engineer"],
            "search.seniority": ["senior", "lead"],
            "search.work_arrangement": ["remote", "hybrid"],
            "search.locations": ["Wroclaw"],
            "salary.range": {"min": 15000, "max": None, "currency": "PLN", "period": "month"},
        },
    )
    outcomes = {p.key: p.outcome for p in result.preferences}
    assert outcomes == {
        "search.target_roles": PreferenceOutcome.FITS,
        "search.seniority": PreferenceOutcome.MISMATCH,
        "search.work_arrangement": PreferenceOutcome.FITS,
        "search.locations": PreferenceOutcome.FITS,
        "salary.range": PreferenceOutcome.MISMATCH,
    }
    assert result.score == 0.6
    # The salary preference is sensitive: no amounts in what is stored.
    assert "15000" not in str(result.gaps_json()) and "12000" not in str(result.gaps_json())


def test_unknowns_are_uncertain_not_gaps() -> None:
    requirements = _reqs(
        experience_level="Wizard",
        salaries=(SalaryRange(min=5000, max=6000, currency="EUR", period="month"),),
    )
    result = _match(
        requirements,
        title="Gadget Wrangler",
        location="Gdańsk",
        preferences={
            "search.target_roles": ["Widget Engineer"],
            "search.seniority": ["senior"],
            "search.work_arrangement": ["remote"],
            "search.locations": ["Poland"],
            "salary.range": {"min": 15000, "max": None, "currency": "PLN", "period": "month"},
        },
    )
    assert result.preferences == ()
    assert {u.code for u in result.uncertainty} >= {
        "title_not_target",
        "seniority_unknown",
        "arrangement_unknown",
        "location_unclear",
        "salary_not_comparable",
    }
    assert not result.scored
