"""Storing connector listings as jobs: new, changed, reappeared and removed (T10, ADR 0013).

`store_listings` is the storage half and takes listings a connector already returned.
`fetch_and_store` runs a connector end to end: pause check, fetch, store, a `source_fetch_runs`
row for every run that reached the source, and a `system` pause when the source refuses access
(ADR 0011). Removal is only judged after a complete fetch of a target, and it never changes
`jobs.status`, which stays the user's lifecycle (ADR 0005). Logs carry counts only.
"""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from jaa.connectors.base import Connector, JobListing, SourceIssue, effective_state
from jaa.connectors.http import Transport
from jaa.connectors.run import FetchOutcome, run_connector
from jaa.core.clock import utcnow
from jaa.core.dedupe import canonical_url, dedupe_key
from jaa.core.errors import (
    ConnectorError,
    SourceAccessDeniedError,
    SourceDataError,
    SourceUnavailableError,
)
from jaa.core.models import (
    FetchRunOutcome,
    Job,
    JsonValue,
    SourceFetchRun,
    SourceStatus,
    StatusActor,
)
from jaa.storage.duplicates import flag_duplicates
from jaa.storage.jobs import insert_job
from jaa.storage.repository import jobs, source_fetch_runs
from jaa.storage.sources import latest_status_event, set_source_status

log = logging.getLogger(__name__)

MAX_ERROR_LENGTH: Final = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class ListingChanges:
    """Job ids by what happened to them in one fetch."""

    new: tuple[int, ...] = ()
    changed: tuple[int, ...] = ()
    unchanged: tuple[int, ...] = ()
    # Listed again after a fetch had marked them removed.
    reappeared: tuple[int, ...] = ()
    # Listed before in this target, missing from this complete fetch.
    removed: tuple[int, ...] = ()
    # Ids of new possible-duplicate flags (ADR 0014).
    flagged: tuple[int, ...] = ()


def content_sha256(listing: JobListing) -> str:
    """Hash of everything a listing says, to tell a changed listing from a re-fetched one."""
    payload = [
        listing.url,
        listing.title,
        listing.company,
        listing.location,
        listing.description,
        listing.posted_at.isoformat() if listing.posted_at else None,
        listing.updated_at.isoformat() if listing.updated_at else None,
        [[q.key, q.label, q.kind.value, q.required, list(q.options)] for q in listing.questions],
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _existing(conn: sqlite3.Connection, source: str, source_job_id: str) -> Job | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?", (source, source_job_id)
    ).fetchone()
    return None if row is None else jobs.from_row(row)


def store_listings(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    listings: Sequence[JobListing],
    *,
    at: datetime,
) -> ListingChanges:
    """Insert new listings, update changed ones and mark the target's missing jobs removed.

    `listings` must be a complete fetch of `target`: every job of this source and target that
    is not in it is marked removed. Runs in one transaction.
    """
    with conn:
        return _store(conn, source, target, listings, at)


def _store(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    listings: Sequence[JobListing],
    at: datetime,
) -> ListingChanges:
    new: list[int] = []
    changed: list[int] = []
    unchanged: list[int] = []
    reappeared: list[int] = []
    seen: set[str] = set()
    # Jobs whose identity (canonical URL or duplicate key) is new or changed: checked for
    # duplicates below.
    rekeyed: list[int] = []
    for listing in listings:
        seen.add(listing.source_job_id)
        digest = content_sha256(listing)
        keys = _keys(listing)
        current = _existing(conn, source, listing.source_job_id)
        if current is None:
            stored = insert_job(
                conn,
                Job(
                    source=source,
                    source_job_id=listing.source_job_id,
                    canonical_url=keys["canonical_url"] or listing.url,
                    source_url=listing.url,
                    dedupe_key=keys["dedupe_key"],
                    employer=listing.company,
                    title=listing.title,
                    location=listing.location,
                    description=listing.description,
                    posted_at=listing.posted_at,
                    first_seen_at=at,
                    last_seen_at=at,
                    status_changed_at=at,
                    source_target=target,
                    content_sha256=digest,
                    source_updated_at=listing.updated_at,
                ),
            )
            new.append(_id(stored))
            rekeyed.append(_id(stored))
            continue
        job_id = _id(current)
        updates: dict[str, object] = {"last_seen_at": at, "source_target": target}
        # Also fills in the keys of jobs stored before they existed.
        if (current.canonical_url, current.dedupe_key, current.source_url) != (
            keys["canonical_url"],
            keys["dedupe_key"],
            listing.url,
        ):
            updates |= keys | {"source_url": listing.url}
            rekeyed.append(job_id)
        if current.removed_at is not None:
            updates["removed_at"] = None
            reappeared.append(job_id)
        if current.content_sha256 != digest:
            updates |= {
                "employer": listing.company,
                "title": listing.title,
                "location": listing.location,
                "description": listing.description,
                "posted_at": listing.posted_at,
                "content_sha256": digest,
                "source_updated_at": listing.updated_at,
            }
            changed.append(job_id)
        else:
            unchanged.append(job_id)
        jobs.update(conn, job_id, **updates)
    removed = _mark_removed(conn, source, target, seen, at)
    flagged = flag_duplicates(conn, rekeyed, at)
    return ListingChanges(
        new=tuple(new),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        reappeared=tuple(reappeared),
        removed=removed,
        flagged=tuple(flagged),
    )


