from dataclasses import replace

from jaa.core.models import FactOrigin, FactState, JsonValue, ProfileFact
from jaa.documents.linkedin_suggestions import (
    Suggestion,
    SuggestionKind,
    TargetJob,
    check_suggestion,
    suggest,
)
from jaa.matching.match import match_job
from jaa.matching.requirements import (
    Importance,
    JobRequirements,
    Requirement,
    RequirementKind,
    RequirementOrigin,
)
from tests.fixtures.cv_profile import PROFILE, T0, confirmed_facts

CONFIRMED = confirmed_facts()
BY_KEY = {f.field_key: f for f in CONFIRMED}


def _id(key: str) -> int:
    fact_id = BY_KEY[key].id
    assert fact_id is not None
    return fact_id


def _linkedin(
    values: dict[str, JsonValue], state: FactState = FactState.UNREVIEWED, start: int = 1000
) -> list[ProfileFact]:
    """Facts as a LinkedIn import stores them; ids do not clash with CONFIRMED."""
    return [
        ProfileFact(
            id=start + i,
            field_key=key,
            value_raw=value if isinstance(value, str) else str(value),
            value_normalized=value,
            state=state,
            origin=FactOrigin.IMPORT,
            created_at=T0,
            updated_at=T0,
        )
        for i, (key, value) in enumerate(values.items())
    ]


def _same_as_confirmed(*keys: str) -> list[ProfileFact]:
    """LinkedIn shows exactly these confirmed facts (the import linked to the same rows)."""
    return [BY_KEY[k] for k in keys]


EVERYTHING = [
    f for f in CONFIRMED if not f.field_key.startswith(("contact.", "identity.", "hobbies."))
]


def _target(job_id: int, *requirements: Requirement) -> TargetJob:
    result = match_job(
        JobRequirements(requirements=requirements, has_description=True, sections_found=True),
        title="Widget Engineer",
        location=None,
        confirmed=CONFIRMED,
    )
    return TargetJob(job_id=job_id, checks=result.checks)


def _skill(text: str, importance: Importance = Importance.REQUIRED) -> Requirement:
    return Requirement(
        text=text,
        kind=RequirementKind.SKILL,
        importance=importance,
        origin=RequirementOrigin.LISTED,
    )


def _by_address(found: tuple[Suggestion, ...]) -> dict[str, Suggestion]:
    return {s.address: s for s in found}


def test_nothing_to_suggest_when_linkedin_shows_the_confirmed_profile() -> None:
    result = suggest(EVERYTHING, CONFIRMED)
    assert result.suggestions == ()
    assert result.gaps == ()
    assert result.withheld == 0


def test_missing_items_are_added_with_confirmed_wording() -> None:
    linkedin = _same_as_confirmed(
        "experience.example-co.employer",
        "experience.example-co.title",
        "experience.example-co.dates",
        "experience.example-co.description",
        "skills.python",
    )
    found = _by_address(suggest(linkedin, CONFIRMED).suggestions)

    gadgets = found["experience.gadgets"]
    assert gadgets.kind is SuggestionKind.ADD
    assert gadgets.text.startswith("Software Developer · Gadgets Sp. z o.o.")
    assert set(gadgets.fact_ids) == {
        _id("experience.gadgets.title"),
        _id("experience.gadgets.employer"),
        _id("experience.gadgets.dates"),
    }
    assert found["headline"].text == PROFILE["headline"]
    assert found["headline"].current is None
    assert found["skills.react-native"].text == "React Native"
    assert found["skills.react-native"].fact_ids == (_id("skills.react-native"),)
    assert {"education.example-university", "languages.english", "certifications.psm-i"} <= set(
        found
    )
    # Present on LinkedIn: not suggested.
    assert "skills.python" not in found
    assert "experience.example-co" not in found
    # Contact details and other sections are out of scope.
    assert not any(a.startswith(("contact.", "identity.", "hobbies.")) for a in found)


def test_outdated_values_are_updates_showing_what_linkedin_has() -> None:
    linkedin = [
        *_same_as_confirmed("experience.example-co.employer", "experience.example-co.description"),
        *_linkedin(
            {
                "headline": "Widget Engineer",
                "experience.example-co.title": "Widget Engineer",
                "experience.example-co.dates": {"start": "2022-01", "end": None, "current": True},
            }
        ),
    ]
    found = _by_address(suggest(linkedin, EVERYTHING).suggestions)
    title = found["experience.example-co.title"]
    assert title.kind is SuggestionKind.UPDATE
    assert (title.text, title.current) == ("Senior Widget Engineer", "Widget Engineer")
    assert title.fact_ids == (_id("experience.example-co.title"),)
    assert title.linkedin_fact_ids
    assert found["experience.example-co.dates"].kind is SuggestionKind.UPDATE
    assert found["headline"].kind is SuggestionKind.UPDATE


def test_entries_match_by_employer_name_when_keys_differ() -> None:
    linkedin = _linkedin(
        {
            "experience.example-co-2.employer": "EXAMPLE CO",
            "experience.example-co-2.title": "Senior Widget Engineer",
            "experience.example-co-2.dates": {"start": "2021-01", "end": None, "current": True},
            "experience.example-co-2.description": ["Something else, written differently."],
        }
    )
    found = _by_address(suggest(linkedin, EVERYTHING).suggestions)
    assert not any(a.startswith("experience.example-co") for a in found)


