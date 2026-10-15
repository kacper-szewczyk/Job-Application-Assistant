"""Assisted-apply handoff and application tracker (T15, ADR 0018).

The user prepares and submits applications; jaa records what they confirm. `start_application`
opens an application record once possible duplicates are cleared, `handoff` gathers the link,
documents and answers to take to the employer's page, and `record_submission` stores the
submission only on the user's confirmation, with the documents and answer versions used.

Logs carry job, application, artifact and answer ids and counts only.
"""

import hashlib
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from jaa.answers.classify import AnswerAction, Resolution
from jaa.core.applications import (
    BEFORE_SUBMISSION,
    STARTED,
    Conflict,
    check_transition,
    find_conflicts,
)
from jaa.core.clock import utcnow
from jaa.core.errors import ApplicationError, DuplicateApplicationError, NotFoundError
from jaa.core.models import (
    Application,
    ArtifactKind,
    DocumentArtifact,
    DuplicateVerdict,
    Job,
    JobStatus,
    JobStatusEvent,
    JsonObject,
    JsonValue,
    StatusActor,
    SubmissionConfirmation,
)
from jaa.storage.answers import prepare_answers
from jaa.storage.jobs import change_job_status, status_history
from jaa.storage.repository import applications, document_artifacts, jobs

log = logging.getLogger(__name__)

# The documents handed over for an application, newest of each kind.
HANDOFF_KINDS: Final = (ArtifactKind.CV_PDF, ArtifactKind.COVER_LETTER_PDF)
# How far a submission time may lie ahead of the clock (clock skew between devices).
FUTURE_TOLERANCE: Final = timedelta(minutes=5)
MAX_NOTE_CHARS: Final = 2_000
MAX_REFERENCE_CHARS: Final = 200


def get_application(conn: sqlite3.Connection, job_id: int) -> Application | None:
    found = applications.list(conn, job_id=job_id)
    return found[0] if found else None


def is_submitted(application: Application | None) -> bool:
    return application is not None and application.submitted_at is not None


def _judged_different(conn: sqlite3.Connection, job_id: int) -> frozenset[int]:
    """Jobs whose duplicate flag with `job_id` the user last judged `different_jobs`."""
    rows = conn.execute(
        "SELECT d.job_id, d.other_job_id FROM job_duplicates d "
        "WHERE (d.job_id = ? OR d.other_job_id = ?) AND ("
        "SELECT r.verdict FROM job_duplicate_reviews r WHERE r.duplicate_id = d.id "
        "ORDER BY r.id DESC LIMIT 1) = ?",
        (job_id, job_id, DuplicateVerdict.DIFFERENT_JOBS.value),
    ).fetchall()
    return frozenset(
        int(r["other_job_id"]) if int(r["job_id"]) == job_id else int(r["job_id"]) for r in rows
    )


def find_application_conflicts(conn: sqlite3.Connection, job_id: int) -> list[Conflict]:
    """Other jobs already prepared or applied to that may be the same application as `job_id`.

    Compared by canonical link and by employer + role (SPEC §6). Jobs the user judged a
    different job are left out.
    """
    job = jobs.get(conn, job_id)
    started = [s.value for s in JobStatus if s not in {JobStatus.DISCOVERED, JobStatus.SAVED}]
    marks = ", ".join("?" for _ in started)
    rows = conn.execute(
        f"SELECT j.*, a.submitted_at AS application_submitted_at FROM jobs j "  # noqa: S608 - placeholders only
        f"LEFT JOIN applications a ON a.job_id = j.id "
        f"WHERE j.id != ? AND (a.id IS NOT NULL OR j.status IN ({marks})) ORDER BY j.id",
        (job_id, *started),
    ).fetchall()
    others = [(jobs.from_row(r), r["application_submitted_at"] is not None) for r in rows]
    return find_conflicts(job, others, judged_different=_judged_different(conn, job_id))


def _clean(text: str | None, limit: int, what: str) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) > limit:
        raise ApplicationError(f"the {what} must be at most {limit} characters")
    return text or None


