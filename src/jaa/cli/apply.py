"""`jaa apply …`: hand an application over to the user and track it (T15, ADR 0018).

`start` opens an application once possible duplicates are cleared, `handoff` shows the link,
documents and answers to take to the employer's page (and can open it), `submitted` records a
submission only after the user confirms it, and `status`, `list` and `show` track the rest.
jaa never submits anything itself.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.answers import resolution_table
from jaa.cli.common import console, err_console, open_existing, settings_from
from jaa.core.applications import Conflict, ConflictReason
from jaa.core.errors import ApplicationError, DuplicateApplicationError, JaaError
from jaa.core.models import ArtifactKind, JobStatus, SubmissionConfirmation
from jaa.storage import applications as service
from jaa.storage.applications import HandoffWarning

apply_app = typer.Typer(
    help="Hand applications over to you and track them. You submit; jaa records what you confirm.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals hold answers and notes
)

JobArg = Annotated[int, typer.Argument(help="Job id, see `jaa jobs list`.")]
NoteOpt = Annotated[str | None, typer.Option("--note", help="A note for the status history.")]
AllowDuplicateOpt = Annotated[
    bool,
    typer.Option(
        "--allow-duplicate",
        help="Continue although another prepared or submitted job looks like the same role.",
    ),
]

_REASONS: Final = {
    ConflictReason.CANONICAL_URL: "same link",
    ConflictReason.COMPANY_ROLE: "same employer and role",
}
_KINDS: Final = {
    ArtifactKind.CV_PDF: "CV",
    ArtifactKind.COVER_LETTER_PDF: "Cover letter",
}
_WARNINGS: Final = {
    HandoffWarning.ALREADY_SUBMITTED: "This application is already recorded as submitted.",
    HandoffWarning.NOT_STARTED: "Not started: `jaa apply start {job}` checks for duplicates first.",
    HandoffWarning.POSSIBLE_DUPLICATE: "Another prepared or submitted job looks like this role.",
    HandoffWarning.NO_LONGER_LISTED: "The source no longer lists this job; it may be closed.",
    HandoffWarning.NO_CV: "No CV built for this job yet: `jaa cv tailor propose {job}`.",
    HandoffWarning.DOCUMENT_MISSING: "A document file is missing; build it again.",
    HandoffWarning.DOCUMENT_CHANGED: "A document file changed since jaa built it.",
    HandoffWarning.QUESTIONS_NEED_INPUT: "Questions need your input: `jaa answers review {job}`.",
}


def open_url(url: str) -> None:
    """Open a link in the user's browser; tests replace this."""
    typer.launch(url)


@contextmanager
def _database(ctx: typer.Context) -> Generator[sqlite3.Connection]:
    conn = open_existing(settings_from(ctx))
    try:
        yield conn
    except JaaError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        if isinstance(exc, DuplicateApplicationError):
            err_console.print(
                "If they are different jobs, mark the flag with `jaa jobs duplicates mark`, "
                "or pass --allow-duplicate."
            )
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _when(at: datetime | None) -> str:
    return "-" if at is None else f"{at:%Y-%m-%d %H:%M} UTC"


def print_conflicts(conflicts: list[Conflict]) -> None:
    if not conflicts:
        return
    console.print(Text("Possible duplicate applications:", style="yellow"))
    for c in conflicts:
        state = "submitted" if c.submitted else c.job.status.value
        reasons = ", ".join(_REASONS[r] for r in c.reasons)
        console.print(
            Text(f"  #{c.job.id} {c.job.title} at {c.job.employer} ({state}) - {reasons}")
        )


def _status(text: str) -> JobStatus:
    try:
        return JobStatus(text.strip().casefold().replace("-", "_"))
    except ValueError:
        options = ", ".join(s.value for s in JobStatus if s is not JobStatus.SUBMITTED)
        raise ApplicationError(f"status must be one of {options}") from None