def _keys(listing: JobListing) -> dict[str, str | None]:
    return {
        "canonical_url": canonical_url(listing.canonical_url or listing.url),
        "dedupe_key": dedupe_key(listing.company, listing.title, listing.location),
    }


def _id(job: Job) -> int:
    if job.id is None:
        raise ValueError("stored job has no id")
    return job.id


def _mark_removed(
    conn: sqlite3.Connection, source: str, target: str, seen: set[str], at: datetime
) -> tuple[int, ...]:
    rows = conn.execute(
        "SELECT id, source_job_id FROM jobs "
        "WHERE source = ? AND source_target = ? AND removed_at IS NULL ORDER BY id",
        (source, target),
    ).fetchall()
    gone = tuple(int(r["id"]) for r in rows if r["source_job_id"] not in seen)
    for job_id in gone:
        jobs.update(conn, job_id, removed_at=at)
    return gone


def _issues_json(issues: Sequence[SourceIssue]) -> list[JsonValue]:
    return [
        {
            "code": i.code,
            "severity": i.severity.value,
            "message": i.message,
            "source_job_id": i.source_job_id,
        }
        for i in issues
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchReport:
    run: SourceFetchRun
    changes: ListingChanges
    issues: tuple[SourceIssue, ...]
    # Set when the run failed; the error that ended it.
    error: ConnectorError | None = None
    # True when this run paused the source because it refused access.
    paused: bool = False


def _outcome(
    exc: SourceAccessDeniedError | SourceUnavailableError | SourceDataError,
) -> FetchRunOutcome:
    if isinstance(exc, SourceAccessDeniedError):
        return FetchRunOutcome.DENIED
    if isinstance(exc, SourceUnavailableError):
        return FetchRunOutcome.UNAVAILABLE
    return FetchRunOutcome.FAILED


def fetch_and_store(
    conn: sqlite3.Connection,
    connector: Connector,
    target: str,
    *,
    transport: Transport,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchReport:
    """Fetch one target, store what changed and record the run.

    Raises `SourcePausedError` (paused source) and `ConnectorTargetError` (unknown target)
    before any request, recording nothing. Errors from the source itself end the run: it is
    recorded with its outcome and returned in `FetchReport.error`, and an access denial also
    pauses the source until the user resumes it.
    """
    policy = connector.policy
    state = effective_state(policy, latest_status_event(conn, policy.key))
    target = connector.target_key(target)
    started = utcnow()
    try:
        outcome: FetchOutcome = run_connector(
            connector, target, state=state, transport=transport, clock=clock, sleep=sleep
        )
    except (SourceAccessDeniedError, SourceUnavailableError, SourceDataError) as exc:
        kind = _outcome(exc)
        with conn:
            run = source_fetch_runs.insert(
                conn,
                SourceFetchRun(
                    source=policy.key,
                    target=target,
                    started_at=started,
                    finished_at=utcnow(),
                    outcome=kind,
                    error=str(exc)[:MAX_ERROR_LENGTH],
                ),
            )
        paused = False
        if kind is FetchRunOutcome.DENIED:
            paused = (
                set_source_status(
                    conn, policy.key, SourceStatus.PAUSED, StatusActor.SYSTEM, reason=str(exc)
                )
                is not None
            )
        log.warning("source=%s fetch %s; paused=%s", policy.key, kind.value, paused)
        return FetchReport(run=run, changes=ListingChanges(), issues=(), error=exc, paused=paused)

    finished = utcnow()
    with conn:
        changes = _store(conn, policy.key, target, outcome.listings, finished)
        run = source_fetch_runs.insert(
            conn,
            SourceFetchRun(
                source=policy.key,
                target=target,
                started_at=started,
                finished_at=finished,
                outcome=FetchRunOutcome.OK,
                requests=outcome.requests_made,
                listings=len(outcome.listings),
                new=len(changes.new),
                changed=len(changes.changed),
                unchanged=len(changes.unchanged),
                reappeared=len(changes.reappeared),
                removed=len(changes.removed),
                issues=_issues_json(outcome.issues),
            ),
        )
    log.info(
        "source=%s stored new=%s changed=%s unchanged=%s reappeared=%s removed=%s flagged=%s",
        policy.key,
        len(changes.new),
        len(changes.changed),
        len(changes.unchanged),
        len(changes.reappeared),
        len(changes.removed),
        len(changes.flagged),
    )
    return FetchReport(run=run, changes=changes, issues=outcome.issues)


def fetch_history(conn: sqlite3.Connection, source: str) -> list[SourceFetchRun]:
    return source_fetch_runs.list(conn, source=source)
