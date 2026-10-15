"""Personio career pages, through the XML feed of open positions (#71, ADR 0025).

Personio documents `https://<company>.jobs.personio.de/xml` for retrieving a company's open
positions (https://developer.personio.de/docs/retrieving-open-job-positions); the company has to
publish its career page for the feed to exist. A target is one company's career page on
`<company>.jobs.personio.de` (or `.com`), chosen by the user.

The feed names no employer unless the position belongs to a subcompany, so the subdomain stands
in for it. Listings are read-only (SPEC §6).
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Final
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
from jaa.connectors.fields import child_text, children, iso_datetime
from jaa.connectors.html_text import html_to_text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.job_details import JobDetails

DOMAINS: Final = ("jobs.personio.de", "jobs.personio.com")
_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

POLICY: Final = SourcePolicy(
    key="personio",
    name="Personio career pages",
    access_method=AccessMethod.FEED,
    allowed_hosts=frozenset(),
    host_suffixes=frozenset(DOMAINS),
    terms_url="https://developer.personio.de/docs/retrieving-open-job-positions",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    # The whole feed is one request.
    rate_limit=RateLimit(requests=10, per_seconds=60.0, max_requests_per_run=3),
    retry=RetryPolicy(max_retries=2, backoff_seconds=2.0, max_wait_seconds=60.0),
    notes=(
        "XML feed of open positions "
        "(https://developer.personio.de/docs/retrieving-open-job-positions), available when "
        "the company publishes its career page. Targets are career pages on "
        "<company>.jobs.personio.de or .com that the user chooses. The employer is the "
        "position's subcompany, else the subdomain. Read-only."
    ),
)


def page_host(target: str) -> str:
    """`acme`, `acme.jobs.personio.de` or `acme.jobs.personio.com` → the career page's host."""
    value = target.strip().lower().removesuffix("/")
    label, _, domain = value.partition(".")
    if not _LABEL.fullmatch(label) or domain not in ("", *DOMAINS):
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a Personio career page; use e.g. `acme` for "
            f"acme.{DOMAINS[0]}"
        )
    return f"{label}.{domain or DOMAINS[0]}"


def description(position: ET.Element) -> str | None:
    """The position's titled sections as text, each title on its own line."""
    parts: list[str] = []
    for group in children(position, "jobDescriptions"):
        for section in children(group, "jobDescription"):
            heading = child_text(section, "name")
            value = child_text(section, "value")
            body = html_to_text(value) if value else ""
            parts.append("\n".join(p for p in (heading, body) if p))
    return "\n\n".join(p for p in parts if p) or None


def _offices(position: ET.Element) -> list[str]:
    places = [office] if (office := child_text(position, "office")) else []
    for group in children(position, "additionalOffices"):
        for extra in children(group, "office"):
            name = "".join(extra.itertext()).strip()
            if name and name not in places:
                places.append(name)
    return places


def parse_position(position: ET.Element, host: str) -> JobListing | SourceIssue:
    """One position as a listing, or the reason it cannot be used."""
    job_id = child_text(position, "id")
    if job_id is None or not job_id.isdigit():
        return _issue("position has no numeric id", None)
    title = child_text(position, "name")
    if title is None:
        return _issue("position has no name", job_id)
    return JobListing(
        source_job_id=job_id,
        url=f"https://{host}/job/{job_id}",
        title=title,
        company=child_text(position, "subcompany") or host.partition(".")[0],
        location=", ".join(_offices(position)) or None,
        description=description(position),
        posted_at=iso_datetime(child_text(position, "createdAt")),
        details=JobDetails(experience_level=child_text(position, "seniority")),
    )


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class Personio:
    """Reads every open position on one Personio career page, or one position from it."""

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return page_host(target)

    def _positions(self, client: SourceClient, host: str) -> list[ET.Element]:
        root = client.get_xml(f"https://{host}/xml")
        if root.tag != "workzag-jobs":
            raise SourceDataError(f"{POLICY.key}: response is not a Personio positions feed")
        return list(children(root, "position"))

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        host = page_host(target)
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for position in self._positions(client, host):
            parsed = parse_position(position, host)
            if isinstance(parsed, SourceIssue):
                issues.append(parsed)
            else:
                listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))

    def fetch_job(self, client: SourceClient, target: str, job_id: str) -> JobListing | None:
        """The feed has no single-position address, so this reads it and picks the position."""
        host = page_host(target)
        if not job_id.isascii() or not job_id.isdigit():
            raise ConnectorTargetError(f"{POLICY.key}: {job_id!r} is not a Personio position id")
        for position in self._positions(client, host):
            if child_text(position, "id") == job_id:
                parsed = parse_position(position, host)
                if isinstance(parsed, SourceIssue):
                    raise SourceDataError(f"{POLICY.key}: {parsed.message}")
                return parsed
        return None
