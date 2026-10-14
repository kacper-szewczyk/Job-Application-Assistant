"""A fake job board connector and a replaying transport, for exercising the connector contract.

The board's responses are recorded fixtures (`tests/fixtures/connectors/`); nothing reaches the
network.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final, cast

from jaa.connectors.base import (
    AccessMethod,
    ApplicationQuestion,
    FetchResult,
    IssueSeverity,
    JobListing,
    ListingField,
    QuestionKind,
    RateLimit,
    RetryPolicy,
    SourceIssue,
    SourcePolicy,
)
from jaa.connectors.http import HttpRequest, HttpResponse, SourceClient, TransportError
from jaa.core.errors import SourceDataError
from jaa.core.models import JsonObject

FIXTURES: Final = Path(__file__).parent / "connectors"
HOST: Final = "boards.example.test"


def board_url(target: str) -> str:
    return f"https://{HOST}/v1/boards/{target}/jobs"


def make_policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "key": "fake_board",
        "name": "Fake Board",
        "access_method": AccessMethod.PUBLIC_API,
        "allowed_hosts": frozenset({HOST}),
        "terms_url": "https://boards.example.test/terms",
        "terms_reviewed_on": date(2026, 10, 14),
        "permitted_fields": frozenset(ListingField),
        "rate_limit": RateLimit(requests=2, per_seconds=1.0, max_requests_per_run=10),
        "retry": RetryPolicy(max_retries=2, backoff_seconds=1.0, max_wait_seconds=30.0),
    }
    values.update(overrides)
    return SourcePolicy(**values)  # pyright: ignore[reportArgumentType] - test helper


def _datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


@dataclass
class FakeBoard:
    """Reads `GET /v1/boards/<target>/jobs`: one JSON document listing every job."""

    policy: SourcePolicy = field(default_factory=make_policy)

    def target_key(self, target: str) -> str:
        return target

    def fetch(self, client: SourceClient, target: str) -> FetchResult:
        data = client.get_json(board_url(target))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceDataError(f"{self.policy.key}: response has no jobs list")
        listings: list[JobListing] = []
        issues: list[SourceIssue] = []
        for raw in cast(list[object], data["jobs"]):
            if not isinstance(raw, dict) or "id" not in raw:
                issues.append(
                    SourceIssue(
                        code="unreadable_listing",
                        severity=IssueSeverity.ERROR,
                        message="listing has no id",
                    )
                )
                continue
            job = cast(JsonObject, raw)
            questions = tuple(
                ApplicationQuestion(
                    key=str(q["name"]),
                    label=str(q["label"]),
                    kind=QuestionKind(q["type"]),
                    required=bool(q["required"]),
                )
                for q in cast(list[JsonObject], job.get("questions") or [])
            )
            listings.append(
                JobListing(
                    source_job_id=str(job["id"]),
                    url=str(job["url"]),
                    title=str(job["title"]),
                    company=str(job["company"]),
                    location=cast(str | None, job.get("location")),
                    description=cast(str | None, job.get("content")),
                    posted_at=_datetime(job.get("posted")),
                    updated_at=_datetime(job.get("updated")),
                    questions=questions,
                )
            )
        return FetchResult(listings=tuple(listings), issues=tuple(issues))


@dataclass
class ReplayTransport:
    """Answers each URL with its recorded responses in order; the last one repeats.

    A `TransportError` instance in the list is raised instead of answered.
    """

    responses: Mapping[str, list[HttpResponse | TransportError]]
    sent: list[HttpRequest] = field(default_factory=list[HttpRequest])

    def send(self, request: HttpRequest, *, timeout: float, max_bytes: int) -> HttpResponse:
        self.sent.append(request)
        queue = self.responses[request.url]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, TransportError):
            raise answer
        return answer


def json_response(url: str, body: bytes, status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        url=url,
        headers={"content-type": "application/json", **headers},
        body=body,
    )


def recorded_board(target: str = "example") -> ReplayTransport:
    url = board_url(target)
    body = (FIXTURES / "fake_board.json").read_bytes()
    return ReplayTransport({url: [json_response(url, body)]})


class FakeClock:
    """Monotonic time that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