def _submitted_at(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ApplicationError("--at must be YYYY-MM-DD or YYYY-MM-DDTHH:MM") from None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


@apply_app.command()
def start(
    ctx: typer.Context,
    job: JobArg,
    allow_duplicate: AllowDuplicateOpt = False,
    note: NoteOpt = None,
) -> None:
    """Start preparing an application: check for duplicates and move the job to preparing."""
    with _database(ctx) as conn:
        try:
            started = service.start_application(
                conn, job, allow_duplicates=allow_duplicate, note=note
            )
        except DuplicateApplicationError:
            print_conflicts(service.find_application_conflicts(conn, job))
            raise
    if not started.created:
        console.print(f"Job #{job} already has an application ({started.job.status.value}).")
        print_conflicts(started.conflicts)
        return
    print_conflicts(started.conflicts)
    console.print(f"Started the application for job #{job} ({started.job.status.value}).")
    console.print(f"When your documents and answers are ready: `jaa apply handoff {job}`.")


def _print_documents(handoff: service.Handoff) -> None:
    if not handoff.documents:
        console.print("Documents: none built for this job.")
        return
    console.print("Documents:")
    for d in handoff.documents:
        label = _KINDS.get(d.artifact.kind, d.artifact.kind.value)
        state = "" if d.unchanged else " (changed since built)" if d.exists else " (missing)"
        line = Text(f"  {label} (document #{d.id}): {d.artifact.path}")
        line.append(state, style="yellow")
        console.print(line)


@apply_app.command()
def handoff(
    ctx: typer.Context,
    job: JobArg,
    open_page: Annotated[
        bool, typer.Option("--open", help="Open the application page in your browser.")
    ] = False,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show sensitive values.")] = False,
) -> None:
    """Show the link, documents and answers to take to the employer's application page."""
    with _database(ctx) as conn:
        found = service.handoff(conn, job)
    j = found.job
    console.print(Text(f"{j.title} - {j.employer}", style="bold"))
    console.print(f"Status: {j.status.value}")
    console.print(Text(f"Apply at: {found.url}"))
    _print_documents(found)
    if found.resolutions:
        console.print("Answers for the form (* = required):")
        console.print(resolution_table(found.resolutions, reveal=reveal))
    else:
        console.print(
            "No application questions recorded. Copy any from the form with "
            f'`jaa answers add-question {job} "QUESTION"`.'
        )
    print_conflicts(found.conflicts)
    for warning in found.warnings:
        console.print(Text(_WARNINGS[warning].format(job=job), style="yellow"))
    if open_page:
        if found.url.startswith(("https://", "http://")):
            open_url(found.url)
            console.print("Opened the application page. Fill in the form and submit it yourself.")
        else:
            err_console.print(Text(f"Not a web link, not opened: {found.url}"))
    if HandoffWarning.ALREADY_SUBMITTED not in found.warnings:
        console.print(f"After you submit, record it with `jaa apply submitted {job}`.")


@apply_app.command()
def submitted(
    ctx: typer.Context,
    job: JobArg,
    at: Annotated[
        str | None,
        typer.Option("--at", help="When you submitted, YYYY-MM-DD[THH:MM] (UTC). Default: now."),
    ] = None,
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="The employer's confirmation number or reference."),
    ] = None,
    document: Annotated[
        list[int] | None,
        typer.Option(
            "--document",
            help="Document id you sent (repeatable). Default: the newest CV and cover letter.",
        ),
    ] = None,
    note: NoteOpt = None,
    allow_duplicate: AllowDuplicateOpt = False,
) -> None:
    """Record that you submitted the application yourself. Always asks you to confirm."""
    with _database(ctx) as conn:
        found = service.handoff(conn, job)
        if HandoffWarning.ALREADY_SUBMITTED in found.warnings:
            raise ApplicationError(f"job #{job} is already recorded as submitted")
        print_conflicts(found.conflicts)
        # Asked every time, with no flag to skip it: a submission is recorded only on the
        # user's own word (SPEC §6 Human control).
        confirmed = typer.confirm(
            f"Did you submit your application for {found.job.title} at {found.job.employer} "
            "yourself?",
            default=False,
        )
        if not confirmed:
            console.print("Nothing recorded.")
            raise typer.Exit(code=1)
        stored = service.record_submission(
            conn,
            job,
            confirmation=SubmissionConfirmation.USER,
            submitted_at=_submitted_at(at),
            reference=reference,
            note=note,
            document_ids=document,
            allow_duplicates=allow_duplicate,
        )
        record = service.application_record(conn, job)
    console.print(
        f"[green]Recorded:[/green] job #{job} submitted {_when(stored.submitted_at)}, "
        f"{len(record.documents)} document(s), {len(record.answer_ids)} saved answer(s)."
    )


