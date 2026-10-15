"""What a job link the user brings points at, without opening it (#71, ADR 0025).

`recognize` is pure. A link to a career board jaa has a connector for names that connector's
target, and the offer's id when it points at one offer. A link to a board jaa may not read says
why, so the user can paste the offer instead (ADR 0023). Anything else is unknown: jaa does not
open arbitrary pages.
"""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs, urlsplit

from jaa.connectors import greenhouse, lever, personio, teamtailor


@dataclass(frozen=True, slots=True, kw_only=True)
class OfferLink:
    """One offer on a board jaa can read through its API or feed."""

    source: str
    target: str
    job_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BoardLink:
    """A company's whole board (or a page of it) on a source jaa can read."""

    source: str
    target: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedLink:
    """A site whose terms or technical signals do not allow jaa to read it."""

    host: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownLink:
    host: str


type Link = OfferLink | BoardLink | BlockedLink | UnknownLink

_LINKEDIN: Final = (
    "LinkedIn does not allow automated access, and jaa never opens LinkedIn pages; "
    "LinkedIn data comes only from your own data archive"
)
_ROBOTS: Final = "its robots.txt disallows the API and feeds jaa would need"
_NO_API: Final = "it offers no public API or feed for job listings"
# Domain → why not; subdomains are covered too. Findings from #21
# (docs/research/2026-10-sources-and-linkedin-portability.md).
BLOCKED: Final = {
    "linkedin.com": _LINKEDIN,
    "lnkd.in": _LINKEDIN,
    "justjoin.it": _ROBOTS,
    "bulldogjob.pl": _ROBOTS,
    "bulldogjob.com": _ROBOTS,
    "rocketjobs.pl": _ROBOTS,
    "nofluffjobs.com": "it blocks automated clients and documents no API",
    "pracuj.pl": _NO_API,
    "theprotocol.it": _NO_API,
    "indeed.com": "its job search API is closed to new integrations",
    "glassdoor.com": _NO_API,
}

_GREENHOUSE_HOSTS: Final = {
    "boards.greenhouse.io": "",
    "job-boards.greenhouse.io": "",
    "boards.eu.greenhouse.io": "eu",
    "job-boards.eu.greenhouse.io": "eu",
}
_LEVER_HOSTS: Final = {"jobs.lever.co": "", "jobs.eu.lever.co": "eu"}
_DIGITS: Final = re.compile(r"[0-9]{1,20}")


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _blocked(host: str) -> str | None:
    for domain, reason in BLOCKED.items():
        if _within(host, domain):
            return reason
    # Indeed and Glassdoor run a site per country (pl.indeed.com, indeed.de, glassdoor.co.uk).
    labels = host.split(".")
    for name in ("indeed", "glassdoor"):
        if name in labels[:-1]:
            return BLOCKED[f"{name}.com"]
    return None


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _greenhouse(region: str, path: str, query: str) -> Link | None:
    parts = _segments(path)
    params = parse_qs(query)
    prefix = f"{region}:" if region else ""
    if parts[:1] == ["embed"]:
        # Embedded boards: /embed/job_board?for=acme and /embed/job_app?for=acme&token=123.
        token = (params.get("for") or [""])[0].lower()
        if not token:
            return None
        job_id = (params.get("token") or [""])[0]
        if parts[1:2] == ["job_app"] and _DIGITS.fullmatch(job_id):
            return OfferLink(source=greenhouse.POLICY.key, target=prefix + token, job_id=job_id)
        return BoardLink(source=greenhouse.POLICY.key, target=prefix + token)
    if not parts:
        return None
    target = prefix + parts[0].lower()
    if len(parts) >= 3 and parts[1] == "jobs" and _DIGITS.fullmatch(parts[2]):
        return OfferLink(source=greenhouse.POLICY.key, target=target, job_id=parts[2])
    return BoardLink(source=greenhouse.POLICY.key, target=target)


def _lever(region: str, path: str) -> Link | None:
    parts = _segments(path)
    if not parts:
        return None
    target = (f"{region}:" if region else "") + parts[0].lower()
    if len(parts) >= 2 and lever.posting_id(parts[1]) is not None:
        return OfferLink(source=lever.POLICY.key, target=target, job_id=parts[1].lower())
    return BoardLink(source=lever.POLICY.key, target=target)


def _teamtailor(host: str, url: str) -> Link:
    job_id = teamtailor.job_id_from_url(url)
    if job_id is not None:
        return OfferLink(source=teamtailor.POLICY.key, target=host, job_id=job_id)
    return BoardLink(source=teamtailor.POLICY.key, target=host)


def _personio(host: str, path: str) -> Link:
    parts = _segments(path)
    if len(parts) >= 2 and parts[0] == "job" and _DIGITS.fullmatch(parts[1]):
        return OfferLink(source=personio.POLICY.key, target=host, job_id=parts[1])
    return BoardLink(source=personio.POLICY.key, target=host)


def recognize(url: str) -> Link:
    """What `url` points at. Never makes a request."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return UnknownLink(host="")
    if parts.scheme.lower() not in ("http", "https") or not host:
        return UnknownLink(host=host)
    if (reason := _blocked(host)) is not None:
        return BlockedLink(host=host, reason=reason)
    found: Link | None = None
    if host in _GREENHOUSE_HOSTS:
        found = _greenhouse(_GREENHOUSE_HOSTS[host], parts.path, parts.query)
    elif host in _LEVER_HOSTS:
        found = _lever(_LEVER_HOSTS[host], parts.path)
    elif teamtailor.POLICY.is_company_host(host):
        found = _teamtailor(host, url)
    elif personio.POLICY.is_company_host(host):
        found = _personio(host, parts.path)
    return found or UnknownLink(host=host)


def board_link(source: str, target: str) -> str | None:
    """The public page of a board, for showing the user what they follow."""
    if source == greenhouse.POLICY.key:
        region, _, token = target.rpartition(":")
        return f"https://job-boards.{'eu.' if region else ''}greenhouse.io/{token}"
    if source == lever.POLICY.key:
        region, _, name = target.rpartition(":")
        return f"https://jobs.{'eu.' if region else ''}lever.co/{name}"
    if source in (teamtailor.POLICY.key, personio.POLICY.key):
        return f"https://{target}"
    return None
