"""Replaying the recorded remote job feed responses in `tests/fixtures/connectors/feeds/`."""

from pathlib import Path
from typing import Final

from jaa.connectors import arbeitnow, remotive, weworkremotely
from jaa.connectors.http import HttpResponse
from tests.fixtures.fake_connector import ReplayTransport, json_response

FIXTURES: Final = Path(__file__).parent / "connectors" / "feeds"

REMOTIVE_ALL: Final = remotive.API
REMOTIVE_DEV: Final = f"{remotive.API}?category=software-dev"
ARBEITNOW_PAGE: Final = f"{arbeitnow.API}?page="
WWR_BACK_END: Final = f"{weworkremotely.BASE}{weworkremotely.FEEDS['back-end-programming']}"


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def remotive_feed() -> ReplayTransport:
    return ReplayTransport(
        {url: [json_response(url, _body("remotive.json"))] for url in (REMOTIVE_ALL, REMOTIVE_DEV)}
    )


def arbeitnow_feed() -> ReplayTransport:
    return ReplayTransport(
        {
            f"{ARBEITNOW_PAGE}{n}": [
                json_response(f"{ARBEITNOW_PAGE}{n}", _body(f"arbeitnow_page{n}.json"))
            ]
            for n in (1, 2)
        }
    )


def weworkremotely_feed() -> ReplayTransport:
    return ReplayTransport(
        {
            WWR_BACK_END: [
                HttpResponse(
                    status=200,
                    url=WWR_BACK_END,
                    headers={"content-type": "application/rss+xml; charset=utf-8"},
                    body=_body("weworkremotely.rss"),
                )
            ]
        }
    )
