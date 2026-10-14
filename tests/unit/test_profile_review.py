import io
import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

from jaa.core import models as m
from jaa.core.errors import FactConflictError, NotFoundError, ReviewError, StorageError
from jaa.core.models import FactState
from jaa.core.review import find_conflicts
from jaa.storage import profile as svc
from jaa.storage import repository as repo
from tests.conftest import SYNTHETIC_EMAIL, SYNTHETIC_TITLE, T0, ident

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
TITLE_KEY = "experience.example-co.title"


def _run(at: datetime = T0) -> m.ImportRun:
    return m.ImportRun(
        source_type=m.ImportSourceType.TEXT,
        source_path="/synthetic/cv.txt",
        imported_at=at,
        status=m.ImportStatus.SUCCEEDED,
        retention=m.RetentionChoice.KEEP,
    )


def _candidate(
    key: str,
    value: str,
    *,
    confidence: float | None = None,
    source_location: str | None = None,
    source_excerpt: str | None = None,
) -> m.FactCandidate:
    return m.FactCandidate(
        field_key=key,
        value_raw=value,
        value_normalized=value,
        confidence=confidence,
        source_location=source_location,
        source_excerpt=source_excerpt,
    )


def _import(conn: sqlite3.Connection, *candidates: m.FactCandidate, at: datetime = T0) -> list[int]:
    result = svc.add_imported_facts(conn, _run(at), candidates)
    return [ident(f.id) for f in result.added]


def _state(conn: sqlite3.Connection, fact_id: int) -> FactState:
    return repo.profile_facts.get(conn, fact_id).state


def test_import_stores_unreviewed_candidates_with_provenance(conn: sqlite3.Connection) -> None:
    result = svc.add_imported_facts(
        conn,
        _run(),
        [
            _candidate(TITLE_KEY, SYNTHETIC_TITLE, confidence=0.7, source_location="line 3"),
            _candidate("contact.email", SYNTHETIC_EMAIL),
        ],
    )
    title, email = result.added
    assert {f.state for f in result.added} == {FactState.UNREVIEWED}
    assert {f.origin for f in result.added} == {m.FactOrigin.IMPORT}
    assert title.import_run_id == result.run.id
    assert title.created_at == T0
    assert (title.confidence, title.source_location) == (0.7, "line 3")
    # Sensitivity defaults from the field key.
    assert (title.is_sensitive, email.is_sensitive) == (False, True)
    assert svc.active_version_id(conn) is None


def test_reimport_skips_identical_values(conn: sqlite3.Connection) -> None:
    [fact_id] = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE))
    svc.confirm_facts(conn, [fact_id], at=T1)
    result = svc.add_imported_facts(
        conn,
        _run(T2),
        [_candidate(TITLE_KEY, SYNTHETIC_TITLE), _candidate("skills.go", "Go")] * 2,
    )
    assert [f.field_key for f in result.added] == ["skills.go"]
    assert result.duplicates == 3


def test_confirm_creates_active_version(conn: sqlite3.Connection) -> None:
    a, b = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE), _candidate("skills.go", "Go"))
    result = svc.confirm_facts(conn, [a, b], at=T1)
    version = result.version
    assert version is not None
    assert version.created_at == T1
    assert svc.active_version_id(conn) == version.id
    assert svc.version_fact_ids(conn, ident(version.id)) == [a, b]
    fact = repo.profile_facts.get(conn, a)
    assert (fact.state, fact.confirmed_at, fact.updated_at) == (FactState.CONFIRMED, T1, T1)
    assert [
        (e.from_state, e.to_state, e.profile_version_id) for e in svc.fact_history(conn, a)
    ] == [(FactState.UNREVIEWED, FactState.CONFIRMED, version.id)]


