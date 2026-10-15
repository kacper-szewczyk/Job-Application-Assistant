"""Small readers for the values job sources send: JSON strings and dates, XML feed elements.

They return None for anything missing or malformed, so connectors decide per field whether a gap
skips the listing or just leaves the field empty.
"""

import html
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast
from xml.etree import ElementTree as ET

from jaa.connectors.html_text import html_to_text
from jaa.core.models import JsonValue


def text(value: JsonValue) -> str | None:
    """A non-blank string, stripped, or None."""
    return value.strip() or None if isinstance(value, str) else None


def iso_datetime(value: JsonValue) -> datetime | None:
    """An ISO 8601 timestamp with an offset (`Z` included), or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def epoch_millis(value: JsonValue) -> datetime | None:
    """Milliseconds since the Unix epoch, as an aware UTC datetime, or None."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def rfc822_datetime(value: str | None) -> datetime | None:
    """An RSS `pubDate` such as `Mon, 13 Oct 2026 08:00:00 +0200`, or None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def escaped_html_text(value: JsonValue) -> str | None:
    """Text from HTML that the source sent entity-encoded (`&lt;p&gt;…`), or None."""
    if not isinstance(value, str):
        return None
    return html_to_text(html.unescape(value)) or None


def strings(value: JsonValue) -> list[str]:
    """The non-blank strings in a JSON list, in order."""
    if not isinstance(value, list):
        return []
    return [s for s in (text(v) for v in cast(list[JsonValue], value)) if s]


def local_name(element: ET.Element) -> str:
    """The tag without its namespace: `{https://…}location` is `location`."""
    return element.tag.rpartition("}")[2]


def children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    """Direct children with this local name, whatever their namespace."""
    return (child for child in element if local_name(child) == name)


def child_text(element: ET.Element, name: str) -> str | None:
    """The stripped text of the first direct child with this local name, or None."""
    for child in children(element, name):
        return "".join(child.itertext()).strip() or None
    return None
