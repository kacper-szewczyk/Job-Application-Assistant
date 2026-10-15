"""Jobs the user adds by hand from pasted text (#46, ADR 0023).

For boards without a permitted API (justjoin.it, bulldogjob.pl, nofluffjobs.com) the user copies
an offer into jaa. The job is stored with source `manual`, no connector and no target, so matching,
tailoring and the tracker treat it like a fetched job. Its URL is recorded only: nothing here, or
anywhere else in jaa, requests it. Logs carry ids only.
"""

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

from jaa.connectors.base import JobListing
from jaa.core.clock import utcnow
from jaa.core.dedupe import canonical_url, dedupe_key
from jaa.core.errors import JobEntryError
from jaa.core.models import Job, StatusActor
from jaa.storage.duplicates import flag_duplicates
from jaa.storage.jobs import insert_job
from jaa.storage.listings import content_sha256, requirements_json

log = logging.getLogger(__name__)

MANUAL_SOURCE: Final = "manual"
# Long enough for any job offer, short enough that a wrong file is refused.
MAX_DESCRIPTION_CHARS: Final = 100_000
MAX_FIELD_CHARS: Final = 300


@dataclass(frozen=True, slots=True, kw_only=True)
class AddedJob:
    job: Job
    # Ids of new possible-duplicate flags (ADR 0014).
    flagged: tuple[int, ...] = ()


def _required(name: str, value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise JobEntryError(f"the {name} is empty")
    if len(cleaned) > MAX_FIELD_CHARS:
        raise JobEntryError(f"the {name} is longer than {MAX_FIELD_CHARS} characters")
    return cleaned


def _url(value: str) -> str:
    url = value.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise JobEntryError("the URL is not a valid web address") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise JobEntryError("the URL must be a web address starting with https:// or http://")
    return url


def add_manual_job(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str,
    company: str,
    location: str | None = None,
    description: str | None = None,
    at: datetime | None = None,
) -> AddedJob:
    """Store a job the user typed or pasted, and flag possible duplicates.

    Raises `JobEntryError` for an empty field, a URL that is not http(s), or a URL already
    added by hand (the error names that job). A fetched job with the same link is not an
    error: the new job is flagged as its possible duplicate for the user to decide.
    """
    at = at or utcnow()
    listing = JobListing(
        source_job_id=uuid.uuid4().hex,
        url=_url(url),
        title=_required("title", title),
        company=_required("company", company),
        location=" ".join((location or "").split()) or None,
        description=(description or "").strip() or None,
    )
    if listing.description and len(listing.description) > MAX_DESCRIPTION_CHARS:
        raise JobEntryError(f"the description is longer than {MAX_DESCRIPTION_CHARS} characters")
    canonical = canonical_url(listing.url)
    with conn:
        earlier = conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND canonical_url = ? ORDER BY id LIMIT 1",
            (MANUAL_SOURCE, canonical),
        ).fetchone()
        if earlier is not None:
            raise JobEntryError(
                f"this link was already added as job {earlier['id']}; "
                f"see `jaa jobs show {earlier['id']}`"
            )
        stored = insert_job(
            conn,
            Job(
                source=MANUAL_SOURCE,
                source_job_id=listing.source_job_id,
                canonical_url=canonical,
                source_url=listing.url,
                dedupe_key=dedupe_key(listing.company, listing.title, listing.location),
                employer=listing.company,
                title=listing.title,
                location=listing.location,
                description=listing.description,
                first_seen_at=at,
                last_seen_at=at,
                status_changed_at=at,
                content_sha256=content_sha256(listing),
                requirements=requirements_json(listing),
            ),
            StatusActor.USER,
        )
        if stored.id is None:
            raise JobEntryError("the job was not stored")
        flagged = flag_duplicates(conn, [stored.id], at)
    log.info("added manual job id=%s flagged=%s", stored.id, len(flagged))
    return AddedJob(job=stored, flagged=tuple(flagged))
