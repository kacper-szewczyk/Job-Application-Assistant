"""Pure rules for reviewing profile facts: transitions, value equality, conflicts (ADR 0006)."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from jaa.core.errors import ReviewError
from jaa.core.models import FactState, JsonValue, ProfileFact

# Facts never return to `unreviewed`; a decision can only be reversed by the opposite decision.
ALLOWED_TRANSITIONS: Final[dict[FactState, frozenset[FactState]]] = {
    FactState.UNREVIEWED: frozenset({FactState.CONFIRMED, FactState.REJECTED}),
    FactState.CONFIRMED: frozenset({FactState.REJECTED}),
    FactState.REJECTED: frozenset({FactState.CONFIRMED}),
}


def check_transition(fact_id: int | None, current: FactState, target: FactState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ReviewError(f"fact {fact_id} cannot go from {current} to {target}")


def value_key(value: JsonValue) -> str:
    """Canonical form for comparing normalized values (so 1, 1.0 and True stay distinct)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def same_value(a: JsonValue, b: JsonValue) -> bool:
    return value_key(a) == value_key(b)


@dataclass(frozen=True, slots=True)
class FieldConflict:
    """Competing values for one field key that the user has to choose between.

    `current` is the confirmed fact, if any; `candidates` are unreviewed facts whose values
    differ from it (or from each other, when nothing is confirmed yet).
    """

    field_key: str
    current: ProfileFact | None
    candidates: tuple[ProfileFact, ...]


def find_conflicts(facts: Iterable[ProfileFact]) -> list[FieldConflict]:
    """Group facts by field key and report keys whose pending values disagree, sorted by key."""
    by_key: dict[str, list[ProfileFact]] = {}
    for fact in facts:
        by_key.setdefault(fact.field_key, []).append(fact)

    conflicts: list[FieldConflict] = []
    for key in sorted(by_key):
        group = by_key[key]
        current = next((f for f in group if f.state is FactState.CONFIRMED), None)
        pending = [f for f in group if f.state is FactState.UNREVIEWED]
        if current is not None:
            differing = [
                f for f in pending if not same_value(f.value_normalized, current.value_normalized)
            ]
        elif len({value_key(f.value_normalized) for f in pending}) > 1:
            differing = pending
        else:
            differing = []
        if differing:
            conflicts.append(FieldConflict(key, current, tuple(differing)))
    return conflicts
