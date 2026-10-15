from dataclasses import replace

import pytest

from jaa.core.errors import TailoringError
from jaa.core.models import FactState, JsonValue
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.latex import get_template, render
from jaa.documents.tailoring import (
    Edit,
    EditKind,
    IssueCode,
    Paragraph,
    Plan,
    claims,
    job_terms,
    plan_from_json,
    propose,
    tailor,
)
from tests.fixtures.cv_profile import confirmed_facts

FACTS = confirmed_facts()
BY_KEY = {f.field_key: f for f in FACTS}
TERMS = job_terms(["Kubernetes", "Python", "English"])
BULLET_1 = "experience.example-co.description[1]"
BULLET_2 = "experience.example-co.description[2]"


def _id(key: str) -> int:
    fact_id = BY_KEY[key].id
    assert fact_id is not None
    return fact_id


def _codes(plan: Plan) -> list[set[str]]:
    result = tailor(plan, FACTS, job_terms=TERMS)
    return [{i.code.value for i in c.issues} for c in result.edit_checks]


def test_empty_plan_is_the_baseline() -> None:
    result = tailor(Plan(), FACTS)
    assert result.content == result.baseline
    assert result.content == build_content(FACTS).content
    assert not result.changes and not result.blocked


def test_select_reorder_and_emphasize() -> None:
    plan = Plan(
        edits=(
            Edit(kind=EditKind.MOVE, target="skills.react-native", position=1),
            Edit(kind=EditKind.EMPHASIZE, target="skills.react-native"),
            Edit(kind=EditKind.HIDE, target="skills.c-plus-plus"),
            Edit(kind=EditKind.MOVE, target=BULLET_2, position=1),
            Edit(kind=EditKind.EMPHASIZE, target=BULLET_1),
            Edit(kind=EditKind.HIDE, target="experience.gadgets"),
            Edit(kind=EditKind.HIDE, target="certifications.psm-i"),
        )
    )
    result = tailor(plan, FACTS)
    assert not result.blocked
    content = result.content
    assert content.skills == ("React Native", "Python")
    assert content.skill_keys == ("skills.react-native", "skills.python")
    assert content.emphasized_skills == (0,)
    [entry] = content.experience
    assert entry.items[0].startswith("Mentored four engineers")
    assert entry.emphasized == (1,)  # the first baseline bullet, now second
    assert content.certifications == ()
    # Dates, titles and employers are untouched.
    assert entry.dates == result.baseline.experience[0].dates
    assert entry.role == result.baseline.experience[0].role
    # Hidden facts no longer count as used; the others still do.
    assert _id("skills.c-plus-plus") not in content.fact_ids
    assert _id("experience.gadgets.title") not in content.fact_ids
    assert _id("certifications.psm-i") not in content.fact_ids
    assert _id("skills.python") in content.fact_ids
    kinds = [(c.kind, c.target) for c in result.changes]
    assert kinds[0] == (EditKind.MOVE, "skills.react-native")
    move = result.changes[0]
    assert (move.before, move.after) == ("position 3", "position 1")
    assert move.fact_ids == (_id("skills.react-native"),)
    hide = result.changes[2]
    assert hide.before == "C++" and hide.after is None


def test_every_edit_links_to_the_targets_fact() -> None:
    plan = Plan(edits=(Edit(kind=EditKind.EMPHASIZE, target=BULLET_1),))
    [check] = tailor(plan, FACTS).edit_checks
    assert check.fact_ids == (_id("experience.example-co.description"),)


def test_rewrite_backed_by_cited_facts_is_applied() -> None:
    text = "Led the migration of widget pipelines to event streaming, using Python."
    plan = Plan(
        edits=(
            Edit(
                kind=EditKind.REWRITE, target=BULLET_1, text=text, fact_ids=(_id("skills.python"),)
            ),
        )
    )
    result = tailor(plan, FACTS, job_terms=TERMS)
    assert not result.blocked
    assert result.content.experience[0].items[0] == text
    [change] = result.changes
    assert change.before == "Led the migration of widget pipelines to event streaming."
    assert change.after == text
    assert set(change.fact_ids) == {
        _id("experience.example-co.description"),
        _id("skills.python"),
    }
    assert _id("skills.python") in result.content.fact_ids