def test_every_confirmation_is_a_new_immutable_version(conn: sqlite3.Connection) -> None:
    a, b = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE), _candidate("skills.go", "Go"))
    first = svc.confirm_facts(conn, [a], at=T1).version
    second = svc.confirm_facts(conn, [b], at=T2).version
    assert first is not None and second is not None
    assert first.id != second.id
    assert svc.version_fact_ids(conn, ident(first.id)) == [a]
    assert svc.version_fact_ids(conn, ident(second.id)) == [a, b]
    assert svc.active_version_id(conn) == second.id
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE profile_versions SET note = 'x'")


def test_confirming_confirmed_fact_is_a_no_op(conn: sqlite3.Connection) -> None:
    [a] = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE))
    svc.confirm_facts(conn, [a], at=T1)
    result = svc.confirm_facts(conn, [a], at=T2)
    assert result.changed == ()
    assert result.version is None
    assert repo.profile_facts.get(conn, a).confirmed_at == T1


def test_reimport_never_overwrites_confirmed_fact(conn: sqlite3.Connection) -> None:
    [old] = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE))
    svc.confirm_facts(conn, [old], at=T1)
    [new] = _import(conn, _candidate(TITLE_KEY, "Chief Widget Officer"), at=T2)

    assert _state(conn, old) is FactState.CONFIRMED
    assert _state(conn, new) is FactState.UNREVIEWED
    [conflict] = find_conflicts(repo.profile_facts.list(conn))
    assert conflict.current is not None and conflict.current.id == old
    assert [c.id for c in conflict.candidates] == [new]

    with pytest.raises(FactConflictError, match=f"confirmed fact {old}"):
        svc.confirm_facts(conn, [new])
    assert (_state(conn, old), _state(conn, new)) == (FactState.CONFIRMED, FactState.UNREVIEWED)


def test_keep_current_by_rejecting_incoming(conn: sqlite3.Connection) -> None:
    [old] = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE))
    svc.confirm_facts(conn, [old], at=T1)
    [new] = _import(conn, _candidate(TITLE_KEY, "Chief Widget Officer"), at=T2)
    result = svc.reject_facts(conn, [new], at=T2)
    assert result.version is None  # the confirmed set did not change
    assert repo.profile_facts.get(conn, new).rejected_at == T2
    assert find_conflicts(repo.profile_facts.list(conn)) == []


def test_accept_incoming_with_replace(conn: sqlite3.Connection) -> None:
    [old] = _import(conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE))
    svc.confirm_facts(conn, [old], at=T1)
    [new] = _import(conn, _candidate(TITLE_KEY, "Chief Widget Officer"), at=T2)
    result = svc.confirm_facts(conn, [new], replace=True, at=T2)
    assert result.version is not None
    assert [(f.id, f.state) for f in result.changed] == [
        (old, FactState.REJECTED),
        (new, FactState.CONFIRMED),
    ]
    assert svc.version_fact_ids(conn, ident(result.version.id)) == [new]


def test_one_confirmed_fact_per_key_is_enforced_by_the_database(conn: sqlite3.Connection) -> None:
    a, b = _import(conn, _candidate("location", "Berlin"), _candidate("location", "Munich"))
    svc.confirm_facts(conn, [a])
    with pytest.raises(StorageError, match="constraint"):
        repo.profile_facts.update(conn, b, state=FactState.CONFIRMED)


def test_cannot_confirm_two_values_for_one_key(conn: sqlite3.Connection) -> None:
    a, b = _import(conn, _candidate("location", "Berlin"), _candidate("location", "Munich"))
    with pytest.raises(ReviewError, match="same field key"):
        svc.confirm_facts(conn, [a, b])


def test_failed_review_changes_nothing(conn: sqlite3.Connection) -> None:
    [a] = _import(conn, _candidate("location", "Berlin"))
    with pytest.raises(NotFoundError):
        svc.confirm_facts(conn, [a, 999])
    assert _state(conn, a) is FactState.UNREVIEWED
    assert repo.profile_versions.list(conn) == []
    assert svc.fact_history(conn, a) == []