def _refuse_duplicates(conflicts: list[Conflict], *, allow: bool, doing: str) -> None:
    if conflicts and not allow:
        ids = tuple(c.job.id for c in conflicts if c.job.id is not None)
        listed = ", ".join(f"#{i}" for i in ids)
        raise DuplicateApplicationError(
            f"possible duplicate application: job(s) {listed} look like the same role; "
            f"check them before {doing}",
            ids,
        )


def _override_note(conflicts: list[Conflict], note: str | None) -> str | None:
    if not conflicts:
        return note
    ids = ", ".join(f"#{c.job.id}" for c in conflicts)
    override = f"continued despite possible duplicate(s) {ids}"
    return f"{note}\n{override}" if note else override


@dataclass(frozen=True, slots=True, kw_only=True)
class Started:
    application: Application
    job: Job
    created: bool
    conflicts: list[Conflict]


def start_application(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    allow_duplicates: bool = False,
    note: str | None = None,
    at: datetime | None = None,
) -> Started:
    """Open an application for a job and move it to `preparing`.

    Refuses with `DuplicateApplicationError` while another prepared or submitted job looks like
    the same role, unless `allow_duplicates` (the override is noted in the status history).
    Starting again returns the existing record unchanged.
    """
    at = at or utcnow()
    note = _clean(note, MAX_NOTE_CHARS, "note")
    with conn:
        job = jobs.get(conn, job_id)
        existing = get_application(conn, job_id)
        conflicts = find_application_conflicts(conn, job_id)
        if existing is not None:
            return Started(application=existing, job=job, created=False, conflicts=conflicts)
        _refuse_duplicates(conflicts, allow=allow_duplicates, doing="preparing another")
        stored = applications.insert(conn, Application(job_id=job_id, created_at=at))
        if job.status in {JobStatus.DISCOVERED, JobStatus.SAVED}:
            job = change_job_status(
                conn,
                job_id,
                JobStatus.PREPARING,
                StatusActor.USER,
                _override_note(conflicts, note),
                at,
            )
    log.info("started application id=%s job=%s conflicts=%s", stored.id, job_id, len(conflicts))
    return Started(application=stored, job=job, created=True, conflicts=conflicts)


def set_application_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: JobStatus,
    *,
    note: str | None = None,
    at: datetime | None = None,
) -> Job:
    """Change a job's status on the user's request, following the tracker rules.

    `submitted` is refused here: use `record_submission`. `preparing` and `ready_for_review`
    need an application record, so that `start_application` checked for duplicates first.
    """
    note = _clean(note, MAX_NOTE_CHARS, "note")
    with conn:
        job = jobs.get(conn, job_id)
        application = get_application(conn, job_id)
        check_transition(job.status, status, submitted=is_submitted(application))
        if status in STARTED and application is None:
            raise ApplicationError(
                f"start the application first, which checks for duplicates: "
                f"`jaa apply start {job_id}`"
            )
        changed = change_job_status(conn, job_id, status, StatusActor.USER, note, at or utcnow())
    log.info("job=%s status %s -> %s", job_id, job.status.value, changed.status.value)
    return changed


class HandoffWarning(StrEnum):
    """Something to sort out before submitting. Codes only; the CLI words them."""

    NOT_STARTED = "not_started"
    NO_CV = "no_cv"
    DOCUMENT_MISSING = "document_missing"
    DOCUMENT_CHANGED = "document_changed"
    QUESTIONS_NEED_INPUT = "questions_need_input"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    NO_LONGER_LISTED = "no_longer_listed"
    ALREADY_SUBMITTED = "already_submitted"


@dataclass(frozen=True, slots=True, kw_only=True)
class HandoffDocument:
    artifact: DocumentArtifact
    exists: bool
    # False when the file on disk is not the one jaa built (edited or replaced since).
    unchanged: bool

    @property
    def id(self) -> int:
        if self.artifact.id is None:
            raise ValueError("artifact has no id")
        return self.artifact.id


@dataclass(frozen=True, slots=True, kw_only=True)
class Handoff:
    """Everything to take to the employer's application page."""

    job: Job
    application: Application | None
    url: str
    documents: list[HandoffDocument]
    resolutions: list[Resolution]
    conflicts: list[Conflict]
    warnings: tuple[HandoffWarning, ...]


def _sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        return None


