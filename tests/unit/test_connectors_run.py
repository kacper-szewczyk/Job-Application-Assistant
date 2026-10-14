from datetime import UTC, datetime

import pytest

from jaa.connectors.base import (
    REQUIRED_FIELDS,
    JobListing,
    ListingField,
    SourceState,
)
from jaa.connectors.run import FetchOutcome, run_connector, validate_listings
from jaa.core.errors import SourceAccessDeniedError, SourceDataError, SourcePausedError
from jaa.core.models import SourceStatus
from tests.fixtures.fake_connector import (
    FakeBoard,
    FakeClock,
    ReplayTransport,
    board_url,
    json_response,
    make_policy,
    recorded_board,
)

ACTIVE = SourceState(status=SourceStatus.ACTIVE)


def _run(board: FakeBoard, transport: ReplayTransport, state: SourceState = ACTIVE) -> FetchOutcome:
    clock = FakeClock()
    return run_connector(
        board, "example", state=state, transport=transport, clock=clock, sleep=clock.sleep
    )


def test_fake_connector_produces_normalized_listings() -> None:
    outcome = _run(FakeBoard(), recorded_board())
    assert outcome.source == "fake_board"
    assert outcome.requests_made == 1
    assert [x.source_job_id for x in outcome.listings] == ["1001", "1002"]

    widget = outcome.listings[0]
    assert widget.title == "Widget Engineer"
    assert widget.location == "Remote, EU"
    assert widget.posted_at == datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    assert [q.key for q in widget.questions] == ["cover_letter", "work_auth"]
    assert widget.questions[1].required

    codes = sorted(i.code for i in outcome.issues)
    assert codes == ["duplicate_listing", "invalid_listing", "unreadable_listing"]
    invalid = next(i for i in outcome.issues if i.code == "invalid_listing")
    assert invalid.source_job_id == "1003"


def test_fields_outside_policy_are_dropped_and_reported_once() -> None:
    board = FakeBoard(policy=make_policy(permitted_fields=REQUIRED_FIELDS))
    outcome = _run(board, recorded_board())
    widget = outcome.listings[0]
    assert (widget.location, widget.description, widget.posted_at, widget.questions) == (
        None,
        None,
        None,
        (),
    )
    dropped = [i.message for i in outcome.issues if i.code == "field_not_permitted"]
    assert dropped == [
        "dropped description from 1 listing(s); the source policy does not permit keeping it",
        "dropped location from 1 listing(s); the source policy does not permit keeping it",
        "dropped posted_at from 1 listing(s); the source policy does not permit keeping it",
        "dropped questions from 1 listing(s); the source policy does not permit keeping it",
        "dropped updated_at from 1 listing(s); the source policy does not permit keeping it",
    ]
    assert ListingField.LOCATION not in board.policy.permitted_fields


def test_paused_source_sends_nothing() -> None:
    transport = recorded_board()
    paused = SourceState(status=SourceStatus.PAUSED, reason="terms changed")
    with pytest.raises(SourcePausedError, match="terms changed"):
        _run(FakeBoard(), transport, paused)
    assert transport.sent == []


def test_client_errors_propagate() -> None:
    url = board_url("example")
    denied = ReplayTransport({url: [json_response(url, b"{}", status=403)]})
    with pytest.raises(SourceAccessDeniedError):
        _run(FakeBoard(), denied)
    garbage = ReplayTransport({url: [json_response(url, b'{"items": []}')]})
    with pytest.raises(SourceDataError, match="no jobs list"):
        _run(FakeBoard(), garbage)


@pytest.mark.parametrize(
    ("listing", "problem"),
    [
        (JobListing(source_job_id=" ", url="https://x.test/1", title="T", company="C"), "empty"),
        (JobListing(source_job_id="1", url="ftp://x.test/1", title="T", company="C"), "http"),
        (
            JobListing(source_job_id="1", url="https://u:p@x.test/1", title="T", company="C"),
            "credentials",
        ),
        (
            JobListing(
                source_job_id="1",
                url="https://x.test/1",
                title="T",
                company="C",
                posted_at=datetime(2026, 10, 1),  # noqa: DTZ001 - the naive date under test
            ),
            "timezone",
        ),
        (JobListing(source_job_id="1" * 201, url="https://x.test", title="T", company="C"), "long"),
    ],
)
def test_invalid_listings_are_reported(listing: JobListing, problem: str) -> None:
    kept, issues = validate_listings(FakeBoard(), (listing,))
    assert kept == ()
    assert problem in issues[0].message
