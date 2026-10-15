"""Application tracker rules (T15, ADR 0018). Pure: no storage, no clock.

`jobs.status` is the one lifecycle (ADR 0005). These rules say which changes the user may make
and when two jobs look like the same application.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jaa.core.dedupe import company_key, title_key
from jaa.core.errors import ApplicationError
from jaa.core.models import Job, JobStatus

# Statuses that describe a job before anything was sent to the employer.
BEFORE_SUBMISSION: Final = frozenset(
    {JobStatus.DISCOVERED, JobStatus.SAVED, JobStatus.PREPARING, JobStatus.READY_FOR_REVIEW}
)
# Statuses that only make sense once an application went out.
AFTER_SUBMISSION: Final = frozenset({JobStatus.INTERVIEWING, JobStatus.REJECTED})
# Statuses that mean work on an application has started; they need an application record.
STARTED: Final = frozenset({JobStatus.PREPARING, JobStatus.READY_FOR_REVIEW})
# Finished jobs. They stay visible, marked archived; nothing is deleted automatically (ADR 0021).
ARCHIVED: Final = frozenset({JobStatus.REJECTED, JobStatus.WITHDRAWN, JobStatus.CLOSED})


def is_archived(status: JobStatus) -> bool:
    """Whether a job in this status is archived: kept and shown, but finished."""
    return status in ARCHIVED


def check_transition(current: JobStatus, target: JobStatus, *, submitted: bool) -> None:
    """Raise `ApplicationError` unless the user may move a job from `current` to `target`.

    `submitted` says whether a submission is recorded. `submitted` itself is never a plain
    status change: it is recorded with its confirmation (`record_submission`).
    """
    if target is current:
        return
    if target is JobStatus.SUBMITTED:
        raise ApplicationError(
            "an application is marked submitted only when you confirm you submitted it"
        )
    if submitted and target in BEFORE_SUBMISSION:
        raise ApplicationError(
            f"the application was already submitted; it cannot go back to {target.value}"
        )
    if not submitted and target in AFTER_SUBMISSION:
        raise ApplicationError(f"{target.value} needs a recorded submission first")


class ConflictReason(StrEnum):
    # The same normalized link (ADR 0014).
    CANONICAL_URL = "canonical_url"
    # The same employer and role after folding, wherever it is located.
    COMPANY_ROLE = "company_role"


def conflict_reasons(job: Job, other: Job) -> tuple[ConflictReason, ...]:
    """Why `other` may be the same application as `job`. Empty when it does not look so.

    One application per job row covers the same source and source id (`jobs` is unique on
    them), so only links and employer + role are compared across rows.
    """
    reasons: list[ConflictReason] = []
    if job.canonical_url == other.canonical_url:
        reasons.append(ConflictReason.CANONICAL_URL)
    company = company_key(job.employer)
    role = title_key(job.title)
    if (
        company
        and role
        and (company, role) == (company_key(other.employer), title_key(other.title))
    ):
        reasons.append(ConflictReason.COMPANY_ROLE)
    return tuple(reasons)


@dataclass(frozen=True, slots=True, kw_only=True)
class Conflict:
    """Another job, already prepared or applied to, that may be the same application."""

    job: Job
    reasons: tuple[ConflictReason, ...]
    submitted: bool


def find_conflicts(
    job: Job,
    others: Iterable[tuple[Job, bool]],
    *,
    judged_different: frozenset[int] = frozenset(),
) -> list[Conflict]:
    """Conflicts between `job` and `others` (each with whether its submission is recorded).

    Jobs the user judged a different job (ADR 0014 verdicts) are left out.
    """
    found: list[Conflict] = []
    for other, submitted in others:
        if other.id is None or other.id == job.id or other.id in judged_different:
            continue
        reasons = conflict_reasons(job, other)
        if reasons:
            found.append(Conflict(job=other, reasons=reasons, submitted=submitted))
    return found
