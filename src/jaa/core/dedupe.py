"""Canonical job URLs and the conservative duplicate key (T11, ADR 0014).

Pure functions. A canonical URL identifies a job page regardless of tracking parameters,
letter case in the host, default ports, fragments or a trailing slash. The duplicate key is the
fallback for listings without a shared URL: employer, title and location, folded so that case,
diacritics, punctuation, legal forms and gender tags do not matter. Both only *flag* possible
duplicates; nothing is merged.
"""

import re
import unicodedata
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that track the visit rather than identify the page.
TRACKING_PARAMS: Final = frozenset({"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "trk"})
TRACKING_PREFIXES: Final = ("utm_",)
DEFAULT_PORTS: Final = {"http": 80, "https": 443}

# Letters NFKD does not decompose into a base letter plus a mark.
_LETTERS: Final = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"})
_NON_WORD: Final = re.compile(r"[^0-9a-z]+")
# Legal forms at the end of a company name, as folded words.
LEGAL_FORMS: Final = (
    "sp z o o sp k",
    "sp z o o",
    "spolka z ograniczona odpowiedzialnoscia",
    "spolka akcyjna",
    "sp k",
    "sp j",
    "s a",
    "sa",
    "gmbh",
    "inc",
    "ltd",
    "llc",
    "plc",
    "ag",
    "bv",
    "s r o",
)
# Gender tags in job titles: "(m/f/d)", "(K/M)", "(f/m/x)".
_GENDER_TAG: Final = re.compile(r"\(\s*[kmfdxw](\s*/\s*[kmfdxw]){1,2}\s*\)", re.IGNORECASE)


def canonical_url(url: str) -> str:
    """The URL with scheme and host lowercased, default port, fragment, tracking parameters
    and trailing slash removed, and the remaining query parameters sorted.

    A URL that does not parse as http(s) with a host is returned stripped but otherwise as is.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").removeprefix("www.")
    if scheme not in DEFAULT_PORTS or not host:
        return url
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def fold(text: str) -> str:
    """Lowercase ASCII words separated by single spaces: no diacritics or punctuation."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().translate(_LETTERS))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_WORD.sub(" ", stripped.translate(_LETTERS)).strip()


def company_key(company: str) -> str:
    folded = fold(company)
    for form in LEGAL_FORMS:
        if folded.endswith(" " + form):
            return folded.removesuffix(" " + form)
    return folded


def title_key(title: str) -> str:
    return fold(_GENDER_TAG.sub(" ", title))


def dedupe_key(company: str, title: str, location: str | None) -> str | None:
    """The fallback duplicate key, or None when a part is missing.

    Conservative: all three must be present and equal after folding. Listings at the same
    employer with the same title in different places are not flagged.
    """
    parts = (company_key(company), title_key(title), fold(location or ""))
    if not all(parts):
        return None
    return " | ".join(parts)
