"""Import the user's own LinkedIn data through the Member Data Portability API (#72, ADR 0026).

EEA and Swiss members can generate a self-serve access token for their own data. The CLI asks
for it with hidden input and hands it here wrapped in `Sensitive`; it is sent only to
`api.linkedin.com` in the Authorization header, for one pull, and is never stored, logged or put
in an error message. Claude and the MCP server never handle it.

Only the domains jaa maps are requested, one at a time, never the all-domains call (which also
returns messages, connections, logins and ads data). The rows use the archive's column labels,
so profile domains go through the archive parser (ADR 0010) and land on the same field keys.
Job domains are returned as data for the CLI to show; nothing here fetches a LinkedIn job page.
"""

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from urllib.parse import urlencode, urlsplit

from jaa.connectors.http import USER_AGENT, HttpRequest, Transport, TransportError
from jaa.core.clock import utcnow
from jaa.core.errors import ImportSourceError
from jaa.core.models import (
    FactCandidate,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JsonValue,
    RetentionChoice,
)
from jaa.importers.base import ImportIssue, IssueSeverity
from jaa.importers.linkedin import Row, RowTable, parse_linkedin_tables
from jaa.logging import Sensitive

log = logging.getLogger(__name__)

API_URL: Final = "https://api.linkedin.com/rest/memberSnapshotData"
_HOST: Final = "api.linkedin.com"
# The only version the Snapshot API accepts; others fail with 426.
API_VERSION: Final = "202312"
TIMEOUT_SECONDS: Final = 30.0
MAX_RESPONSE_BYTES: Final = 10 * 1024 * 1024
# A profile domain is a few pages at most; the caps stop a runaway loop, not real data.
MAX_PAGES_PER_DOMAIN: Final = 50
MAX_REQUESTS: Final = 300
# LinkedIn signals the end of the pages (and a domain with no data) with this message.
_NO_DATA: Final = b"no data found for this memberid"

INSTRUCTIONS: Final = """\
How to import from LinkedIn's Member Data Portability API (EEA and Switzerland)

1. Sign in to https://developer.linkedin.com and create an app. As its company, pick the
   default page "Member Data Portability (Member) Default Company".
2. On the app's Products tab, request "Member Data Portability API (Member)" and accept its
   terms. Access is granted straight away.
3. Open the OAuth Token Generator (Docs and tools → OAuth Token Tools), pick the app and the
   Member Data Portability scope, and generate a token. It lasts about 60 days.
4. Run:  jaa import linkedin-api   and paste the token when asked. It is hidden as you type.

jaa uses the token for this one import and never stores or logs it. It asks LinkedIn only for
the sections it imports (profile, contact details, positions, education, skills, languages,
certifications, projects, volunteering, publications, patents, honors, courses, saved jobs,
job applications, job alerts and job seeker preferences), never for messages, connections or
other activity. Revoke the token in LinkedIn's settings when you
are done if you like.

Docs: https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member
"""

# Profile domains and the archive file whose columns they share.
PROFILE_DOMAINS: Final[Mapping[str, str]] = {
    "PROFILE": "profile.csv",
    "EMAIL_ADDRESSES": "emailaddresses.csv",
    "PHONE_NUMBERS": "phonenumbers.csv",
    "POSITIONS": "positions.csv",
    "EDUCATION": "education.csv",
    "SKILLS": "skills.csv",
    "LANGUAGES": "languages.csv",
    "CERTIFICATIONS": "certifications.csv",
    "PROJECTS": "projects.csv",
    "VOLUNTEERING_EXPERIENCES": "volunteering.csv",
    "PUBLICATIONS": "publications.csv",
    "PATENTS": "patents.csv",
    "HONORS": "honors.csv",
    "COURSES": "courses.csv",
}
SAVED_JOBS: Final = "SAVED_JOBS"
JOB_APPLICATIONS: Final = "JOB_APPLICATIONS"
SAVED_JOB_ALERTS: Final = "SAVED_JOB_ALERTS"
JOB_SEEKER_PREFERENCES: Final = "JOB_SEEKER_PREFERENCES"
JOB_DOMAINS: Final = (SAVED_JOBS, JOB_APPLICATIONS, SAVED_JOB_ALERTS, JOB_SEEKER_PREFERENCES)
DOMAINS: Final = (*PROFILE_DOMAINS, *JOB_DOMAINS)

# Job seeker preferences shown as onboarding suggestions. The rest of that domain (phone
# number, commute starting address, …) is left out on purpose (data minimization).
_PREFERENCE_LABELS: Final = (
    "job titles",
    "locations",
    "preferred job types",
    "industries",
    "company employee count",
    "dream companies",
    "preferred start time range",
    "open to recruiters",
    "job seeker activity level",
    "job seeking urgency level",
)
_ALERT_LABELS: Final = ("alert date", "saved date", "alert parameters", "search url")

# OAuth access tokens are URL-safe text; anything else (spaces, newlines) would be pasted by
# mistake and must not reach a header.
_TOKEN: Final = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")


