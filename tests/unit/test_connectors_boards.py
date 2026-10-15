"""Greenhouse, Lever, Teamtailor and Personio connectors against recorded responses (#71)."""

from datetime import UTC, datetime

import pytest

from jaa.connectors import greenhouse, lever, personio, registry, teamtailor
from jaa.connectors.base import (
    Connector,
    OfferConnector,
    QuestionKind,
    SourcePolicy,
    SourceState,
)
from jaa.connectors.http import HttpResponse
from jaa.connectors.run import FetchOutcome, run_connector, run_connector_job
from jaa.core.errors import ConnectorTargetError, SourceDataError
from jaa.core.job_details import SalaryRange, WorkArrangement
from jaa.core.models import SourceStatus
from tests.fixtures import boards
from tests.fixtures.fake_connector import FakeClock, ReplayTransport

ACTIVE = SourceState(status=SourceStatus.ACTIVE)
POLICIES = [greenhouse.POLICY, lever.POLICY, teamtailor.POLICY, personio.POLICY]


def _run(source: str, transport: ReplayTransport, target: str) -> FetchOutcome:
    clock = FakeClock()
    connector = boards.connectors()[source]
    return run_connector(
        connector, target, state=ACTIVE, transport=transport, clock=clock, sleep=clock.sleep
    )


def _one(source: str, transport: ReplayTransport, target: str, job_id: str) -> FetchOutcome:
    clock = FakeClock()
    connector = boards.connectors()[source]
    assert isinstance(connector, OfferConnector)
    return run_connector_job(
        connector, target, job_id, state=ACTIVE, transport=transport, clock=clock, sleep=clock.sleep
    )


@pytest.mark.parametrize("policy", POLICIES, ids=lambda p: p.key)
def test_policies_are_registered_reviewed_and_public(policy: SourcePolicy) -> None:
    connector: Connector = registry.available_connectors()[policy.key]
    assert connector.policy is policy
    assert isinstance(connector, OfferConnector)
    assert policy.terms_reviewed_on is not None
    assert policy.terms_url.startswith("https://")
    assert not policy.requires_credentials
    assert policy.paused_reason is None


def test_per_company_hosts_stay_on_the_provider() -> None:
    assert teamtailor.POLICY.allows_host("acme.teamtailor.com")
    assert personio.POLICY.allows_host("acme.jobs.personio.com")
    for host in ("teamtailor.com", "a.b.teamtailor.com", "acme.teamtailor.com.evil.test",
                 "careers.acme.com", "evilteamtailor.com"):  # fmt: skip
        assert not teamtailor.POLICY.allows_host(host), host
    assert not greenhouse.POLICY.allows_host("acme.greenhouse.io")


@pytest.mark.parametrize(
    ("connector", "target", "expected"),
    [
        (greenhouse.Greenhouse(), " ExampleWidgets ", "examplewidgets"),
        (greenhouse.Greenhouse(), "EU:acme", "eu:acme"),
        (lever.Lever(), "acme-corp", "acme-corp"),
        (lever.Lever(), "eu:acme", "eu:acme"),
        (teamtailor.Teamtailor(), "Acme", "acme.teamtailor.com"),
        (teamtailor.Teamtailor(), "acme.teamtailor.com/", "acme.teamtailor.com"),
        (personio.Personio(), "acme", "acme.jobs.personio.de"),
        (personio.Personio(), "acme.jobs.personio.com", "acme.jobs.personio.com"),
    ],
)
def test_target_keys(connector: Connector, target: str, expected: str) -> None:
    assert connector.target_key(target) == expected


@pytest.mark.parametrize(
    ("connector", "target"),
    [
        (greenhouse.Greenhouse(), "us:acme"),
        (greenhouse.Greenhouse(), "acme/jobs"),
        (lever.Lever(), ""),
        (teamtailor.Teamtailor(), "careers.acme.com"),
        (teamtailor.Teamtailor(), "a.b"),
        (personio.Personio(), "acme.jobs.personio.evil.test"),
    ],
)
def test_bad_targets_are_refused_before_any_request(connector: Connector, target: str) -> None:
    with pytest.raises(ConnectorTargetError):
        connector.target_key(target)


def test_greenhouse_board() -> None:
    transport = boards.greenhouse()
    outcome = _run("greenhouse", transport, "examplewidgets")
    assert [i.code for i in outcome.issues] == ["unreadable_listing"]
    first, second = outcome.listings
    assert first.source_job_id == "4012345"
    assert first.company == "Example Widgets"
    assert first.title == "Senior Widget Engineer"
    assert first.location == "Warsaw, Poland"
    assert first.url == "https://job-boards.greenhouse.io/examplewidgets/jobs/4012345"
    assert first.description == "Build widgets.\n\nRequirements\n\n- Python\n- PostgreSQL"
    assert first.updated_at == datetime(2026, 10, 10, 13, 30, tzinfo=UTC)
    assert second.location == "Remote"
    assert outcome.requests_made == 2