def latest_documents(conn: sqlite3.Connection, job_id: int) -> list[HandoffDocument]:
    """The newest document of each handed-over kind built for the job, with its file state."""
    found: list[HandoffDocument] = []
    for kind in HANDOFF_KINDS:
        built = document_artifacts.list(conn, job_id=job_id, kind=kind)
        if not built:
            continue
        artifact = built[-1]
        digest = _sha256(Path(artifact.path))
        found.append(
            HandoffDocument(
                artifact=artifact,
                exists=digest is not None,
                unchanged=digest == artifact.content_sha256,
            )
        )
    return found


def application_url(job: Job) -> str:
    """The link to apply at: the listing as its source gave it, else the canonical link."""
    return job.source_url or job.canonical_url


def handoff(conn: sqlite3.Connection, job_id: int, *, now: datetime | None = None) -> Handoff:
    """Gather the link, documents and answers for a job's application, with warnings."""
    job = jobs.get(conn, job_id)
    application = get_application(conn, job_id)
    documents = latest_documents(conn, job_id)
    resolutions = prepare_answers(conn, job_id, now=now)
    conflicts = find_application_conflicts(conn, job_id)
    checks = (
        (HandoffWarning.ALREADY_SUBMITTED, is_submitted(application)),
        (HandoffWarning.NOT_STARTED, application is None),
        (HandoffWarning.POSSIBLE_DUPLICATE, bool(conflicts)),
        (HandoffWarning.NO_LONGER_LISTED, job.removed_at is not None),
        (HandoffWarning.NO_CV, not any(d.artifact.kind is ArtifactKind.CV_PDF for d in documents)),
        (HandoffWarning.DOCUMENT_MISSING, any(not d.exists for d in documents)),
        (HandoffWarning.DOCUMENT_CHANGED, any(d.exists and not d.unchanged for d in documents)),
        (
            HandoffWarning.QUESTIONS_NEED_INPUT,
            any(r.action is AnswerAction.ASK for r in resolutions),
        ),
    )
    warnings = tuple(w for w, applies in checks if applies)
    log.info("handoff job=%s documents=%s warnings=%s", job_id, len(documents), len(warnings))
    return Handoff(
        job=job,
        application=application,
        url=application_url(job),
        documents=documents,
        resolutions=resolutions,
        conflicts=conflicts,
        warnings=warnings,
    )


def _question_snapshot(r: Resolution) -> JsonObject:
    p = r.provenance
    fact_ids: list[JsonValue] = list(p.fact_ids)
    return {
        "key": r.question_key,
        "class": r.answer_class.value,
        "action": r.action.value,
        "answer_id": p.answer_id,
        "fact_ids": fact_ids,
        "preference_key": p.preference_key,
    }


def _chosen_documents(
    conn: sqlite3.Connection, job_id: int, document_ids: Sequence[int] | None
) -> list[DocumentArtifact]:
    if document_ids is None:
        return [d.artifact for d in latest_documents(conn, job_id) if d.exists]
    chosen: list[DocumentArtifact] = []
    for artifact_id in dict.fromkeys(document_ids):
        try:
            artifact = document_artifacts.get(conn, artifact_id)
        except NotFoundError:
            raise ApplicationError(f"there is no document #{artifact_id}") from None
        if artifact.job_id != job_id:
            raise ApplicationError(f"document #{artifact_id} was not built for job #{job_id}")
        chosen.append(artifact)
    return chosen


