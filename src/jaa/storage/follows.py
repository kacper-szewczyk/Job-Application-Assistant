"""Source targets the user follows, such as companies' career boards (#71, ADR 0025).

Following only says what `jaa jobs fetch --followed` fetches. History is append-only: the latest
event per (source, target) decides. Callers pass canonical targets (`Connector.target_key`).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.models import FollowAction, SourceFollow
from jaa.storage.repository import source_follows


@dataclass(frozen=True, slots=True, kw_only=True)
class Followed:
    source: str
    target: str
    since: datetime


def _latest(conn: sqlite3.Connection, source: str, target: str) -> SourceFollow | None:
    row = conn.execute(
        "SELECT * FROM source_follows WHERE source = ? AND target = ? ORDER BY id DESC LIMIT 1",
        (source, target),
    ).fetchone()
    return None if row is None else source_follows.from_row(row)


def _record(conn: sqlite3.Connection, source: str, target: str, action: FollowAction) -> bool:
    with conn:
        latest = _latest(conn, source, target)
        current = FollowAction.UNFOLLOW if latest is None else latest.action
        if current is action:
            return False
        source_follows.insert(
            conn,
            SourceFollow(source=source, target=target, action=action, occurred_at=utcnow()),
        )
        return True


def follow(conn: sqlite3.Connection, source: str, target: str) -> bool:
    """Follow a target. False, recording nothing, if it is already followed."""
    return _record(conn, source, target, FollowAction.FOLLOW)


def unfollow(conn: sqlite3.Connection, source: str, target: str) -> bool:
    """Stop following a target. False, recording nothing, if it was not followed.

    Jobs already fetched from it stay, with their history.
    """
    return _record(conn, source, target, FollowAction.UNFOLLOW)


def followed(conn: sqlite3.Connection) -> list[Followed]:
    """Every followed target, by source and target, with when it was last followed."""
    rows = conn.execute(
        "SELECT f.* FROM source_follows f "
        "JOIN (SELECT MAX(id) AS id FROM source_follows GROUP BY source, target) latest "
        "ON latest.id = f.id ORDER BY f.source, f.target"
    ).fetchall()
    events = (source_follows.from_row(r) for r in rows)
    return [
        Followed(source=e.source, target=e.target, since=e.occurred_at)
        for e in events
        if e.action is FollowAction.FOLLOW
    ]
