"""A replayed LinkedIn Member Snapshot API for tests (#72). No request ever leaves the process.

Pages come from `connectors/linkedin_api/snapshot.json` (synthetic values only). The fake
answers like LinkedIn: `start=N` serves page N, and past the last page, or for a domain with
no data, a 404 whose message is "No data found for this memberId".
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast
from urllib.parse import parse_qs, urlsplit

from jaa.connectors.http import HttpRequest, HttpResponse
from jaa.core.models import JsonValue

FIXTURE: Final = Path(__file__).parent / "connectors" / "linkedin_api" / "snapshot.json"
TOKEN: Final = "AQV-synthetic.token_value~0123456789abcdefSYNTH"  # noqa: S105 - synthetic
NO_DATA: Final = b'{"status":404,"code":"NOT_FOUND","message":"No data found for this memberId"}'

type Pages = Mapping[str, list[list[dict[str, JsonValue]]]]


def recorded_pages() -> Pages:
    data = cast(dict[str, JsonValue], json.loads(FIXTURE.read_text(encoding="utf-8")))
    data.pop("_comment", None)
    return cast(Pages, data)


def page_body(domain: str, start: int, rows: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps(
        {
            "paging": {"start": start, "count": 10, "links": [], "total": 1},
            "elements": [{"snapshotData": rows, "snapshotDomain": domain}],
        }
    ).encode()


@dataclass
class FakeSnapshotApi:
    pages: Pages = field(default_factory=recorded_pages)
    # (domain, start) → a response to send instead, e.g. a 401.
    overrides: dict[tuple[str, int], HttpResponse] = field(
        default_factory=dict[tuple[str, int], HttpResponse]
    )
    sent: list[HttpRequest] = field(default_factory=list[HttpRequest])

    def send(self, request: HttpRequest, *, timeout: float, max_bytes: int) -> HttpResponse:
        self.sent.append(request)
        query = parse_qs(urlsplit(request.url).query)
        domain = query["domain"][0]
        start = int(query["start"][0])
        if (domain, start) in self.overrides:
            return self.overrides[(domain, start)]
        pages = self.pages.get(domain, [])
        if start >= len(pages):
            return HttpResponse(status=404, url=request.url, body=NO_DATA)
        return HttpResponse(
            status=200,
            url=request.url,
            headers={"content-type": "application/json"},
            body=page_body(domain, start, pages[start]),
        )
