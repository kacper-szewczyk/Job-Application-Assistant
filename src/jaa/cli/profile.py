"""`jaa profile …`: review imported facts, inspect provenance and profile versions (ADR 0006).

Values are printed to the terminal for the user to review, never logged. Sensitive values are
masked in listings unless `--reveal` is given; `show` always displays the requested fact.
"""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import (
    MASK,
    YesOpt,
    confirm_or_exit,
    console,
    err_console,
    open_existing,
    settings_from,
)
from jaa.core.errors import FactConflictError, JaaError
from jaa.core.models import FactOrigin, FactState, JsonValue, ProfileFact
from jaa.core.review import find_conflicts
from jaa.documents.linkedin_suggestions import SuggestionKind
from jaa.storage import profile as service
from jaa.storage.linkedin_profile import linkedin_suggestions
from jaa.storage.repository import import_runs, profile_facts, profile_versions

profile_app = typer.Typer(
    help="Review profile facts, their provenance and profile versions.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals may hold CV contents
)

MAX_CELL: Final = 60

RevealOption = Annotated[bool, typer.Option("--reveal", help="Show sensitive values.")]
ReplaceOption = Annotated[
    bool,
    typer.Option("--replace", help="Replace the value already confirmed for the same field."),
]


@contextmanager
def _database(ctx: typer.Context) -> Generator[sqlite3.Connection]:
    conn = open_existing(settings_from(ctx))
    try:
        yield conn
    except FactConflictError as exc:
        err_console.print(f"[red]Conflict:[/red] {escape(str(exc))}")
        err_console.print(
            "Compare them with `jaa profile conflicts`; pass --replace to use the new value."
        )
        raise typer.Exit(code=1) from exc
    except JaaError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _value(fact: ProfileFact, *, reveal: bool, truncate: bool = True) -> str:
    if fact.is_sensitive and not reveal:
        return MASK
    text = fact.value_raw.replace("\n", " ")
    if truncate and len(text) > MAX_CELL:
        return text[: MAX_CELL - 1] + "…"
    return text


def _source(fact: ProfileFact) -> str:
    if fact.origin is FactOrigin.CORRECTION:
        return f"correction of #{fact.corrected_from_fact_id}"
    if fact.origin is FactOrigin.USER:
        return "entered by you"
    where = f", {fact.source_location}" if fact.source_location else ""
    return f"import #{fact.import_run_id}{where}" if fact.import_run_id else f"import{where}"


def _confidence(fact: ProfileFact) -> str:
    return "-" if fact.confidence is None else f"{fact.confidence:.0%}"


@profile_app.command("facts")
def list_facts(
    ctx: typer.Context,
    state: Annotated[
        FactState | None, typer.Option("--state", help="Only facts in this state.")
    ] = None,
    reveal: RevealOption = False,
) -> None:
    """List profile facts with their state, confidence and source."""
    with _database(ctx) as conn:
        facts = profile_facts.list(conn, state=state) if state else profile_facts.list(conn)
    if not facts:
        console.print("No facts." if state is None else f"No {state} facts.")
        return
    table = Table("ID", "Field", "Value", "State", "Confidence", "Source")
    table.columns[1].overflow = "fold"  # field keys must stay readable in narrow terminals
    for fact in facts:
        table.add_row(
            str(fact.id),
            Text(fact.field_key),
            Text(_value(fact, reveal=reveal)),
            fact.state.value,
            _confidence(fact),
            Text(_source(fact)),
        )
    console.print(table)
    if not reveal and any(f.is_sensitive for f in facts):
        console.print("Sensitive values are hidden; pass --reveal to show them.")


