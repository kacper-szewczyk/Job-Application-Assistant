"""Storing connector listings as jobs: new, changed, reappeared and removed (T10, ADR 0013).

`store_listings` is the storage half and takes listings a connector already returned.
`fetch_and_store` runs a connector end to end: pause check, fetch, store, a `source_fetch_runs`
row for every run that reached the source, and a `system` pause when the source refuses access
(ADR 0011). Removal is only judged after a complete fetch of a target, and it never changes
`jobs.status`, which stays the user's lifecycle (ADR 0005). Sources with a daily request
allowance are refused before a run once the stored runs of the last 24 hours used it up
(ADR 0028). Logs carry counts only.
"""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from jaa.connectors.base import (
    Connector,
    JobListing,
    OfferConnector,
    SourceIssue,
    SourcePolicy,
    effective_state,
)
from jaa.connectors.http import Transport
from jaa.connectors.run import FetchOutcome, run_connector, run_connector_job
from jaa.core.clock import utcnow
from jaa.core.dedupe import canonical_url, dedupe_key
from jaa.core.errors import (
    ConnectorError,
    SourceAccessDeniedError,
    SourceDataError,
    SourceQuotaError,
    SourceUnavailableError,
)
from jaa.core.models import (
    FetchRunOutcome,
    Job,
    JsonObject,
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
DAY: Final = timedelta(days=1)


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
    payload: list[JsonValue] = [
        listing.url,
        listing.title,
        listing.company,
        listing.location,
        listing.description,
        listing.posted_at.isoformat() if listing.posted_at else None,
        listing.updated_at.isoformat() if listing.updated_at else None,
        [[q.key, q.label, q.kind.value, q.required, list(q.options)] for q in listing.questions],
    ]
    # Added in T12: listings without details hash as before, so storing details does not mark
    # every job changed on the first fetch after upgrading; listings with details do change once.
    if not listing.details.is_empty():
        payload.append(listing.details.to_json())
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def requirements_json(listing: JobListing) -> JsonObject:
    """What `jobs.requirements` holds: the listing's details and application questions."""
    data = listing.details.to_json()
    if listing.questions:
        data["questions"] = [
            {
                "key": q.key,
                "label": q.label,
                "kind": q.kind.value,
                "required": q.required,
                "options": list(q.options),
            }
            for q in listing.questions
        ]
    return data


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
    *,
    complete: bool = True,
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
                    requirements=requirements_json(listing),
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
                "requirements": requirements_json(listing),
            }
            changed.append(job_id)
        else:
            unchanged.append(job_id)
        jobs.update(conn, job_id, **updates)
    # A single offer says nothing about the rest of the board.
    removed = _mark_removed(conn, source, target, seen, at) if complete else ()
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


def _record_failure(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    started: datetime,
    exc: SourceAccessDeniedError | SourceUnavailableError | SourceDataError,
) -> FetchReport:
    kind = _outcome(exc)
    with conn:
        run = source_fetch_runs.insert(
            conn,
            SourceFetchRun(
                source=source,
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
                conn, source, SourceStatus.PAUSED, StatusActor.SYSTEM, reason=str(exc)
            )
            is not None
        )
    log.warning("source=%s fetch %s; paused=%s", source, kind.value, paused)
    return FetchReport(run=run, changes=ListingChanges(), issues=(), error=exc, paused=paused)


def _record_success(
    conn: sqlite3.Connection, outcome: FetchOutcome, started: datetime, *, complete: bool
) -> FetchReport:
    finished = utcnow()
    with conn:
        changes = _store(
            conn, outcome.source, outcome.target, outcome.listings, finished, complete=complete
        )
        run = source_fetch_runs.insert(
            conn,
            SourceFetchRun(
                source=outcome.source,
                target=outcome.target,
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
        outcome.source,
        len(changes.new),
        len(changes.changed),
        len(changes.unchanged),
        len(changes.reappeared),
        len(changes.removed),
        len(changes.flagged),
    )
    return FetchReport(run=run, changes=changes, issues=outcome.issues)


def check_daily_quota(
    conn: sqlite3.Connection, policy: SourcePolicy, *, now: datetime | None = None
) -> None:
    """Raise `SourceQuotaError` when one more run could exceed the source's daily allowance.

    Every stored run reached the source, so a run counts as at least one request even when it
    failed before its requests were counted.
    """
    limit = policy.rate_limit
    if limit.max_requests_per_day is None:
        return
    now = now or utcnow()
    recent = [
        run for run in source_fetch_runs.list(conn, source=policy.key) if now - run.started_at < DAY
    ]
    used = sum(max(run.requests, 1) for run in recent)
    if used + limit.max_requests_per_run <= limit.max_requests_per_day:
        return
    earliest = min(run.started_at for run in recent) + DAY
    raise SourceQuotaError(
        f"{policy.key} allows {limit.max_requests_per_day} requests a day and {used} were made "
        f"in the last 24 hours; try again after {earliest:%Y-%m-%d %H:%M} UTC"
    )


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

    Raises `SourcePausedError` (paused source), `ConnectorTargetError` (unknown target) and
    `SourceQuotaError` (daily allowance used up) before any request, recording nothing. Removal
    is judged only when the connector saw the whole target (`FetchResult.complete`). Errors from
    the source itself end the run: it is recorded with its outcome and returned in
    `FetchReport.error`, and an access denial also pauses the source until the user resumes it.
    """
    policy = connector.policy
    state = effective_state(policy, latest_status_event(conn, policy.key))
    target = connector.target_key(target)
    if state.status is SourceStatus.ACTIVE:
        check_daily_quota(conn, policy)
    started = utcnow()
    try:
        outcome = run_connector(
            connector, target, state=state, transport=transport, clock=clock, sleep=sleep
        )
    except (SourceAccessDeniedError, SourceUnavailableError, SourceDataError) as exc:
        return _record_failure(conn, policy.key, target, started, exc)
    return _record_success(conn, outcome, started, complete=outcome.complete)


def fetch_and_store_job(
    conn: sqlite3.Connection,
    connector: OfferConnector,
    target: str,
    job_id: str,
    *,
    transport: Transport,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchReport:
    """Fetch one offer of `target` and store it, for a link the user brought (ADR 0025).

    Like `fetch_and_store`, but nothing else of the target is marked removed. An offer the
    source no longer lists leaves `FetchReport.changes` empty.
    """
    policy = connector.policy
    state = effective_state(policy, latest_status_event(conn, policy.key))
    target = connector.target_key(target)
    if state.status is SourceStatus.ACTIVE:
        check_daily_quota(conn, policy)
    started = utcnow()
    try:
        outcome = run_connector_job(
            connector, target, job_id, state=state, transport=transport, clock=clock, sleep=sleep
        )
    except (SourceAccessDeniedError, SourceUnavailableError, SourceDataError) as exc:
        return _record_failure(conn, policy.key, target, started, exc)
    return _record_success(conn, outcome, started, complete=False)


def fetch_history(conn: sqlite3.Connection, source: str) -> list[SourceFetchRun]:
    return source_fetch_runs.list(conn, source=source)
