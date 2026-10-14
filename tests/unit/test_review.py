import pytest

from jaa.core import models as m
from jaa.core.errors import ReviewError
from jaa.core.models import FactState
from jaa.core.review import check_transition, find_conflicts, same_value
from tests.conftest import T0


def _fact(id_: int, key: str, value: m.JsonValue, state: FactState) -> m.ProfileFact:
    return m.ProfileFact(
        id=id_,
        field_key=key,
        value_raw=str(value),
        value_normalized=value,
        state=state,
        origin=m.FactOrigin.IMPORT,
        created_at=T0,
        updated_at=T0,
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (FactState.UNREVIEWED, FactState.CONFIRMED),
        (FactState.UNREVIEWED, FactState.REJECTED),
        (FactState.CONFIRMED, FactState.REJECTED),
        (FactState.REJECTED, FactState.CONFIRMED),
    ],
)
def test_allowed_transitions(current: FactState, target: FactState) -> None:
    check_transition(1, current, target)


@pytest.mark.parametrize("current", list(FactState))
def test_facts_never_return_to_unreviewed(current: FactState) -> None:
    with pytest.raises(ReviewError):
        check_transition(1, current, FactState.UNREVIEWED)


def test_same_value_is_type_aware() -> None:
    assert same_value({"a": 1, "b": [2]}, {"b": [2], "a": 1})
    assert not same_value(1, True)
    assert not same_value(1, "1")


def test_incoming_value_conflicts_with_confirmed() -> None:
    confirmed = _fact(1, "experience.acme.title", "Engineer", FactState.CONFIRMED)
    same = _fact(2, "experience.acme.title", "Engineer", FactState.UNREVIEWED)
    different = _fact(3, "experience.acme.title", "Senior Engineer", FactState.UNREVIEWED)
    [conflict] = find_conflicts([confirmed, same, different])
    assert conflict.field_key == "experience.acme.title"
    assert conflict.current == confirmed
    assert conflict.candidates == (different,)


def test_disagreeing_candidates_without_confirmed_value() -> None:
    a = _fact(1, "location", "Berlin", FactState.UNREVIEWED)
    b = _fact(2, "location", "Munich", FactState.UNREVIEWED)
    [conflict] = find_conflicts([a, b])
    assert conflict.current is None
    assert conflict.candidates == (a, b)


def test_no_conflict_cases() -> None:
    facts = [
        _fact(1, "location", "Berlin", FactState.CONFIRMED),
        _fact(2, "location", "Munich", FactState.REJECTED),  # already decided
        _fact(3, "skills.python", "Python", FactState.UNREVIEWED),  # new key, single value
        _fact(4, "skills.go", "Go", FactState.CONFIRMED),
    ]
    assert find_conflicts(facts) == []