def test_rewording_with_the_targets_own_words_needs_no_citation() -> None:
    plan = Plan(
        edits=(
            Edit(kind=EditKind.REWRITE, target=BULLET_1, text="Led widget pipelines' migration."),
        )
    )
    assert _codes(plan) == [set()]


@pytest.mark.parametrize(
    ("text", "unsupported"),
    [
        ("Led the migration of 40 widget pipelines.", "40"),  # a new number
        ("Led the migration of widget pipelines at Google.", "Google"),  # a new name
        ("Led the kubernetes migration of widget pipelines.", "kubernetes"),  # a job keyword
        ("Led the migration of widget pipelines in C#.", "C#"),
    ],
)
def test_rewrite_with_unsupported_claims_is_blocked(text: str, unsupported: str) -> None:
    plan = Plan(edits=(Edit(kind=EditKind.REWRITE, target=BULLET_1, text=text),))
    result = tailor(plan, FACTS, job_terms=TERMS)
    [check] = result.edit_checks
    assert {i.code for i in check.issues} == {IssueCode.UNSUPPORTED_TERMS}
    assert unsupported in check.issues[0].message
    assert check.blocked and check.approvable
    assert result.blocked
    # Blocked edits are not applied.
    assert result.content == result.baseline
    assert not result.changes


def test_approved_rewrite_is_applied() -> None:
    edit = Edit(kind=EditKind.REWRITE, target="headline", text="Kubernetes Engineer")
    result = tailor(
        Plan(edits=(edit,)), FACTS, job_terms=TERMS, approvals=frozenset({edit.fingerprint()})
    )
    [check] = result.edit_checks
    assert check.issues and check.approved and not check.blocked
    assert result.content.headline == "Kubernetes Engineer"
    # A changed edit is a different edit: the approval does not carry over.
    changed = replace(edit, text="Kubernetes Lead")
    again = tailor(
        Plan(edits=(changed,)), FACTS, job_terms=TERMS, approvals=frozenset({edit.fingerprint()})
    )
    assert again.blocked


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (Edit(kind=EditKind.HIDE, target="experience.example-co.dates"), "unknown_target"),
        (Edit(kind=EditKind.REWRITE, target="experience.example-co", text="x"), "not_allowed"),
        (Edit(kind=EditKind.REWRITE, target="skills.python", text="Python 3"), "not_allowed"),
        (Edit(kind=EditKind.HIDE, target="headline"), "not_allowed"),
        (Edit(kind=EditKind.MOVE, target="skills.python"), "missing_position"),
        (Edit(kind=EditKind.REWRITE, target="summary", text="  "), "empty_text"),
        (Edit(kind=EditKind.EMPHASIZE, target="skills.python", fact_ids=(999,)), "unknown_fact"),
        (Edit(kind=EditKind.HIDE, target=f"{BULLET_1[:-3]}[9]"), "unknown_target"),
    ],
)
def test_invalid_edits_cannot_be_approved(edit: Edit, code: str) -> None:
    result = tailor(Plan(edits=(edit,)), FACTS, approvals=frozenset({edit.fingerprint()}))
    [check] = result.edit_checks
    assert code in {i.code.value for i in check.issues}
    assert not check.approvable and check.blocked


def test_edits_on_hidden_targets_are_rejected() -> None:
    plan = Plan(
        edits=(
            Edit(kind=EditKind.HIDE, target="experience.example-co"),
            Edit(kind=EditKind.EMPHASIZE, target=BULLET_1),
        )
    )
    assert _codes(plan) == [set(), {"not_allowed"}]


def test_unreviewed_facts_need_approval() -> None:
    pending = replace(
        BY_KEY["skills.python"],
        id=500,
        field_key="skills.kubernetes",
        value_raw="Kubernetes",
        value_normalized="Kubernetes",
        state=FactState.UNREVIEWED,
    )
    edit = Edit(
        kind=EditKind.REWRITE,
        target=BULLET_1,
        text="Led the migration of widget pipelines to Kubernetes.",
        fact_ids=(500,),
    )
    result = tailor(Plan(edits=(edit,)), FACTS, unreviewed=[pending], job_terms=TERMS)
    [check] = result.edit_checks
    assert {i.code for i in check.issues} == {IssueCode.UNREVIEWED_FACT}
    assert check.blocked and check.approvable


