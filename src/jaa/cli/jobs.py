"""`jaa jobs …`: fetch listings from approved sources, then search, inspect and dedupe the
stored jobs (T10/T11, ADR 0013 and 0014)."""

import sqlite3
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
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
from jaa.core.models import DuplicateVerdict, Job, JobStatus
from jaa.core.search import JobQuery
from jaa.storage import duplicates as dupes
from jaa.storage import listings as service
from jaa.storage import searches
from jaa.storage.repository import jobs as jobs_table

jobs_app = typer.Typer(
    help="Fetch job listings from approved sources and list stored jobs.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
duplicates_app = typer.Typer(
    help="Review jobs that may be the same job. Nothing is merged.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
searches_app = typer.Typer(
    help="Run and manage saved job searches.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
jobs_app.add_typer(duplicates_app, name="duplicates")
jobs_app.add_typer(searches_app, name="searches")


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
    if report.changes.flagged:
        console.print(
            Text(
                f"  {len(report.changes.flagged)} possible duplicate(s) flagged; "
                "see `jaa jobs duplicates list`.",
                style="yellow",
            )
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


TextOpt = Annotated[
    str | None, typer.Option("--query", "-q", help="Words that must all appear (any order).")
]
CompanyOpt = Annotated[str | None, typer.Option("--company", help="Employer name contains.")]
LocationOpt = Annotated[str | None, typer.Option("--location", help="Location contains.")]
SourceOpt = Annotated[list[str] | None, typer.Option("--source", help="Only this source.")]
StatusOpt = Annotated[
    list[JobStatus] | None, typer.Option("--status", help="Only jobs with this status.")
]
RemovedOpt = Annotated[
    bool, typer.Option("--include-removed", help="Also show jobs no longer listed.")
]
LimitOpt = Annotated[int, typer.Option("--limit", min=1, help="Show at most this many.")]


def _query(
    text: str | None,
    company: str | None,
    location: str | None,
    source: list[str] | None,
    status: list[JobStatus] | None,
    include_removed: bool,
) -> JobQuery:
    return JobQuery(
        text=(text or "").strip() or None,
        company=(company or "").strip() or None,
        location=(location or "").strip() or None,
        sources=tuple(dict.fromkeys(source or ())),
        statuses=tuple(dict.fromkeys(status or ())),
        include_removed=include_removed,
    )


def _status_text(job: Job, partners: list[int]) -> str:
    status = job.status.value + (" (removed)" if job.removed_at else "")
    if partners:
        status += "; duplicate? " + ", ".join(f"#{p}" for p in partners)
    return status


def _jobs_table(
    found: list[Job],
    partners: dict[int, list[int]],
    limit: int,
    new_ids: frozenset[int] = frozenset(),
) -> None:
    table = Table("ID", "Title", "Company", "Location", "Source", "First seen", "Status")
    for job in found[:limit]:
        job_id = job.id or 0
        marker = Text(" new", style="green") if job_id in new_ids else ""
        table.add_row(
            str(job_id),
            Text.assemble(job.title, marker),
            Text(job.employer),
            Text(job.location or "-"),
            job.source,
            f"{job.first_seen_at:%Y-%m-%d}",
            _status_text(job, partners.get(job_id, [])),
        )
    console.print(table)
    if len(found) > limit:
        console.print(f"{len(found) - limit} more; use --limit to see them.")


@jobs_app.command("list")
def list_jobs(
    ctx: typer.Context,
    text: TextOpt = None,
    company: CompanyOpt = None,
    location: LocationOpt = None,
    source: SourceOpt = None,
    status: StatusOpt = None,
    include_removed: RemovedOpt = False,
    limit: LimitOpt = 50,
    save: Annotated[
        str | None, typer.Option("--save", help="Also save these filters under this name.")
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="With --save: replace a search of that name.")
    ] = False,
) -> None:
    """Search stored jobs, most recently found first.

    Matching ignores case, Polish and other diacritics, and punctuation.
    """
    query = _query(text, company, location, source, status, include_removed)
    with _database(ctx) as conn:
        if save is not None:
            saved = searches.save_search(conn, save, query, replace=replace)
            console.print(f"Saved search {saved.name!r}: {escape(query.describe())}.")
        found = searches.search_jobs(conn, query)
        partners = dupes.flagged_job_ids(conn)
        empty = not found and jobs_table.list(conn) == []
    if empty:
        console.print("No jobs stored yet. Fetch some with `jaa jobs fetch <source> <target>`.")
        return
    if not found:
        console.print(f"No jobs match {escape(query.describe())}.")
        return
    _jobs_table(found, partners, limit)


def _when(at: datetime | None) -> str:
    return "never" if at is None else f"{at:%Y-%m-%d %H:%M} UTC"


@jobs_app.command()
def show(
    ctx: typer.Context,
    job_id: Annotated[int, typer.Argument(help="Job ID from `jaa jobs list`.")],
) -> None:
    """Show one job with its source link(s) and possible duplicates."""
    with _database(ctx) as conn:
        job = jobs_table.get(conn, job_id)
        flags = dupes.flags_for_job(conn, job_id)
    console.print(Text(job.title, style="bold"))
    console.print(Text(f"{job.employer}  ·  {job.location or 'location not given'}"))
    console.print(f"Status: {job.status.value}")
    seen = f"First seen {_when(job.first_seen_at)}, last seen {_when(job.last_seen_at)}"
    if job.removed_at:
        seen += f", no longer listed since {_when(job.removed_at)}"
    console.print(seen + ".")
    console.print("Source links:")
    console.print(Text(f"  {job.source}: {job.source_url or job.canonical_url}"))
    for flag in flags:
        if flag.verdict is DuplicateVerdict.SAME_JOB:
            other = flag.partner(job_id)
            console.print(Text(f"  {other.source}: {other.source_url or other.canonical_url}"))
    open_flags = [f for f in flags if f.verdict is None]
    if open_flags:
        console.print(Text("Possible duplicates (unreviewed):", style="yellow"))
        for flag in open_flags:
            _print_partner(flag, job_id)
    if job.description:
        console.print()
        console.print(Text(job.description))


def _print_partner(flag: dupes.DuplicateFlag, job_id: int) -> None:
    other = flag.partner(job_id)
    console.print(
        Text(
            f"  flag {flag.id}: #{other.id} {other.title} at {other.employer} "
            f"({other.source}, {other.status.value}) - {_reason(flag)}"
        )
    )
    console.print(Text(f"    {other.source_url or other.canonical_url}"))


def _reason(flag: dupes.DuplicateFlag) -> str:
    return {
        "canonical_url": "same link",
        "employer_title_location": "same employer, title and location",
    }[flag.flag.reason.value]


@duplicates_app.command("list")
def list_duplicates(
    ctx: typer.Context,
    include_reviewed: Annotated[
        bool, typer.Option("--all", help="Also show flags you have already decided on.")
    ] = False,
) -> None:
    """List pairs of jobs that may be the same job."""
    with _database(ctx) as conn:
        flags = dupes.list_flags(conn, include_reviewed=include_reviewed)
    if not flags:
        console.print("No possible duplicates to review.")
        return
    table = Table("Flag", "Job", "Other job", "Why", "Verdict")
    for flag in flags:
        table.add_row(
            str(flag.id),
            _pair_cell(flag.job),
            _pair_cell(flag.other),
            _reason(flag),
            flag.verdict.value.replace("_", " ") if flag.verdict else "-",
        )
    console.print(table)
    busy = [f for f in flags if {f.job.status, f.other.status} & dupes.IN_PROGRESS]
    if busy:
        console.print(
            Text(
                f"{len(busy)} flag(s) involve a job you are already working on or applied to; "
                "check them before applying again.",
                style="yellow",
            )
        )
    console.print("Decide with `jaa jobs duplicates mark <flag> same|different`.")


def _pair_cell(job: Job) -> Text:
    return Text(f"#{job.id} {job.title}\n{job.employer} · {job.source} · {job.status.value}")


VERDICTS = {"same": DuplicateVerdict.SAME_JOB, "different": DuplicateVerdict.DIFFERENT_JOBS}


@duplicates_app.command("mark")
def mark_duplicate(
    ctx: typer.Context,
    flag_id: Annotated[int, typer.Argument(help="Flag from `jaa jobs duplicates list`.")],
    verdict: Annotated[str, typer.Argument(help="`same` (one job) or `different` (two jobs).")],
) -> None:
    """Record whether a flagged pair is the same job. Neither job is changed or merged."""
    chosen = VERDICTS.get(verdict.strip().lower())
    if chosen is None:
        err_console.print("[red]Error:[/red] the verdict is `same` or `different`.")
        raise typer.Exit(code=2)
    with _database(ctx) as conn:
        flag = dupes.get_flag(conn, flag_id)
        dupes.review_flag(conn, flag_id, chosen)
    if chosen is DuplicateVerdict.SAME_JOB:
        console.print(
            f"Marked #{flag.job.id} and #{flag.other.id} as the same job; "
            "each shows the other's source link."
        )
    else:
        console.print(f"Marked #{flag.job.id} and #{flag.other.id} as different jobs.")


@searches_app.command("list")
def list_searches(ctx: typer.Context) -> None:
    """List saved searches and when each was last refreshed."""
    with _database(ctx) as conn:
        saved = searches.list_searches(conn)
        fetched = searches.last_fetched(conn)
    if not saved:
        console.print("No saved searches. Save one with `jaa jobs list … --save <name>`.")
        return
    table = Table("Name", "Filters", "Last refreshed", "Results")
    for search in saved:
        table.add_row(
            Text(search.name),
            Text(JobQuery.from_json(search.query).describe()),
            _when(search.last_refreshed_at),
            "-" if search.last_result_count is None else str(search.last_result_count),
        )
    console.print(table)
    console.print(f"Jobs last fetched: {_when(fetched)}.")


@searches_app.command("run")
def run_search(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Saved search name.")],
    limit: LimitOpt = 50,
) -> None:
    """Run a saved search; jobs first seen since its last run are marked new."""
    with _database(ctx) as conn:
        result = searches.run_search(conn, name)
        partners = dupes.flagged_job_ids(conn)
    console.print(
        Text(
            f"{result.search.name}: {result.query.describe()}. {len(result.jobs)} job(s), "
            f"{len(result.new_since_last)} new since {_when(result.previous_refresh)}. "
            f"Jobs last fetched: {_when(result.data_as_of)}."
        )
    )
    if result.jobs:
        _jobs_table(result.jobs, partners, limit, result.new_since_last)


@searches_app.command("delete")
def delete_search(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Saved search name.")],
) -> None:
    """Delete a saved search. Jobs are not affected."""
    with _database(ctx) as conn:
        searches.delete_search(conn, name)
    console.print(f"Deleted saved search {name.strip()!r}.")
