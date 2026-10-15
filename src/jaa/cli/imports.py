"""`jaa import …`: import CVs, LinkedIn archives and text as reviewable facts; inspect runs.

Extracted values are never printed here; the user reviews them with `jaa profile facts`.
Issue messages may quote a short piece of the CV so the user can find what was not imported.
"""

import sqlite3
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import (
    YesOpt,
    confirm_or_exit,
    console,
    err_console,
    open_existing,
    settings_from,
)
from jaa.connectors.http import Transport
from jaa.connectors.urllib_transport import UrllibTransport
from jaa.core.clock import utcnow
from jaa.core.errors import ImportSourceError, JaaError
from jaa.core.models import (
    FactCandidate,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JsonValue,
    RetentionChoice,
)
from jaa.importers import linkedin_api
from jaa.importers.base import read_source
from jaa.importers.cv_file import prepare_cv_import
from jaa.importers.linkedin import INSTRUCTIONS, delete_sources, prepare_linkedin_import
from jaa.importers.text import prepare_text_import
from jaa.logging import Sensitive
from jaa.storage import linkedin_jobs, privacy
from jaa.storage.profile import ImportResult, delete_import, store_import
from jaa.storage.repository import import_runs
from jaa.storage.templates import list_template_candidates, register_template_candidate

import_app = typer.Typer(
    help="Import CVs and profile sources as facts to review.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals hold CV contents
)


@contextmanager
def _database(ctx: typer.Context) -> Generator[sqlite3.Connection]:
    conn = open_existing(settings_from(ctx))
    try:
        yield conn
    except JaaError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _print_issues(errors: list[JsonValue]) -> None:
    for issue in errors:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity", "error"))
        colour = "red" if severity == "error" else "yellow"
        where = f"{issue['location']}: " if issue.get("location") else ""
        line = Text(f"  {severity}: ", style=colour)
        line.append(f"{where}{issue.get('message', '')}")
        console.print(line)


def _sections_line(summary: dict[str, JsonValue]) -> Text | None:
    headings = summary.get("headings")
    if not isinstance(headings, list) or not headings:
        return None
    read: list[str] = []
    skipped: list[str] = []
    for heading in headings:
        if isinstance(heading, dict):
            title = str(heading.get("title", ""))
            (skipped if heading.get("section") == "unsupported" else read).append(title)
    text = Text("Sections:  ")
    text.append(", ".join(read) or "none recognized")
    if skipped:
        text.append(f"; not imported: {', '.join(skipped)}")
    return text


