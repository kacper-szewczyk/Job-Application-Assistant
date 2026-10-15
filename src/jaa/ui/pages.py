"""Page handlers. Each opens the database for one request and calls the CLI's services.

Reads render a page; every change is a form POST that checks the CSRF token, calls one service
function and redirects back (post/redirect/get) with a short message. Messages name ids and
counts, never values. Redirect targets are built here, never taken from the request.
"""

import logging
import sqlite3
from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from jaa.answers.classify import AnswerAction, AnswerClass, Note, Resolution
from jaa.config import Settings
from jaa.core.applications import ARCHIVED, BEFORE_SUBMISSION, STARTED, is_archived
from jaa.core.clock import utcnow
from jaa.core.errors import DuplicateApplicationError, FactConflictError, JaaError, NotFoundError
from jaa.core.models import (
    Application,
    ArtifactKind,
    DocumentArtifact,
    DuplicateVerdict,
    FactOrigin,
    FactState,
    ImportRun,
    Job,
    JobStatus,
    ProfileFact,
    SubmissionConfirmation,
)
from jaa.core.review import FieldConflict, find_conflicts
from jaa.storage import answers as answer_service
from jaa.storage import applications as application_service
from jaa.storage import duplicates as duplicate_service
from jaa.storage import matches as match_service
from jaa.storage import profile as profile_service
from jaa.storage import tailoring as tailoring_service
from jaa.storage.applications import HandoffWarning
from jaa.storage.db import connect
from jaa.storage.profile import active_version_id
from jaa.storage.repository import document_artifacts, import_runs, jobs, profile_facts
from jaa.ui.security import UiSession

log = logging.getLogger(__name__)

TEMPLATES: Final = Path(__file__).parent / "templates"
MASK: Final = "•••• (sensitive)"
MAX_LIST: Final = 300
PDF_KINDS: Final = frozenset({ArtifactKind.CV_PDF, ArtifactKind.COVER_LETTER_PDF})
JOB_TABS: Final = ("overview", "cv", "questions", "apply")

PIPELINE: Final = (
    JobStatus.SAVED,
    JobStatus.PREPARING,
    JobStatus.READY_FOR_REVIEW,
    JobStatus.SUBMITTED,
    JobStatus.INTERVIEWING,
    JobStatus.REJECTED,
    JobStatus.WITHDRAWN,
    JobStatus.CLOSED,
)
STATUS_LABELS: Final = {
    JobStatus.DISCOVERED: "Discovered",
    JobStatus.SAVED: "Saved",
    JobStatus.PREPARING: "Preparing",
    JobStatus.READY_FOR_REVIEW: "Ready for review",
    JobStatus.SUBMITTED: "Submitted",
    JobStatus.INTERVIEWING: "Interviewing",
    JobStatus.REJECTED: "Rejected",
    JobStatus.WITHDRAWN: "Withdrawn",
    JobStatus.CLOSED: "Closed",
}
CLASS_LABELS: Final = {
    AnswerClass.PROFILE_BACKED: "from your profile",
    AnswerClass.REUSABLE_ANSWER: "saved answer",
    AnswerClass.INFERRED_SUGGESTION: "suggestion (inferred)",
    AnswerClass.UNANSWERED: "unanswered",
}
ACTION_LABELS: Final = {
    AnswerAction.FILL: "ready",
    AnswerAction.REVIEW: "check it",
    AnswerAction.ASK: "needs your answer",
}
NOTE_LABELS: Final = {
    Note.SENSITIVE: "sensitive: needs your explicit answer",
    Note.SAFE_REUSE: "you set this answer to be reused without asking",
    Note.EXPIRED: "review date passed: confirm it again",
    Note.UNCONFIRMED: "not confirmed",
    Note.ALWAYS_ASK: "you asked to be asked every time",
    Note.REVIEW_POLICY: "reused with review",
    Note.DIFFERENT_WORDING: "saved for a differently worded question",
    Note.UNREVIEWED_FACT: "from an unreviewed profile fact",
    Note.FROM_PREFERENCE: "inferred from your preferences",
    Note.NOT_AN_OPTION: "not one of the question's options",
    Note.UNKNOWN_QUESTION: "new question",
}
WARNING_LABELS: Final = {
    HandoffWarning.ALREADY_SUBMITTED: "This application is already recorded as submitted.",
    HandoffWarning.NOT_STARTED: "Not started yet. Starting checks for duplicate applications.",
    HandoffWarning.POSSIBLE_DUPLICATE: "Another prepared or submitted job looks like this role.",
    HandoffWarning.NO_LONGER_LISTED: "The source no longer lists this job; it may be closed.",
    HandoffWarning.NO_CV: "No CV built for this job yet.",
    HandoffWarning.DOCUMENT_MISSING: "A document file is missing; build it again.",
    HandoffWarning.DOCUMENT_CHANGED: "A document file changed since jaa built it.",
    HandoffWarning.QUESTIONS_NEED_INPUT: "Some questions need your answer.",
}
KIND_LABELS: Final = {
    ArtifactKind.CV_PDF: "CV",
    ArtifactKind.COVER_LETTER_PDF: "Cover letter",
    ArtifactKind.CV_SOURCE: "CV source",
    ArtifactKind.COVER_LETTER_SOURCE: "Cover letter source",
}

