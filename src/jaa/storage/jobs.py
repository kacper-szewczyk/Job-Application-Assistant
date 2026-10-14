"""Job persistence that keeps `jobs.status` and `job_status_events` consistent (ADR 0005)."""

import sqlite3
from dataclasses import replace
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import StorageError
from jaa.core.models import Job, JobStatus, JobStatusEvent, StatusActor
from jaa.storage.repository import job_status_events, jobs


def add_job(conn: sqlite3.Connection, job: Job, actor: StatusActor = StatusActor.SYSTEM) -> Job:
    """Insert a job and its initial status event in one transaction."""
    with conn:
        stored = jobs.insert(conn, job)
        if stored.id is None:
            raise StorageError("jobs: insert did not assign an id")
        job_status_events.insert(
            conn,
            JobStatusEvent(
                job_id=stored.id,
                from_status=None,
                to_status=stored.status,
                actor=actor,
                occurred_at=stored.status_changed_at,
            ),
        )
    return stored


def set_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: JobStatus,
    actor: StatusActor,
    note: str | None = None,
    at: datetime | None = None,
) -> Job:
    """Change a job's status and append the history event atomically.

    Transition rules (e.g. `submitted` only after user confirmation) belong to the
    application tracker (T15); this function only guarantees status and history agree.
    """
    at = at or utcnow()
    with conn:
        current = jobs.get(conn, job_id)
        if current.status == status:
            return current
        job_status_events.insert(
            conn,
            JobStatusEvent(
                job_id=job_id,
                from_status=current.status,
                to_status=status,
                actor=actor,
                occurred_at=at,
                note=note,
            ),
        )
        jobs.update(conn, job_id, status=status, status_changed_at=at)
    return replace(current, status=status, status_changed_at=at)


def status_history(conn: sqlite3.Connection, job_id: int) -> list[JobStatusEvent]:
    return job_status_events.list(conn, job_id=job_id)
