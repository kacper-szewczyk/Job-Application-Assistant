"""The policy-enforcing HTTP client every connector uses (ADR 0011).

The network sits behind `Transport` so tests replay recorded responses and never reach a real
source. `SourceClient` adds what the policy requires: https and allowed hosts only, rate
limiting, a per-run request budget, size caps, retries for transient failures, and a hard stop
on anything that looks like a login wall or bot check. Logs carry the source key, host, status
and counts, never response bodies.
"""

import json
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final, Protocol
from urllib.parse import urlsplit

from jaa import __version__
from jaa.connectors.base import SourcePolicy
from jaa.core.errors import (
    SourceAccessDeniedError,
    SourceDataError,
    SourceUnavailableError,
)
from jaa.core.models import JsonValue

log = logging.getLogger(__name__)

USER_AGENT: Final = f"jaa/{__version__} (local job application assistant)"

# Statuses that mean "you may not have this", whatever the reason. Never retried.
DENIED_STATUSES: Final = frozenset({401, 403, 407, 451})
# Statuses worth trying again after a pause.
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpResponse:
    status: int
    # Final URL after any redirects the transport followed.
    url: str
    # Header names in lower case.
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes = field(default=b"", repr=False)


class TransportError(Exception):
    """The request did not produce a response (DNS, connection, timeout, oversized body)."""


class Transport(Protocol):
    """Performs one GET and returns any HTTP status as a response.

    Implementations read at most `max_bytes + 1` bytes of the body, so the client can tell an
    oversized body from a full one, and raise `TransportError` when no response arrives.
    """

    def send(self, request: HttpRequest, *, timeout: float, max_bytes: int) -> HttpResponse: ...


class RateLimiter:
    """Sliding window: at most `requests` calls in any `per_seconds`. Clock and sleep injectable."""

    def __init__(
        self,
        requests: int,
        per_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._requests = requests
        self._per_seconds = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()

    def acquire(self) -> None:
        """Block until one more request fits in the window, then count it."""
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self._per_seconds:
                self._sent.popleft()
            if len(self._sent) < self._requests:
                self._sent.append(now)
                return
            self._sleep(self._per_seconds - (now - self._sent[0]))


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse `Retry-After` (delay seconds or an HTTP date). None if absent or unreadable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _looks_like_bot_check(response: HttpResponse) -> bool:
    # Cloudflare marks challenge pages explicitly; other vendors are caught by the HTML check
    # in `get_json` or by their 403s.
    return response.headers.get("cf-mitigated", "").lower() == "challenge"


class SourceClient:
    """GET requests for one connector run, within the connector's `SourcePolicy`."""

    def __init__(
        self,
        policy: SourcePolicy,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy
        self._transport = transport
        self._sleep = sleep
        self._limiter = RateLimiter(
            policy.rate_limit.requests, policy.rate_limit.per_seconds, clock=clock, sleep=sleep
        )
        self.requests_made = 0

    def _check_url(self, url: str, *, redirected: bool = False) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in self.policy.allowed_hosts:
            if redirected:
                # Typically a login page or a bot check on another host; following it would be
                # working around the source's access control.
                raise SourceAccessDeniedError(
                    f"{self.policy.key}: redirected to {parts.scheme}://{host}, "
                    "which the source policy does not allow"
                )
            raise SourceDataError(
                f"{self.policy.key}: {parts.scheme}://{host} is not an allowed https host"
            )
        if parts.username is not None or parts.password is not None:
            raise SourceDataError(f"{self.policy.key}: URLs must not carry credentials")
        return host

    def _send_once(self, url: str, host: str, headers: Mapping[str, str]) -> HttpResponse | None:
        """One attempt. None means a transport error worth retrying."""
        if self.requests_made >= self.policy.rate_limit.max_requests_per_run:
            raise SourceDataError(
                f"{self.policy.key}: request budget of "
                f"{self.policy.rate_limit.max_requests_per_run} per run exhausted"
            )
        self._limiter.acquire()
        self.requests_made += 1
        request = HttpRequest(url=url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            return self._transport.send(
                request,
                timeout=self.policy.retry.timeout_seconds,
                max_bytes=self.policy.max_response_bytes,
            )
        except TransportError as exc:
            log.warning(
                "source=%s host=%s transport error: %s", self.policy.key, host, type(exc).__name__
            )
            return None

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        """GET `url`, retrying transient failures. Returns only 2xx responses.

        Raises `SourceAccessDeniedError` for denials (never retried), `SourceUnavailableError`
        when retries run out, and `SourceDataError` for policy violations.
        """
        host = self._check_url(url)
        retry = self.policy.retry
        for attempt in range(retry.max_retries + 1):
            response = self._send_once(url, host, headers or {})
            wait = retry.backoff_seconds * 2**attempt
            if response is not None:
                self._check_url(response.url, redirected=True)
                log.debug(
                    "source=%s host=%s status=%s bytes=%s",
                    self.policy.key,
                    host,
                    response.status,
                    len(response.body),
                )
                if response.status in DENIED_STATUSES or _looks_like_bot_check(response):
                    raise SourceAccessDeniedError(
                        f"{self.policy.key}: {host} refused access (HTTP {response.status})"
                    )
                if len(response.body) > self.policy.max_response_bytes:
                    raise SourceDataError(
                        f"{self.policy.key}: response from {host} exceeds "
                        f"{self.policy.max_response_bytes} bytes"
                    )
                if 200 <= response.status < 300:
                    return response
                if response.status not in RETRY_STATUSES:
                    raise SourceDataError(
                        f"{self.policy.key}: unexpected HTTP {response.status} from {host}"
                    )
                asked = retry_after_seconds(response.headers.get("retry-after"))
                if asked is not None:
                    if asked > retry.max_wait_seconds:
                        raise SourceUnavailableError(
                            f"{self.policy.key}: {host} asked to wait {asked:.0f}s; try later"
                        )
                    wait = asked
            if attempt < retry.max_retries:
                log.info(
                    "source=%s host=%s retry %s/%s in %.1fs",
                    self.policy.key,
                    host,
                    attempt + 1,
                    retry.max_retries,
                    wait,
                )
                self._sleep(wait)
        raise SourceUnavailableError(
            f"{self.policy.key}: {host} still failing after {retry.max_retries + 1} attempts"
        )

    def get_json(self, url: str) -> JsonValue:
        """GET `url` and decode JSON. An HTML answer is treated as a login wall or bot check."""
        response = self.get(url, headers={"Accept": "application/json"})
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith("text/html"):
            raise SourceAccessDeniedError(
                f"{self.policy.key}: got an HTML page instead of data "
                "(possibly a login wall or bot check)"
            )
        try:
            data: JsonValue = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceDataError(f"{self.policy.key}: response is not valid JSON") from exc
        return data
