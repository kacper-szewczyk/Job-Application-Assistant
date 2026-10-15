"""Remotive, Arbeitnow and We Work Remotely against recorded responses (#74, ADR 0028)."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jaa.connectors import arbeitnow, registry, remotive, weworkremotely
from jaa.connectors.base import (
    Connector,
    RateLimit,
    SourcePolicy,
    SourceState,
    effective_state,
    policy_summary,
)
from jaa.connectors.http import HttpResponse
from jaa.connectors.run import FetchOutcome, run_connector
from jaa.core.errors import ConnectorTargetError, SourcePausedError, SourceQuotaError
from jaa.core.job_details import WorkArrangement
from jaa.core.models import FetchRunOutcome, SourceFetchRun, SourceStatus
from jaa.storage import listings
from jaa.storage.repository import job_duplicates, jobs, source_fetch_runs
from tests.fixtures import feeds
from tests.fixtures.fake_connector import FakeClock, ReplayTransport

ACTIVE = SourceState(status=SourceStatus.ACTIVE)


def _run(connector: Connector, transport: ReplayTransport, target: str) -> FetchOutcome:
    clock = FakeClock()
    return run_connector(
        connector, target, state=ACTIVE, transport=transport, clock=clock, sleep=clock.sleep
    )


def _store(
    conn: sqlite3.Connection, connector: Connector, transport: ReplayTransport, target: str
) -> listings.FetchReport:
    clock = FakeClock()
    return listings.fetch_and_store(
        conn, connector, target, transport=transport, clock=clock, sleep=clock.sleep
    )


@pytest.mark.parametrize("policy", [remotive.POLICY, arbeitnow.POLICY], ids=lambda p: p.key)
def test_reviewed_sources_are_registered_and_active(policy: SourcePolicy) -> None:
    connector = registry.available_connectors()[policy.key]
    assert connector.policy is policy
    assert connector.policy.terms_reviewed_on is not None
    assert effective_state(connector.policy, None).status is SourceStatus.ACTIVE


def test_we_work_remotely_ships_locked_paused() -> None:
    connector = registry.available_connectors()["weworkremotely"]
    state = effective_state(connector.policy, None)
    assert state.status is SourceStatus.PAUSED and state.locked
    assert state.reason is not None and "terms not confirmed" in state.reason
    with pytest.raises(SourcePausedError):
        run_connector(
            connector, "all", state=state, transport=ReplayTransport({}), sleep=lambda _: None
        )


def test_remotive_policy_encodes_its_terms() -> None:
    limit = remotive.POLICY.rate_limit
    assert (limit.requests, limit.per_seconds) == (1, 60.0)
    assert (limit.max_requests_per_run, limit.max_requests_per_day) == (1, 4)
    assert remotive.POLICY.retry.max_retries == 0
    assert "4 per day" in policy_summary(remotive.POLICY)["Rate limit"]


def test_daily_limit_must_allow_one_run() -> None:
    with pytest.raises(ValueError, match="at least one full run"):
        RateLimit(requests=1, per_seconds=1.0, max_requests_per_run=5, max_requests_per_day=4)


@pytest.mark.parametrize(
    ("connector", "target", "expected"),
    [
        (remotive.Remotive(), " Software-Dev ", "software-dev"),
        (remotive.Remotive(), "ALL", "all"),
        (arbeitnow.Arbeitnow(), "Remote", "remote"),
        (weworkremotely.WeWorkRemotely(), "Back-End-Programming", "back-end-programming"),
    ],
)
def test_target_keys(connector: Connector, target: str, expected: str) -> None:
    assert connector.target_key(target) == expected


@pytest.mark.parametrize(
    ("connector", "target"),
    [
        (remotive.Remotive(), "software dev"),
        (remotive.Remotive(), "../api"),
        (arbeitnow.Arbeitnow(), "berlin"),
        (weworkremotely.WeWorkRemotely(), "remote-jobs"),
    ],
)
def test_unknown_targets_are_refused(connector: Connector, target: str) -> None:
    with pytest.raises(ConnectorTargetError):
        connector.target_key(target)


def test_remotive_jobs_are_remote_and_link_back() -> None:
    outcome = _run(remotive.Remotive(), feeds.remotive_feed(), "software-dev")
    first, second = outcome.listings
    assert first.source_job_id == "2000001"
    assert first.url == "https://remotive.com/remote-jobs/software-dev/backend-engineer-2000001"
    assert (first.company, first.title) == ("Example Widgets", "Backend Engineer (Python)")
    assert first.location == "Europe (remote)"
    assert first.posted_at == datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
    assert first.description is not None and "3+ years of Python" in first.description
    assert first.details.work_arrangement is WorkArrangement.REMOTE
    assert second.location == "Remote"
    assert [(i.code, i.source_job_id) for i in outcome.issues] == [
        ("unreadable_listing", "2000003")
    ]
    assert outcome.requests_made == 1 and outcome.complete


def test_remotive_all_asks_for_every_category() -> None:
    transport = feeds.remotive_feed()
    _run(remotive.Remotive(), transport, "all")
    assert [r.url for r in transport.sent] == [feeds.REMOTIVE_ALL]


def test_arbeitnow_reads_pages_until_the_last() -> None:
    transport = feeds.arbeitnow_feed()
    outcome = _run(arbeitnow.Arbeitnow(), transport, "all")
    assert [r.url for r in transport.sent] == [
        f"{feeds.ARBEITNOW_PAGE}1",
        f"{feeds.ARBEITNOW_PAGE}2",
    ]
    assert outcome.complete
    by_id = {listing.source_job_id: listing for listing in outcome.listings}
    assert list(by_id) == [
        "backend-engineer-berlin-100001",
        "werkstudent-marketing-munich-100002",
        "platform-engineer-hamburg-100003",
    ]
    backend = by_id["backend-engineer-berlin-100001"]
    assert backend.location == "Europe (remote)"
    assert backend.description is not None and "Build APIs for widgets." in backend.description
    assert backend.posted_at == datetime.fromtimestamp(1792065600, tz=UTC)
    student = by_id["werkstudent-marketing-munich-100002"]
    assert student.location == "Munich" and student.details.work_arrangement is None
    assert student.description == "Unterstützung im Marketing."
    assert by_id["platform-engineer-hamburg-100003"].location == "Remote"
    assert [i.code for i in outcome.issues] == ["unreadable_listing"]


def test_arbeitnow_remote_keeps_remote_jobs_only() -> None:
    outcome = _run(arbeitnow.Arbeitnow(), feeds.arbeitnow_feed(), "remote")
    assert [listing.source_job_id for listing in outcome.listings] == [
        "backend-engineer-berlin-100001",
        "platform-engineer-hamburg-100003",
    ]
    assert outcome.issues == ()


def test_arbeitnow_stops_at_its_page_cap_and_says_the_fetch_is_partial() -> None:
    transport = feeds.arbeitnow_feed()
    outcome = _run(arbeitnow.Arbeitnow(max_pages=1), transport, "all")
    assert len(transport.sent) == 1
    assert not outcome.complete


def test_we_work_remotely_items() -> None:
    connector = weworkremotely.WeWorkRemotely()
    outcome = _run(connector, feeds.weworkremotely_feed(), "back-end-programming")
    go, rails = outcome.listings
    assert (go.source_job_id, go.company, go.title) == (
        "example-widgets-senior-go-engineer",
        "Example Widgets",
        "Senior Go Engineer",
    )
    assert go.location == "Anywhere in the World (remote)"
    assert go.description is not None and "Write Go services." in go.description
    assert go.posted_at == datetime(2026, 10, 15, 7, 30, tzinfo=UTC)
    assert rails.location == "Europe Only, Poland (remote)"
    assert [i.source_job_id for i in outcome.issues] == ["untitled-company-job"]
    assert not outcome.complete


def test_partial_fetches_mark_nothing_removed(conn: sqlite3.Connection) -> None:
    first = _store(conn, arbeitnow.Arbeitnow(), feeds.arbeitnow_feed(), "all")
    assert len(first.changes.new) == 3
    second = _store(conn, arbeitnow.Arbeitnow(max_pages=1), feeds.arbeitnow_feed(), "all")
    assert second.changes.removed == ()
    assert all(job.removed_at is None for job in jobs.list(conn, source="arbeitnow"))


def test_the_same_job_on_two_feeds_is_flagged(conn: sqlite3.Connection) -> None:
    _store(conn, remotive.Remotive(), feeds.remotive_feed(), "software-dev")
    report = _store(conn, arbeitnow.Arbeitnow(), feeds.arbeitnow_feed(), "remote")
    assert len(report.changes.flagged) == 1
    (flag,) = job_duplicates.list(conn)
    pair = {jobs.get(conn, flag.job_id).source, jobs.get(conn, flag.other_job_id).source}
    assert pair == {"remotive", "arbeitnow"}


def _past_run(conn: sqlite3.Connection, hours_ago: float, requests: int) -> None:
    at = datetime.now(UTC) - timedelta(hours=hours_ago)
    with conn:
        source_fetch_runs.insert(
            conn,
            SourceFetchRun(
                source="remotive",
                target="all",
                started_at=at,
                finished_at=at,
                outcome=FetchRunOutcome.OK if requests else FetchRunOutcome.UNAVAILABLE,
                requests=requests,
            ),
        )


def test_remotive_daily_allowance_is_kept_across_runs(conn: sqlite3.Connection) -> None:
    _past_run(conn, 30, 1)  # older than a day: not counted
    for hours in (20, 10):
        _past_run(conn, hours, 1)
    _past_run(conn, 5, 0)  # a failed run still reached the source
    report = _store(conn, remotive.Remotive(), feeds.remotive_feed(), "all")
    assert report.error is None
    transport = feeds.remotive_feed()
    with pytest.raises(SourceQuotaError, match="allows 4 requests a day") as caught:
        _store(conn, remotive.Remotive(), transport, "software-dev")
    expected = (datetime.now(UTC) - timedelta(hours=20) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert expected in str(caught.value)
    assert transport.sent == []
    assert len(source_fetch_runs.list(conn, source="remotive")) == 5


def test_sources_without_a_daily_limit_are_not_counted(conn: sqlite3.Connection) -> None:
    for _ in range(3):
        assert _store(conn, arbeitnow.Arbeitnow(), feeds.arbeitnow_feed(), "all").error is None


def test_an_html_answer_pauses_remotive(conn: sqlite3.Connection) -> None:
    url = feeds.REMOTIVE_ALL
    page = HttpResponse(status=200, url=url, headers={"content-type": "text/html"}, body=b"<html>")
    report = _store(conn, remotive.Remotive(), ReplayTransport({url: [page]}), "all")
    assert report.paused and report.run.outcome is FetchRunOutcome.DENIED


def test_job_text_stays_out_of_the_logs(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jaa")
    _store(conn, remotive.Remotive(), feeds.remotive_feed(), "all")
    _store(conn, arbeitnow.Arbeitnow(), feeds.arbeitnow_feed(), "all")
    for secret in ("Build APIs for widgets", "Example Widgets", "Werkstudent"):
        assert secret not in caplog.text
