"""Running a connector: pause check, a policy-bound client, and validation of its output.

This returns data only. The caller stores listings and, on `SourceAccessDeniedError`, pauses the
source with `jaa.storage.sources.set_source_status` (ADR 0011).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Final
from urllib.parse import urlsplit

from jaa.connectors.base import (
    Connector,
    IssueSeverity,
    JobListing,
    ListingField,
    SourceIssue,
    SourceState,
)
from jaa.connectors.http import SourceClient, Transport
from jaa.core.errors import SourcePausedError
from jaa.core.models import SourceStatus

log = logging.getLogger(__name__)

MAX_ID_LENGTH: Final = 200
MAX_TEXT_LENGTH: Final = 500
# Long descriptions are real; this only stops a broken feed from filling the database.
MAX_DESCRIPTION_LENGTH: Final = 200_000


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchOutcome:
    source: str
    target: str
    listings: tuple[JobListing, ...]
    issues: tuple[SourceIssue, ...]
    requests_made: int


def _problem(listing: JobListing) -> str | None:
    """Why the listing cannot be used at all, or None."""
    for name in ("source_job_id", "title", "company"):
        value: str = getattr(listing, name)
        if not value.strip():
            return f"{name} is empty"
    if len(listing.source_job_id) > MAX_ID_LENGTH:
        return "source_job_id is too long"
    if len(listing.title) > MAX_TEXT_LENGTH or len(listing.company) > MAX_TEXT_LENGTH:
        return "title or company is too long"
    parts = urlsplit(listing.url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "url is not an http(s) URL"
    if parts.username is not None or parts.password is not None:
        return "url carries credentials"
    for stamp in (listing.posted_at, listing.updated_at):
        if stamp is not None and stamp.tzinfo is None:
            return "dates must be timezone-aware"
    return None


def _text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())[:limit] or None


def _clean(listing: JobListing, permitted: frozenset[ListingField]) -> tuple[JobListing, list[str]]:
    """Tidy whitespace, convert dates to UTC and drop fields the policy does not permit."""
    dropped: list[str] = []

    def allowed[T](name: ListingField, value: T, empty: T) -> T:
        if value != empty and name not in permitted:
            dropped.append(name.value)
            return empty
        return value

    location = allowed(ListingField.LOCATION, listing.location, None)
    description = allowed(ListingField.DESCRIPTION, listing.description, None)
    posted_at = allowed(ListingField.POSTED_AT, listing.posted_at, None)
    updated_at = allowed(ListingField.UPDATED_AT, listing.updated_at, None)
    cleaned = JobListing(
        source_job_id=listing.source_job_id.strip(),
        url=listing.url.strip(),
        title=" ".join(listing.title.split()),
        company=" ".join(listing.company.split()),
        location=_text(location, MAX_TEXT_LENGTH),
        description=(description or "")[:MAX_DESCRIPTION_LENGTH] or None,
        posted_at=posted_at.astimezone(UTC) if posted_at else None,
        updated_at=updated_at.astimezone(UTC) if updated_at else None,
        questions=allowed(ListingField.QUESTIONS, listing.questions, ()),
    )
    return cleaned, dropped


def validate_listings(
    connector: Connector, listings: tuple[JobListing, ...]
) -> tuple[tuple[JobListing, ...], list[SourceIssue]]:
    """Keep valid, policy-conforming listings; report the rest. Duplicate IDs keep the first."""
    policy = connector.policy
    kept: list[JobListing] = []
    issues: list[SourceIssue] = []
    seen: set[str] = set()
    dropped_counts: dict[str, int] = {}
    for listing in listings:
        problem = _problem(listing)
        job_id = listing.source_job_id.strip() or None
        if problem is not None:
            issues.append(
                SourceIssue(
                    code="invalid_listing",
                    severity=IssueSeverity.ERROR,
                    message=f"listing skipped: {problem}",
                    source_job_id=job_id,
                )
            )
            continue
        cleaned, dropped = _clean(listing, policy.permitted_fields)
        if cleaned.source_job_id in seen:
            issues.append(
                SourceIssue(
                    code="duplicate_listing",
                    severity=IssueSeverity.WARNING,
                    message="the source listed this job more than once; kept the first",
                    source_job_id=cleaned.source_job_id,
                )
            )
            continue
        seen.add(cleaned.source_job_id)
        for name in dropped:
            dropped_counts[name] = dropped_counts.get(name, 0) + 1
        kept.append(cleaned)
    for name, count in sorted(dropped_counts.items()):
        # A connector bug, not a source problem: report once per field, not once per listing.
        issues.append(
            SourceIssue(
                code="field_not_permitted",
                severity=IssueSeverity.WARNING,
                message=f"dropped {name} from {count} listing(s); the source policy does not "
                "permit keeping it",
            )
        )
    return tuple(kept), issues


def run_connector(
    connector: Connector,
    target: str,
    *,
    state: SourceState,
    transport: Transport,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchOutcome:
    """Fetch one target, refusing paused sources and validating the output against the policy.

    `state` comes from `effective_state(policy, latest_status_event(conn, key))`. Connector
    errors (`SourceAccessDeniedError`, `SourceUnavailableError`, `SourceDataError`) propagate.
    """
    policy = connector.policy
    if state.status is SourceStatus.PAUSED:
        raise SourcePausedError(f"{policy.key} is paused: {state.reason or 'no reason given'}")
    client = SourceClient(policy, transport, clock=clock, sleep=sleep)
    result = connector.fetch(client, target)
    listings, issues = validate_listings(connector, result.listings)
    log.info(
        "source=%s fetched listings=%s kept=%s issues=%s requests=%s",
        policy.key,
        len(result.listings),
        len(listings),
        len(result.issues) + len(issues),
        client.requests_made,
    )
    return FetchOutcome(
        source=policy.key,
        target=target,
        listings=listings,
        issues=(*result.issues, *issues),
        requests_made=client.requests_made,
    )