def test_greenhouse_offer_reads_its_questions() -> None:
    outcome = _one("greenhouse", boards.greenhouse(), "examplewidgets", boards.GREENHOUSE_JOB)
    (job,) = outcome.listings
    assert job.posted_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert [(q.key, q.kind, q.required) for q in job.questions] == [
        ("first_name", QuestionKind.TEXT, True),
        ("resume", QuestionKind.FILE, True),
        ("question_5550001", QuestionKind.SINGLE_SELECT, False),
    ]
    assert job.questions[2].options == ("No", "Yes")
    assert outcome.requests_made == 1


def test_greenhouse_offer_that_is_gone() -> None:
    assert _one("greenhouse", boards.greenhouse(), "examplewidgets", "1").listings == ()
    with pytest.raises(ConnectorTargetError):
        _one("greenhouse", boards.greenhouse(), "examplewidgets", "../1")


def test_greenhouse_eu_boards_use_the_eu_host() -> None:
    url = "https://boards-api.eu.greenhouse.io/v1/boards/acme"
    transport = ReplayTransport({url: [HttpResponse(status=404, url=url)]})
    with pytest.raises(SourceDataError):
        _run("greenhouse", transport, "eu:acme")
    assert transport.sent[0].url == url


def test_lever_pages_and_details() -> None:
    transport = boards.lever()
    outcome = _run("lever", transport, "examplewidgets")
    assert [r.url for r in transport.sent] == [boards.lever_page_url(0), boards.lever_page_url(2)]
    backend, support, office = outcome.listings
    assert backend.company == "examplewidgets"
    assert backend.location == "Kraków, Wrocław (hybrid)"
    assert backend.posted_at == datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
    assert backend.description == (
        "Join our platform team.\n\nRequirements\n- Go or Python\n- Kubernetes\n\n"
        "Nice to have\n- Terraform\n\nWe offer a widget allowance."
    )
    assert backend.details.work_arrangement is WorkArrangement.HYBRID
    assert backend.details.salaries == (
        SalaryRange(min=18000, max=24000, currency="PLN", period="month"),
    )
    assert support.location == "Remote (remote)"
    assert office.details.work_arrangement is WorkArrangement.ON_SITE
    assert office.location == "Gdańsk"


def test_lever_offer() -> None:
    outcome = _one("lever", boards.lever(), "examplewidgets", boards.LEVER_POSTING.upper())
    (job,) = outcome.listings
    assert job.source_job_id == boards.LEVER_POSTING
    with pytest.raises(ConnectorTargetError):
        _one("lever", boards.lever(), "examplewidgets", "123")


def test_teamtailor_feed() -> None:
    outcome = _run("teamtailor", boards.teamtailor(), "examplewidgets")
    assert [i.code for i in outcome.issues] == ["unreadable_listing"]
    analyst, tester = outcome.listings
    assert analyst.source_job_id == "6100001"
    assert analyst.company == "Example Widgets"
    assert analyst.location == "Poznań (hybrid)"
    assert analyst.posted_at == datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    assert analyst.description == "Analyse widgets.\n\nRequirements\n\n- SQL\n- Excel"
    assert tester.location == "Remote"
    assert tester.details.work_arrangement is WorkArrangement.REMOTE


def test_teamtailor_offer_is_picked_from_the_feed() -> None:
    (job,) = _one("teamtailor", boards.teamtailor(), "examplewidgets", "6100002").listings
    assert job.title == "Widget Tester"
    assert _one("teamtailor", boards.teamtailor(), "examplewidgets", "999").listings == ()


def test_personio_feed() -> None:
    outcome = _run("personio", boards.personio(), "examplewidgets")
    assert [i.code for i in outcome.issues] == ["unreadable_listing"]
    frontend, student = outcome.listings
    assert frontend.url == f"https://{boards.PERSONIO_HOST}/job/1700001"
    assert frontend.company == "Example Widgets GmbH"
    assert frontend.location == "Berlin, Szczecin"
    assert frontend.description == (
        "Your tasks\n- Build widget UIs\n\nYour profile\n- TypeScript\n- React"
    )
    assert frontend.details.experience_level == "experienced"
    assert student.company == "examplewidgets"
    (one,) = _one("personio", boards.personio(), "examplewidgets", "1700002").listings
    assert one.title == "Working Student Marketing"


def test_feeds_with_a_doctype_are_refused() -> None:
    url = boards.personio_url()
    body = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><workzag-jobs/>'
    transport = ReplayTransport({url: [HttpResponse(status=200, url=url, headers={}, body=body)]})
    with pytest.raises(SourceDataError, match="not a plain XML document"):
        _run("personio", transport, "examplewidgets")


def test_a_board_that_moved_to_its_own_domain_fails_only_that_target() -> None:
    url = boards.teamtailor_url()
    transport = ReplayTransport(
        {url: [HttpResponse(status=301, url="https://careers.examplewidgets.test/jobs.rss")]}
    )
    with pytest.raises(SourceDataError, match="redirects to"):
        _run("teamtailor", transport, "examplewidgets")