def record_submission(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    confirmation: SubmissionConfirmation,
    submitted_at: datetime | None = None,
    reference: str | None = None,
    note: str | None = None,
    document_ids: Sequence[int] | None = None,
    allow_duplicates: bool = False,
    now: datetime | None = None,
) -> Application:
    """Record that the user submitted the application, and what they used.

    Only the user's own confirmation is accepted: no authorized submission integration exists,
    so an integration receipt cannot be verified and is refused (SPEC §6 Human control). The
    caller must have asked the user. Stores the time, the link, the documents (by default the
    newest CV and cover letter built for the job) and the answer versions offered for filling,
    and moves the job to `submitted`. A second submission, or one while another job looks like
    the same role, is refused unless `allow_duplicates`.
    """
    if confirmation is not SubmissionConfirmation.USER:
        raise ApplicationError(
            "no authorized submission integration exists, so a receipt cannot be verified; "
            "confirm the submission yourself"
        )
    now = now or utcnow()
    at = submitted_at or now
    if at.tzinfo is None:
        raise ApplicationError("the submission time needs a time zone")
    if at > now + FUTURE_TOLERANCE:
        raise ApplicationError("the submission time is in the future")
    reference = _clean(reference, MAX_REFERENCE_CHARS, "reference")
    note = _clean(note, MAX_NOTE_CHARS, "note")
    with conn:
        job = jobs.get(conn, job_id)
        application = get_application(conn, job_id)
        if is_submitted(application):
            raise ApplicationError(f"job #{job_id} is already recorded as submitted")
        if job.status not in BEFORE_SUBMISSION | {JobStatus.WITHDRAWN, JobStatus.CLOSED}:
            raise ApplicationError(f"job #{job_id} is {job.status.value}; it cannot be submitted")
        conflicts = find_application_conflicts(conn, job_id)
        _refuse_duplicates(conflicts, allow=allow_duplicates, doing="submitting again")
        if application is None:
            application = applications.insert(conn, Application(job_id=job_id, created_at=now))
        application_id = application.id
        if application_id is None:
            raise ApplicationError("application has no id")
        documents = _chosen_documents(conn, job_id, document_ids)
        resolutions = prepare_answers(conn, job_id, now=now)
        used = sorted(
            {
                r.provenance.answer_id
                for r in resolutions
                if r.action is not AnswerAction.ASK and r.provenance.answer_id is not None
            }
        )
        conn.executemany(
            "INSERT INTO application_answers (application_id, answer_id) VALUES (?, ?)",
            [(application_id, answer_id) for answer_id in used],
        )
        for artifact in documents:
            if artifact.id is not None:
                document_artifacts.update(conn, artifact.id, application_id=application_id)
        snapshot: JsonObject = {
            "questions": [_question_snapshot(r) for r in resolutions],
            "documents": [a.id for a in documents],
            "duplicates_overridden": [c.job.id for c in conflicts],
        }
        stored = applications.update(
            conn,
            application_id,
            submitted_at=at,
            submission_confirmed_by=confirmation,
            source_url=application_url(job),
            external_reference=reference,
            handoff=snapshot,
        )
        change_job_status(
            conn,
            job_id,
            JobStatus.SUBMITTED,
            StatusActor.USER,
            _override_note(conflicts, note),
            at,
        )
    log.info(
        "recorded submission application=%s job=%s documents=%s answers=%s",
        application_id,
        job_id,
        len(documents),
        len(used),
    )
    return stored


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationRecord:
    """A job's application with its history, linked documents and answer versions."""

    job: Job
    application: Application | None
    history: list[JobStatusEvent]
    documents: list[DocumentArtifact]
    answer_ids: tuple[int, ...]


def application_record(conn: sqlite3.Connection, job_id: int) -> ApplicationRecord:
    job = jobs.get(conn, job_id)
    application = get_application(conn, job_id)
    documents: list[DocumentArtifact] = []
    answer_ids: tuple[int, ...] = ()
    if application is not None and application.id is not None:
        documents = document_artifacts.list(conn, application_id=application.id)
        answer_ids = tuple(
            int(r[0])
            for r in conn.execute(
                "SELECT answer_id FROM application_answers WHERE application_id = ? "
                "ORDER BY answer_id",
                (application.id,),
            )
        )
    return ApplicationRecord(
        job=job,
        application=application,
        history=status_history(conn, job_id),
        documents=documents,
        answer_ids=answer_ids,
    )


def list_applications(
    conn: sqlite3.Connection, statuses: Sequence[JobStatus] = ()
) -> list[tuple[Job, Application]]:
    """Jobs with an application record, most recently changed first."""
    found: list[tuple[Job, Application]] = []
    for application in applications.list(conn):
        job = jobs.get(conn, application.job_id)
        if not statuses or job.status in statuses:
            found.append((job, application))
    found.sort(key=lambda pair: pair[0].status_changed_at, reverse=True)
    return found
