"""The job source connector contract: source policy metadata, listing output and the interface.

Every connector declares a `SourcePolicy` that says how it may reach its source and what it may
keep. Connectors never open connections themselves: they get a `SourceClient`
(`jaa.connectors.http`) bound to their policy, which enforces hosts, rate limits, retries and
access-denied handling for every connector alike. `run_connector` (`jaa.connectors.run`) checks
the pause state and validates what the connector returns. See docs/connectors.md and ADR 0011.

Connectors return data and never write to storage.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from jaa.core.job_details import JobDetails
from jaa.core.models import SourceStatus, SourceStatusEvent

if TYPE_CHECKING:
    from jaa.connectors.http import SourceClient

_KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,39}")
_HOST_PATTERN: Final = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)
# One DNS label: the company's part of a per-company host such as `acme.teamtailor.com`.
_LABEL_PATTERN: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class AccessMethod(StrEnum):
    """How a connector reaches its source. HTML scraping is deliberately absent (ADR 0011)."""

    # A documented, public API meant for programmatic use (e.g. Greenhouse job board GET API).
    PUBLIC_API = "public_api"
    # A published RSS/Atom/JSON/XML feed the employer or board offers for syndication.
    FEED = "feed"


class ListingField(StrEnum):
    """Fields of a `JobListing`. A policy lists which ones the connector may keep."""

    SOURCE_JOB_ID = "source_job_id"
    URL = "url"
    TITLE = "title"
    COMPANY = "company"
    LOCATION = "location"
    DESCRIPTION = "description"
    POSTED_AT = "posted_at"
    UPDATED_AT = "updated_at"
    QUESTIONS = "questions"
    # Structured skills, languages, experience level, work arrangement and salary (ADR 0015).
    DETAILS = "details"


# Without these a listing cannot be identified, deduplicated or shown, so every policy permits them.
REQUIRED_FIELDS: Final = frozenset(
    {ListingField.SOURCE_JOB_ID, ListingField.URL, ListingField.TITLE, ListingField.COMPANY}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    """At most `requests` requests in any `per_seconds` window, and `max_requests_per_run` in total.

    The per-run budget stops a pagination bug from turning into an unbounded crawl.
    `max_requests_per_day` is for sources that ask for only a few calls a day (Remotive): it is
    checked against the stored fetch runs of the last 24 hours before a run starts (ADR 0028).
    """

    requests: int
    per_seconds: float
    max_requests_per_run: int = 100
    max_requests_per_day: int | None = None

    def __post_init__(self) -> None:
        if self.requests < 1 or self.per_seconds <= 0 or self.max_requests_per_run < 1:
            raise ValueError("rate limit values must be positive")
        if self.max_requests_per_day is not None and (
            self.max_requests_per_day < self.max_requests_per_run
        ):
            raise ValueError("max_requests_per_day must allow at least one full run")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """Retries for transient failures: network errors, HTTP 429 and 5xx.

    Waits follow `Retry-After` when the source sends it, otherwise `backoff_seconds * 2**attempt`.
    A source asking for a longer wait than `max_wait_seconds` is not retried in this run.
    Access denials (401/403/451, login redirects, bot checks) are never retried.
    """

    max_retries: int = 2
    backoff_seconds: float = 1.0
    max_wait_seconds: float = 60.0
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.backoff_seconds < 0:
            raise ValueError("retry values must not be negative")
        if self.max_wait_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("wait and timeout must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePolicy:
    """What a connector may access and keep, and why it is allowed to (SPEC §6, §7).

    A policy with `paused_reason` set, or whose terms have not been reviewed
    (`terms_reviewed_on` is None), cannot run and cannot be resumed by the user: unclear access
    means paused (SPEC §10).
    """

    key: str
    name: str
    access_method: AccessMethod
    # Exact host names the client may request. Redirects elsewhere count as access denied.
    allowed_hosts: frozenset[str]
    # Provider domains that serve each company from its own subdomain (ADR 0025): a suffix
    # `teamtailor.com` allows `acme.teamtailor.com`, exactly one label deeper, nothing else.
    host_suffixes: frozenset[str] = frozenset()
    # Where the source's terms or API documentation say programmatic access is permitted.
    terms_url: str
    terms_reviewed_on: date | None
    permitted_fields: frozenset[ListingField]
    rate_limit: RateLimit
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    max_response_bytes: int = 10 * 1024 * 1024
    # Credentials the source requires. Applicant-facing sources must not need any (SPEC §6).
    requires_credentials: bool = False
    paused_reason: str | None = None
    # Anything else a reviewer should know: coverage, known quirks, how failures look.
    notes: str = ""

    def __post_init__(self) -> None:
        if not _KEY_PATTERN.fullmatch(self.key):
            raise ValueError(f"connector key must match {_KEY_PATTERN.pattern}: {self.key!r}")
        if not self.allowed_hosts and not self.host_suffixes:
            raise ValueError(f"{self.key}: allowed_hosts or host_suffixes must not be empty")
        bad_hosts = sorted(
            h for h in self.allowed_hosts | self.host_suffixes if not _HOST_PATTERN.fullmatch(h)
        )
        if bad_hosts:
            raise ValueError(f"{self.key}: invalid host names {bad_hosts}")
        if not self.terms_url.startswith("https://"):
            raise ValueError(f"{self.key}: terms_url must be an https URL")
        missing = REQUIRED_FIELDS - self.permitted_fields
        if missing:
            raise ValueError(f"{self.key}: required fields not permitted {sorted(missing)}")
        if self.max_response_bytes < 1:
            raise ValueError(f"{self.key}: max_response_bytes must be positive")

    def is_company_host(self, host: str) -> bool:
        """Whether `host` is one company's subdomain under one of `host_suffixes`."""
        label, _, rest = host.partition(".")
        return rest in self.host_suffixes and _LABEL_PATTERN.fullmatch(label) is not None

    def allows_host(self, host: str) -> bool:
        """Whether the client may request `host` (already lower-cased)."""
        return host in self.allowed_hosts or self.is_company_host(host)


