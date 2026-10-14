"""Matching stored jobs against the active profile version and storing the result (ADR 0015).

Each run appends a `job_matches` row; nothing is updated. A match records the profile version
it used and the job's content hash, so `outdated_reasons` can tell when the profile or the
listing has changed since. Logs carry ids, counts and scores only.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import MatchError, StorageError
from jaa.core.models import FactState, Job, JobMatch, ProfileFact
from jaa.matching import match as matcher
from jaa.matching.requirements import JobRequirements, extract_requirements
from jaa.storage.preferences import answered_values
from jaa.storage.profile import active_version_id, version_fact_ids
from jaa.storage.repository import job_matches, jobs, profile_facts

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMatch:
    job: Job
    match: JobMatch
    requirements: JobRequirements
    result: matcher.MatchResult


def _facts(conn: sqlite3.Connection, fact_ids: Sequence[int]) -> list[ProfileFact]:
    wanted = set(fact_ids)
    return [f for f in profile_facts.list(conn) if f.id in wanted]


def _version(conn: sqlite3.Connection) -> int:
    version = active_version_id(conn)
    if version is None:
        raise MatchError(
            "no confirmed profile yet: confirm facts with `jaa profile confirm` before matching"
        )
    return version


def match_jobs(
    conn: sqlite3.Connection, job_ids: Sequence[int], *, at: datetime | None = None
) -> list[StoredMatch]:
    """Match each job against the active profile version and store the results."""
    at = at or utcnow()
    with conn:
        version = _version(conn)
        confirmed = _facts(conn, version_fact_ids(conn, version))
        unreviewed = profile_facts.list(conn, state=FactState.UNREVIEWED)
        preferences = answered_values(conn)
        stored: list[StoredMatch] = []
        for job_id in job_ids:
            job = jobs.get(conn, job_id)
            requirements = extract_requirements(job)
            result = matcher.match_job(
                requirements,
                title=job.title,
                location=job.location,
                confirmed=confirmed,
                unreviewed=unreviewed,
                preferences=preferences,
            )
            row = job_matches.insert(
                conn,
                JobMatch(
                    job_id=job_id,
                    profile_version_id=version,
                    score=result.score,
                    evidence=result.evidence_json(),
                    gaps=result.gaps_json(),
                    uncertainty=result.uncertainty_json(),
                    reasons=list(result.reasons),
                    job_content_sha256=job.content_sha256,
                    method=matcher.METHOD,
                    created_at=at,
                ),
            )
            stored.append(StoredMatch(job=job, match=row, requirements=requirements, result=result))
    log.info("matched jobs=%d profile_version=%s", len(stored), version)
    return stored


def latest_matches(conn: sqlite3.Connection) -> dict[int, JobMatch]:
    """The newest match of every matched job, by job id."""
    latest: dict[int, JobMatch] = {}
    for found in job_matches.list(conn):  # ordered by id, so later rows win
        latest[found.job_id] = found
    return latest


def latest_match(conn: sqlite3.Connection, job_id: int) -> JobMatch | None:
    found = job_matches.list(conn, job_id=job_id)
    return found[-1] if found else None


def outdated_reasons(found: JobMatch, job: Job, active_version: int | None) -> list[str]:
    """Why a stored match may no longer be right; empty when it is current."""
    if found.job_id != job.id:
        raise StorageError("match and job do not belong together")
    reasons: list[str] = []
    if found.profile_version_id != active_version:
        reasons.append("profile changed")
    if found.job_content_sha256 != job.content_sha256:
        reasons.append("listing changed")
    if found.method != matcher.METHOD:
        reasons.append("matching rules changed")
    return reasons
