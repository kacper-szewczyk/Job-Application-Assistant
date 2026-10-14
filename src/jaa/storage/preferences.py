"""Preference persistence for onboarding and later edits (ADR 0007).

One row per preference key holds the current answer or a skip; every change is appended to
`preference_events`, which rejects updates. Only keys from `jaa.core.preferences.QUESTIONS`
are accepted, and values are checked against the question before they are stored.

Logging records keys and counts only, never values.
"""

import logging
import sqlite3
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import StorageError
from jaa.core.models import JsonValue, Preference, PreferenceEvent, PreferenceStatus
from jaa.core.preferences import QUESTIONS, check_value, is_sensitive_question, question
from jaa.core.review import same_value
from jaa.storage.repository import preference_events, preferences

log = logging.getLogger(__name__)


def get_preference(conn: sqlite3.Connection, key: str) -> Preference | None:
    question(key)  # raises PreferenceError for unknown keys
    found = preferences.list(conn, key=key)
    return found[0] if found else None


def _store(
    conn: sqlite3.Connection,
    key: str,
    status: PreferenceStatus,
    value: JsonValue,
    at: datetime | None,
) -> Preference:
    at = at or utcnow()
    sensitive = is_sensitive_question(question(key))
    with conn:
        current = get_preference(conn, key)
        if current is not None and current.status is status and same_value(current.value, value):
            return current
        if current is None:
            stored = preferences.insert(
                conn,
                Preference(
                    key=key, value=value, is_sensitive=sensitive, updated_at=at, status=status
                ),
            )
        else:
            if current.id is None:
                raise StorageError("preference row has no id")
            stored = preferences.update(
                conn, current.id, value=value, is_sensitive=sensitive, updated_at=at, status=status
            )
        preference_events.insert(
            conn,
            PreferenceEvent(
                preference_key=key,
                status=status,
                value=value,
                is_sensitive=sensitive,
                occurred_at=at,
            ),
        )
    log.info("preference %s %s", key, status)
    return stored


def set_preference(
    conn: sqlite3.Connection, key: str, value: JsonValue, *, at: datetime | None = None
) -> Preference:
    """Store the user's answer. Setting the current value again records nothing."""
    check_value(question(key), value)
    return _store(conn, key, PreferenceStatus.ANSWERED, value, at)


def skip_preference(
    conn: sqlite3.Connection, key: str, *, at: datetime | None = None
) -> Preference:
    """Record that the user skipped the question, clearing any earlier answer."""
    return _store(conn, key, PreferenceStatus.SKIPPED, None, at)


def list_preferences(conn: sqlite3.Connection) -> dict[str, Preference]:
    """Stored preferences (answered or skipped) by key, in question order."""
    stored = {p.key: p for p in preferences.list(conn)}
    return {q.key: stored[q.key] for q in QUESTIONS if q.key in stored}


def answered_values(conn: sqlite3.Connection) -> dict[str, JsonValue]:
    """Answered values by key, for consumers such as matching and answer reuse."""
    return {
        key: p.value
        for key, p in list_preferences(conn).items()
        if p.status is PreferenceStatus.ANSWERED
    }


def unanswered_keys(conn: sqlite3.Connection) -> list[str]:
    """Keys never asked or skipped, in question order: what onboarding still has to ask."""
    answered = answered_values(conn)
    return [q.key for q in QUESTIONS if q.key not in answered]


def preference_history(conn: sqlite3.Connection, key: str) -> list[PreferenceEvent]:
    question(key)
    return preference_events.list(conn, preference_key=key)
