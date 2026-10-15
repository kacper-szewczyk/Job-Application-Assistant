"""Remotive remote jobs, through its public API (#74, ADR 0028).

`GET https://remotive.com/api/remote-jobs` returns every listed job, optionally narrowed by
`category` (https://github.com/remotive-com/remote-jobs-api). A target is one category slug,
such as `software-dev`, or `all`.

Remotive's terms set the limits encoded in the policy: no more than 4 calls a day, more than 2 a
minute are blocked, jobs appear with a 24-hour delay, and every job must link back to Remotive
and name it as the source. jaa keeps the Remotive link as the job's link and the source key on
the job, and never passes jobs on to other sites.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
from jaa.connectors.fields import text
from jaa.connectors.html_text import html_to_text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.job_details import JobDetails, WorkArrangement
from jaa.core.models import JsonValue

API: Final = "https://remotive.com/api/remote-jobs"
ALL: Final = "all"
_CATEGORY: Final = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

POLICY: Final = SourcePolicy(
    key="remotive",
    name="Remotive",
    access_method=AccessMethod.PUBLIC_API,
    allowed_hosts=frozenset({"remotive.com"}),
    terms_url="https://github.com/remotive-com/remote-jobs-api",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.QUESTIONS},
    # Remotive: "max. 4 times a day", more than 2 a minute blocked. One request per run.
    rate_limit=RateLimit(
        requests=1, per_seconds=60.0, max_requests_per_run=1, max_requests_per_day=4
    ),
    # A retry is another call against the daily allowance, so failures wait for the next run.
    retry=RetryPolicy(max_retries=0),
    notes=(
        "Public API (https://github.com/remotive-com/remote-jobs-api), no key. At most 4 "
        "requests a day, counted across all targets; jobs are shown 24 hours late. Each job "
        "links to its Remotive page and names Remotive as the source; jobs are never "
        "resubmitted elsewhere. Targets: a category slug such as `software-dev`, or `all`. "
        "robots.txt disallows /api/* for crawlers, while the API documentation and the legal "
        "notice in every response grant developers access within these limits (ADR 0028)."
    ),
)


def category(target: str) -> str:
    """`Software-Dev` → `software-dev`; `all` for every category."""
    value = target.strip().lower()
    if not _CATEGORY.fullmatch(value) or len(value) > 60:
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a Remotive category; use a slug such as "
            f"`software-dev`, or `{ALL}`"
        )
    return value


def _published(value: JsonValue) -> datetime | None:
    """`2026-10-11T20:17:27`: Remotive sends no offset; its timestamps are UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_job(raw: JsonValue) -> JobListing | SourceIssue:
    """One job from the API as a listing, or the reason it cannot be used."""
    if not isinstance(raw, dict):
        return _issue("job is not a JSON object", None)
    job_id = raw.get("id")
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        return _issue("job has no id", None)
    source_job_id = str(job_id)
    title, company, url = (
        text(raw.get("title")),
        text(raw.get("company_name")),
        text(raw.get("url")),
    )
    if title is None or company is None or url is None:
        return _issue("job lacks a title, company or url", source_job_id)
    region = text(raw.get("candidate_required_location"))
    body = raw.get("description")
    return JobListing(
        source_job_id=source_job_id,
        url=url,
        title=title,
        company=company,
        location=f"{region} (remote)" if region else "Remote",
        description=(html_to_text(body) or None) if isinstance(body, str) else None,
        posted_at=_published(raw.get("publication_date")),
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
class Remotive:
    """Reads every Remotive job in one category, or in all of them, with one request."""

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return category(target)

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        slug = category(target)
        url = API if slug == ALL else f"{API}?{urlencode({'category': slug})}"
        data = client.get_json(url)
        raw_jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(raw_jobs, list):
            raise SourceDataError(f"{POLICY.key}: response has no list of jobs")
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for raw in cast(list[JsonValue], raw_jobs):
            parsed = parse_job(raw)
            if isinstance(parsed, SourceIssue):
                issues.append(parsed)
            else:
                listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))
