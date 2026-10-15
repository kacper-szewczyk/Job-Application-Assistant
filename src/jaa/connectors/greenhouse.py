"""Greenhouse company job boards, through the official Job Board API (#71, ADR 0025).

Greenhouse documents `GET /v1/boards/{board_token}/jobs` for anyone to read: "Job Board data is
publicly available, so authentication is not required for any GET endpoints"
(https://docs.greenhouse.io/job-board.html). A target is one company's board token, the
part after `boards.greenhouse.io/` in the board's link, chosen by the user; boards hosted in
Greenhouse's EU region are `eu:<token>`. jaa never lists Greenhouse's customers.

Listings are read-only. The API's POST endpoint for applications needs the employer's key and is
never used (SPEC §6).
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Final, cast
from urllib.parse import quote

from jaa.connectors.base import (
    AccessMethod,
    ApplicationQuestion,
    FetchResult,
    IssueSeverity,
    JobListing,
    ListingField,
    QuestionKind,
    RateLimit,
    RetryPolicy,
    SourceIssue,
    SourcePolicy,
)
from jaa.connectors.fields import escaped_html_text, iso_datetime, text
from jaa.connectors.http import SourceClient
from jaa.core.errors import ConnectorTargetError, SourceDataError, SourceNotFoundError
from jaa.core.models import JsonObject, JsonValue

HOSTS: Final = {"": "boards-api.greenhouse.io", "eu": "boards-api.eu.greenhouse.io"}
_TOKEN: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
_JOB_ID: Final = re.compile(r"[0-9]{1,20}")

POLICY: Final = SourcePolicy(
    key="greenhouse",
    name="Greenhouse job boards",
    access_method=AccessMethod.PUBLIC_API,
    allowed_hosts=frozenset(HOSTS.values()),
    terms_url="https://docs.greenhouse.io/job-board.html",
    terms_reviewed_on=date(2026, 10, 15),
    permitted_fields=frozenset(ListingField) - {ListingField.DETAILS},
    # Greenhouse states no limit; a board is two requests, an offer one.
    rate_limit=RateLimit(requests=20, per_seconds=60.0, max_requests_per_run=10),
    retry=RetryPolicy(max_retries=2, backoff_seconds=2.0, max_wait_seconds=60.0),
    # A large board with `content=true` carries every description.
    max_response_bytes=16 * 1024 * 1024,
    notes=(
        "Official Job Board API (https://docs.greenhouse.io/job-board.html): public GET "
        "endpoints, no key. Targets are board tokens the user chooses (`acme`, or `eu:acme` for "
        "boards on job-boards.eu.greenhouse.io). Application questions are read for single "
        "offers added by link. Read-only: the application POST endpoint is never used."
    ),
)


@dataclass(frozen=True, slots=True)
class Board:
    region: str
    token: str

    @property
    def target(self) -> str:
        return f"{self.region}:{self.token}" if self.region else self.token

    @property
    def base(self) -> str:
        return f"https://{HOSTS[self.region]}/v1/boards/{quote(self.token)}"


def board(target: str) -> Board:
    """`acme` or `eu:acme`, case-insensitively. Raises `ConnectorTargetError` otherwise."""
    region, _, token = target.strip().lower().rpartition(":")
    if region not in HOSTS or not _TOKEN.fullmatch(token):
        raise ConnectorTargetError(
            f"{POLICY.key}: {target!r} is not a board token; use e.g. `acme` or `eu:acme`"
        )
    return Board(region, token)


_KINDS: Final = {
    "input_text": QuestionKind.TEXT,
    "textarea": QuestionKind.LONG_TEXT,
    "input_file": QuestionKind.FILE,
    "multi_value_single_select": QuestionKind.SINGLE_SELECT,
    "multi_value_multi_select": QuestionKind.MULTI_SELECT,
}


def _question(raw: JsonValue) -> ApplicationQuestion | None:
    """One form question. A question may group fields (a CV file or pasted text); the first
    visible field names it."""
    if not isinstance(raw, dict) or (label := text(raw.get("label"))) is None:
        return None
    raw_fields = raw.get("fields")
    fields = [
        f
        for f in (cast(list[JsonValue], raw_fields) if isinstance(raw_fields, list) else [])
        if isinstance(f, dict) and f.get("type") != "input_hidden" and text(f.get("name"))
    ]
    if not fields:
        return None
    first = cast(JsonObject, fields[0])
    raw_values = first.get("values")
    options = (
        tuple(
            option
            for v in cast(list[JsonValue], raw_values)
            if isinstance(v, dict) and (option := text(v.get("label")))
        )
        if isinstance(raw_values, list)
        else ()
    )
    return ApplicationQuestion(
        key=str(text(first.get("name"))),
        label=label,
        kind=_KINDS.get(str(first.get("type")), QuestionKind.OTHER),
        required=raw.get("required") is True,
        options=options,
    )


def questions(raw: JsonValue) -> tuple[ApplicationQuestion, ...]:
    """The application form's questions, as the employer wrote them."""
    if not isinstance(raw, list):
        return ()
    return tuple(q for q in map(_question, cast(list[JsonValue], raw)) if q is not None)


