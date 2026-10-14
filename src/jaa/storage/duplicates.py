"""Flagging possible duplicate jobs and recording the user's verdicts (T11, ADR 0014).

Jobs are never merged. Two jobs with the same canonical URL, or with the same employer, title
and location after folding, get one `job_duplicates` flag. The user then says whether they are
the same job; the latest verdict counts. Logs carry ids and counts only.
"""

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from jaa.core.clock import utcnow
from jaa.core.errors import NotFoundError
from jaa.core.models import (
    DuplicateReason,
    DuplicateVerdict,
    Job,
    JobDuplicate,
    JobDuplicateReview,
    JobStatus,
)
from jaa.storage.repository import job_duplicate_reviews, job_duplicates, jobs

log = logging.getLogger(__name__)

# Statuses that mean the user has started on the job; a duplicate of one deserves a warning.
IN_PROGRESS: Final = frozenset(JobStatus) - {JobStatus.DISCOVERED, JobStatus.SAVED}


def flag_duplicates(conn: sqlite3.Connection, job_ids: Iterable[int], at: datetime) -> list[int]:
    """Flag each job against every other job sharing its canonical URL or duplicate key.

    Runs inside the caller's transaction. Pairs already flagged are left alone. Returns the ids
    of new flags.
    """
    flagged: list[int] = []
    for job_id in job_ids:
        row = conn.execute(
            "SELECT canonical_url, dedupe_key FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            continue
        candidates: list[tuple[int, DuplicateReason]] = [
            (int(r["id"]), DuplicateReason.CANONICAL_URL)
            for r in conn.execute(
                "SELECT id FROM jobs WHERE canonical_url = ? AND id != ? ORDER BY id",
                (row["canonical_url"], job_id),
            )
        ]
        if row["dedupe_key"] is not None:
            candidates += [
                (int(r["id"]), DuplicateReason.EMPLOYER_TITLE_LOCATION)
                for r in conn.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ? AND id != ? ORDER BY id",
                    (row["dedupe_key"], job_id),
                )
            ]
        for other, reason in candidates:
            low, high = sorted((job_id, other))
            exists = conn.execute(
                "SELECT 1 FROM job_duplicates WHERE job_id = ? AND other_job_id = ?", (low, high)
            ).fetchone()
            if exists is None:
                stored = job_duplicates.insert(
                    conn,
                    JobDuplicate(job_id=low, other_job_id=high, reason=reason, detected_at=at),
                )
                if stored.id is not None:
                    flagged.append(stored.id)
    if flagged:
        log.info("flagged %s possible duplicate pair(s)", len(flagged))
    return flagged


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateFlag:
    """A flag with both jobs and the latest verdict (None until the user decides)."""

    flag: JobDuplicate
    job: Job
    other: Job
    verdict: DuplicateVerdict | None

    @property
    def id(self) -> int:
        if self.flag.id is None:
            raise ValueError("flag has no id")
        return self.flag.id

    def partner(self, job_id: int) -> Job:
        return self.other if self.job.id == job_id else self.job


def _verdict(conn: sqlite3.Connection, duplicate_id: int) -> DuplicateVerdict | None:
    row = conn.execute(
        "SELECT verdict FROM job_duplicate_reviews WHERE duplicate_id = ? ORDER BY id DESC LIMIT 1",
        (duplicate_id,),
    ).fetchone()
    return None if row is None else DuplicateVerdict(row["verdict"])


def _resolve(conn: sqlite3.Connection, flag: JobDuplicate) -> DuplicateFlag:
    if flag.id is None:
        raise ValueError("flag has no id")
    return DuplicateFlag(
        flag=flag,
        job=jobs.get(conn, flag.job_id),
        other=jobs.get(conn, flag.other_job_id),
        verdict=_verdict(conn, flag.id),
    )


def list_flags(conn: sqlite3.Connection, *, include_reviewed: bool = False) -> list[DuplicateFlag]:
    """Duplicate flags, oldest first; by default only those the user has not decided on."""
    flags = [_resolve(conn, f) for f in job_duplicates.list(conn)]
    return [f for f in flags if include_reviewed or f.verdict is None]


def flags_for_job(conn: sqlite3.Connection, job_id: int) -> list[DuplicateFlag]:
    """Every flag involving `job_id`, decided or not."""
    rows = conn.execute(
        "SELECT * FROM job_duplicates WHERE job_id = ? OR other_job_id = ? ORDER BY id",
        (job_id, job_id),
    ).fetchall()
    return [_resolve(conn, job_duplicates.from_row(r)) for r in rows]


def flagged_job_ids(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """{job id: ids of the jobs it may duplicate}, leaving out pairs judged different jobs."""
    rows = conn.execute(
        "SELECT d.job_id, d.other_job_id FROM job_duplicates d WHERE COALESCE(("
        "SELECT r.verdict FROM job_duplicate_reviews r WHERE r.duplicate_id = d.id "
        "ORDER BY r.id DESC LIMIT 1), '') != ? ORDER BY d.id",
        (DuplicateVerdict.DIFFERENT_JOBS.value,),
    ).fetchall()
    partners: dict[int, list[int]] = {}
    for row in rows:
        job_id, other = int(row["job_id"]), int(row["other_job_id"])
        partners.setdefault(job_id, []).append(other)
        partners.setdefault(other, []).append(job_id)
    return partners


def review_flag(
    conn: sqlite3.Connection,
    duplicate_id: int,
    verdict: DuplicateVerdict,
    at: datetime | None = None,
) -> JobDuplicateReview:
    """Record the user's verdict on a flag. Neither job changes."""
    with conn:
        job_duplicates.get(conn, duplicate_id)
        review = job_duplicate_reviews.insert(
            conn,
            JobDuplicateReview(
                duplicate_id=duplicate_id, verdict=verdict, reviewed_at=at or utcnow()
            ),
        )
    log.info("duplicate flag id=%s reviewed: %s", duplicate_id, verdict.value)
    return review


def get_flag(conn: sqlite3.Connection, duplicate_id: int) -> DuplicateFlag:
    try:
        return _resolve(conn, job_duplicates.get(conn, duplicate_id))
    except NotFoundError as exc:
        raise NotFoundError(f"no duplicate flag {duplicate_id}") from exc
