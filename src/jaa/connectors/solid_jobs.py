"""SOLID.Jobs, a Polish job board, through its official public offers API (T10, ADR 0013).

SOLID.Jobs publishes `GET /public-api/offers/{division}` for integrators and aggregators, AI
assistants included: no key or registration, a self-chosen `campaign` name for their traffic
analytics, 300 requests per minute per IP, responses cached for an hour
(https://solid.jobs/api-ofert-pracy). A target is one division; each fetch reads every page.

Listings are read-only. The API gives no way to apply, and listing access does not imply
permission to submit applications (SPEC §6).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, cast
from urllib.parse import urlencode

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
from jaa.connectors.html_text import html_to_text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.models import JsonObject, JsonValue

BASE_URL: Final = "https://solid.jobs/public-api/offers/"
# Our traffic name for SOLID.Jobs' analytics (lowercase letters, digits, hyphens, at most 64).
CAMPAIGN: Final = "jaa"
# The largest page the API serves; fewer pages means fewer requests.
PAGE_SIZE: Final = 500
DIVISIONS: Final = (
    "IT",
    "Engineering",
    "Marketing",
    "Sales",
    "HR",
    "Logistics",
    "Finances",
    "Other",
)

POLICY: Final = SourcePolicy(
    key="solid_jobs",
    name="SOLID.Jobs",
    access_method=AccessMethod.PUBLIC_API,
    allowed_hosts=frozenset({"solid.jobs"}),
    terms_url="https://solid.jobs/api-ofert-pracy",
    terms_reviewed_on=date(2026, 10, 14),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    # Far below the documented 300 per minute; a full division is a handful of pages.
    rate_limit=RateLimit(requests=30, per_seconds=60.0, max_requests_per_run=20),
    retry=RetryPolicy(max_retries=2, backoff_seconds=2.0, max_wait_seconds=60.0),
    # A page of 500 offers with HTML descriptions is a few MB.
    max_response_bytes=16 * 1024 * 1024,
    notes=(
        "Official public offers API for integrators and aggregators "
        "(https://solid.jobs/api-ofert-pracy); site terms at https://solid.jobs/about/terms. "
        f"No key; requests carry campaign={CAMPAIGN}. Targets are divisions: "
        f"{', '.join(DIVISIONS)}. Responses are cached by the source for an hour, so fetching "
        "more often does not show anything new. Salary, skills and experience level are in the "
        "API but not kept yet. Read-only: no application submission."
    ),
)


def division(target: str) -> str:
    """The division name the API expects for `target`, matched case-insensitively."""
    for name in DIVISIONS:
        if name.lower() == target.strip().lower():
            return name
    raise ConnectorTargetError(
        f"{POLICY.key}: unknown division {target!r}; use one of {', '.join(DIVISIONS)}"
    )


def page_url(division_name: str, page: int, page_size: int = PAGE_SIZE) -> str:
    query = urlencode({"campaign": CAMPAIGN, "pageSize": page_size, "pageIndex": page})
    return f"{BASE_URL}{division_name}?{query}"


def _text(value: JsonValue) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def location_text(job: JsonObject) -> str | None:
    """`Warszawa, Kraków (hybrid)`, `Wrocław (remote)` or `Remote`, from the source's fields."""
    raw = job.get("locations")
    places = (
        [p for p in (_text(v) for v in cast(list[JsonValue], raw)) if p]
        if isinstance(raw, list)
        else []
    )
    mode = (
        "remote"
        if job.get("isRemote") is True
        else "hybrid"
        if job.get("isHybrid") is True
        else None
    )
    if places:
        return ", ".join(places) + (f" ({mode})" if mode else "")
    return mode.capitalize() if mode else None


def _datetime(value: JsonValue) -> datetime | None:
    """An offset timestamp, or None when it is missing, unreadable or has no offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _issue(code: str, message: str, job_id: str | None, *, error: bool = True) -> SourceIssue:
    severity = IssueSeverity.ERROR if error else IssueSeverity.WARNING
    return SourceIssue(code=code, severity=severity, message=message, source_job_id=job_id)


def parse_job(raw: JsonValue) -> JobListing | SourceIssue:
    """One offer from the API as a listing, or the reason it cannot be used."""
    if not isinstance(raw, dict):
        return _issue("unreadable_listing", "offer is not a JSON object", None)
    job_id = _text(raw.get("jobOfferKey"))
    if job_id is None:
        return _issue("unreadable_listing", "offer has no jobOfferKey", None)
    title, company, url = (_text(raw.get(k)) for k in ("title", "company", "url"))
    if title is None or company is None or url is None:
        return _issue("unreadable_listing", "offer lacks a title, company or url", job_id)
    description = raw.get("description")
    return JobListing(
        source_job_id=job_id,
        url=url,
        title=title,
        company=company,
        location=location_text(raw),
        description=html_to_text(description) or None if isinstance(description, str) else None,
        posted_at=_datetime(raw.get("validFrom")),
        updated_at=_datetime(raw.get("updatedAt")),
    )


def _int(envelope: JsonObject, key: str) -> int:
    value = envelope.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SourceDataError(f"{POLICY.key}: response has no valid {key}")
    return value


@dataclass(frozen=True, slots=True)
class SolidJobs:
    """Reads every offer in one SOLID.Jobs division."""

    page_size: int = PAGE_SIZE

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return division(target)

    def _pages(self, client: SourceClient, division_name: str) -> Iterator[list[JsonValue]]:
        page = 0
        while True:
            data = client.get_json(page_url(division_name, page, self.page_size))
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise SourceDataError(f"{POLICY.key}: response has no jobs list")
            jobs = cast(list[JsonValue], data["jobs"])
            total_pages = _int(data, "totalPages")
            if _int(data, "pageIndex") != page:
                raise SourceDataError(f"{POLICY.key}: asked for page {page}, got another one")
            yield jobs
            page += 1
            if not jobs or page >= total_pages:
                return

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        """Every offer in division `target`. Raises `ConnectorTargetError` before any request."""
        division_name = division(target)
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for jobs in self._pages(client, division_name):
            for raw in jobs:
                parsed = parse_job(raw)
                if isinstance(parsed, SourceIssue):
                    issues.append(parsed)
                else:
                    listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))