def parse_job(raw: JsonValue, company: str) -> JobListing | SourceIssue:
    """One job from the API as a listing, or the reason it cannot be used."""
    if not isinstance(raw, dict):
        return _issue("job is not a JSON object", None)
    job_id = raw.get("id")
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        return _issue("job has no numeric id", None)
    title, url = text(raw.get("title")), text(raw.get("absolute_url"))
    if title is None or url is None:
        return _issue("job lacks a title or absolute_url", str(job_id))
    location = raw.get("location")
    return JobListing(
        source_job_id=str(job_id),
        url=url,
        title=title,
        company=text(raw.get("company_name")) or company,
        location=text(location.get("name")) if isinstance(location, dict) else None,
        description=escaped_html_text(raw.get("content")),
        posted_at=iso_datetime(raw.get("first_published")),
        updated_at=iso_datetime(raw.get("updated_at")),
        questions=questions(raw.get("questions")),
    )


def _issue(message: str, job_id: str | None) -> SourceIssue:
    return SourceIssue(
        code="unreadable_listing",
        severity=IssueSeverity.ERROR,
        message=message,
        source_job_id=job_id,
    )


@dataclass(frozen=True, slots=True)
class Greenhouse:
    """Reads every job on one company's Greenhouse board, or one job by its id."""

    @property
    def policy(self) -> SourcePolicy:
        return POLICY

    def target_key(self, target: str) -> str:
        return board(target).target

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        found = board(target)
        info = client.get_json(found.base)
        company = (text(info.get("name")) if isinstance(info, dict) else None) or found.token
        data = client.get_json(f"{found.base}/jobs?content=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceDataError(f"{POLICY.key}: response has no jobs list")
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for raw in cast(list[JsonValue], data["jobs"]):
            parsed = parse_job(raw, company)
            if isinstance(parsed, SourceIssue):
                issues.append(parsed)
            else:
                listings.append(parsed)
        return FetchResult(listings=tuple(listings), issues=tuple(issues))

    def fetch_job(self, client: SourceClient, target: str, job_id: str) -> JobListing | None:
        found = board(target)
        if not _JOB_ID.fullmatch(job_id):
            raise ConnectorTargetError(f"{POLICY.key}: {job_id!r} is not a Greenhouse job id")
        try:
            raw = client.get_json(f"{found.base}/jobs/{job_id}?questions=true")
        except SourceNotFoundError:
            return None
        parsed = parse_job(raw, found.token)
        if isinstance(parsed, SourceIssue):
            raise SourceDataError(f"{POLICY.key}: {parsed.message}")
        return parsed
