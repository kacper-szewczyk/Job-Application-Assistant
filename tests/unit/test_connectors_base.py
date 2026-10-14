from datetime import UTC, datetime

import pytest

from jaa.connectors.base import (
    REQUIRED_FIELDS,
    ListingField,
    RateLimit,
    RetryPolicy,
    effective_state,
    policy_summary,
)
from jaa.core.models import SourceStatus, SourceStatusEvent, StatusActor
from tests.fixtures.fake_connector import make_policy

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _event(status: SourceStatus, reason: str | None = None) -> SourceStatusEvent:
    return SourceStatusEvent(
        source="fake_board", status=status, actor=StatusActor.USER, reason=reason, occurred_at=T0
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"key": "Fake Board"}, "connector key"),
        ({"allowed_hosts": frozenset[str]()}, "allowed_hosts"),
        ({"allowed_hosts": frozenset({"https://boards.example.test"})}, "invalid host"),
        ({"terms_url": "http://boards.example.test/terms"}, "terms_url"),
        ({"permitted_fields": frozenset({ListingField.TITLE})}, "required fields"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
    ],
)
def test_policy_validation(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_policy(**overrides)


def test_rate_limit_and_retry_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimit(requests=0, per_seconds=1)
    with pytest.raises(ValueError, match="negative"):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError, match="positive"):
        RetryPolicy(timeout_seconds=0)


def test_minimal_policy_permits_required_fields_only() -> None:
    policy = make_policy(permitted_fields=REQUIRED_FIELDS)
    assert policy.permitted_fields == REQUIRED_FIELDS


def test_active_by_default_and_user_pause_wins() -> None:
    policy = make_policy()
    assert effective_state(policy, None).status is SourceStatus.ACTIVE
    assert effective_state(policy, _event(SourceStatus.ACTIVE)).status is SourceStatus.ACTIVE
    paused = effective_state(policy, _event(SourceStatus.PAUSED, "terms changed"))
    assert (paused.status, paused.reason, paused.locked) == (
        SourceStatus.PAUSED,
        "terms changed",
        False,
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"paused_reason": "API deprecated"}, "API deprecated"),
        ({"terms_reviewed_on": None}, "not been reviewed"),
        ({"requires_credentials": True}, "credentials"),
    ],
)
def test_policy_pause_is_locked_and_overrides_user_resume(
    overrides: dict[str, object], reason: str
) -> None:
    state = effective_state(make_policy(**overrides), _event(SourceStatus.ACTIVE))
    assert state.status is SourceStatus.PAUSED
    assert state.locked
    assert reason in (state.reason or "")


def test_policy_summary_documents_every_requirement() -> None:
    summary = policy_summary(make_policy())
    assert summary["Access method"] == "public_api"
    assert summary["Allowed hosts"] == "boards.example.test"
    assert summary["Terms reviewed"] == "2026-10-14"
    assert summary["Rate limit"] == "2 per 1s, at most 10 per run"
    assert summary["Retries"].startswith("2 (backoff 1s")
    assert "questions" in summary["Permitted fields"]
