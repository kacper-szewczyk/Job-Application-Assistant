"""`jaa jobs …`: fetch listings from approved sources and list the stored jobs (T10, ADR 0013)."""

import sqlite3
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import console, err_console, open_existing, settings_from
from jaa.connectors import registry
from jaa.connectors.http import Transport
from jaa.connectors.urllib_transport import UrllibTransport
from jaa.core.errors import JaaError, NotFoundError, SourceAccessDeniedError
from jaa.storage import listings as service
from jaa.storage.repository import jobs as jobs_table

jobs_app = typer.Typer(
    help="Fetch job listings from approved sources and list stored jobs.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def make_transport() -> Transport:
    """The network transport; tests replace this with a recorded one."""
    return UrllibTransport()


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


def _report(source: str, report: service.FetchReport) -> None:
    run = report.run
    name = f"{source} {run.target}"
    if report.error is not None:
        err_console.print(Text(f"{name}: {report.error}", style="red"))
        if isinstance(report.error, SourceAccessDeniedError) and report.paused:
            err_console.print(
                f"{source} is now paused. Check the source's terms and status, then run "
                f"`jaa sources resume {source}` if it may be used again."
            )
        return
    console.print(
        f"{name}: {run.listings} listing(s); {run.new} new, {run.changed} changed, "
        f"{run.unchanged} unchanged, {run.reappeared} back again, {run.removed} removed "
        f"({run.requests} request(s))."
    )
    if report.issues:
        counts = Counter(i.code for i in report.issues)
        summary = ", ".join(f"{code} x{n}" for code, n in sorted(counts.items()))
        console.print(Text(f"  {len(report.issues)} issue(s): {summary}", style="yellow"))


@jobs_app.command()
def fetch(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source key, see `jaa sources list`.")],
    targets: Annotated[
        list[str],
        typer.Argument(help="What to fetch from the source, e.g. solid.jobs divisions: IT HR."),
    ],
) -> None:
    """Fetch every listing of each target and store new, changed and removed jobs."""
    failed = False
    with _database(ctx) as conn:
        connector = registry.available_connectors().get(source)
        if connector is None:
            raise NotFoundError(f"unknown source {source!r}; see `jaa sources list`")
        transport = make_transport()
        for target in targets:
            report = service.fetch_and_store(conn, connector, target, transport=transport)
            _report(source, report)
            if report.error is not None:
                failed = True
                if report.paused:
                    break
    if failed:
        raise typer.Exit(code=1)


@jobs_app.command("list")
def list_jobs(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option("--source", help="Only this source.")] = None,
    include_removed: Annotated[
        bool, typer.Option("--include-removed", help="Also show jobs no longer listed.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Show at most this many.")] = 50,
) -> None:
    """List stored jobs, newest first."""
    with _database(ctx) as conn:
        found = jobs_table.list(conn, source=source) if source else jobs_table.list(conn)
    shown = [j for j in reversed(found) if include_removed or j.removed_at is None]
    if not shown:
        console.print("No jobs stored yet. Fetch some with `jaa jobs fetch <source> <target>`.")
        return
    table = Table("ID", "Title", "Company", "Location", "First seen", "Status")
    for job in shown[:limit]:
        status = job.status.value + (" (removed)" if job.removed_at else "")
        table.add_row(
            str(job.id),
            Text(job.title),
            Text(job.employer),
            Text(job.location or "-"),
            f"{job.first_seen_at:%Y-%m-%d}",
            status,
        )
    console.print(table)
    if len(shown) > limit:
        console.print(f"{len(shown) - limit} more; use --limit to see them.")
