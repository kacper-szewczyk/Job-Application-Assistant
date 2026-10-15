"""Recognising job links without opening them (#71, ADR 0025)."""

import pytest

from jaa.connectors.links import (
    BlockedLink,
    BoardLink,
    Link,
    OfferLink,
    UnknownLink,
    board_link,
    recognize,
)

LEVER_ID = "0b1c2d3e-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/4012345?gh_src=x",
            OfferLink(source="greenhouse", target="acme", job_id="4012345"),
        ),
        (
            "https://boards.greenhouse.io/Acme/jobs/4012345",
            OfferLink(source="greenhouse", target="acme", job_id="4012345"),
        ),
        (
            "https://job-boards.eu.greenhouse.io/acme/jobs/4012345",
            OfferLink(source="greenhouse", target="eu:acme", job_id="4012345"),
        ),
        (
            "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
            OfferLink(source="greenhouse", target="acme", job_id="4012345"),
        ),
        ("https://boards.greenhouse.io/acme", BoardLink(source="greenhouse", target="acme")),
        (
            "https://boards.eu.greenhouse.io/embed/job_board?for=acme",
            BoardLink(source="greenhouse", target="eu:acme"),
        ),
        (
            f"https://jobs.lever.co/acme/{LEVER_ID}/apply",
            OfferLink(source="lever", target="acme", job_id=LEVER_ID),
        ),
        (
            f"https://jobs.eu.lever.co/acme/{LEVER_ID.upper()}",
            OfferLink(source="lever", target="eu:acme", job_id=LEVER_ID),
        ),
        ("https://jobs.lever.co/acme?team=Eng", BoardLink(source="lever", target="acme")),
        (
            "https://acme.teamtailor.com/jobs/6100001-data-analyst",
            OfferLink(source="teamtailor", target="acme.teamtailor.com", job_id="6100001"),
        ),
        (
            "https://acme.teamtailor.com/pl/jobs/6100001-analityk/",
            OfferLink(source="teamtailor", target="acme.teamtailor.com", job_id="6100001"),
        ),
        (
            "https://acme.teamtailor.com/jobs",
            BoardLink(source="teamtailor", target="acme.teamtailor.com"),
        ),
        (
            "https://acme.jobs.personio.de/job/1700001?display=en",
            OfferLink(source="personio", target="acme.jobs.personio.de", job_id="1700001"),
        ),
        (
            "https://acme.jobs.personio.com/",
            BoardLink(source="personio", target="acme.jobs.personio.com"),
        ),
    ],
)
def test_boards_jaa_can_read(url: str, expected: Link) -> None:
    assert recognize(url) == expected


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://www.linkedin.com/jobs/view/123", "www.linkedin.com"),
        ("https://lnkd.in/abc", "lnkd.in"),
        ("https://justjoin.it/job-offer/acme-python", "justjoin.it"),
        ("https://nofluffjobs.com/pl/job/x", "nofluffjobs.com"),
        ("https://bulldogjob.pl/companies/jobs/1", "bulldogjob.pl"),
        ("https://www.pracuj.pl/praca/x,oferta,1", "www.pracuj.pl"),
        ("https://pl.indeed.com/viewjob?jk=1", "pl.indeed.com"),
        ("https://www.glassdoor.co.uk/job-listing/x", "www.glassdoor.co.uk"),
    ],
)
def test_blocked_sites_say_why(url: str, host: str) -> None:
    found = recognize(url)
    assert isinstance(found, BlockedLink)
    assert found.host == host
    assert found.reason


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.acme.test/jobs/1",
        "https://greenhouse.io/acme/jobs/1",
        "https://a.b.teamtailor.com/jobs/1-x",
        "https://notlinkedin.com/jobs/1",
        "ftp://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/",
        "not a url",
        "https://[broken",
    ],
)
def test_everything_else_is_unknown(url: str) -> None:
    assert isinstance(recognize(url), UnknownLink)


def test_board_links_for_display() -> None:
    assert board_link("greenhouse", "eu:acme") == "https://job-boards.eu.greenhouse.io/acme"
    assert board_link("lever", "acme") == "https://jobs.lever.co/acme"
    assert board_link("teamtailor", "acme.teamtailor.com") == "https://acme.teamtailor.com"
    assert board_link("solid_jobs", "IT") is None
