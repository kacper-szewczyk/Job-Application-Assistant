"""Teamtailor career sites, through the RSS feed each site publishes (#71, ADR 0025).

Teamtailor documents a public RSS feed on every career site: the jobs page with `.rss` appended,
the first 100 jobs by default, `offset` and `per_page` for more
(https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide). A target is one
company's site on `<company>.teamtailor.com`, chosen by the user. Sites on a company's own domain
are not read: jaa requests only Teamtailor's hosts (ADR 0025).

Listings are read-only; the feed has no way to apply (SPEC §6).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import urlencode, urlsplit
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

DOMAIN: Final = "teamtailor.com"
PAGE_SIZE: Final = 100
_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# `/jobs/123456-backend-developer`: the number is the job's id.
_JOB_PATH: Final = re.compile(r"/jobs/([0-9]{1,20})(?:-[^/]*)?/?$")

POLICY: Final = SourcePolicy(
    key="teamtailor",
    name="Teamtailor career sites",
    access_method=AccessMethod.FEED,
    allowed_hosts=frozenset(),
    host_suffixes=frozenset({DOMAIN}),
    terms_url="https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    rate_limit=RateLimit(requests=20, per_seconds=60.0, max_requests_per_run=20),
    retry=RetryPolicy(max_retries=2, backoff_seconds=2.0, max_wait_seconds=60.0),
    notes=(
        "Public RSS feed of each career site "
        "(https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide). Targets "
        f"are sites on <company>.{DOMAIN} that the user chooses; custom domains are not read, "
        f"and a site that redirects to one fails that target only. Pages of {PAGE_SIZE}. "
        "Read-only."
    ),
)


def site_host(target: str) -> str:
    """`acme` or `acme.teamtailor.com` → `acme.teamtailor.com`."""
    value = target.strip().lower().removesuffix("/")
    label = value.removesuffix(f".{DOMAIN}")
    if not _LABEL.fullmatch(label):
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a Teamtailor site; use e.g. `acme` for acme.{DOMAIN}"
        )
    return f"{label}.{DOMAIN}"


def job_id_from_url(url: str) -> str | None:
    """The numeric id in a Teamtailor job link, or None."""
    found = _JOB_PATH.search(urlsplit(url).path)
    return found.group(1) if found else None


_ARRANGEMENTS: Final = {
    "fully": WorkArrangement.REMOTE,
    "remote": WorkArrangement.REMOTE,
    "hybrid": WorkArrangement.HYBRID,
}


def _places(item: ET.Element) -> list[str]:
    places: list[str] = []
    for group in children(item, "locations"):
        for location in children(group, "location"):
            name = child_text(location, "name") or child_text(location, "city")
            if name and name not in places:
                places.append(name)
    return places


def parse_item(item: ET.Element, company: str) -> JobListing | SourceIssue:
    """One feed item as a listing, or the reason it cannot be used."""
    url = child_text(item, "link")
    job_id = (job_id_from_url(url) if url else None) or child_text(item, "guid")
    if job_id is None:
        return _issue("item has no job link or guid", None)
    title = child_text(item, "title")
    if title is None or url is None:
        return _issue("item lacks a title or link", job_id)
    places = _places(item)
    remote = (child_text(item, "remoteStatus") or "").lower()
    arrangement = _ARRANGEMENTS.get(remote) or (
        WorkArrangement.ON_SITE if remote == "none" and places else None
    )
    location = ", ".join(places) or None
    if arrangement is not None and arrangement is not WorkArrangement.ON_SITE:
        location = f"{location} ({arrangement.value})" if location else arrangement.value.title()
    body = child_text(item, "description")
    return JobListing(
        source_job_id=job_id,
        url=url,
        title=title,
        company=company,
        location=location,
        description=html_to_text(body) or None if body else None,
        posted_at=rfc822_datetime(child_text(item, "pubDate")),
        details=JobDetails(work_arrangement=arrangement),
    )


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class Teamtailor:
    """Reads every job in one Teamtailor career site's feed, or one job from it."""

    page_size: int = PAGE_SIZE

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return site_host(target)

    def _pages(self, client: SourceClient, host: str) -> Iterator[tuple[str, list[ET.Element]]]:
        offset = 0
        while True:
            query = urlencode({"offset": offset, "per_page": self.page_size})
            channel = next(
                children(client.get_xml(f"https://{host}/jobs.rss?{query}"), "channel"), None
            )
            if channel is None:
                raise SourceDataError(f"{POLICY.key}: response is not an RSS feed")
            items = list(children(channel, "item"))
            yield child_text(channel, "title") or host.partition(".")[0], items
            if len(items) < self.page_size:
                return
            offset += len(items)

    def _parsed(self, client: SourceClient, host: str) -> Iterator[JobListing | SourceIssue]:
        for company, items in self._pages(client, host):
            for item in items:
                yield parse_item(item, company)

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for parsed in self._parsed(client, site_host(target)):
            if isinstance(parsed, SourceIssue):
                issues.append(parsed)
            else:
                listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))

    def fetch_job(self, client: SourceClient, target: str, job_id: str) -> JobListing | None:
        """The feed has no single-job address, so this reads it until the job turns up."""
        host = site_host(target)
        if not job_id.isascii() or not job_id.isdigit():
            raise ConnectorTargetError(f"{POLICY.key}: {job_id!r} is not a Teamtailor job id")
        for parsed in self._parsed(client, host):
            if isinstance(parsed, JobListing) and parsed.source_job_id == job_id:
                return parsed
        return None
