"""Replaying the recorded SOLID.Jobs responses in `tests/fixtures/connectors/solid_jobs/`."""

from pathlib import Path
from typing import Final

from jaa.connectors.http import HttpResponse, TransportError
from jaa.connectors.solid_jobs import SolidJobs, page_url
from tests.fixtures.fake_connector import ReplayTransport, json_response

FIXTURES: Final = Path(__file__).parent / "connectors" / "solid_jobs"
PAGE_SIZE: Final = 2

KEY_A: Final = "00000000-0000-4000-8000-00000000000a"
KEY_B: Final = "00000000-0000-4000-8000-00000000000b"
KEY_C: Final = "00000000-0000-4000-8000-00000000000c"
KEY_D: Final = "00000000-0000-4000-8000-00000000000d"


def connector() -> SolidJobs:
    return SolidJobs(page_size=PAGE_SIZE)


def url(page: int, division: str = "IT") -> str:
    return page_url(division, page, PAGE_SIZE)


def page(name: str, page_index: int = 0) -> HttpResponse:
    return json_response(url(page_index), (FIXTURES / name).read_bytes())


def first_fetch() -> ReplayTransport:
    """Two pages: offers A, B, an unreadable one, then C and A again."""
    return ReplayTransport({url(0): [page("it_page0.json")], url(1): [page("it_page1.json", 1)]})


def later_fetch() -> ReplayTransport:
    """One page: A changed, B gone, C unchanged, D new."""
    return ReplayTransport({url(0): [page("it_later.json")]})


def answering(*answers: HttpResponse | TransportError) -> ReplayTransport:
    """Page 0 answered with `answers` in order (the last one repeats)."""
    return ReplayTransport({url(0): list(answers)})