@profile_app.command("show")
def show_fact(ctx: typer.Context, fact_id: Annotated[int, typer.Argument(help="Fact ID.")]) -> None:
    """Show one fact with its full provenance and review history."""
    with _database(ctx) as conn:
        chain = service.correction_chain(conn, fact_id)
        history = service.fact_history(conn, fact_id)
        runs = {
            f.import_run_id: import_runs.get(conn, f.import_run_id)
            for f in chain
            if f.import_run_id
        }

    fact = chain[0]
    console.print(f"[bold]Fact #{fact.id}[/bold]  {escape(fact.field_key)}")
    console.print(Text(f"Value:       {_value(fact, reveal=True, truncate=False)}"))
    if fact.value_normalized != fact.value_raw:
        normalized = json.dumps(fact.value_normalized, ensure_ascii=False)
        console.print(Text(f"Normalized:  {normalized}"))
    console.print(f"State:       {fact.state}")
    console.print(f"Confidence:  {_confidence(fact)}")
    console.print(f"Sensitive:   {'yes' if fact.is_sensitive else 'no'}")

    console.print("\n[bold]Provenance[/bold]")
    for depth, item in enumerate(chain):
        prefix = "  " * depth + ("↳ corrects " if depth else "")
        console.print(
            Text(f"{prefix}#{item.id} {item.origin}, recorded {item.created_at:%Y-%m-%d %H:%M} UTC")
        )
        indent = "  " * (depth + 1)
        if depth:
            console.print(Text(f"{indent}value: {_value(item, reveal=True)}"))
        run = runs.get(item.import_run_id) if item.import_run_id is not None else None
        if run is not None:
            path = f" {run.source_path}" if run.source_path else ""
            when = f"{run.imported_at:%Y-%m-%d %H:%M} UTC"
            console.print(Text(f"{indent}import #{run.id}: {run.source_type}{path}, {when}"))
        elif item.origin is FactOrigin.IMPORT:
            console.print(Text(f"{indent}import record deleted"))
        if item.source_location:
            console.print(Text(f"{indent}location: {item.source_location}"))
        if item.source_excerpt:
            console.print(Text(f"{indent}excerpt: “{item.source_excerpt}”"))

    console.print("\n[bold]Review history[/bold]")
    if fact.confirmed_at:
        console.print(f"Last confirmed: {fact.confirmed_at:%Y-%m-%d %H:%M} UTC")
    if not history:
        console.print("Not reviewed yet.")
    for event in history:
        origin = event.from_state or "created"
        version = (
            f" (profile version {event.profile_version_id})" if event.profile_version_id else ""
        )
        console.print(
            f"{event.occurred_at:%Y-%m-%d %H:%M} UTC  {origin} → {event.to_state}{version}"
        )


@profile_app.command("conflicts")
def show_conflicts(ctx: typer.Context, reveal: RevealOption = False) -> None:
    """Show competing values for the same field side by side."""
    with _database(ctx) as conn:
        facts = profile_facts.list(conn)
    conflicts = find_conflicts(facts)
    if not conflicts:
        console.print("No conflicts.")
        return
    for conflict in conflicts:
        table = Table("", "ID", "Value", "Confidence", "Source", title=Text(conflict.field_key))
        if conflict.current is not None:
            current = conflict.current
            table.add_row(
                "confirmed",
                str(current.id),
                Text(_value(current, reveal=reveal)),
                _confidence(current),
                Text(_source(current)),
            )
        for candidate in conflict.candidates:
            table.add_row(
                "incoming",
                str(candidate.id),
                Text(_value(candidate, reveal=reveal)),
                _confidence(candidate),
                Text(_source(candidate)),
            )
        console.print(table)
    console.print(
        "Keep the confirmed value with `jaa profile reject <incoming id>`, take an incoming one "
        "with `jaa profile confirm <id> --replace`, or enter the right value with "
        "`jaa profile edit <id> <value>`."
    )


def _report(changed: tuple[ProfileFact, ...], version_id: int | None) -> None:
    for fact in changed:
        console.print(f"Fact #{fact.id} ({escape(fact.field_key)}) is now {fact.state}.")
    if not changed:
        console.print("Nothing changed.")
    if version_id is not None:
        console.print(f"Profile version {version_id} created and active.")


@profile_app.command("confirm")
def confirm(
    ctx: typer.Context,
    fact_ids: Annotated[list[int], typer.Argument(help="Fact IDs to confirm.")],
    replace: ReplaceOption = False,
) -> None:
    """Confirm facts as correct. Creates a new profile version."""
    with _database(ctx) as conn:
        result = service.confirm_facts(conn, fact_ids, replace=replace)
    _report(result.changed, result.version.id if result.version else None)


@profile_app.command("reject")
def reject(
    ctx: typer.Context,
    fact_ids: Annotated[list[int], typer.Argument(help="Fact IDs to reject.")],
) -> None:
    """Reject facts as wrong. Rejecting a confirmed fact creates a new profile version."""
    with _database(ctx) as conn:
        result = service.reject_facts(conn, fact_ids)
    _report(result.changed, result.version.id if result.version else None)


