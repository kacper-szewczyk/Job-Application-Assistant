"""LinkedIn profile suggestions from stored data (#73, ADR 0027). Read-only: nothing is written.

Gathers the latest LinkedIn import, the active profile version and the target jobs for
`jaa.documents.linkedin_suggestions.suggest`. Logs carry ids and counts only.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from jaa.core.errors import MatchError
from jaa.core.models import FactState, ImportRun, ImportSourceType, ImportStatus, Job, JobStatus
from jaa.documents.linkedin_suggestions import Suggestions, TargetJob, suggest
from jaa.matching.match import match_job
from jaa.matching.requirements import extract_requirements
from jaa.storage.profile import active_version_id, run_fact_ids, version_fact_ids
from jaa.storage.repository import import_runs, jobs, profile_facts

log = logging.getLogger(__name__)

LINKEDIN_SOURCES: Final = (ImportSourceType.LINKEDIN_ARCHIVE, ImportSourceType.LINKEDIN_API)
# Jobs the user is aiming for, used when no job ids are given.
TARGET_STATUSES: Final = (
    JobStatus.SAVED,
    JobStatus.PREPARING,
    JobStatus.READY_FOR_REVIEW,
    JobStatus.SUBMITTED,
    JobStatus.INTERVIEWING,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkedInReport:
    run: ImportRun
    jobs: tuple[Job, ...]
    result: Suggestions


def latest_linkedin_run(conn: sqlite3.Connection) -> ImportRun | None:
    """The newest LinkedIn archive or API import that stored anything."""
    runs = [
        r
        for r in import_runs.list(conn)
        if r.source_type in LINKEDIN_SOURCES and r.status is not ImportStatus.FAILED
    ]
    return max(runs, key=lambda r: (r.imported_at, r.id or 0)) if runs else None


def target_jobs(conn: sqlite3.Connection, job_ids: Sequence[int] = ()) -> list[Job]:
    """The given jobs, or every job the user saved or is applying to."""
    if job_ids:
        return [jobs.get(conn, i) for i in dict.fromkeys(job_ids)]
    return [j for j in jobs.list(conn) if j.status in TARGET_STATUSES]


def linkedin_suggestions(
    conn: sqlite3.Connection, job_ids: Sequence[int] = ()
) -> LinkedInReport | None:
    """Suggestions for the user's LinkedIn profile, or None without a LinkedIn import.

    Raises `MatchError` when nothing is confirmed yet, since every suggestion needs a
    confirmed fact.
    """
    run = latest_linkedin_run(conn)
    if run is None or run.id is None:
        return None
    version = active_version_id(conn)
    if version is None:
        raise MatchError("no confirmed profile yet: confirm facts with `jaa profile confirm` first")
    by_id = {f.id: f for f in profile_facts.list(conn)}
    confirmed = [by_id[i] for i in version_fact_ids(conn, version) if i in by_id]
    seen = [by_id[i] for i in run_fact_ids(conn, run.id) if i in by_id]
    unreviewed = [f for f in by_id.values() if f.state is FactState.UNREVIEWED]

    targets: list[TargetJob] = []
    chosen = target_jobs(conn, job_ids)
    for job in chosen:
        result = match_job(
            extract_requirements(job),
            title=job.title,
            location=job.location,
            confirmed=confirmed,
            unreviewed=unreviewed,
        )
        targets.append(TargetJob(job_id=job.id or 0, checks=result.checks))
    found = suggest(seen, confirmed, targets)
    log.info(
        "linkedin suggestions run=%s jobs=%d suggestions=%d gaps=%d withheld=%d",
        run.id,
        len(chosen),
        len(found.suggestions),
        len(found.gaps),
        found.withheld,
    )
    return LinkedInReport(run=run, jobs=tuple(chosen), result=found)
