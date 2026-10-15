"""Arbeitnow jobs, mostly in Germany, through its free job board API (#74, ADR 0028).

`GET https://www.arbeitnow.com/api/job-board-api?page=N` returns jobs newest first, a few
hundred per page, with `links.next` pointing at the following page
(https://www.arbeitnow.com/blog/job-board-api). Arbeitnow's terms (§11 API) permit using the API
with a link back, which jaa keeps as the job's link.

The whole board runs to many thousands of jobs over dozens of pages, and the API asks not to be
abused, so jaa reads only the newest `MAX_PAGES` pages. A fetch that stops before the last page
has not seen the whole board, so it does not mark missing jobs removed (`FetchResult.complete`).

Targets: `all`, or `remote` for the jobs the API flags as remote.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, cast

from jaa.connectors.base import (
    AccessMethod,
    FetchResult,
    IssueSeverity,
    JobListing,
    ListingField,
    RateLimit,
    RetryPolicy,
    SourceIssue,
    SourcePolicy,
)
from jaa.connectors.fields import escaped_html_text, text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.job_details import JobDetails, WorkArrangement
from jaa.core.models import JsonValue

API: Final = "https://www.arbeitnow.com/api/job-board-api"
TARGETS: Final = ("all", "remote")
MAX_PAGES: Final = 5

POLICY: Final = SourcePolicy(
    key="arbeitnow",
    name="Arbeitnow",
    access_method=AccessMethod.PUBLIC_API,
    allowed_hosts=frozenset({"www.arbeitnow.com"}),
    terms_url="https://www.arbeitnow.com/terms",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    # No documented limit; "please do not abuse". Newest pages only, slowly.
    rate_limit=RateLimit(requests=6, per_seconds=60.0, max_requests_per_run=MAX_PAGES),
    retry=RetryPolicy(max_retries=2, backoff_seconds=5.0, max_wait_seconds=60.0),
    notes=(
        "Free public API (https://www.arbeitnow.com/blog/job-board-api), no key; the terms "
        "(§11 API) ask for a link back, which is the job's link. Jobs are mostly in Germany. "
        f"Only the newest {MAX_PAGES} pages are read; a fetch that does not reach the last page "
        "marks no jobs removed. "
        "Targets: `all`, or `remote` for jobs flagged remote."
    ),
)


def board(target: str) -> str:
    """`all` or `remote`, case-insensitively."""
    value = target.strip().lower()
    if value not in TARGETS:
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not an Arbeitnow target; use `all` or `remote`"
        )
    return value


def _created(value: JsonValue) -> datetime | None:
    """Seconds since the Unix epoch."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_job(raw: JsonValue) -> JobListing | SourceIssue:
    """One job from the API as a listing, or the reason it cannot be used."""
    if not isinstance(raw, dict):
        return _issue("job is not a JSON object", None)
    slug = text(raw.get("slug"))
    if slug is None:
        return _issue("job has no slug", None)
    title, company, url = (
        text(raw.get("title")),
        text(raw.get("company_name")),
        text(raw.get("url")),
    )
    if title is None or company is None or url is None:
        return _issue("job lacks a title, company or url", slug)
    remote = raw.get("remote") is True
    place = text(raw.get("location"))
    location = (f"{place} (remote)" if place else "Remote") if remote else place
    return JobListing(
        source_job_id=slug,
        url=url,
        title=title,
        company=company,
        location=location,
        description=escaped_html_text(raw.get("description")),
        posted_at=_created(raw.get("created_at")),
        details=JobDetails(work_arrangement=WorkArrangement.REMOTE if remote else None),
    )


def is_remote(raw: JsonValue) -> bool:
    return isinstance(raw, dict) and raw.get("remote") is True


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class Arbeitnow:
    """Reads the newest pages of the Arbeitnow board, optionally only its remote jobs."""

    max_pages: int = MAX_PAGES

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return board(target)

    def _pages(self, client: SourceClient) -> Iterator[tuple[list[JsonValue], bool]]:
        """Each page's jobs, and whether it is the board's last page."""
        for page in range(1, self.max_pages + 1):
            data = client.get_json(f"{API}?page={page}")
            if not isinstance(data, dict) or not isinstance(raw_jobs := data.get("data"), list):
                raise SourceDataError(f"{POLICY.key}: response has no list of jobs")
            links = data.get("links")
            last = not raw_jobs or not isinstance(links, dict) or text(links.get("next")) is None
            yield cast(list[JsonValue], raw_jobs), last
            if last:
                return

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        remote_only = board(target) == "remote"
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        complete = False
        for page, last in self._pages(client):
            complete = last
            for raw in page:
                if remote_only and not is_remote(raw):
                    continue
                parsed = parse_job(raw)
                if isinstance(parsed, SourceIssue):
                    issues.append(parsed)
                else:
                    listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues), complete=complete)
