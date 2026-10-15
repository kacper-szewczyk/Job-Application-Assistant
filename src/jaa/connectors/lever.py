"""Lever company job sites, through the official Postings API (#71, ADR 0025).

Lever's Postings API serves a company's published postings without authentication:
`GET /v0/postings/{site}` with `skip`/`limit` paging and `GET /v0/postings/{site}/{id}` for one
posting (https://github.com/lever/postings-api). A target is one company's site name, the part
after `jobs.lever.co/` in its job site's link, chosen by the user; sites in Lever's EU region are
`eu:<site>`.

The API names no employer, so the site name stands in for it (`acme` for jobs.lever.co/acme).
Listings are read-only; the API's apply endpoint is never used (SPEC §6).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Final, cast
from urllib.parse import quote, urlencode

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
from jaa.connectors.fields import epoch_millis, strings, text
from jaa.connectors.html_text import html_to_text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError, SourceNotFoundError
from jaa.core.job_details import JobDetails, SalaryRange, WorkArrangement
from jaa.core.models import JsonObject, JsonValue

HOSTS: Final = {"": "api.lever.co", "eu": "api.eu.lever.co"}
PAGE_SIZE: Final = 100
_SITE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_POSTING_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

POLICY: Final = SourcePolicy(
    key="lever",
    name="Lever job sites",
    access_method=AccessMethod.PUBLIC_API,
    allowed_hosts=frozenset(HOSTS.values()),
    terms_url="https://github.com/lever/postings-api",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    # Lever documents a limit only for application POSTs (2 per second); reading stays far below.
    rate_limit=RateLimit(requests=20, per_seconds=60.0, max_requests_per_run=20),
    retry=RetryPolicy(max_retries=2, backoff_seconds=2.0, max_wait_seconds=60.0),
    notes=(
        "Official Postings API (https://github.com/lever/postings-api): published postings are "
        "public, no key. Targets are site names the user chooses (`acme`, or `eu:acme` for "
        "jobs.eu.lever.co). The API gives no company name, so the site name is used as the "
        f"employer. Pages of {PAGE_SIZE}. Read-only: the apply endpoint is never used."
    ),
)


@dataclass(frozen=True, slots=True)
class Site:
    region: str
    name: str

    @property
    def target(self) -> str:
        return f"{self.region}:{self.name}" if self.region else self.name

    @property
    def base(self) -> str:
        return f"https://{HOSTS[self.region]}/v0/postings/{quote(self.name)}"


def site(target: str) -> Site:
    """`acme` or `eu:acme`, case-insensitively. Raises `ConnectorTargetError` otherwise."""
    region, _, name = target.strip().lower().rpartition(":")
    if region not in HOSTS or not _SITE.fullmatch(name):
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a Lever site name; use e.g. `acme` or `eu:acme`"
        )
    return Site(region, name)


def posting_id(value: str) -> str | None:
    """A Lever posting id (a UUID) in lower case, or None."""
    lowered = value.strip().lower()
    return lowered if _POSTING_ID.fullmatch(lowered) else None


_ARRANGEMENTS: Final = {
    "remote": WorkArrangement.REMOTE,
    "hybrid": WorkArrangement.HYBRID,
    "on-site": WorkArrangement.ON_SITE,
}
_PERIODS: Final = ("year", "month", "week", "day", "hour")


def _salary(raw: JsonValue) -> SalaryRange | None:
    """`{"currency": "EUR", "interval": "per-year-salary", "min": 60000, "max": 80000}`."""
    if not isinstance(raw, dict) or (currency := text(raw.get("currency"))) is None:
        return None
    interval = (text(raw.get("interval")) or "").lower()
    period = next((p for p in _PERIODS if p in interval), None)
    low, high = (
        round(a) if isinstance(a, int | float) and not isinstance(a, bool) and a > 0 else None
        for a in (raw.get("min"), raw.get("max"))
    )
    if period is None or (low is None and high is None):
        return None
    return SalaryRange(min=low, max=high, currency=currency.upper(), period=period)


def description(posting: JsonObject) -> str | None:
    """Opening, description, the titled lists (requirements, benefits) and closing, as text.

    List titles become their own lines, so requirement headings survive for matching.
    """
    parts: list[str] = []
    for key in ("opening", "description"):
        if isinstance(value := posting.get(key), str) and (body := html_to_text(value)):
            parts.append(body)
            # Older postings repeat the opening inside `description`.
            break
    raw_lists = posting.get("lists")
    for item in cast(list[JsonValue], raw_lists) if isinstance(raw_lists, list) else []:
        if not isinstance(item, dict):
            continue
        heading = text(item.get("text"))
        content = item.get("content")
        body = html_to_text(f"<ul>{content}</ul>") if isinstance(content, str) else ""
        parts.append("\n".join(p for p in (heading, body) if p))
    if isinstance(value := posting.get("additional"), str) and (body := html_to_text(value)):
        parts.append(body)
    return "\n\n".join(p for p in parts if p) or None


def parse_posting(raw: JsonValue, company: str) -> JobListing | SourceIssue:
    """One posting from the API as a listing, or the reason it cannot be used."""
    if not isinstance(raw, dict):
        return _issue("posting is not a JSON object", None)
    posting_id = text(raw.get("id"))
    if posting_id is None:
        return _issue("posting has no id", None)
    title, url = text(raw.get("text")), text(raw.get("hostedUrl"))
    if title is None or url is None:
        return _issue("posting lacks a title or hostedUrl", posting_id)
    categories = raw.get("categories")
    places: list[str] = []
    if isinstance(categories, dict):
        main = text(categories.get("location"))
        places = strings(categories.get("allLocations")) or ([main] if main else [])
    arrangement = _ARRANGEMENTS.get(text(raw.get("workplaceType")) or "")
    location = ", ".join(places) or None
    if arrangement is not None and arrangement is not WorkArrangement.ON_SITE:
        location = f"{location} ({arrangement.value})" if location else arrangement.value.title()
    salary = _salary(raw.get("salaryRange"))
    return JobListing(
        source_job_id=posting_id,
        url=url,
        title=title,
        company=company,
        location=location,
        description=description(raw),
        posted_at=epoch_millis(raw.get("createdAt")),
        details=JobDetails(
            work_arrangement=arrangement, salaries=(salary,) if salary is not None else ()
        ),
    )


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class Lever:
    """Reads every published posting on one company's Lever site, or one posting by its id."""

    page_size: int = PAGE_SIZE

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return site(target).target

    def _pages(self, client: SourceClient, found: Site) -> Iterator[list[JsonValue]]:
        skip = 0
        while True:
            query = urlencode({"mode": "json", "skip": skip, "limit": self.page_size})
            data = client.get_json(f"{found.base}?{query}")
            if not isinstance(data, list):
                raise SourceDataError(f"{POLICY.key}: response is not a list of postings")
            page = cast(list[JsonValue], data)
            yield page
            if len(page) < self.page_size:
                return
            skip += len(page)

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        found = site(target)
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for page in self._pages(client, found):
            for raw in page:
                parsed = parse_posting(raw, found.name)
                if isinstance(parsed, SourceIssue):
                    issues.append(parsed)
                else:
                    listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))

    def fetch_job(self, client: SourceClient, target: str, job_id: str) -> JobListing | None:
        found = site(target)
        posting = posting_id(job_id)
        if posting is None:
            raise ConnectorTargetError(f"{POLICY.key}: {job_id!r} is not a Lever posting id")
        try:
            raw = client.get_json(f"{found.base}/{posting}?mode=json")
        except SourceNotFoundError:
            return None
        parsed = parse_posting(raw, found.name)
        if isinstance(parsed, SourceIssue):
            raise SourceDataError(f"{POLICY.key}: {parsed.message}")
        return parsed
