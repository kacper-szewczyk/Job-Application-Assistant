"""`jaa jobs add <link>`, `jaa jobs follow|unfollow|following` and `jaa jobs fetch --followed`
against recorded career board responses (#71, ADR 0025)."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.cli import jobs as jobs_cli
from jaa.connectors import registry
from jaa.connectors.http import HttpRequest, HttpResponse, TransportError
from tests.fixtures import boards
from tests.fixtures.fake_connector import ReplayTransport

runner = CliRunner()

GREENHOUSE_OFFER = "https://job-boards.greenhouse.io/examplewidgets/jobs/4012345"


class _Recorded:
    """Every recorded board at once; any other URL fails the test."""

    def __init__(self, *transports: ReplayTransport) -> None:
        self.responses: dict[str, list[HttpResponse | TransportError]] = {}
        for transport in transports:
            self.responses.update(transport.responses)
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest, *, timeout: float, max_bytes: int) -> HttpResponse:
        self.sent.append(request)
        answers: Mapping[str, list[HttpResponse | TransportError]] = self.responses
        if request.url not in answers:
            raise AssertionError(f"unexpected request: {request.url}")
        answer = answers[request.url][0]
        assert isinstance(answer, HttpResponse)
        return answer


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    recorded = _Recorded(
        boards.greenhouse(), boards.lever(), boards.teamtailor(), boards.personio()
    )
    monkeypatch.setattr(jobs_cli, "make_transport", lambda: recorded)
    return recorded


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(registry, "available_connectors", boards.connectors)
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


def _run(env: dict[str, str], *args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env)
    assert result.exit_code == code, result.output
    return result.output


def test_add_an_offer_by_its_link(env: dict[str, str], transport: _Recorded) -> None:
    out = _run(env, "jobs", "add", GREENHOUSE_OFFER)
    assert "Added job 1 from greenhouse: Senior Widget Engineer at Example Widgets." in out
    assert [r.url for r in transport.sent] == [
        f"{boards.GREENHOUSE_API}/jobs/{boards.GREENHOUSE_JOB}?questions=true"
    ]
    shown = _run(env, "jobs", "show", "1")
    assert "greenhouse" in shown and "- Python" in shown

    again = _run(env, "jobs", "add", "--url", GREENHOUSE_OFFER)
    assert "Updated job 1 from greenhouse" in again

    teamtailor = "https://examplewidgets.teamtailor.com/jobs/6100002-widget-tester"
    assert "Added job 2 from teamtailor: Widget Tester" in _run(env, "jobs", "add", teamtailor)


def test_offer_link_with_pasted_fields_needs_no_fetch(
    env: dict[str, str], transport: _Recorded
) -> None:
    pasted = ["--title", "Senior Widget Engineer", "--company", "Example Widgets"]
    out = _run(env, "jobs", "add", GREENHOUSE_OFFER, *pasted, code=2)
    assert "--no-fetch" in out
    out = _run(env, "jobs", "add", GREENHOUSE_OFFER, *pasted, "--no-fetch")
    assert "Added job 1: Senior Widget Engineer" in out
    assert transport.sent == []
    assert "manual" in _run(env, "jobs", "list")


def test_offer_that_is_gone(env: dict[str, str], transport: _Recorded) -> None:
    out = _run(env, "jobs", "add", "https://boards.greenhouse.io/examplewidgets/jobs/1", code=1)
    assert "no longer lists this offer" in out


def test_paused_source_points_to_pasting(env: dict[str, str], transport: _Recorded) -> None:
    _run(env, "sources", "pause", "greenhouse")
    out = _run(env, "jobs", "add", GREENHOUSE_OFFER, code=1)
    assert "greenhouse is paused" in out and "--no-fetch" in out
    assert transport.sent == []


def test_board_link_offers_to_follow(env: dict[str, str], transport: _Recorded) -> None:
    out = _run(env, "jobs", "add", "https://jobs.lever.co/examplewidgets", code=1)
    assert "jaa jobs follow https://jobs.lever.co/examplewidgets" in out
    assert "jaa jobs fetch lever examplewidgets" in out
    assert transport.sent == []


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.linkedin.com/jobs/view/123", "LinkedIn does not allow automated access"),
        ("https://justjoin.it/job-offer/x", "robots.txt"),
        ("https://careers.example.test/jobs/1", "does not open pages on careers.example.test"),
    ],
)
def test_other_links_need_the_offer_pasted(
    env: dict[str, str], transport: _Recorded, url: str, reason: str
) -> None:
    out = _run(env, "jobs", "add", url, code=2)
    assert reason in out and "--title" in out
    assert transport.sent == []
    pasted = _run(env, "jobs", "add", url, "--title", "Gadget Engineer", "--company", "Acme")
    assert "Added job 1: Gadget Engineer at Acme." in pasted
    assert transport.sent == []


def test_link_given_twice_or_not_at_all(env: dict[str, str]) -> None:
    assert "give the offer's link once" in _run(env, "jobs", "add", code=2)
    out = _run(env, "jobs", "add", GREENHOUSE_OFFER, "--url", GREENHOUSE_OFFER, code=2)
    assert "give the offer's link once" in out


def test_follow_fetch_and_unfollow(env: dict[str, str], transport: _Recorded) -> None:
    assert "You follow no boards yet" in _run(env, "jobs", "following")
    assert "You follow no boards yet" in _run(env, "jobs", "fetch", "--followed")

    out = _run(env, "jobs", "follow", "https://job-boards.greenhouse.io/examplewidgets")
    assert "Following greenhouse examplewidgets." in out
    assert "Already following" in _run(env, "jobs", "follow", "greenhouse", "ExampleWidgets")
    _run(env, "jobs", "follow", "https://jobs.lever.co/examplewidgets/" + boards.LEVER_POSTING)
    _run(env, "jobs", "follow", "teamtailor", "examplewidgets")
    _run(env, "jobs", "follow", "personio", "examplewidgets")

    listed = _run(env, "jobs", "following")
    for text in (
        "https://job-boards.greenhouse.io/examplewidgets",
        "https://jobs.lever.co/examplewidgets",
        "examplewidgets.teamtailor.com",
        "examplewidgets.jobs.personio.de",
    ):
        assert text in listed

    out = _run(env, "jobs", "fetch", "--followed")
    assert "greenhouse examplewidgets: 2 listing(s); 2 new" in out
    assert "lever examplewidgets: 3 listing(s); 3 new" in out
    assert "teamtailor examplewidgets.teamtailor.com: 2 listing(s); 2 new" in out
    assert "personio examplewidgets.jobs.personio.de: 2 listing(s); 2 new" in out

    assert "No longer following lever" in _run(env, "jobs", "unfollow", "lever", "examplewidgets")
    assert "jobs.lever.co" not in _run(env, "jobs", "following")
    assert "Office Manager" in _run(env, "jobs", "list")


def test_follow_refuses_what_jaa_cannot_read(env: dict[str, str]) -> None:
    out = _run(env, "jobs", "follow", "https://justjoin.it/", code=1)
    assert "jaa cannot follow justjoin.it" in out
    assert "not a link to a board" in _run(
        env, "jobs", "follow", "https://careers.example.test", code=1
    )
    assert "unknown source 'nope'" in _run(env, "jobs", "follow", "nope", "x", code=1)
    assert "not a Teamtailor site" in _run(
        env, "jobs", "follow", "teamtailor", "careers.example.test", code=1
    )


def test_fetch_needs_a_source_or_followed(env: dict[str, str]) -> None:
    assert "--followed on its own" in _run(env, "jobs", "fetch", code=2)
    assert "--followed on its own" in _run(env, "jobs", "fetch", "lever", "--followed", code=2)
    assert "name what to fetch" in _run(env, "jobs", "fetch", "lever", code=2)