def test_cover_letter_paragraphs_need_backing_facts() -> None:
    plan = Plan(
        cover_letter=(
            Paragraph(text="I build widgets in Python.", fact_ids=(_id("skills.python"),)),
            Paragraph(text="I have 10 years of Kubernetes experience."),
        )
    )
    result = tailor(plan, FACTS, job_terms=TERMS)
    first, second = result.paragraph_checks
    assert not first.issues and first.fact_ids == (_id("skills.python"),)
    assert {i.code for i in second.issues} == {IssueCode.UNSUPPORTED_TERMS, IssueCode.NO_EVIDENCE}
    assert result.blocked


def test_claims_ignore_sentence_initial_capitals() -> None:
    assert claims("Led teams. Built APIs for Example Co.", frozenset()) == ["APIs", "Example", "Co"]
    assert claims("built python tools", frozenset({"python"})) == ["python"]


def _evidence(fact_id: int, key: str, term: str, importance: str = "required") -> JsonValue:
    return {
        "type": "requirement",
        "requirement": term,
        "importance": importance,
        "outcome": "met",
        "facts": [{"fact_id": fact_id, "field_key": key, "state": "confirmed", "term": term}],
    }


def test_proposal_reorders_and_emphasizes_matched_skills_and_bullets() -> None:
    evidence: list[JsonValue] = [
        _evidence(_id("skills.react-native"), "skills.react-native", "React Native", "preferred"),
        _evidence(_id("skills.python"), "skills.python", "Python"),
        _evidence(
            _id("experience.example-co.description"),
            "experience.example-co.description",
            "engineers",
        ),
        {"type": "requirement", "outcome": "missing", "requirement": "Go", "facts": []},
        {
            "type": "requirement",
            "outcome": "unreviewed_only",
            "requirement": "Rust",
            "facts": [{"fact_id": 99, "field_key": "skills.rust", "state": "unreviewed"}],
        },
    ]
    plan = propose(FACTS, evidence)
    summary = [(e.kind, e.target, e.position) for e in plan.edits]
    # Python is already first, so it is not moved.
    assert summary == [
        (EditKind.EMPHASIZE, "skills.python", None),
        (EditKind.MOVE, "skills.react-native", 2),
        (EditKind.EMPHASIZE, "skills.react-native", None),
        (EditKind.MOVE, BULLET_2, 1),
    ]
    assert plan.edits[0].reason == "the job asks for Python"
    assert all(not (e.kind is EditKind.MOVE and e.target == "skills.python") for e in plan.edits)
    result = tailor(plan, FACTS)
    assert not result.blocked
    assert result.content.skills == ("Python", "React Native", "C++")
    assert result.content.experience[0].items[0].startswith("Mentored")


def test_plan_json_round_trip_and_validation() -> None:
    plan = Plan(
        edits=(
            Edit(kind=EditKind.MOVE, target="skills.python", position=2, reason="why"),
            Edit(kind=EditKind.REWRITE, target="summary", text="New", fact_ids=(1, 2)),
        ),
        cover_letter=(Paragraph(text="Hello", fact_ids=(3,)),),
    )
    assert plan_from_json(plan.to_json()) == plan
    with pytest.raises(TailoringError, match="unknown field"):
        plan_from_json({"edits": [{"kind": "hide", "target": "summary", "approved": True}]})
    with pytest.raises(TailoringError, match="kind must be one of"):
        plan_from_json({"edits": [{"kind": "invent", "target": "summary"}]})
    with pytest.raises(TailoringError, match="position"):
        plan_from_json({"edits": [{"kind": "move", "target": "summary", "position": 0}]})
    with pytest.raises(TailoringError, match="fact_ids"):
        plan_from_json({"edits": [{"kind": "hide", "target": "summary", "fact_ids": ["1"]}]})
    with pytest.raises(TailoringError, match="text is empty"):
        plan_from_json({"cover_letter": [{"text": " "}]})


def test_emphasis_is_rendered_bold() -> None:
    plan = Plan(
        edits=(
            Edit(kind=EditKind.EMPHASIZE, target="skills.python"),
            Edit(kind=EditKind.EMPHASIZE, target=BULLET_1),
        )
    )
    content = tailor(plan, FACTS).content
    source = render(get_template("moderncv-classic"), content, CvLanguage.EN)
    assert r"\textbf{Python}, C++, React Native" in source
    assert r"\cvlistitem{\textbf{Led the migration" in source
    baseline = render(get_template("moderncv-classic"), build_content(FACTS).content, CvLanguage.EN)
    assert r"\textbf{Python}" not in baseline