def test_a_missing_description_is_pointed_out_but_differing_wording_is_not() -> None:
    linkedin = _same_as_confirmed(
        "experience.example-co.employer",
        "experience.example-co.title",
        "experience.example-co.dates",
    )
    found = _by_address(suggest(linkedin, EVERYTHING).suggestions)
    description = found["experience.example-co.description"]
    assert description.kind is SuggestionKind.ADD
    assert description.text.splitlines()[0].startswith("Led the migration")


def test_certifications_without_issuer_match_the_linkedin_one() -> None:
    cert = BY_KEY["certifications.psm-i"]
    mine = replace(cert, value_normalized={"name": "Professional Scrum Master I", "date": None})
    linkedin = _same_as_confirmed("certifications.psm-i")
    found = _by_address(suggest(linkedin, [mine]).suggestions)
    assert found == {}


def test_target_jobs_rank_suggestions_and_name_the_jobs() -> None:
    linkedin = _same_as_confirmed("skills.python")
    targets = [
        _target(7, _skill("React Native"), _skill("Kubernetes")),
        _target(9, _skill("React Native", Importance.PREFERRED), _skill("Python")),
    ]
    result = suggest(linkedin, EVERYTHING, targets)
    first = result.suggestions[0]
    assert first.address == "skills.react-native"
    assert first.job_ids == (7, 9)
    assert first.required_by_jobs
    assert all(not s.job_ids for s in result.suggestions[1:])
    [gap] = result.gaps
    assert gap.requirement == "Kubernetes"
    assert gap.job_ids == (7,)
    assert gap.importance is Importance.REQUIRED
    # A gap is never turned into a suggestion.
    assert not any("kubernetes" in s.text.lower() for s in result.suggestions)


def test_skills_mentioned_in_confirmed_experience_are_suggested_in_the_facts_words() -> None:
    linkedin = _same_as_confirmed(*(f.field_key for f in EVERYTHING))
    result = suggest(linkedin, EVERYTHING, [_target(3, _skill("event streaming"))])
    [s] = result.suggestions
    assert s.kind is SuggestionKind.ADD
    assert s.address == "skills.event-streaming"
    assert s.text == "event streaming"
    assert s.fact_ids == (_id("experience.example-co.description"),)
    assert s.job_ids == (3,)
    assert check_suggestion(s, {f.id or 0: f for f in EVERYTHING}) == []


def test_unreviewed_facts_back_nothing() -> None:
    pending = [replace(f, state=FactState.UNREVIEWED) for f in EVERYTHING]
    result = suggest([], pending)
    assert result.suggestions == ()


def test_unreviewed_linkedin_values_are_counted() -> None:
    linkedin = _linkedin({"skills.go": "Go", "skills.rust": "Rust"})
    result = suggest(linkedin, EVERYTHING)
    assert result.unreviewed_linkedin == 2
    assert not any(s.address in {"skills.go", "skills.rust"} for s in result.suggestions)


def test_a_rejected_linkedin_value_with_nothing_confirmed_is_a_check() -> None:
    linkedin = [
        *_same_as_confirmed(*(f.field_key for f in EVERYTHING if f.field_key != "headline")),
        *_linkedin(
            {
                "skills.cobol": "COBOL",
                "experience.acme.employer": "Acme",
                "experience.acme.title": "Intern",
                # Rejected, but a confirmed headline covers it: that is an update, not a check.
                "headline": "Widget Person",
            },
            FactState.REJECTED,
        ),
    ]
    found = _by_address(suggest(linkedin, EVERYTHING).suggestions)
    assert found["skills.cobol"].kind is SuggestionKind.CHECK
    assert found["skills.cobol"].current == "COBOL"
    assert found["skills.cobol"].fact_ids == ()
    assert found["experience.acme.employer"].kind is SuggestionKind.CHECK
    assert "experience.acme.title" not in found
    assert found["headline"].kind is SuggestionKind.UPDATE


def test_sensitive_facts_are_left_out() -> None:
    secret = replace(BY_KEY["skills.python"], is_sensitive=True)
    assert suggest([], [secret]).suggestions == ()


def test_every_suggestion_passes_the_claim_check() -> None:
    targets = [_target(1, _skill("Python"), _skill("event streaming"), _skill("Go"))]
    result = suggest([], CONFIRMED, targets)
    facts = {f.id or 0: f for f in CONFIRMED}
    assert result.suggestions
    assert result.withheld == 0
    for s in result.suggestions:
        assert check_suggestion(s, facts) == [], s.address


def test_the_claim_check_flags_words_the_facts_do_not_contain() -> None:
    facts = {f.id or 0: f for f in CONFIRMED}
    invented = Suggestion(
        kind=SuggestionKind.UPDATE,
        address="experience.example-co.title",
        text="Principal Widget Engineer, 12 reports",
        fact_ids=(_id("experience.example-co.title"),),
    )
    assert set(check_suggestion(invented, facts)) >= {"Principal", "12", "reports"}
    uncited = replace(invented, text="Senior Widget Engineer", fact_ids=())
    assert check_suggestion(uncited, facts) == ["<no confirmed evidence>"]
    pending = {
        **facts,
        invented.fact_ids[0]: replace(
            BY_KEY["experience.example-co.title"], state=FactState.UNREVIEWED
        ),
    }
    assert check_suggestion(replace(invented, text="Senior Widget Engineer"), pending) == [
        "<unconfirmed evidence>"
    ]
