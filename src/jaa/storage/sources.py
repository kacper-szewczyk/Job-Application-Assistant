"""Pause/resume state of job source connectors (ADR 0011). History is append-only."""

import sqlite3

from jaa.core.clock import utcnow
from jaa.core.models import SourceStatus, SourceStatusEvent, StatusActor
from jaa.storage.repository import source_status_events


def latest_status_event(conn: sqlite3.Connection, source: str) -> SourceStatusEvent | None:
    """The most recent pause/resume of `source`, or None if it was never paused."""
    row = conn.execute(
        "SELECT * FROM source_status_events WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)
    ).fetchone()
    return None if row is None else source_status_events.from_row(row)


def set_source_status(
    conn: sqlite3.Connection,
    source: str,
    status: SourceStatus,
    actor: StatusActor,
    reason: str | None = None,
) -> SourceStatusEvent | None:
    """Record a pause or resume. Returns None, recording nothing, if the status is unchanged."""
    with conn:
        latest = latest_status_event(conn, source)
        current = SourceStatus.ACTIVE if latest is None else latest.status
        if current == status:
            return None
        return source_status_events.insert(
            conn,
            SourceStatusEvent(
                source=source, status=status, actor=actor, reason=reason, occurred_at=utcnow()
            ),
        )


def status_history(conn: sqlite3.Connection, source: str) -> list[SourceStatusEvent]:
    return source_status_events.list(conn, source=source)
