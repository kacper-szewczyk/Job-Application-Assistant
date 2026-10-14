"""`jaa sources …`: inspect job source policies and pause or resume sources (ADR 0011)."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import console, err_console, open_existing, settings_from
from jaa.connectors import registry
from jaa.connectors.base import Connector, SourceState, effective_state, policy_summary
from jaa.core.errors import JaaError, NotFoundError
from jaa.core.models import SourceStatus, StatusActor
from jaa.storage import sources as service

sources_app = typer.Typer(
    help="Show job sources, their access policies, and pause or resume them.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

KeyArgument = Annotated[str, typer.Argument(help="Source key, see `jaa sources list`.")]


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


def _connector(key: str) -> Connector:
    connector = registry.available_connectors().get(key)
    if connector is None:
        raise NotFoundError(f"unknown source {key!r}; see `jaa sources list`")
    return connector


def _state(conn: sqlite3.Connection, connector: Connector) -> SourceState:
    return effective_state(
        connector.policy, service.latest_status_event(conn, connector.policy.key)
    )


def _describe(state: SourceState) -> Text:
    if state.status is SourceStatus.ACTIVE:
        return Text("active", style="green")
    label = "paused by policy" if state.locked else "paused"
    return Text(f"{label}: {state.reason or 'no reason given'}", style="yellow")


@sources_app.command("list")
def list_sources(ctx: typer.Context) -> None:
    """List the job sources jaa can read and whether each one is active."""
    with _database(ctx) as conn:
        connectors = registry.available_connectors()
        if not connectors:
            console.print("No job sources are available yet.")
            return
        table = Table("Key", "Name", "Access", "Status")
        for key, connector in sorted(connectors.items()):
            policy = connector.policy
            table.add_row(
                key, policy.name, policy.access_method.value, _describe(_state(conn, connector))
            )
        console.print(table)


@sources_app.command()
def show(ctx: typer.Context, key: KeyArgument) -> None:
    """Show a source's access policy, status and pause history."""
    with _database(ctx) as conn:
        connector = _connector(key)
        table = Table("Policy", "Value", show_header=False)
        for label, value in policy_summary(connector.policy).items():
            table.add_row(label, Text(value))
        table.add_row("Status", _describe(_state(conn, connector)))
        console.print(table)
        history = service.status_history(conn, key)
        if history:
            console.print("History:")
        for event in history:
            line = (
                f"  {event.occurred_at:%Y-%m-%d %H:%M} {event.status.value} by {event.actor.value}"
            )
            console.print(Text(line + (f": {event.reason}" if event.reason else "")))


@sources_app.command()
def pause(
    ctx: typer.Context,
    key: KeyArgument,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why, e.g. 'terms changed'.")
    ] = None,
) -> None:
    """Stop jaa from fetching from a source until you resume it."""
    with _database(ctx) as conn:
        _connector(key)
        event = service.set_source_status(conn, key, SourceStatus.PAUSED, StatusActor.USER, reason)
        console.print(f"{key} paused." if event else f"{key} was already paused.")


@sources_app.command()
def resume(ctx: typer.Context, key: KeyArgument) -> None:
    """Allow fetching from a paused source again. Policy pauses cannot be resumed here."""
    with _database(ctx) as conn:
        connector = _connector(key)
        state = _state(conn, connector)
        if state.locked:
            err_console.print(
                Text(f"{key} is paused by its source policy: {state.reason}. "),
                "It can only be resumed by updating the connector after its terms are reviewed.",
            )
            raise typer.Exit(code=1)
        event = service.set_source_status(conn, key, SourceStatus.ACTIVE, StatusActor.USER)
        console.print(f"{key} resumed." if event else f"{key} was not paused.")
