"""We Work Remotely, through its public RSS feeds (#74, ADR 0028). Ships paused.

We Work Remotely publishes one RSS feed for the whole board and one per category
(https://weworkremotely.com/remote-job-rss-feed) and its robots.txt allows them. The feed page
reportedly asks only for a link back, but it sits behind a bot check, so the terms could not be
read and confirmed. Until someone confirms them with the operator the policy keeps the source
paused (SPEC §10); the connector is complete so that only the policy needs to change.

Item titles are `Company: Job title`. Each feed lists the board's current jobs, but nothing says
it lists all of them, so a fetch never marks missing jobs removed.
"""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

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
from jaa.connectors.fields import child_text, children, rfc822_datetime
from jaa.connectors.html_text import html_to_text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.job_details import JobDetails, WorkArrangement

BASE: Final = "https://weworkremotely.com"
# Target → feed path, as listed on the feed page. A fixed list: jaa does not guess feed URLs.
FEEDS: Final = {
    "all": "/remote-jobs.rss",
    "programming": "/categories/remote-programming-jobs.rss",
    "full-stack-programming": "/categories/remote-full-stack-programming-jobs.rss",
    "back-end-programming": "/categories/remote-back-end-programming-jobs.rss",
    "front-end-programming": "/categories/remote-front-end-programming-jobs.rss",
    "devops-sysadmin": "/categories/remote-devops-sysadmin-jobs.rss",
    "design": "/categories/remote-design-jobs.rss",
    "product": "/categories/remote-product-jobs.rss",
    "customer-support": "/categories/remote-customer-support-jobs.rss",
    "sales-and-marketing": "/categories/remote-sales-and-marketing-jobs.rss",
    "management-and-finance": "/categories/remote-management-and-finance-jobs.rss",
    "other": "/categories/all-other-remote-jobs.rss",
}
_JOB_PATH: Final = re.compile(r"/remote-jobs/([a-z0-9][a-z0-9-]{0,199})/?$")

POLICY: Final = SourcePolicy(
    key="weworkremotely",
    name="We Work Remotely",
    access_method=AccessMethod.FEED,
    allowed_hosts=frozenset({"weworkremotely.com"}),
    terms_url="https://weworkremotely.com/remote-job-rss-feed",
    terms_reviewed_on=None,
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    rate_limit=RateLimit(requests=4, per_seconds=60.0, max_requests_per_run=1),
    retry=RetryPolicy(max_retries=2, backoff_seconds=5.0, max_wait_seconds=60.0),
    paused_reason=(
        "terms not confirmed: the feed page is behind a bot check, so its conditions could not "
        "be read; confirm them with We Work Remotely before enabling (ADR 0028)"
    ),
    notes=(
        "Public RSS feeds (https://weworkremotely.com/remote-job-rss-feed); robots.txt allows "
        "them. One request per target; a fetch never marks jobs removed. Targets: "
        + ", ".join(f"`{t}`" for t in FEEDS)
        + "."
    ),
)


def feed(target: str) -> str:
    """A target name from `FEEDS`, case-insensitively."""
    value = target.strip().lower()
    if value not in FEEDS:
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a We Work Remotely feed; use one of "
            + ", ".join(FEEDS)
        )
    return value


def job_id_from_url(url: str) -> str | None:
    """The slug in a `/remote-jobs/<slug>` link, or None."""
    found = _JOB_PATH.search(urlsplit(url).path)
    return found.group(1) if found else None


def _location(item: ET.Element) -> str:
    places = [
        p for p in (child_text(item, n) for n in ("region", "country", "state")) if p is not None
    ]
    return f"{', '.join(places)} (remote)" if places else "Remote"


def parse_item(item: ET.Element) -> JobListing | SourceIssue:
    """One feed item as a listing, or the reason it cannot be used."""
    url = child_text(item, "link")
    job_id = job_id_from_url(url) if url else None
    if url is None or job_id is None:
        return _issue("item has no job link", None)
    company, _, title = (child_text(item, "title") or "").partition(": ")
    if not company.strip() or not title.strip():
        return _issue("item title is not `Company: Job title`", job_id)
    body = child_text(item, "description")
    return JobListing(
        source_job_id=job_id,
        url=url,
        title=title.strip(),
        company=company.strip(),
        location=_location(item),
        description=(html_to_text(body) or None) if body else None,
        posted_at=rfc822_datetime(child_text(item, "pubDate")),
        details=JobDetails(work_arrangement=WorkArrangement.REMOTE),
    )


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class WeWorkRemotely:
    """Reads one We Work Remotely feed."""

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return feed(target)

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        root = client.get_xml(f"{BASE}{FEEDS[feed(target)]}")
        channel = next(children(root, "channel"), None)
        if channel is None:
            raise SourceDataError(f"{POLICY.key}: response is not an RSS feed")
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for item in children(channel, "item"):
            parsed = parse_item(item)
            if isinstance(parsed, SourceIssue):
                issues.append(parsed)
            else:
                listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues), complete=False)
