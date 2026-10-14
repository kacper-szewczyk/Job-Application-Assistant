"""`jaa import …`: import CV files as reviewable facts and inspect past import runs.

Extracted values are never printed here; the user reviews them with `jaa profile facts`.
Issue messages may quote a short piece of the CV so the user can find what was not imported.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import console, err_console, open_existing, settings_from
from jaa.core.errors import ImportSourceError, JaaError
from jaa.core.models import ImportRun, ImportStatus, JsonValue
from jaa.core.review import find_conflicts
from jaa.importers.cv_file import prepare_cv_import
from jaa.storage.profile import add_imported_facts
from jaa.storage.repository import import_runs, profile_facts

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


@import_app.command("cv")
def import_cv(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A PDF or DOCX CV.")],
) -> None:
    """Extract facts from a PDF or DOCX CV. Nothing is confirmed until you review it."""
    with _database(ctx) as conn:
        try:
            prepared = prepare_cv_import(path)
        except ImportSourceError as exc:
            err_console.print(f"[red]Cannot import:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        earlier = import_runs.list(conn, source_sha256=prepared.run.source_sha256)
        result = add_imported_facts(conn, prepared.run, prepared.candidates)
        added_keys = {f.field_key for f in result.added}
        conflicts = [
            c for c in find_conflicts(profile_facts.list(conn)) if c.field_key in added_keys
        ]

    run = result.run
    console.print(
        Text(f"Import #{run.id}: {run.source_type} {run.source_path} — {run.status}", style="bold")
    )
    if earlier:
        ids = ", ".join(f"#{r.id}" for r in earlier)
        console.print(f"This file was imported before ({ids}); known values were skipped.")
    console.print(
        f"{len(result.added)} new fact{'s' if len(result.added) != 1 else ''} to review, "
        f"{result.duplicates} already known."
    )
    if run.errors:
        console.print(f"\n[bold]Issues ({len(run.errors)})[/bold]")
        _print_issues(run.errors)
    if result.added:
        console.print("\nReview them with `jaa profile facts --state unreviewed`.")
    if conflicts:
        console.print(
            f"{len(conflicts)} field{'s' if len(conflicts) != 1 else ''} now "
            f"{'have' if len(conflicts) != 1 else 'has'} competing values; "
            "see `jaa profile conflicts`."
        )
    if run.status is ImportStatus.FAILED:
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
    console.print(f"Facts:     {facts} stored from this import")
    if run.source_deleted_at:
        console.print(f"Source deleted: {run.source_deleted_at:%Y-%m-%d %H:%M} UTC")
    sections = run.summary.get("sections")
    if isinstance(sections, dict) and sections:
        found = ", ".join(f"{name} {count}" for name, count in sections.items())
        console.print(Text(f"Found:     {found}"))
    if run.errors:
        console.print(f"\n[bold]Issues ({len(run.errors)})[/bold]")
        _print_issues(run.errors)
    else:
        console.print("No issues.")