def test_reject_confirmed_fact_creates_version(conn: sqlite3.Connection) -> None:
    a, b = _import(conn, _candidate("location", "Berlin"), _candidate("skills.go", "Go"))
    svc.confirm_facts(conn, [a, b], at=T1)
    result = svc.reject_facts(conn, [a], at=T2)
    assert result.version is not None
    assert svc.version_fact_ids(conn, ident(result.version.id)) == [b]
    # A rejected fact can be confirmed again later.
    assert svc.confirm_facts(conn, [a]).changed[0].state is FactState.CONFIRMED


def test_correct_fact_keeps_original_and_provenance(conn: sqlite3.Connection) -> None:
    [orig] = _import(conn, _candidate(TITLE_KEY, "Principal Widget Enginer", source_excerpt="x"))
    result = svc.correct_fact(conn, orig, SYNTHETIC_TITLE, SYNTHETIC_TITLE, at=T1)
    original, correction = result.changed
    assert original.id == orig and original.state is FactState.REJECTED
    assert original.value_raw == "Principal Widget Enginer"
    assert original.source_excerpt == "x"
    assert correction.origin is m.FactOrigin.CORRECTION
    assert correction.state is FactState.CONFIRMED
    assert correction.corrected_from_fact_id == orig
    assert correction.confirmed_at == T1
    assert result.version is not None
    assert svc.version_fact_ids(conn, ident(result.version.id)) == [ident(correction.id)]
    assert [f.id for f in svc.correction_chain(conn, ident(correction.id))] == [correction.id, orig]
    assert [(e.from_state, e.to_state) for e in svc.fact_history(conn, ident(correction.id))] == [
        (None, FactState.CONFIRMED)
    ]


def test_correct_confirmed_fact(conn: sqlite3.Connection) -> None:
    [orig] = _import(conn, _candidate("contact.email", "old@example.test"))
    svc.confirm_facts(conn, [orig])
    result = svc.correct_fact(conn, orig, SYNTHETIC_EMAIL, SYNTHETIC_EMAIL)
    assert result.changed[-1].is_sensitive  # sensitivity carries over
    assert _state(conn, orig) is FactState.REJECTED


def test_correct_candidate_conflicting_with_confirmed_needs_replace(
    conn: sqlite3.Connection,
) -> None:
    a, b = _import(conn, _candidate("location", "Berlin"), _candidate("location", "Munich"))
    svc.confirm_facts(conn, [a])
    with pytest.raises(FactConflictError):
        svc.correct_fact(conn, b, "Hamburg", "Hamburg")
    result = svc.correct_fact(conn, b, "Hamburg", "Hamburg", replace=True)
    assert [f.state for f in result.changed] == [
        FactState.REJECTED,
        FactState.REJECTED,
        FactState.CONFIRMED,
    ]


def test_correct_with_same_value_is_refused(conn: sqlite3.Connection) -> None:
    [a] = _import(conn, _candidate("location", "Berlin"))
    with pytest.raises(ReviewError, match="confirm it instead"):
        svc.correct_fact(conn, a, "Berlin", "Berlin")


def test_review_history_is_append_only(conn: sqlite3.Connection) -> None:
    [a] = _import(conn, _candidate("location", "Berlin"))
    svc.confirm_facts(conn, [a])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE profile_fact_events SET to_state = 'rejected'")


def test_review_workflow_never_logs_values(conn: sqlite3.Connection) -> None:
    logger = logging.getLogger("jaa")
    raw = io.StringIO()
    handler = logging.StreamHandler(raw)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        title, email = _import(
            conn, _candidate(TITLE_KEY, SYNTHETIC_TITLE), _candidate("contact.email", "a@b.test")
        )
        svc.confirm_facts(conn, [title, email])
        svc.correct_fact(conn, email, SYNTHETIC_EMAIL, SYNTHETIC_EMAIL)
        svc.reject_facts(conn, [title])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    text = raw.getvalue()  # unscrubbed: proves values are never passed to the logger
    assert "created profile version" in text
    for secret in (SYNTHETIC_TITLE, SYNTHETIC_EMAIL, "a@b.test"):
        assert secret not in text