def parse_token(text: str) -> Sensitive[str]:
    """Validate a pasted access token. The error never repeats what was pasted."""
    token = text.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not _TOKEN.fullmatch(token):
        raise ImportSourceError(
            "that does not look like a LinkedIn access token; copy the whole token from the "
            "OAuth Token Generator"
        )
    return Sensitive(token)


# --- fetching ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainData:
    domain: str
    rows: tuple[Mapping[str, JsonValue], ...] = field(repr=False)
    pages: int
    # The page cap was reached before LinkedIn said there was no more data.
    truncated: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    domains: tuple[DomainData, ...]
    requests: int

    def get(self, domain: str) -> DomainData | None:
        return next((d for d in self.domains if d.domain == domain), None)


def _url(domain: str, start: int) -> str:
    return f"{API_URL}?{urlencode({'q': 'criteria', 'domain': domain, 'start': start})}"


def _refusal(status: int) -> str:
    if status == 401:
        return (
            "LinkedIn did not accept the token (HTTP 401). It may have expired or been "
            "revoked; generate a new one and try again"
        )
    if status == 403:
        return (
            "LinkedIn refused access (HTTP 403). Check that the app has the Member Data "
            "Portability API (Member) product and that the token was generated for it"
        )
    if status == 426:
        return "LinkedIn no longer accepts this API version (HTTP 426); jaa needs an update"
    if status == 429:
        return "LinkedIn is limiting requests (HTTP 429); try again later"
    return f"LinkedIn answered HTTP {status}"


