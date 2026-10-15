"""Saved jobs and job applications from LinkedIn's portability API (#72, ADR 0026).

Saved jobs become manual jobs (ADR 0023) with the title, company and link LinkedIn sent; their
descriptions are pasted by the user, because LinkedIn job pages are never fetched. Applications
are only matched against jobs already in jaa so the user can link them; recording an
application stays the user's own step (`jaa apply`). Logs carry counts only.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jaa.core.dedupe import canonical_url
from jaa.core.errors import JobEntryError
from jaa.storage.manual_jobs import add_manual_job

log = logging.getLogger(__name__)


class LinkedInJob(Protocol):
    """A saved job or application as `jaa.importers.linkedin_api` returns it."""

    @property
    def title(self) -> str: ...
    @property
    def company(self) -> str: ...
    @property
    def url(self) -> str | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedJobsResult:
    added: tuple[int, ...]
    # Saved jobs already in jaa (same link added by hand before), by job id.
    existing: tuple[int, ...]
    # Saved jobs without a usable link, which a manual job needs.
    without_link: int
    flagged: int


def _job_with_link(conn: sqlite3.Connection, url: str, source: str | None) -> int | None:
    sql = "SELECT id FROM jobs WHERE canonical_url = ?"
    params: list[str] = [canonical_url(url)]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    row = conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    return int(row["id"]) if row is not None else None


def add_saved_jobs(
    conn: sqlite3.Connection, jobs: Sequence[LinkedInJob], *, at: datetime | None = None
) -> SavedJobsResult:
    """Add LinkedIn saved jobs as manual jobs, skipping links already added by hand."""
    added: list[int] = []
    existing: list[int] = []
    without_link = 0
    flagged = 0
    for job in jobs:
        if not job.url:
            without_link += 1
            continue
        try:
            stored = add_manual_job(conn, url=job.url, title=job.title, company=job.company, at=at)
        except JobEntryError:
            known = _job_with_link(conn, job.url, "manual")
            if known is None:
                without_link += 1  # not an http(s) link
            else:
                existing.append(known)
            continue
        if stored.job.id is not None:
            added.append(stored.job.id)
        flagged += len(stored.flagged)
    log.info(
        "added linkedin saved jobs added=%d existing=%d without_link=%d flagged=%d",
        len(added),
        len(existing),
        without_link,
        flagged,
    )
    return SavedJobsResult(
        added=tuple(added),
        existing=tuple(existing),
        without_link=without_link,
        flagged=flagged,
    )


def matching_jobs(conn: sqlite3.Connection, job: LinkedInJob) -> tuple[int, ...]:
    """Jobs in jaa that may be this LinkedIn application: same link, or same company and title."""
    ids: list[int] = []
    if job.url and (by_link := _job_with_link(conn, job.url, None)) is not None:
        ids.append(by_link)
    rows = conn.execute(
        "SELECT id FROM jobs WHERE lower(employer) = lower(?) AND lower(title) = lower(?) "
        "ORDER BY id",
        (job.company, job.title),
    ).fetchall()
    ids.extend(int(r["id"]) for r in rows if int(r["id"]) not in ids)
    return tuple(ids)