@apply_app.command()
def status(
    ctx: typer.Context,
    job: JobArg,
    new_status: Annotated[
        str,
        typer.Argument(
            metavar="STATUS",
            help="saved, preparing, ready_for_review, interviewing, rejected, withdrawn, "
            "closed or discovered. Use `jaa apply submitted` for submissions.",
        ),
    ],
    note: NoteOpt = None,
) -> None:
    """Change a job's application status; the change is kept in its history."""
    target = _status(new_status)
    with _database(ctx) as conn:
        changed = service.set_application_status(conn, job, target, note=note)
    console.print(f"Job #{job} is {changed.status.value}.")


@apply_app.command("list")
def list_applications(
    ctx: typer.Context,
    status_filter: Annotated[
        list[str] | None, typer.Option("--status", help="Only these statuses (repeatable).")
    ] = None,
) -> None:
    """List your applications, most recently changed first."""
    with _database(ctx) as conn:
        statuses = [_status(s) for s in status_filter or ()]
        found = service.list_applications(conn, statuses)
    if not found:
        console.print("No applications yet. Start one with `jaa apply start JOB`.")
        return
    table = Table("Job", "Title", "Company", "Status", "Since", "Submitted")
    for job, application in found:
        table.add_row(
            str(job.id),
            Text(job.title),
            Text(job.employer),
            job.status.value,
            _when(job.status_changed_at),
            _when(application.submitted_at),
        )
    console.print(table)


@apply_app.command()
def show(ctx: typer.Context, job: JobArg) -> None:
    """Show a job's application: status history, submission, documents and answers used."""
    with _database(ctx) as conn:
        record = service.application_record(conn, job)
    j, application = record.job, record.application
    console.print(Text(f"{j.title} - {j.employer}", style="bold"))
    console.print(f"Status: {j.status.value} since {_when(j.status_changed_at)}")
    if application is None:
        console.print(f"No application yet. Start one with `jaa apply start {job}`.")
    else:
        console.print(f"Started: {_when(application.created_at)}")
        if application.submitted_at is not None:
            by = application.submission_confirmed_by
            console.print(
                f"Submitted: {_when(application.submitted_at)} "
                f"(confirmed by {by.value if by else '-'})"
            )
            console.print(Text(f"Applied at: {application.source_url or '-'}"))
            if application.external_reference:
                console.print(Text(f"Reference: {application.external_reference}"))
        for artifact in record.documents:
            label = _KINDS.get(artifact.kind, artifact.kind.value)
            console.print(Text(f"{label} sent (document #{artifact.id}): {artifact.path}"))
        if record.answer_ids:
            ids = ", ".join(f"#{i}" for i in record.answer_ids)
            console.print(f"Saved answers used: {ids} (see `jaa answers list`).")
    history = Table("When", "From", "To", "By", "Note")
    for event in record.history:
        history.add_row(
            _when(event.occurred_at),
            event.from_status.value if event.from_status else "-",
            event.to_status.value,
            event.actor.value,
            Text(event.note or ""),
        )
    console.print(history)
