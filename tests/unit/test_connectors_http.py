import json
import logging
from datetime import UTC, datetime

import pytest

from jaa.connectors.base import RateLimit, RetryPolicy, SourcePolicy
from jaa.connectors.http import (
    USER_AGENT,
    HttpResponse,
    RateLimiter,
    SourceClient,
    TransportError,
    retry_after_seconds,
)
from jaa.core.errors import (
    SourceAccessDeniedError,
    SourceDataError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from tests.fixtures.fake_connector import (
    HOST,
    FakeClock,
    ReplayTransport,
    json_response,
    make_policy,
)

URL = f"https://{HOST}/v1/boards/example/jobs"
OK = json_response(URL, b'{"jobs": []}')


def _client(
    transport: ReplayTransport, clock: FakeClock, policy: SourcePolicy | None = None
) -> SourceClient:
    return SourceClient(policy or make_policy(), transport, clock=clock, sleep=clock.sleep)


def _status(status: int, **headers: str) -> HttpResponse:
    return json_response(URL, b"{}", status=status, **headers)


def test_rate_limiter_spaces_requests_within_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 1.0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    # Two per second: requests 3 and 5 wait for the window to clear.
    assert clock.sleeps == [1.0, 1.0]


def test_get_json_sends_user_agent_and_respects_rate_limit() -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [OK]})
    client = _client(transport, clock)
    for _ in range(3):
        assert client.get_json(URL) == {"jobs": []}
    assert client.requests_made == 3
    assert clock.sleeps == [1.0]
    assert transport.sent[0].headers["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.example.test/v1/jobs",
        "https://elsewhere.example.test/v1/jobs",
        "https://user:pw@boards.example.test/v1/jobs",
    ],
)
def test_refuses_urls_outside_policy_without_sending(url: str) -> None:
    transport = ReplayTransport({})
    with pytest.raises(SourceDataError):
        _client(transport, FakeClock()).get(url)
    assert transport.sent == []


@pytest.mark.parametrize(
    "response",
    [
        _status(401),
        _status(403),
        _status(451),
        _status(503, **{"cf-mitigated": "challenge"}),
        json_response("https://login.example.test/sign-in", b"{}"),
        HttpResponse(status=200, url=URL, headers={"content-type": "text/html"}, body=b"<html>"),
    ],
    ids=["401", "403", "451", "bot-check", "login-redirect", "html"],
)
def test_access_denied_is_never_retried(response: HttpResponse) -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [response]})
    with pytest.raises(SourceAccessDeniedError):
        _client(transport, clock).get_json(URL)
    assert len(transport.sent) == 1
    assert clock.sleeps == []


def test_retries_transient_failures_with_backoff() -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [TransportError("reset"), _status(503), OK]})
    assert _client(transport, clock).get_json(URL) == {"jobs": []}
    assert len(transport.sent) == 3
    # Exponential backoff (1s, 2s); the rate limiter never had to wait on top of it.
    assert clock.sleeps == [1.0, 2.0]


def test_retry_after_is_honoured() -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [_status(429, **{"retry-after": "7"}), OK]})
    _client(transport, clock).get(URL)
    assert clock.sleeps == [7.0]


def test_long_retry_after_gives_up_immediately() -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [_status(429, **{"retry-after": "3600"})]})
    with pytest.raises(SourceUnavailableError, match="wait 3600s"):
        _client(transport, clock).get(URL)
    assert len(transport.sent) == 1


def test_gives_up_after_max_retries() -> None:
    clock = FakeClock()
    transport = ReplayTransport({URL: [_status(502)]})
    with pytest.raises(SourceUnavailableError, match="3 attempts"):
        _client(transport, clock).get(URL)
    assert len(transport.sent) == 3


def test_unexpected_status_is_a_data_error() -> None:
    with pytest.raises(SourceDataError, match="410"):
        _client(ReplayTransport({URL: [_status(410)]}), FakeClock()).get(URL)