env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
# Labels every template may use.
LABELS: Final[dict[str, object]] = {
    "STATUS_LABELS": STATUS_LABELS,
    "CLASS_LABELS": CLASS_LABELS,
    "ACTION_LABELS": ACTION_LABELS,
    "NOTE_LABELS": NOTE_LABELS,
    "WARNING_LABELS": WARNING_LABELS,
    "KIND_LABELS": KIND_LABELS,
    "MASK": MASK,
}


def _when(value: datetime | None) -> str:
    return "-" if value is None else f"{value:%Y-%m-%d %H:%M}"


def _day(value: datetime | None) -> str:
    return "-" if value is None else f"{value:%Y-%m-%d}"


env.filters["when"] = _when
env.filters["day"] = _day


class Pages:
    """The UI's handlers, bound to one data directory and one session."""

    def __init__(self, settings: Settings, session: UiSession) -> None:
        self.settings = settings
        self.session = session

    # -- plumbing -------------------------------------------------------------------------

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        conn = connect(self.settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    def _render(
        self, request: Request, template: str, code: int = 200, **context: object
    ) -> Response:
        return self._page(request, template, context, code)

    def _page(
        self, request: Request, template: str, context: Mapping[str, object], code: int = 200
    ) -> Response:
        with self._db() as conn:
            nav = _nav_counts(conn)
        html = env.get_template(template).render(
            request_path=request.url.path,
            csrf=self.session.csrf,
            flashes=self.session.take_flashes(),
            nav=nav,
            data_dir=str(self.settings.data_dir),
            origin=self.session.origin,
            **LABELS,
            **context,
        )
        return HTMLResponse(html, status_code=code)

    def _not_found(self, request: Request, exc: NotFoundError) -> Response:
        return self._render(request, "error.html", code=404, message=str(exc))

    @asynccontextmanager
    async def _change(self, request: Request) -> AsyncGenerator[FormData]:
        """Read a form post after checking its CSRF token; turn domain errors into messages."""
        form = await request.form()
        token = form.get("csrf")
        if not isinstance(token, str) or not self.session.check_csrf(request, token):
            raise CsrfError
        try:
            yield form
        except DuplicateApplicationError as exc:
            others = ", ".join(f"#{i}" for i in exc.job_ids)
            self.session.flash("bad", f"{exc}. Possible duplicates: {others}.")
        except FactConflictError as exc:
            self.session.flash(
                "bad", f"{exc}. Tick “replace the confirmed value” to use the new one."
            )
        except JaaError as exc:
            self.session.flash("bad", str(exc))

    async def login(self, request: Request) -> Response:
        token = request.query_params.get("token", "")
        if not self.session.redeem(token):
            return self._plain(
                403,
                "This link was already used or is wrong. Stop <code>jaa ui</code> and start it "
                "again for a new one.",
            )
        log.info("ui session started")
        # A same-origin refresh, so the browser sends the new SameSite=Strict cookie with it.
        response = self._plain(200, "Opening jaa…", refresh=True)
        response.set_cookie(
            "jaa_ui", self.session.session_id, httponly=True, samesite="strict", path="/"
        )
        return response

    @staticmethod
    def _plain(status: int, text: str, *, refresh: bool = False) -> Response:
        meta = '<meta http-equiv="refresh" content="0;url=/">' if refresh else ""
        body = (
            f"<!doctype html><meta charset='utf-8'>{meta}<title>jaa</title>"
            "<link rel='stylesheet' href='/static/app.css'>"
            f"<main class='refusal'><p>{text}</p></main>"
        )
        return HTMLResponse(body, status_code=status)

    async def stylesheet(self, request: Request) -> Response:
        return FileResponse(Path(__file__).parent / "static" / "app.css", media_type="text/css")

    # -- overview ---------------------------------------------------------------------------

    async def overview(self, request: Request) -> Response:
        with self._db() as conn:
            facts = profile_facts.list(conn)
            apps = application_service.list_applications(conn)
            flagged = duplicate_service.flagged_job_ids(conn)
            open_flags = duplicate_service.list_flags(conn)
            asking = 0
            for job, _ in apps:
                if job.status in STARTED and job.id is not None:
                    resolutions = answer_service.prepare_answers(conn, job.id)
                    asking += sum(r.action is AnswerAction.ASK for r in resolutions)
            counts = {s: sum(j.status is s for j, _ in apps) for s in PIPELINE}
        return self._render(
            request,
            "overview.html",
            unreviewed=sum(f.state is FactState.UNREVIEWED for f in facts),
            conflicts=len(find_conflicts(facts)),
            asking=asking,
            open_flags=len(open_flags),
            pipeline=[(s, counts[s]) for s in PIPELINE],
            recent=[_row(j, a, flagged) for j, a in apps[:8]],
        )

    # -- profile review ---------------------------------------------------------------------

    async def profile(self, request: Request) -> Response:
        state = _state(request.query_params.get("state"))
        reveal = request.query_params.get("reveal") == "1"
        with self._db() as conn:
            every = profile_facts.list(conn)
            runs = {r.id: r for r in import_runs.list(conn)}
        shown = [f for f in every if state is None or f.state is state]
        conflicts = find_conflicts(every)
        return self._render(
            request,
            "profile.html",
            state=state.value if state else "all",
            states=["unreviewed", "confirmed", "rejected", "all"],
            facts=[_fact_view(f, runs, reveal=reveal) for f in shown[:MAX_LIST]],
            hidden=max(0, len(shown) - MAX_LIST),
            conflicts=[_conflict_view(c, runs, reveal=reveal) for c in conflicts],
            reveal=reveal,
            any_sensitive=any(f.is_sensitive for f in shown),
        )

    async def decide(self, request: Request) -> Response:
        back = "/profile"
        async with self._change(request) as form:
            state = _state(_text(form, "state"))
            back = f"/profile?state={state.value if state else 'all'}"
            ids = _ids(form.getlist("fact_id"))
            if _text(form, "return") == "fact" and len(ids) == 1:
                back = f"/profile/facts/{ids[0]}"
            action = _text(form, "action")
            with self._db() as conn:
                if action == "confirm":
                    result = profile_service.confirm_facts(
                        conn, ids, replace=_checked(form, "replace")
                    )
                elif action == "reject":
                    result = profile_service.reject_facts(conn, ids)
                else:
                    raise JaaError("choose confirm or reject")
            self.session.flash("ok", _changed_text(action, len(result.changed), result.version))
        return _back(back)

    async def fact(self, request: Request) -> Response:
        fact_id = int(request.path_params["fact_id"])
        try:
            with self._db() as conn:
                chain = profile_service.correction_chain(conn, fact_id)
                history = profile_service.fact_history(conn, fact_id)
                runs = {r.id: r for r in import_runs.list(conn)}
                confirmed = profile_facts.list(
                    conn, field_key=chain[0].field_key, state=FactState.CONFIRMED
                )
        except NotFoundError as exc:
            return self._not_found(request, exc)
        fact = chain[0]
        other = next((f for f in confirmed if f.id != fact.id), None)
        return self._render(
            request,
            "fact.html",
            fact=_fact_view(fact, runs, reveal=True),
            chain=[_fact_view(f, runs, reveal=True) for f in chain[1:]],
            history=history,
            other_confirmed=other.id if other else None,
        )

    async def correct(self, request: Request) -> Response:
        fact_id = int(request.path_params["fact_id"])
        back = f"/profile/facts/{fact_id}"
        async with self._change(request) as form:
            value = _text(form, "value").strip()
            if not value:
                raise JaaError("type the correct value")
            with self._db() as conn:
                result = profile_service.correct_fact(
                    conn, fact_id, value, value, replace=_checked(form, "replace")
                )
            new = result.changed[-1]
            back = f"/profile/facts/{new.id}"
            self.session.flash("ok", f"Saved your correction as fact #{new.id}.")
        return _back(back)

    # -- jobs -------------------------------------------------------------------------------

    async def job_list(self, request: Request) -> Response:
        wanted = _status(request.query_params.get("status"))
        with self._db() as conn:
            found = jobs.list(conn, status=wanted) if wanted else jobs.list(conn)
            flagged = duplicate_service.flagged_job_ids(conn)
            apps = {a.job_id: a for _, a in application_service.list_applications(conn)}
        found.sort(key=lambda j: j.status_changed_at, reverse=True)
        return self._render(
            request,
            "jobs.html",
            status=wanted.value if wanted else "",
            statuses=list(JobStatus),
            rows=[_row(j, apps.get(_job_id(j)), flagged) for j in found[:MAX_LIST]],
            hidden=max(0, len(found) - MAX_LIST),
        )

    async def job(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        tab = request.query_params.get("tab", "overview")
        tab = tab if tab in JOB_TABS else "overview"
        reveal = request.query_params.get("reveal") == "1"
        try:
            with self._db() as conn:
                context = _job_context(conn, job_id, tab, reveal=reveal)
        except NotFoundError as exc:
            return self._not_found(request, exc)
        return self._page(request, "job.html", {"tab": tab, "reveal": reveal, **context})

    async def answer(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        async with self._change(request) as form:
            key = _text(form, "key")
            with self._db() as conn:
                found = next(
                    (
                        r
                        for r in answer_service.prepare_answers(conn, job_id)
                        if r.question_key == key
                    ),
                    None,
                )
                if found is None:
                    raise NotFoundError(f"job #{job_id} has no such question")
                if _text(form, "use") == "suggestion":
                    if found.sensitive and _text(form, "seen") != "1":
                        raise JaaError("show the sensitive suggestion before using it")
                    stored = answer_service.accept_suggestion(conn, found, job_id)
                else:
                    stored = answer_service.save_answer(
                        conn,
                        question_text=found.question.text,
                        value=_text(form, "value"),
                        key=found.question_key,
                        job_id=job_id,
                    )
            self.session.flash("ok", f"Saved answer #{stored.id}.")
        return _back(f"/jobs/{job_id}?tab=questions")

    async def add_question(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        async with self._change(request) as form:
            with self._db() as conn:
                stored = answer_service.add_job_question(
                    conn, job_id, _text(form, "text"), required=_checked(form, "required")
                )
            self.session.flash("ok", f"Added question #{stored.id}.")
        return _back(f"/jobs/{job_id}?tab=questions")

    async def start(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        async with self._change(request) as form:
            with self._db() as conn:
                started = application_service.start_application(
                    conn, job_id, allow_duplicates=_checked(form, "allow_duplicate")
                )
            text = "Application started." if started.created else "Already started."
            self.session.flash("ok", f"{text} Job #{job_id} is {started.job.status.value}.")
        return _back(f"/jobs/{job_id}?tab=apply")

    async def status(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        async with self._change(request) as form:
            target = _status(_text(form, "status"))
            if target is None:
                raise JaaError("choose a status")
            note = _text(form, "note").strip() or None
            with self._db() as conn:
                changed = application_service.set_application_status(
                    conn, job_id, target, note=note
                )
            self.session.flash("ok", f"Job #{job_id} is {changed.status.value}.")
        return _back(f"/jobs/{job_id}?tab=apply")

    async def submitted(self, request: Request) -> Response:
        job_id = int(request.path_params["job_id"])
        async with self._change(request) as form:
            # The user's own statement, ticked on this form; nothing else records a submission.
            if not _checked(form, "self_submitted"):
                raise JaaError("nothing recorded: tick “I submitted this application myself” first")
            with self._db() as conn:
                stored = application_service.record_submission(
                    conn,
                    job_id,
                    confirmation=SubmissionConfirmation.USER,
                    submitted_at=_submitted_at(_text(form, "submitted_on")),
                    reference=_text(form, "reference").strip() or None,
                    allow_duplicates=_checked(form, "allow_duplicate"),
                )
            self.session.flash(
                "ok", f"Recorded: job #{job_id} submitted {_when(stored.submitted_at)}."
            )
        return _back(f"/jobs/{job_id}?tab=apply")

    async def duplicate(self, request: Request) -> Response:
        flag_id = int(request.path_params["flag_id"])
        back = "/applications"
        async with self._change(request) as form:
            verdict = DuplicateVerdict(_text(form, "verdict"))
            with self._db() as conn:
                flag = duplicate_service.get_flag(conn, flag_id)
                duplicate_service.review_flag(conn, flag_id, verdict)
            job_id = _job_id(flag.job)
            back = f"/jobs/{job_id}"
            self.session.flash(
                "ok", f"Marked jobs #{job_id} and #{flag.other.id}: {verdict.value}."
            )
        return _back(back)

    # -- applications and documents ---------------------------------------------------------

    async def applications(self, request: Request) -> Response:
        with self._db() as conn:
            apps = application_service.list_applications(conn)
            flagged = duplicate_service.flagged_job_ids(conn)
        groups = [
            (status, [_row(j, a, flagged) for j, a in apps if j.status is status])
            for status in (*BEFORE_SUBMISSION, JobStatus.SUBMITTED, JobStatus.INTERVIEWING)
        ]
        archived = [_row(j, a, flagged) for j, a in apps if j.status in ARCHIVED]
        return self._render(
            request,
            "applications.html",
            groups=[(s, rows) for s, rows in groups if rows],
            archived=archived,
            total=len(apps),
        )

    async def documents(self, request: Request) -> Response:
        with self._db() as conn:
            found = [d for d in document_artifacts.list(conn) if d.kind in PDF_KINDS]
            titles = {j.id: f"{j.title} at {j.employer}" for j in jobs.list(conn)}
        found.reverse()
        selected = request.query_params.get("show")
        chosen = next((d for d in found if str(d.id) == selected), found[0] if found else None)
        return self._render(
            request,
            "documents.html",
            documents=[_document_view(d, titles) for d in found[:MAX_LIST]],
            chosen=_document_view(chosen, titles) if chosen else None,
        )

    async def file(self, request: Request) -> Response:
        artifact_id = int(request.path_params["artifact_id"])
        try:
            with self._db() as conn:
                artifact = document_artifacts.get(conn, artifact_id)
        except NotFoundError as exc:
            return self._not_found(request, exc)
        path = self._servable(artifact)
        if path is None:
            return self._render(
                request,
                "error.html",
                code=404,
                message=f"document #{artifact_id} is not available",
            )
        return FileResponse(
            path,
            media_type="application/pdf",
            content_disposition_type="inline",
            filename=f"jaa-document-{artifact_id}.pdf",
        )

    def _servable(self, artifact: DocumentArtifact) -> Path | None:
        """The PDF's path if it is a generated PDF inside the data directory, else None."""
        if artifact.kind not in PDF_KINDS:
            return None
        path = Path(artifact.path).resolve()
        if not path.is_relative_to(self.settings.data_dir.resolve()) or not path.is_file():
            return None
        return path


class CsrfError(Exception):
    """A form post without this session's token: refused before anything runs."""


# -- view helpers ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FactView:
    id: int
    key: str
    value: str
    state: str
    sensitive: bool
    confidence: str
    source: str
    location: str | None
    excerpt: str | None
    origin: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ConflictView:
    key: str
    current: FactView | None
    candidates: list[FactView]


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRow:
    id: int
    title: str
    employer: str
    location: str | None
    status: str
    archived: bool
    changed: datetime
    submitted: datetime | None
    duplicates: list[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentView:
    id: int
    kind: str
    created: datetime
    job_id: int | None
    job: str | None
    name: str


def _job_id(job: Job) -> int:
    if job.id is None:
        raise NotFoundError("job has no id")
    return job.id


def _nav_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM profile_facts WHERE state = 'unreviewed') AS facts, "
        "(SELECT COUNT(*) FROM applications) AS apps"
    ).fetchone()
    return {
        "facts": int(row["facts"]),
        "apps": int(row["apps"]),
        "flags": len(duplicate_service.list_flags(conn)),
    }


def _source(fact: ProfileFact, runs: dict[int | None, ImportRun]) -> str:
    if fact.origin is FactOrigin.CORRECTION:
        return f"your correction of #{fact.corrected_from_fact_id}"
    if fact.origin is FactOrigin.USER:
        return "entered by you"
    run = runs.get(fact.import_run_id) if fact.import_run_id is not None else None
    if run is None:
        return "import (record deleted)"
    name = Path(run.source_path).name if run.source_path else run.source_type.value
    return f"import #{run.id}: {name}"


def _fact_view(fact: ProfileFact, runs: dict[int | None, ImportRun], *, reveal: bool) -> FactView:
    if fact.id is None:
        raise NotFoundError("fact has no id")
    return FactView(
        id=fact.id,
        key=fact.field_key,
        value=MASK if fact.is_sensitive and not reveal else fact.value_raw,
        state=fact.state.value,
        sensitive=fact.is_sensitive,
        confidence="-" if fact.confidence is None else f"{fact.confidence:.0%}",
        source=_source(fact, runs),
        location=fact.source_location,
        excerpt=None if fact.is_sensitive and not reveal else fact.source_excerpt,
        origin=fact.origin.value,
    )


def _conflict_view(
    conflict: FieldConflict, runs: dict[int | None, ImportRun], *, reveal: bool
) -> ConflictView:
    return ConflictView(
        key=conflict.field_key,
        current=_fact_view(conflict.current, runs, reveal=reveal) if conflict.current else None,
        candidates=[_fact_view(f, runs, reveal=reveal) for f in conflict.candidates],
    )


def _row(job: Job, application: Application | None, flagged: dict[int, list[int]]) -> JobRow:
    job_id = _job_id(job)
    return JobRow(
        id=job_id,
        title=job.title,
        employer=job.employer,
        location=job.location,
        status=job.status.value,
        archived=is_archived(job.status),
        changed=job.status_changed_at,
        submitted=application.submitted_at if application else None,
        duplicates=flagged.get(job_id, []),
    )


def _document_view(artifact: DocumentArtifact, titles: dict[int | None, str]) -> DocumentView:
    if artifact.id is None:
        raise NotFoundError("document has no id")
    return DocumentView(
        id=artifact.id,
        kind=KIND_LABELS.get(artifact.kind, artifact.kind.value),
        created=artifact.created_at,
        job_id=artifact.job_id,
        job=titles.get(artifact.job_id) if artifact.job_id is not None else None,
        name=Path(artifact.path).name,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class QuestionView:
    key: str
    text: str
    required: bool
    options: tuple[str, ...]
    sensitive: bool
    action: AnswerAction
    answer_class: AnswerClass
    # None when there is nothing to suggest, or it is sensitive and not revealed.
    value: str | None
    has_value: bool
    notes: list[str]
    source: str


def _provenance(r: Resolution) -> str:
    p = r.provenance
    parts: list[str] = []
    if p.answer_id is not None:
        parts.append(f"answer #{p.answer_id}")
    if p.fact_ids:
        parts.append("facts " + ", ".join(f"#{i}" for i in p.fact_ids))
    if p.preference_key:
        parts.append(f"preference {p.preference_key}")
    return "; ".join(parts) or "-"


def _question_view(r: Resolution, *, reveal: bool) -> QuestionView:
    return QuestionView(
        key=r.question_key,
        text=r.question.text,
        required=r.question.required,
        options=r.question.options,
        sensitive=r.sensitive,
        action=r.action,
        answer_class=r.answer_class,
        value=None if r.sensitive and not reveal else r.value,
        has_value=r.value is not None,
        notes=[NOTE_LABELS[n] for n in r.notes],
        source=_provenance(r),
    )


def _match_summary(conn: sqlite3.Connection, job: Job) -> dict[str, object] | None:
    found = match_service.latest_match(conn, _job_id(job))
    if found is None:
        return None
    no_score = any(
        isinstance(u, dict) and u.get("code") == "nothing_to_compare" for u in found.uncertainty
    )
    return {
        "score": None if no_score else f"{found.score:.0%}",
        "version": found.profile_version_id,
        "when": found.created_at,
        "outdated": match_service.outdated_reasons(found, job, active_version_id(conn)),
    }


def _tailoring(conn: sqlite3.Connection, job_id: int) -> dict[str, object] | None:
    if tailoring_service.latest(conn, job_id) is None:
        return None
    evaluated = tailoring_service.evaluate(conn, job_id)
    result = evaluated.result
    changes = {c.number: c for c in result.changes}
    edits = [
        {
            "number": check.number,
            "kind": edit.kind.value,
            "target": edit.target,
            "before": changes[check.number].before if check.number in changes else None,
            "after": changes[check.number].after if check.number in changes else edit.text,
            "reason": edit.reason,
            "facts": check.fact_ids,
            "blocked": check.blocked,
            "issues": [i.message for i in check.issues],
        }
        for edit, check in zip(evaluated.plan.edits, result.edit_checks, strict=True)
    ]
    paragraphs = [
        {"text": p.text, "facts": c.fact_ids, "blocked": c.blocked}
        for p, c in zip(evaluated.plan.cover_letter, result.paragraph_checks, strict=True)
    ]
    return {
        "revision": evaluated.row.revision,
        "origin": evaluated.row.origin.value,
        "version": evaluated.row.profile_version_id,
        "outdated": evaluated.row.profile_version_id != active_version_id(conn),
        "edits": edits,
        "paragraphs": paragraphs,
        "blocked": result.blocked,
    }


def _job_context(
    conn: sqlite3.Connection, job_id: int, tab: str, *, reveal: bool
) -> dict[str, object]:
    job = jobs.get(conn, job_id)
    application = application_service.get_application(conn, job_id)
    flags = duplicate_service.flags_for_job(conn, job_id)
    context: dict[str, object] = {
        "job": job,
        "status": job.status.value,
        "archived": is_archived(job.status),
        "application": application,
        "flags": [
            {
                "id": f.id,
                "partner": f.partner(job_id),
                "reason": f.flag.reason.value,
                "verdict": f.verdict,
            }
            for f in flags
        ],
        "url": application_service.application_url(job),
        "tabs": JOB_TABS,
    }
    if tab == "overview":
        context["match"] = _match_summary(conn, job)
        context["history"] = application_service.application_record(conn, job_id).history
    elif tab == "cv":
        context["tailoring"] = _tailoring(conn, job_id)
        context["documents"] = [
            {"view": _document_view(d.artifact, {}), "exists": d.exists, "unchanged": d.unchanged}
            for d in application_service.latest_documents(conn, job_id)
            if d.artifact.kind in PDF_KINDS
        ]
    elif tab == "questions":
        resolutions = answer_service.prepare_answers(conn, job_id)
        context["questions"] = [_question_view(r, reveal=reveal) for r in resolutions]
        context["any_hidden"] = any(r.sensitive and r.value is not None for r in resolutions)
    else:
        found = application_service.handoff(conn, job_id)
        submitted = application_service.is_submitted(application)
        context["handoff"] = found
        context["warnings"] = [WARNING_LABELS[w] for w in found.warnings]
        context["asking"] = sum(r.action is AnswerAction.ASK for r in found.resolutions)
        context["conflicts"] = found.conflicts
        context["submitted"] = submitted
        context["targets"] = _targets(
            job.status, submitted=submitted, started=application is not None
        )
        context["today"] = utcnow().date().isoformat()
    return context


def _targets(current: JobStatus, *, submitted: bool, started: bool) -> list[JobStatus]:
    """Statuses the user can pick on the form; the service checks the rules again."""
    found: list[JobStatus] = []
    for status in JobStatus:
        if status in (current, JobStatus.SUBMITTED):
            continue
        if submitted and status in BEFORE_SUBMISSION:
            continue
        if not submitted and status in (JobStatus.INTERVIEWING, JobStatus.REJECTED):
            continue
        if not started and status in STARTED:
            continue
        found.append(status)
    return found


# -- form helpers ---------------------------------------------------------------------------


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _checked(form: FormData, name: str) -> bool:
    return _text(form, name) == "1"


def _ids(values: Sequence[object]) -> list[int]:
    ids: list[int] = []
    for value in values:
        if not isinstance(value, str) or not value.isdigit():
            raise JaaError("fact ids must be numbers")
        ids.append(int(value))
    if not ids:
        raise JaaError("tick at least one fact")
    return ids


def _state(value: str | None) -> FactState | None:
    if value is None:
        return FactState.UNREVIEWED
    try:
        return FactState(value)
    except ValueError:
        return None


def _status(value: str | None) -> JobStatus | None:
    try:
        return JobStatus(value) if value else None
    except ValueError:
        return None


def _submitted_at(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise JaaError("the submission date must be YYYY-MM-DD") from exc
    if day == utcnow().date():
        return None
    return datetime.combine(day, time(12, 0), tzinfo=UTC)


def _changed_text(action: str, count: int, version: object) -> str:
    done = "Confirmed" if action == "confirm" else "Rejected"
    suffix = " A new profile version is active." if version is not None else ""
    return f"{done} {count} fact(s).{suffix}"


def _back(path: str) -> Response:
    return RedirectResponse(path, status_code=303)
