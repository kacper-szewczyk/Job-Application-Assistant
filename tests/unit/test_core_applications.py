import pytest

from jaa.core import models as m
from jaa.core.applications import (
    ConflictReason,
    check_transition,
    conflict_reasons,
    find_conflicts,
)
from jaa.core.errors import ApplicationError
from tests.conftest import T0

S = m.JobStatus


def _job(id_: int, url: str, employer: str = "Example Co", title: str = "Widget Engineer") -> m.Job:
    return m.Job(
        id=id_,
        source="example_board",
        source_job_id=str(id_),
        canonical_url=url,
        employer=employer,
        title=title,
        first_seen_at=T0,
        last_seen_at=T0,
        status_changed_at=T0,
    )


@pytest.mark.parametrize("submitted", [False, True])
def test_submitted_is_never_a_plain_status_change(submitted: bool) -> None:
    with pytest.raises(ApplicationError, match="confirm"):
        check_transition(S.READY_FOR_REVIEW, S.SUBMITTED, submitted=submitted)


@pytest.mark.parametrize(
    ("current", "target", "submitted", "allowed"),
    [
        (S.DISCOVERED, S.SAVED, False, True),
        (S.SAVED, S.READY_FOR_REVIEW, False, True),
        (S.PREPARING, S.WITHDRAWN, False, True),
        (S.PREPARING, S.INTERVIEWING, False, False),
        (S.PREPARING, S.REJECTED, False, False),
        (S.SUBMITTED, S.INTERVIEWING, True, True),
        (S.INTERVIEWING, S.REJECTED, True, True),
        (S.SUBMITTED, S.CLOSED, True, True),
        (S.SUBMITTED, S.PREPARING, True, False),
        (S.REJECTED, S.SAVED, True, False),
        (S.SUBMITTED, S.SUBMITTED, True, True),
    ],
)
def test_transitions(
    current: m.JobStatus, target: m.JobStatus, submitted: bool, allowed: bool
) -> None:
    if allowed:
        check_transition(current, target, submitted=submitted)
    else:
        with pytest.raises(ApplicationError):
            check_transition(current, target, submitted=submitted)


def test_conflicts_by_link_and_by_employer_and_role() -> None:
    job = _job(1, "https://boards.example.test/jobs/1")
    same_link = _job(2, "https://boards.example.test/jobs/1", employer="Other", title="Other")
    same_role = _job(
        3,
        "https://other.example.test/9",
        employer="Example Co sp. z o.o.",
        title="Widget Engineer (m/f/d)",
    )
    unrelated = _job(4, "https://other.example.test/10", title="Gadget Engineer")
    assert conflict_reasons(job, same_link) == (ConflictReason.CANONICAL_URL,)
    assert conflict_reasons(job, same_role) == (ConflictReason.COMPANY_ROLE,)
    assert conflict_reasons(job, unrelated) == ()

    found = find_conflicts(
        job,
        [(job, False), (same_link, True), (same_role, False), (unrelated, False)],
        judged_different=frozenset({3}),
    )
    assert [(c.job.id, c.submitted) for c in found] == [(2, True)]