def test_missing_page_is_not_found() -> None:
    with pytest.raises(SourceNotFoundError, match="404"):
        _client(ReplayTransport({URL: [_status(404)]}), FakeClock()).get(URL)


COMPANY_URL = "https://acme.ats.test/jobs.rss"


def _company_policy() -> SourcePolicy:
    return make_policy(allowed_hosts=frozenset[str](), host_suffixes=frozenset({"ats.test"}))


def _feed(body: bytes, content_type: str = "application/rss+xml") -> ReplayTransport:
    return ReplayTransport(
        {
            COMPANY_URL: [
                HttpResponse(
                    status=200, url=COMPANY_URL, headers={"content-type": content_type}, body=body
                )
            ]
        }
    )


def test_company_hosts_are_requested_and_parsed_as_xml() -> None:
    client = _client(_feed(b"<rss><channel/></rss>"), FakeClock(), _company_policy())
    assert client.get_xml(COMPANY_URL).tag == "rss"
    with pytest.raises(SourceDataError, match="not an allowed https host"):
        client.get_xml("https://a.b.ats.test/jobs.rss")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b'<!DOCTYPE rss [<!ENTITY a "b">]><rss>&a;</rss>', SourceDataError),
        (b"<rss><channel></rss>", SourceDataError),
        (b"not xml", SourceDataError),
    ],
)
def test_unsafe_or_broken_xml_is_refused(body: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        _client(_feed(body), FakeClock(), _company_policy()).get_xml(COMPANY_URL)


def test_html_instead_of_a_feed_is_access_denied() -> None:
    client = _client(_feed(b"<html></html>", "text/html"), FakeClock(), _company_policy())
    with pytest.raises(SourceAccessDeniedError):
        client.get_xml(COMPANY_URL)


def test_redirect_off_a_company_host_fails_that_board_only() -> None:
    moved = HttpResponse(status=301, url="https://careers.acme.test/jobs.rss")
    client = _client(ReplayTransport({COMPANY_URL: [moved]}), FakeClock(), _company_policy())
    with pytest.raises(SourceDataError, match="redirects to") as caught:
        client.get(COMPANY_URL)
    assert not isinstance(caught.value, SourceAccessDeniedError)


def test_request_budget_per_run() -> None:
    policy = make_policy(rate_limit=RateLimit(requests=10, per_seconds=1, max_requests_per_run=2))
    client = _client(ReplayTransport({URL: [OK]}), FakeClock(), policy)
    client.get(URL)
    client.get(URL)
    with pytest.raises(SourceDataError, match="budget"):
        client.get(URL)


def test_oversized_response_is_refused() -> None:
    policy = make_policy(max_response_bytes=4)
    with pytest.raises(SourceDataError, match="exceeds"):
        _client(ReplayTransport({URL: [OK]}), FakeClock(), policy).get(URL)


def test_invalid_json_is_a_data_error() -> None:
    transport = ReplayTransport({URL: [json_response(URL, b"{not json")]})
    with pytest.raises(SourceDataError, match="JSON"):
        _client(transport, FakeClock()).get_json(URL)


def test_retry_after_parsing() -> None:
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    assert retry_after_seconds(None) is None
    assert retry_after_seconds(" 12 ") == 12.0
    assert retry_after_seconds("Wed, 14 Oct 2026 09:00:30 GMT", now=now) == 30.0
    assert retry_after_seconds("Wed, 14 Oct 2026 08:00:00 GMT", now=now) == 0.0
    assert retry_after_seconds("soon") is None


def test_logs_hold_no_response_bodies(caplog: pytest.LogCaptureFixture) -> None:
    marker = "synthetic-body-marker"
    body = json.dumps({"jobs": [], "note": marker}).encode()
    transport = ReplayTransport({URL: [_status(503), json_response(URL, body)]})
    policy = make_policy(retry=RetryPolicy(max_retries=1, backoff_seconds=0))
    with caplog.at_level(logging.DEBUG, logger="jaa"):
        _client(transport, FakeClock(), policy).get_json(URL)
    assert "status=200" in caplog.text
    assert marker not in caplog.text