@profile_app.command("edit")
def edit(
    ctx: typer.Context,
    fact_id: Annotated[int, typer.Argument(help="Fact ID to correct.")],
    value: Annotated[str, typer.Argument(help="The correct value.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Parse VALUE as JSON for the normalized value.")
    ] = False,
    replace: ReplaceOption = False,
) -> None:
    """Correct a fact. The correction is stored as a new confirmed fact; the original is kept."""
    normalized: JsonValue = value
    if as_json:
        try:
            normalized = json.loads(value)
        except json.JSONDecodeError as exc:
            err_console.print("[red]Error:[/red] VALUE is not valid JSON.")
            raise typer.Exit(code=1) from exc
    with _database(ctx) as conn:
        result = service.correct_fact(conn, fact_id, value, normalized, replace=replace)
    _report(result.changed, result.version.id if result.version else None)


@profile_app.command("delete")
def delete(
    ctx: typer.Context,
    fact_ids: Annotated[list[int], typer.Argument(help="Fact IDs to delete.")],
    yes: YesOpt = False,
) -> None:
    """Delete facts for good: their values, review history and place in profile versions.

    To mark a fact as wrong but keep a record of it, use `jaa profile reject` instead.
    """
    confirm_or_exit(
        f"Delete {len(fact_ids)} fact(s) and their history? This cannot be undone", yes=yes
    )
    with _database(ctx) as conn:
        result = service.delete_facts(conn, fact_ids)
    console.print(f"Deleted {result.facts} fact(s).")
    if result.version is not None:
        console.print(f"Profile version {result.version.id} created and active.")


@profile_app.command("versions")
def versions(ctx: typer.Context) -> None:
    """List profile versions. Generated documents record the version they used."""
    with _database(ctx) as conn:
        all_versions = profile_versions.list(conn)
        counts = service.version_fact_counts(conn)
        active = service.active_version_id(conn)
    if not all_versions:
        console.print("No profile versions yet. Confirm facts to create one.")
        return
    table = Table("Version", "Created (UTC)", "Confirmed facts", "Note", "")
    for version in reversed(all_versions):
        table.add_row(
            str(version.id),
            f"{version.created_at:%Y-%m-%d %H:%M}",
            str(counts.get(version.id or 0, 0)),
            Text(version.note or ""),
            "active" if version.id == active else "",
        )
    console.print(table)


_KIND_LABEL: Final = {
    SuggestionKind.ADD: "add",
    SuggestionKind.UPDATE: "update",
    SuggestionKind.CHECK: "check",
}


@profile_app.command("linkedin")
def linkedin(
    ctx: typer.Context,
    job: Annotated[
        list[int] | None,
        typer.Option(
            "--job", help="Compare with this job (repeatable). Default: saved and applied jobs."
        ),
    ] = None,
) -> None:
    """Suggest LinkedIn profile changes backed by your confirmed facts (read-only).

    Compares the latest LinkedIn import with your confirmed facts and the jobs you target.
    You make the changes on LinkedIn yourself; jaa never edits or opens LinkedIn.
    """
    with _database(ctx) as conn:
        report = linkedin_suggestions(conn, job or ())
    if report is None:
        console.print(
            "No LinkedIn import yet. Import your data archive with `jaa import linkedin`."
        )
        return
    run, found = report.run, report.result
    console.print(
        f"Compared LinkedIn import #{run.id} ({run.imported_at:%Y-%m-%d}) with your confirmed "
        f"facts and {len(report.jobs)} target job{'s' if len(report.jobs) != 1 else ''}."
    )
    if found.suggestions:
        table = Table("#", "Change", "Suggested wording", "LinkedIn now", "Based on", "Jobs")
        table.columns[1].overflow = "fold"
        for index, s in enumerate(found.suggestions, 1):
            table.add_row(
                str(index),
                Text(f"{_KIND_LABEL[s.kind]} {s.address}"),
                Text(s.text or "(remove it, or confirm it with `jaa profile`)"),
                Text(s.current or "-"),
                Text(", ".join(f"#{i}" for i in s.fact_ids) or "rejected value"),
                Text(", ".join(f"#{i}" for i in s.job_ids) or "-"),
            )
        console.print(table)
    else:
        console.print("Your LinkedIn profile already shows what your confirmed facts say.")
    if found.gaps:
        console.print("\nTarget jobs ask for these, and no confirmed fact shows them:")
        for gap in found.gaps:
            jobs_text = ", ".join(f"#{i}" for i in gap.job_ids)
            note = " (only unreviewed facts back it)" if gap.unreviewed_only else ""
            console.print(Text(f"  - {gap.requirement} [{gap.importance}] jobs {jobs_text}{note}"))
        console.print("jaa does not suggest claiming these; add a fact first if one is true.")
    if found.unreviewed_linkedin:
        console.print(
            f"{found.unreviewed_linkedin} LinkedIn value(s) are not reviewed yet; "
            "review them with `jaa profile facts --state unreviewed`."
        )