class QuestionKind(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    BOOLEAN = "boolean"
    FILE = "file"
    OTHER = "other"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationQuestion:
    """A question the employer's application form asks, as the source publishes it.

    These are the employer's words, not answers; classifying and answering them is T14.
    """

    key: str
    label: str
    kind: QuestionKind
    required: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class JobListing:
    """One job as a connector reports it, before normalization and dedupe (T11).

    Datetimes must be timezone-aware. `url` is the listing's public page as the source links it.
    `canonical_url` is set when the connector knows a cleaner identity for that page (e.g. without
    a campaign segment); storage normalizes it, or `url` when it is None (ADR 0014).
    """

    source_job_id: str
    url: str
    canonical_url: str | None = None
    title: str
    company: str
    location: str | None = None
    description: str | None = field(default=None, repr=False)
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    questions: tuple[ApplicationQuestion, ...] = ()
    details: JobDetails = field(default_factory=JobDetails)


class IssueSeverity(StrEnum):
    # The listing (or the whole target) was not used.
    ERROR = "error"
    # The listing was used, but part of it was dropped.
    WARNING = "warning"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceIssue:
    """Something about a fetch the user should know. Messages hold no personal data."""

    code: str
    severity: IssueSeverity
    message: str
    source_job_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchResult:
    """What a connector returns for one target: listings plus problems with individual items."""

    listings: tuple[JobListing, ...]
    issues: tuple[SourceIssue, ...] = ()
    # False when the source shows only part of the target (e.g. a feed of its newest jobs), so
    # a job missing from this fetch may still be open and must not be marked removed (ADR 0028).
    complete: bool = True


class Connector(Protocol):
    """One adapter per approved source (SPEC §7).

    `fetch` reads every listing for one `target`, a connector-specific identifier such as a job
    board token or a feed URL path. It must make all requests through `client`, raise
    `SourceDataError` for responses it cannot parse, and let `SourceAccessDeniedError` and
    `SourceUnavailableError` from the client propagate. Items it cannot read go into
    `FetchResult.issues` rather than failing the whole fetch.
    """

    @property
    def policy(self) -> SourcePolicy: ...

    def target_key(self, target: str) -> str:
        """The canonical form of `target` (e.g. "it" → "IT"), used to store and compare it.

        Raises `ConnectorTargetError` for a target the connector cannot fetch, before any request.
        """
        ...

    def fetch(self, client: "SourceClient", target: str) -> FetchResult: ...


@runtime_checkable
class OfferConnector(Connector, Protocol):
    """A connector that can also read one offer by its id, for links the user brings (ADR 0025).

    `fetch_job` returns None when the source no longer lists the offer. It raises
    `ConnectorTargetError` for an id the connector cannot use, before any request.
    """

    def fetch_job(self, client: "SourceClient", target: str, job_id: str) -> JobListing | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceState:
    """Whether a source may run now, combining its policy with the stored pause history."""

    status: SourceStatus
    reason: str | None = None
    # Paused by the policy itself; only a code change (new review, new policy) can resume it.
    locked: bool = False


def effective_state(policy: SourcePolicy, latest: SourceStatusEvent | None) -> SourceState:
    """Policy pauses win over the user's choice; otherwise the latest stored event decides."""
    if policy.paused_reason is not None:
        return SourceState(status=SourceStatus.PAUSED, reason=policy.paused_reason, locked=True)
    if policy.terms_reviewed_on is None:
        return SourceState(
            status=SourceStatus.PAUSED, reason="source terms have not been reviewed", locked=True
        )
    if policy.requires_credentials:
        return SourceState(
            status=SourceStatus.PAUSED,
            reason="source requires credentials; connectors may only use public access",
            locked=True,
        )
    if latest is not None and latest.status is SourceStatus.PAUSED:
        return SourceState(status=SourceStatus.PAUSED, reason=latest.reason)
    return SourceState(status=SourceStatus.ACTIVE)


def policy_summary(policy: SourcePolicy) -> Mapping[str, str]:
    """The policy as label → text, for `jaa sources show` and docs."""
    limit = policy.rate_limit
    retry = policy.retry
    return {
        "Key": policy.key,
        "Name": policy.name,
        "Access method": policy.access_method.value,
        "Allowed hosts": ", ".join(
            [*sorted(policy.allowed_hosts), *(f"*.{s}" for s in sorted(policy.host_suffixes))]
        ),
        "Terms": policy.terms_url,
        "Terms reviewed": (
            policy.terms_reviewed_on.isoformat() if policy.terms_reviewed_on else "not reviewed"
        ),
        "Permitted fields": ", ".join(
            f.value for f in ListingField if f in policy.permitted_fields
        ),
        "Rate limit": (
            f"{limit.requests} per {limit.per_seconds:g}s, "
            f"at most {limit.max_requests_per_run} per run"
            + (
                f", {limit.max_requests_per_day} per day"
                if limit.max_requests_per_day is not None
                else ""
            )
        ),
        "Retries": (
            f"{retry.max_retries} (backoff {retry.backoff_seconds:g}s, "
            f"max wait {retry.max_wait_seconds:g}s, timeout {retry.timeout_seconds:g}s)"
        ),
        "Max response": f"{policy.max_response_bytes // 1024} KiB",
        "Requires credentials": "yes" if policy.requires_credentials else "no",
        "Notes": policy.notes or "-",
    }
