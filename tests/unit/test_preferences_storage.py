import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from jaa.core.errors import PreferenceError, StorageError
from jaa.core.models import JsonValue, PreferenceStatus
from jaa.storage import preferences as prefs
from jaa.storage.db import Migration, connect, load_migrations, migrate, open_database
from jaa.storage.repository import preference_events
from tests.conftest import T0

SALARY: JsonValue = {"min": 987654, "max": None, "currency": "EUR", "period": "year"}
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def test_set_skip_and_history(conn: sqlite3.Connection) -> None:
    prefs.set_preference(conn, "search.target_roles", ["Widget Engineer"], at=T0)
    prefs.set_preference(conn, "search.target_roles", ["Gadget Engineer"], at=T1)
    stored = prefs.skip_preference(conn, "search.target_roles", at=T2)

    assert stored.status is PreferenceStatus.SKIPPED
    assert stored.value is None
    assert stored.updated_at == T2
    history = prefs.preference_history(conn, "search.target_roles")
    assert [(e.status, e.value, e.occurred_at) for e in history] == [
        (PreferenceStatus.ANSWERED, ["Widget Engineer"], T0),
        (PreferenceStatus.ANSWERED, ["Gadget Engineer"], T1),
        (PreferenceStatus.SKIPPED, None, T2),
    ]


def test_repeating_the_same_answer_records_nothing(conn: sqlite3.Connection) -> None:
    prefs.set_preference(conn, "search.travel", "none", at=T0)
    again = prefs.set_preference(conn, "search.travel", "none", at=T1)
    assert again.updated_at == T0
    prefs.skip_preference(conn, "search.relocation", at=T0)
    prefs.skip_preference(conn, "search.relocation", at=T1)
    assert len(prefs.preference_history(conn, "search.travel")) == 1
    assert len(prefs.preference_history(conn, "search.relocation")) == 1


def test_sensitive_flag_comes_from_the_question(conn: sqlite3.Connection) -> None:
    salary = prefs.set_preference(conn, "salary.range", SALARY)
    sponsorship = prefs.skip_preference(conn, "sponsorship.required")
    travel = prefs.set_preference(conn, "search.travel", "none")
    assert (salary.is_sensitive, sponsorship.is_sensitive, travel.is_sensitive) == (
        True,
        True,
        False,
    )
    assert prefs.preference_history(conn, "salary.range")[0].is_sensitive


def test_unknown_keys_and_bad_values_are_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(PreferenceError):
        prefs.set_preference(conn, "search.favourite_colour", "blue")
    with pytest.raises(PreferenceError):
        prefs.skip_preference(conn, "search.favourite_colour")
    with pytest.raises(PreferenceError):
        prefs.set_preference(conn, "search.travel", "sometimes")
    assert prefs.list_preferences(conn) == {}


def test_unanswered_keys_include_skipped(conn: sqlite3.Connection) -> None:
    prefs.set_preference(conn, "search.travel", "none")
    prefs.skip_preference(conn, "search.relocation")
    pending = prefs.unanswered_keys(conn)
    assert "search.travel" not in pending
    assert "search.relocation" in pending
    assert pending[0] == "search.target_roles"
    assert prefs.answered_values(conn) == {"search.travel": "none"}


def test_preferences_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "jaa.sqlite3"
    first = open_database(path)
    prefs.set_preference(first, "salary.range", SALARY)
    first.close()
    second = open_database(path)
    try:
        assert prefs.answered_values(second) == {"salary.range": SALARY}
    finally:
        second.close()


def test_history_is_append_only(conn: sqlite3.Connection) -> None:
    prefs.set_preference(conn, "search.travel", "none")
    event = prefs.preference_history(conn, "search.travel")[0]
    assert event.id is not None
    with pytest.raises(StorageError, match="append-only"):
        preference_events.update(conn, event.id, status=PreferenceStatus.SKIPPED)
    conn.rollback()


def test_logs_keys_not_values(conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        prefs.set_preference(conn, "salary.range", SALARY)
        prefs.set_preference(conn, "search.target_roles", ["Synthetic Role Zeta"])
    assert "salary.range" in caplog.text
    assert "987654" not in caplog.text
    assert "Synthetic Role Zeta" not in caplog.text


def test_migration_keeps_existing_preferences_answered(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    migrations: list[Migration] = load_migrations()
    migrate(conn, migrations[:2])
    conn.execute(
        "INSERT INTO preferences (key, value, is_sensitive, updated_at) VALUES (?, ?, 0, ?)",
        ("search.travel", '"none"', T0.isoformat()),
    )
    conn.commit()
    migrate(conn, migrations)
    try:
        stored = prefs.get_preference(conn, "search.travel")
        assert stored is not None
        assert stored.status is PreferenceStatus.ANSWERED
    finally:
        conn.close()