def _page(
    transport: Transport, token: Sensitive[str], domain: str, start: int
) -> list[Mapping[str, JsonValue]] | None:
    """One page of a domain's rows, or None when LinkedIn says there is no (more) data."""
    request = HttpRequest(
        url=_url(domain, start),
        headers={
            "Authorization": f"Bearer {token.reveal()}",
            "Linkedin-Version": API_VERSION,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        response = transport.send(request, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES)
    except TransportError as exc:
        # The message names the failure type only, never the request headers.
        raise ImportSourceError(f"could not reach LinkedIn: {exc}") from None
    if (urlsplit(response.url).hostname or "").lower() != _HOST:
        raise ImportSourceError("LinkedIn redirected the request elsewhere; nothing was imported")
    if response.status == 404 and _NO_DATA in response.body.lower():
        return None
    if response.status != 200:
        raise ImportSourceError(_refusal(response.status))
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ImportSourceError("LinkedIn sent a response larger than jaa accepts")
    try:
        payload: JsonValue = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportSourceError("LinkedIn sent a response that is not JSON") from None
    elements: JsonValue = payload.get("elements") if isinstance(payload, dict) else None
    if isinstance(elements, dict):
        elements = [elements]
    if not isinstance(elements, list):
        raise ImportSourceError("LinkedIn's response has an unexpected format")
    rows: list[Mapping[str, JsonValue]] = []
    for element in elements:
        if not isinstance(element, dict) or element.get("snapshotDomain") != domain:
            raise ImportSourceError("LinkedIn's response has an unexpected format")
        data = element.get("snapshotData", [])
        if not isinstance(data, list):
            raise ImportSourceError("LinkedIn's response has an unexpected format")
        rows.extend(item for item in data if isinstance(item, dict))
    return rows


def fetch_snapshot(
    token: Sensitive[str], transport: Transport, domains: Sequence[str] = DOMAINS
) -> Snapshot:
    """Fetch the given domains page by page until LinkedIn reports no more data.

    Raises `ImportSourceError` when LinkedIn refuses the token or answers in a way we do not
    understand; nothing is imported then. Logs carry domains and counts only.
    """
    unknown = [d for d in domains if d not in DOMAINS]
    if unknown:
        raise ValueError(f"domains jaa does not import: {unknown}")
    fetched: list[DomainData] = []
    requests = 0
    for domain in domains:
        rows: list[Mapping[str, JsonValue]] = []
        pages = 0
        truncated = False
        while True:
            if pages >= MAX_PAGES_PER_DOMAIN:
                truncated = True
                break
            if requests >= MAX_REQUESTS:
                raise ImportSourceError("LinkedIn kept sending pages; the import was stopped")
            requests += 1
            page = _page(transport, token, domain, pages)
            if page is None:
                break
            rows.extend(page)
            pages += 1
        fetched.append(
            DomainData(domain=domain, rows=tuple(rows), pages=pages, truncated=truncated)
        )
        log.info("fetched linkedin domain=%s pages=%d rows=%d", domain, pages, len(rows))
    return Snapshot(domains=tuple(fetched), requests=requests)


# --- turning the snapshot into an import -----------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkedInJob:
    """A saved job or job application from LinkedIn. The link is shown, never fetched."""

    title: str = field(repr=False)
    company: str = field(repr=False)
    url: str | None = field(default=None, repr=False)
    date: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedLinkedInApiImport:
    run: ImportRun
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]
    saved_jobs: tuple[LinkedInJob, ...]
    applications: tuple[LinkedInJob, ...]
    # Label → value pairs; shown as suggestions, never stored as answered preferences.
    seeker_preferences: tuple[tuple[str, str], ...] = field(repr=False)
    job_alerts: tuple[tuple[tuple[str, str], ...], ...] = field(repr=False)
    # Domains LinkedIn had no data for (empty sections, or not prepared yet).
    empty_domains: tuple[str, ...]


def _row(item: Mapping[str, JsonValue]) -> Row:
    """Lower-cased labels and text values, like an archive CSV row. Nested values are dropped."""
    row: Row = {}
    for label, value in item.items():
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        elif isinstance(value, str | int | float):
            text = str(value).strip()
        else:
            continue
        row[label.strip().lower()] = text
    return row


def _rows(snapshot: Snapshot, domain: str) -> list[Row]:
    data = snapshot.get(domain)
    return [r for r in (_row(item) for item in data.rows) if any(r.values())] if data else []


def _jobs(rows: Sequence[Row]) -> tuple[LinkedInJob, ...]:
    jobs: list[LinkedInJob] = []
    for row in rows:
        title = " ".join(row.get("job title", "").split())
        company = " ".join(row.get("company name", "").split())
        if not title or not company:
            continue
        url = row.get("job url", "") or None
        date = row.get("saved date") or row.get("application date") or None
        jobs.append(LinkedInJob(title=title, company=company, url=url, date=date))
    return tuple(jobs)


def _labelled(row: Row, labels: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple((label, row[label]) for label in labels if row.get(label))


def _sha256(snapshot: Snapshot) -> str:
    """Hash of the profile data, so an identical re-import names the earlier run."""
    content = {d.domain: list(d.rows) for d in snapshot.domains if d.domain in PROFILE_DOMAINS}
    text = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def prepare_linkedin_api_import(
    snapshot: Snapshot, *, at: datetime | None = None
) -> PreparedLinkedInApiImport:
    """Build the import run, fact candidates and job data from a fetched snapshot. Pure."""
    at = at or utcnow()
    tables = [
        RowTable(name=f"LinkedIn {domain}", kind=kind, rows=tuple(_rows(snapshot, domain)))
        for domain, kind in PROFILE_DOMAINS.items()
        if snapshot.get(domain) is not None
    ]
    parsed = parse_linkedin_tables(tables)
    issues = list(parsed.issues)
    for data in snapshot.domains:
        if data.truncated:
            issues.append(
                ImportIssue(
                    code="too_many_pages",
                    severity=IssueSeverity.WARNING,
                    message=f"More than {MAX_PAGES_PER_DOMAIN} pages; the rest were not read.",
                    location=f"LinkedIn {data.domain}",
                )
            )
    empty = tuple(d.domain for d in snapshot.domains if not d.rows)
    if not parsed.candidates:
        issues.append(
            ImportIssue(
                code="no_facts",
                severity=IssueSeverity.ERROR,
                message="LinkedIn sent no profile values. If you generated the token just now, "
                "LinkedIn may still be preparing your data; try again later.",
            )
        )
    saved = _jobs(_rows(snapshot, SAVED_JOBS))
    applications = _jobs(_rows(snapshot, JOB_APPLICATIONS))
    preference_rows = _rows(snapshot, JOB_SEEKER_PREFERENCES)
    preferences = _labelled(preference_rows[0], _PREFERENCE_LABELS) if preference_rows else ()
    alerts = tuple(
        pairs
        for row in _rows(snapshot, SAVED_JOB_ALERTS)
        if (pairs := _labelled(row, _ALERT_LABELS))
    )
    status = (
        ImportStatus.FAILED
        if not parsed.candidates
        else (ImportStatus.PARTIAL if issues else ImportStatus.SUCCEEDED)
    )
    run = ImportRun(
        source_type=ImportSourceType.LINKEDIN_API,
        source_path=None,
        source_sha256=_sha256(snapshot),
        imported_at=at,
        status=status,
        summary={
            "requests": snapshot.requests,
            "domains": {d.domain: len(d.rows) for d in snapshot.domains},
            "empty_domains": list[JsonValue](empty),
            "files": list[JsonValue](parsed.imported_files),
            "sections": dict[str, JsonValue](parsed.sections),
            "candidates": len(parsed.candidates),
            "issues": len(issues),
            "saved_jobs": len(saved),
            "job_applications": len(applications),
            "job_alerts": len(alerts),
        },
        errors=[issue.to_json() for issue in issues],
        # Nothing was written to disk; the response is dropped once the facts are stored.
        retention=RetentionChoice.KEEP,
    )
    log.info(
        "prepared linkedin api import status=%s candidates=%d issues=%d saved_jobs=%d "
        "applications=%d",
        status,
        len(parsed.candidates),
        len(issues),
        len(saved),
        len(applications),
    )
    return PreparedLinkedInApiImport(
        run=run,
        candidates=parsed.candidates,
        issues=tuple(issues),
        saved_jobs=saved,
        applications=applications,
        seeker_preferences=preferences,
        job_alerts=alerts,
        empty_domains=empty,
    )
