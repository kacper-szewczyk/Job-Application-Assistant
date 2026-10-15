"""Replaying the recorded career board responses in `tests/fixtures/connectors/boards/`."""

from pathlib import Path
from typing import Final

from jaa.connectors.base import Connector
from jaa.connectors.greenhouse import Greenhouse
from jaa.connectors.http import HttpResponse
from jaa.connectors.lever import Lever
from jaa.connectors.personio import Personio
from jaa.connectors.solid_jobs import SolidJobs
from jaa.connectors.teamtailor import Teamtailor
from tests.fixtures.fake_connector import ReplayTransport, json_response

FIXTURES: Final = Path(__file__).parent / "connectors" / "boards"
LEVER_PAGE_SIZE: Final = 2

GREENHOUSE_API: Final = "https://boards-api.greenhouse.io/v1/boards/examplewidgets"
GREENHOUSE_JOB: Final = "4012345"
LEVER_API: Final = "https://api.lever.co/v0/postings/examplewidgets"
LEVER_POSTING: Final = "0b1c2d3e-0000-4000-8000-000000000001"
TEAMTAILOR_HOST: Final = "examplewidgets.teamtailor.com"
PERSONIO_HOST: Final = "examplewidgets.jobs.personio.de"


def connectors() -> dict[str, Connector]:
    """The shipped connectors, with Lever paging by two so the fixtures cover paging."""
    found: list[Connector] = [
        SolidJobs(),
        Greenhouse(),
        Lever(page_size=LEVER_PAGE_SIZE),
        Teamtailor(),
        Personio(),
    ]
    return {c.policy.key: c for c in found}


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def xml_response(url: str, name: str) -> HttpResponse:
    return HttpResponse(
        status=200, url=url, headers={"content-type": "application/xml"}, body=_body(name)
    )


def greenhouse() -> ReplayTransport:
    return ReplayTransport(
        {
            GREENHOUSE_API: [json_response(GREENHOUSE_API, _body("greenhouse_board.json"))],
            f"{GREENHOUSE_API}/jobs?content=true": [
                json_response(GREENHOUSE_API, _body("greenhouse_jobs.json"))
            ],
            f"{GREENHOUSE_API}/jobs/{GREENHOUSE_JOB}?questions=true": [
                json_response(GREENHOUSE_API, _body("greenhouse_job.json"))
            ],
            f"{GREENHOUSE_API}/jobs/1?questions=true": [
                HttpResponse(status=404, url=GREENHOUSE_API)
            ],
        }
    )


def lever_page_url(skip: int) -> str:
    return f"{LEVER_API}?mode=json&skip={skip}&limit={LEVER_PAGE_SIZE}"


def lever() -> ReplayTransport:
    return ReplayTransport(
        {
            lever_page_url(0): [json_response(lever_page_url(0), _body("lever_page0.json"))],
            lever_page_url(2): [json_response(lever_page_url(2), _body("lever_page1.json"))],
            f"{LEVER_API}/{LEVER_POSTING}?mode=json": [
                json_response(LEVER_API, _body("lever_posting.json"))
            ],
        }
    )


def teamtailor_url(offset: int = 0) -> str:
    return f"https://{TEAMTAILOR_HOST}/jobs.rss?offset={offset}&per_page=100"


def teamtailor() -> ReplayTransport:
    return ReplayTransport({teamtailor_url(): [xml_response(teamtailor_url(), "teamtailor.rss")]})


def personio_url() -> str:
    return f"https://{PERSONIO_HOST}/xml"


def personio() -> ReplayTransport:
    return ReplayTransport({personio_url(): [xml_response(personio_url(), "personio.xml")]})