@import_app.command("cv")
def import_cv(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A PDF, DOCX or LaTeX (.tex) CV.")],
    as_template: Annotated[
        bool,
        typer.Option(
            "--as-template",
            help="Also keep a LaTeX CV's source as a template candidate for generated CVs.",
        ),
    ] = False,
) -> None:
    """Extract facts from a PDF, DOCX or LaTeX CV. Nothing is confirmed until you review it."""
    with _database(ctx) as conn:
        try:
            prepared = prepare_cv_import(path)
        except ImportSourceError as exc:
            err_console.print(f"[red]Cannot import:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        if as_template and prepared.template is None:
            err_console.print(
                "[red]Cannot import:[/red] --as-template needs a LaTeX source; "
                "this file is not LaTeX."
            )
            raise typer.Exit(code=2)
        result, earlier, conflicts = _store(conn, prepared.run, prepared.candidates)
        template = None
        if as_template and prepared.template is not None:
            template = register_template_candidate(
                conn, prepared.template, import_run_id=result.run.id
            )

    notes: list[str] = []
    if template is not None:
        candidate, created = template
        files = len(candidate.files)
        notes.append(
            f"{'Kept' if created else 'Already kept'} as template candidate #{candidate.id} "
            f"({candidate.document_class or 'unknown class'}, {files} "
            f"file{'s' if files != 1 else ''}); see `jaa import templates`."
        )
    _report(result, earlier, conflicts, notes)
    if result.run.status is ImportStatus.FAILED:
        raise typer.Exit(code=1)


def _store(
    conn: sqlite3.Connection, run: ImportRun, candidates: Sequence[FactCandidate]
) -> tuple[ImportResult, list[ImportRun], int]:
    """Store the run and facts; return the result, earlier runs of the source and conflicts."""
    stored = store_import(conn, run, candidates)
    return stored.result, list(stored.earlier), len(stored.conflicts)


def _report(
    result: ImportResult, earlier: list[ImportRun], conflicts: int, notes: Sequence[str] = ()
) -> None:
    run = result.run
    source = run.source_path or (
        "(LinkedIn API)" if run.source_type is ImportSourceType.LINKEDIN_API else "(pasted text)"
    )
    console.print(
        Text(
            f"Import #{run.id}: {run.source_type} {source} — {run.status}",
            style="bold",
        )
    )
    if earlier:
        ids = ", ".join(f"#{r.id}" for r in earlier)
        console.print(f"This source was imported before ({ids}); known values were skipped.")
    console.print(
        f"{len(result.added)} new fact{'s' if len(result.added) != 1 else ''} to review, "
        f"{result.duplicates} already known."
    )
    if (sections := _sections_line(run.summary)) is not None:
        console.print(sections)
    _print_skipped(run.summary)
    for note in notes:
        console.print(note)
    if run.errors:
        console.print(f"\n[bold]Issues ({len(run.errors)})[/bold]")
        _print_issues(run.errors)
    if result.added:
        console.print("\nReview them with `jaa profile facts --state unreviewed`.")
    if conflicts:
        console.print(
            f"{conflicts} field{'s' if conflicts != 1 else ''} now "
            f"{'have' if conflicts != 1 else 'has'} competing values; "
            "see `jaa profile conflicts`."
        )


_MAX_SKIPPED_SHOWN = 10


def _print_skipped(summary: dict[str, JsonValue]) -> None:
    skipped = summary.get("skipped_files")
    if not isinstance(skipped, list) or not skipped:
        return
    names = [str(n) for n in skipped]
    shown = ", ".join(names[:_MAX_SKIPPED_SHOWN])
    more = f" and {len(names) - _MAX_SKIPPED_SHOWN} more" if len(names) > _MAX_SKIPPED_SHOWN else ""
    console.print(
        Text(
            f"Skipped {len(names)} file{'s' if len(names) != 1 else ''} that are not profile "
            f"data: {shown}{more}."
        )
    )


@import_app.command("linkedin")
def import_linkedin(
    ctx: typer.Context,
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Your LinkedIn data archive (.zip), or CSV files from it. "
            "Omit to see how to download the archive.",
            show_default=False,
        ),
    ] = None,
    delete_source: Annotated[
        bool | None,
        typer.Option(
            "--delete-source/--keep-source",
            help="Delete the archive (or CSV files) after a successful import, or keep it. "
            "Asked when not given.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Import your own LinkedIn data archive. Nothing is confirmed until you review it.

    jaa reads only the files you give it: it never signs in to LinkedIn or opens profile URLs.
    """
    if not paths:
        console.print(INSTRUCTIONS)
        return
    if delete_source is None:
        names = ", ".join(p.name for p in paths)
        delete_source = typer.confirm(
            f"Delete {names} after a successful import? The extracted facts are kept in jaa "
            "either way",
            default=False,
        )
    retention = RetentionChoice.DELETE_AFTER_IMPORT if delete_source else RetentionChoice.KEEP
    with _database(ctx) as conn:
        try:
            prepared = prepare_linkedin_import(paths, retention=retention)
        except ImportSourceError as exc:
            err_console.print(f"[red]Cannot import:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        result, earlier, conflicts = _store(conn, prepared.run, prepared.candidates)
        run = result.run
        deletion_error: str | None = None
        if (
            retention is RetentionChoice.DELETE_AFTER_IMPORT
            and run.status is not ImportStatus.FAILED
        ):
            try:
                delete_sources(prepared.sources)
            except ImportSourceError as exc:
                deletion_error = str(exc)
            else:
                with conn:
                    run = import_runs.update(conn, run.id or 0, source_deleted_at=utcnow())

    _report(result, earlier, conflicts)
    if run.source_deleted_at is not None:
        console.print("Deleted the imported file(s) as requested.")
    elif retention is RetentionChoice.DELETE_AFTER_IMPORT:
        reason = deletion_error or "the import failed, so you can try again"
        console.print(Text(f"Kept the imported file(s): {reason}.", style="yellow"))
    else:
        console.print("Kept the imported file(s) where they are.")
    if run.status is ImportStatus.FAILED:
        raise typer.Exit(code=1)


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def _read_token() -> Sensitive[str]:
    """Ask for the token with hidden input. Never accepted from an argument, env var or pipe."""
    text: str = typer.prompt("LinkedIn access token (hidden)", hide_input=True)
    return linkedin_api.parse_token(text)


def _transport() -> Transport:
    return UrllibTransport()


def _print_jobs_from_linkedin(
    conn: sqlite3.Connection,
    prepared: linkedin_api.PreparedLinkedInApiImport,
    add_saved: bool | None,
) -> None:
    saved = prepared.saved_jobs
    if saved:
        console.print(f"\n[bold]Saved jobs on LinkedIn ({len(saved)})[/bold]")
        for job in saved:
            console.print(Text(f"  {job.title} — {job.company}"))
        if add_saved is None:
            add_saved = typer.confirm(
                f"Add the {len(saved)} saved job{'s' if len(saved) != 1 else ''} to jaa? "
                "You paste each description yourself; LinkedIn job pages are never opened",
                default=True,
            )
        if add_saved:
            added = linkedin_jobs.add_saved_jobs(conn, saved)
            console.print(
                f"Added {len(added.added)} job{'s' if len(added.added) != 1 else ''}"
                + (f" ({', '.join(f'#{i}' for i in added.added)})" if added.added else "")
                + (f"; {len(added.existing)} already in jaa" if added.existing else "")
                + (f"; {added.without_link} without a link skipped" if added.without_link else "")
                + "."
            )
            if added.flagged:
                console.print("Some look like jobs fetched before; see `jaa jobs duplicates list`.")
            if added.added:
                console.print(
                    Text(
                        "LinkedIn sends no job descriptions, so these jobs have no requirements "
                        "to match against yet.",
                        style="yellow",
                    )
                )
    if prepared.applications:
        apps = prepared.applications
        console.print(f"\n[bold]Applications on LinkedIn ({len(apps)})[/bold]")
        for job in apps:
            ids = linkedin_jobs.matching_jobs(conn, job)
            hint = f" — may be job {', '.join(f'#{i}' for i in ids)}" if ids else ""
            when = f" ({job.date})" if job.date else ""
            console.print(Text(f"  {job.title} — {job.company}{when}{hint}"))
        console.print(
            "jaa did not record these. To track one, run `jaa apply start JOB` and "
            "`jaa apply submitted JOB` yourself."
        )
    if prepared.job_alerts:
        console.print(f"\n[bold]Job alerts on LinkedIn ({len(prepared.job_alerts)})[/bold]")
        for alert in prepared.job_alerts:
            console.print(Text("  " + " · ".join(value for _, value in alert)))
        console.print("Use them as ideas for `jaa jobs list --save` searches.")
    if prepared.seeker_preferences:
        console.print("\n[bold]Job seeker preferences on LinkedIn[/bold]")
        for label, value in prepared.seeker_preferences:
            console.print(Text(f"  {label.capitalize()}: {value}"))
        console.print(
            "These were not saved. Set the ones you want with `jaa onboard` or "
            "`jaa preferences set`."
        )


@import_app.command("linkedin-api")
def import_linkedin_api(
    ctx: typer.Context,
    how: Annotated[bool, typer.Option("--how", help="Show how to get a token, then exit.")] = False,
    add_saved: Annotated[
        bool | None,
        typer.Option(
            "--add-saved-jobs/--no-saved-jobs",
            help="Add your LinkedIn saved jobs as jobs in jaa, or not. Asked when not given.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Import your LinkedIn profile through LinkedIn's data portability API (EEA/Switzerland).

    You paste your own self-serve access token when asked; it is used for this one import and
    never stored or logged. Nothing is confirmed until you review it.
    """
    if how:
        console.print(linkedin_api.INSTRUCTIONS)
        return
    if not _is_terminal():
        err_console.print(
            "[red]Cannot import:[/red] run this in your own terminal. The token is only "
            "accepted at the hidden prompt. See `jaa import linkedin-api --how`."
        )
        raise typer.Exit(code=2)
    with _database(ctx) as conn:
        try:
            token = _read_token()
            snapshot = linkedin_api.fetch_snapshot(token, _transport())
        except ImportSourceError as exc:
            err_console.print(f"[red]Cannot import:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        del token
        prepared = linkedin_api.prepare_linkedin_api_import(snapshot)
        result, earlier, conflicts = _store(conn, prepared.run, prepared.candidates)
        _report(result, earlier, conflicts)
        if prepared.empty_domains:
            console.print(
                Text(
                    "LinkedIn had no data for: "
                    + ", ".join(d.lower().replace("_", " ") for d in prepared.empty_domains)
                    + ". Empty sections are normal; if you just generated the token, LinkedIn "
                    "may still be preparing them, so run the import again later."
                )
            )
        _print_jobs_from_linkedin(conn, prepared, add_saved)
    if result.run.status is ImportStatus.FAILED:
        raise typer.Exit(code=1)


@import_app.command("text")
def import_text(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Argument(help="A UTF-8 text file. Omit to paste text instead.", show_default=False),
    ] = None,
) -> None:
    """Import pasted profile sections (e.g. copied from LinkedIn) or a plain-text CV.

    Use the usual headings (Experience, Education, Skills, …) so the sections are recognized.
    """
    with _database(ctx) as conn:
        try:
            if path is None:
                if sys.stdin.isatty():
                    err_console.print("Paste the text, then press Ctrl-D on an empty line.")
                text = sys.stdin.read()
                source = None
            else:
                source, data = read_source(path)
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ImportSourceError(f"not UTF-8 text: {source}") from exc
            prepared = prepare_text_import(text, source_path=source)
        except ImportSourceError as exc:
            err_console.print(f"[red]Cannot import:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        result, earlier, conflicts = _store(conn, prepared.run, prepared.candidates)
    _report(result, earlier, conflicts)
    if result.run.status is ImportStatus.FAILED:
        raise typer.Exit(code=1)


def _fact_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT import_run_id AS run, COUNT(*) AS n FROM profile_facts "
        "WHERE import_run_id IS NOT NULL GROUP BY import_run_id"
    ).fetchall()
    return {int(r["run"]): int(r["n"]) for r in rows}


@import_app.command("runs")
def list_runs(ctx: typer.Context) -> None:
    """List past imports with their status and number of issues."""
    with _database(ctx) as conn:
        runs = import_runs.list(conn)
        counts = _fact_counts(conn)
    if not runs:
        console.print("No imports yet. Try `jaa import cv <file>`.")
        return
    table = Table("ID", "Imported (UTC)", "Type", "Status", "Facts", "Issues", "Source")
    for run in reversed(runs):
        table.add_row(
            str(run.id),
            f"{run.imported_at:%Y-%m-%d %H:%M}",
            run.source_type.value,
            run.status.value,
            str(counts.get(run.id or 0, 0)),
            str(len(run.errors)),
            Text(run.source_path or ""),
        )
    console.print(table)


@import_app.command("show")
def show_run(
    ctx: typer.Context, run_id: Annotated[int, typer.Argument(help="Import run ID.")]
) -> None:
    """Show one import run: summary and every issue reported."""
    with _database(ctx) as conn:
        run: ImportRun = import_runs.get(conn, run_id)
        facts = _fact_counts(conn).get(run_id, 0)
    console.print(
        Text(f"Import #{run.id}: {run.source_type} {run.source_path or ''}", style="bold")
    )
    console.print(f"Imported:  {run.imported_at:%Y-%m-%d %H:%M} UTC")
    console.print(f"Status:    {run.status}")
    console.print(f"Retention: {run.retention.value.replace('_', ' ')}")
    console.print(f"Facts:     {facts} stored from this import")
    if run.source_deleted_at:
        console.print(f"Source deleted: {run.source_deleted_at:%Y-%m-%d %H:%M} UTC")
    if (headings := _sections_line(run.summary)) is not None:
        console.print(headings)
    sections = run.summary.get("sections")
    if isinstance(sections, dict) and sections:
        found = ", ".join(f"{name} {count}" for name, count in sections.items())
        console.print(Text(f"Found:     {found}"))
    _print_skipped(run.summary)
    if run.errors:
        console.print(f"\n[bold]Issues ({len(run.errors)})[/bold]")
        _print_issues(run.errors)
    else:
        console.print("No issues.")


RunArg = Annotated[int, typer.Argument(help="Import run ID (see `jaa import runs`).")]


@import_app.command("delete")
def delete_run(ctx: typer.Context, run_id: RunArg, yes: YesOpt = False) -> None:
    """Delete an import and every fact it added, confirmed or not. Your file is not touched."""
    with _database(ctx) as conn:
        facts = _fact_counts(conn).get(run_id, 0)
        import_runs.get(conn, run_id)
        confirm_or_exit(
            f"Delete import #{run_id} and the {facts} fact(s) it added? This cannot be undone",
            yes=yes,
        )
        result = delete_import(conn, run_id)
    console.print(f"Deleted import #{run_id} and {result.facts} fact(s).")
    if result.version is not None:
        console.print(f"Profile version {result.version.id} created and active.")


@import_app.command("delete-source")
def delete_source(ctx: typer.Context, run_id: RunArg, yes: YesOpt = False) -> None:
    """Delete the file(s) an import read, if unchanged since. The facts in jaa are kept."""
    with _database(ctx) as conn:
        paths = privacy.import_source_paths(import_runs.get(conn, run_id))
        names = ", ".join(str(p) for p in paths) or "nothing"
        confirm_or_exit(f"Delete {names}?", yes=yes)
        deleted = privacy.delete_import_source(conn, run_id)
    for path in deleted:
        console.print(Text(f"Deleted {path}."))


@import_app.command("templates")
def list_templates(ctx: typer.Context) -> None:
    """List LaTeX CVs kept as template candidates (`jaa import cv <file.tex> --as-template`)."""
    with _database(ctx) as conn:
        candidates = list_template_candidates(conn)
    if not candidates:
        console.print("No template candidates. Import a LaTeX CV with `--as-template`.")
        return
    table = Table("ID", "Kept (UTC)", "Class", "Main file", "Files", "Import")
    for candidate in candidates:
        table.add_row(
            str(candidate.id),
            f"{candidate.registered_at:%Y-%m-%d %H:%M}",
            Text(candidate.document_class or "—"),
            Text(candidate.main_file),
            str(len(candidate.files)),
            f"#{candidate.import_run_id}" if candidate.import_run_id else "—",
        )
    console.print(table)
