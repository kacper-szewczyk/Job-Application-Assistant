import json
from datetime import UTC, datetime

import pytest

from jaa.connectors import registry
from jaa.connectors.base import IssueSeverity, JobListing, SourceIssue, SourceState
from jaa.connectors.run import FetchOutcome, run_connector
from jaa.connectors.solid_jobs import POLICY, division, location_text, page_url, parse_job
from jaa.core.errors import ConnectorTargetError, SourceAccessDeniedError, SourceDataError
from jaa.core.models import SourceStatus
from tests.fixtures import solid_jobs as rec
from tests.fixtures.fake_connector import FakeClock, json_response

ACTIVE = SourceState(status=SourceStatus.ACTIVE)


def _run(transport: rec.ReplayTransport, target: str = "IT") -> FetchOutcome:
    clock = FakeClock()
    return run_connector(
        rec.connector(), target, state=ACTIVE, transport=transport, clock=clock, sleep=clock.sleep
    )


def test_policy_is_registered_and_reviewed() -> None:
    assert registry.available_connectors()["solid_jobs"].policy is POLICY
    assert POLICY.allowed_hosts == {"solid.jobs"}
    assert POLICY.terms_reviewed_on is not None
    assert not POLICY.requires_credentials
    # Well under the documented 300 requests per minute.
    assert POLICY.rate_limit.requests / POLICY.rate_limit.per_seconds * 60 <= 300


def test_page_url_names_the_campaign() -> None:
    assert page_url("IT", 3) == (
        "https://solid.jobs/public-api/offers/IT?campaign=jaa&pageSize=500&pageIndex=3"
    )


@pytest.mark.parametrize(("target", "expected"), [("it", "IT"), (" Finances ", "Finances")])
def test_division_is_matched_case_insensitively(target: str, expected: str) -> None:
    assert division(target) == expected


def test_unknown_division_fails_before_any_request() -> None:
    transport = rec.first_fetch()
    with pytest.raises(ConnectorTargetError, match="use one of IT, Engineering"):
        _run(transport, "Astrology")
    assert transport.sent == []


def test_fetch_reads_every_page() -> None:
    transport = rec.first_fetch()
    outcome = _run(transport)
    assert [r.url for r in transport.sent] == [rec.url(0), rec.url(1)]
    assert all(r.headers["Accept"] == "application/json" for r in transport.sent)
    assert [x.source_job_id for x in outcome.listings] == [rec.KEY_A, rec.KEY_B, rec.KEY_C]
    assert outcome.requests_made == 2
    codes = {(i.code, i.severity, i.source_job_id) for i in outcome.issues}
    assert codes == {
        ("unreadable_listing", IssueSeverity.ERROR, None),
        ("duplicate_listing", IssueSeverity.WARNING, rec.KEY_A),
    }


def test_listing_fields() -> None:
    listings = {x.source_job_id: x for x in _run(rec.first_fetch()).listings}
    a, b, c = listings[rec.KEY_A], listings[rec.KEY_B], listings[rec.KEY_C]
    assert a.url == "https://solid.jobs/o/syntha01/jaa"
    assert (a.title, a.company) == ("Senior Widget Engineer", "Example Widgets Sp. z o.o.")
    assert a.location == "Warszawa (hybrid)"
    assert a.description == (
        "Czym będziesz się zajmować?\n\nBudowa syntetycznych widgetów.\n\n"
        "- Python\n- Event streaming"
    )
    assert a.posted_at == datetime(2026, 10, 1, 7, 0, 0, 159090, tzinfo=UTC)
    assert a.updated_at is not None
    assert a.questions == ()
    assert (b.location, b.description) == ("Remote", "Fully remote & synthetic.")
    assert c.location == "Kraków, Wrocław"
    assert c.updated_at is None  # "not a date" in the source


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("text", "not a JSON object"),
        ({"title": "x", "company": "y", "url": "https://solid.jobs/o/x"}, "no jobOfferKey"),
        ({"jobOfferKey": "k", "title": " ", "company": "y", "url": "u"}, "title, company or url"),
    ],
)
def test_unreadable_offers_become_issues(raw: object, message: str) -> None:
    parsed = parse_job(raw)  # pyright: ignore[reportArgumentType] - deliberately malformed
    assert isinstance(parsed, SourceIssue)
    assert message in parsed.message


def test_naive_dates_are_dropped() -> None:
    parsed = parse_job(
        {
            "jobOfferKey": "k",
            "title": "t",
            "company": "c",
            "url": "https://solid.jobs/o/k/jaa",
            "validFrom": "2026-10-01T09:00:00",
        }
    )
    assert isinstance(parsed, JobListing)
    assert parsed.posted_at is None


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"locations": ["Łódź"], "isRemote": True, "isHybrid": True}, "Łódź (remote)"),
        ({"locations": [], "isHybrid": True}, "Hybrid"),
        ({"locations": ["Poznań", " "]}, "Poznań"),
        ({}, None),
    ],
)
def test_location_text(job: dict[str, object], expected: str | None) -> None:
    assert location_text(job) == expected  # pyright: ignore[reportArgumentType] - JSON literal


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"pageIndex": 0, "totalPages": 1}, "no jobs list"),
        ({"pageIndex": 4, "totalPages": 5, "jobs": []}, "asked for page 0"),
        ({"pageIndex": 0, "jobs": [], "totalPages": "many"}, "no valid totalPages"),
    ],
)
def test_malformed_envelopes_fail_the_fetch(body: dict[str, object], message: str) -> None:
    transport = rec.answering(json_response(rec.url(0), json.dumps(body).encode()))
    with pytest.raises(SourceDataError, match=message):
        _run(transport)


def test_an_html_page_counts_as_access_denied() -> None:
    page = json_response(rec.url(0), b"<html>Verify you are human</html>")
    transport = rec.answering(
        type(page)(status=200, url=page.url, headers={"content-type": "text/html"}, body=page.body)
    )
    with pytest.raises(SourceAccessDeniedError):
        _run(transport)
