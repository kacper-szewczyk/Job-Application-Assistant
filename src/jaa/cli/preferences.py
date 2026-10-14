"""`jaa onboard` and `jaa preferences …`: the preference interview and later edits (ADR 0007).

Every answer is saved as soon as it is given, so quitting part-way keeps progress. Values are
printed for the user only, never logged; sensitive values are masked in listings unless
`--reveal` is given.
"""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import MASK, console, err_console, open_existing, settings_from
from jaa.core.errors import JaaError, PreferenceError
from jaa.core.models import JsonValue, Preference, PreferenceStatus
from jaa.core.preferences import (
    QUESTIONS,
    Question,
    format_value,
    is_sensitive_question,
    parse_answer,
    question,
)
from jaa.storage import preferences as service

preferences_app = typer.Typer(
    help="Show and change your search and application preferences.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals may hold salary or eligibility answers
)

RevealOption = Annotated[bool, typer.Option("--reveal", help="Show sensitive values.")]
KeyArgument = Annotated[str, typer.Argument(help="Preference key, see `jaa preferences list`.")]


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


def _shown(q: Question, value: JsonValue, *, reveal: bool) -> str:
    if is_sensitive_question(q) and not reveal:
        return MASK
    return format_value(q, value)


def _ask(q: Question, current: Preference | None) -> tuple[PreferenceStatus, JsonValue] | None:
    """Prompt until the answer parses. None means keep the current answer."""
    console.print(f"[bold]{escape(q.prompt)}[/bold]")
    for number, choice in enumerate(q.choices, start=1):
        console.print(Text(f"  {number}. {choice.label}"))
    console.print(Text(f"  ({q.hint})", style="dim"))
    keeps = current is not None and current.status is PreferenceStatus.ANSWERED
    if current is not None and keeps:
        console.print(Text(f"  Current: {_shown(q, current.value, reveal=False)}"))
        console.print("  Press Enter to keep it, or type a new answer.")
    else:
        console.print("  Press Enter to skip.")
    while True:
        text: str = typer.prompt(">", default="", show_default=False, prompt_suffix=" ")
        if not text.strip():
            return None if keeps else (PreferenceStatus.SKIPPED, None)
        try:
            return PreferenceStatus.ANSWERED, parse_answer(q, text)
        except PreferenceError as exc:
            err_console.print(f"[red]{escape(str(exc))}[/red]")


def onboard(
    ctx: typer.Context,
    revisit: Annotated[
        bool, typer.Option("--all", help="Ask every question again, including answered ones.")
    ] = False,
) -> None:
    """Interview for search and application preferences. Any question can be skipped."""
    with _database(ctx) as conn:
        stored = service.list_preferences(conn)
        pending = set(service.unanswered_keys(conn))
        todo = [q for q in QUESTIONS if revisit or q.key in pending]
        if not todo:
            console.print("All preference questions are answered.")
            console.print("Revisit them with `jaa onboard --all` or `jaa preferences set`.")
            return
        console.print(
            "Your answers are saved as you go and stay on this computer. "
            "Skip anything you like and answer it later with `jaa onboard`."
        )
        answered = skipped = 0
        for number, q in enumerate(todo, start=1):
            console.print(f"\n[dim]{number}/{len(todo)}[/dim]", end=" ")
            result = _ask(q, stored.get(q.key))
            if result is None:
                answered += 1
                continue
            status, value = result
            if status is PreferenceStatus.SKIPPED:
                service.skip_preference(conn, q.key)
                skipped += 1
            else:
                service.set_preference(conn, q.key, value)
                answered += 1
    console.print(f"\nDone: {answered} answered, {skipped} skipped.")
    console.print("Review them with `jaa preferences list`.")


@preferences_app.command("list")
def list_preferences(ctx: typer.Context, reveal: RevealOption = False) -> None:
    """List every preference question with its current answer."""
    with _database(ctx) as conn:
        stored = service.list_preferences(conn)
    table = Table("Key", "Answer", "Updated (UTC)", "Question")
    table.columns[0].no_wrap = True  # keys are typed back into `set`, so keep them whole
    for q in QUESTIONS:
        pref = stored.get(q.key)
        if pref is None:
            answer, updated = Text("not asked yet", style="dim"), ""
        elif pref.status is PreferenceStatus.SKIPPED:
            answer, updated = Text("skipped", style="dim"), f"{pref.updated_at:%Y-%m-%d %H:%M}"
        else:
            answer = Text(_shown(q, pref.value, reveal=reveal))
            updated = f"{pref.updated_at:%Y-%m-%d %H:%M}"
        table.add_row(Text(q.key), answer, updated, Text(q.prompt))
    console.print(table)
    if not reveal and any(is_sensitive_question(q) for q in QUESTIONS if q.key in stored):
        console.print("Sensitive values are hidden; pass --reveal to show them.")


@preferences_app.command("set")
def set_preference(
    ctx: typer.Context,
    key: KeyArgument,
    value: Annotated[str, typer.Argument(help="Your answer, in the format the question shows.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="VALUE is the normalized JSON value.")
    ] = False,
) -> None:
    """Answer or change one preference."""
    with _database(ctx) as conn:
        q = question(key)
        if as_json:
            try:
                parsed: JsonValue = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PreferenceError("VALUE is not valid JSON") from exc
        else:
            parsed = parse_answer(q, value)
        service.set_preference(conn, key, parsed)
    console.print(Text(f"Saved {key}: {_shown(q, parsed, reveal=False)}"))


@preferences_app.command("skip")
def skip_preference(ctx: typer.Context, key: KeyArgument) -> None:
    """Mark a preference as skipped, clearing its answer. Earlier answers stay in history."""
    with _database(ctx) as conn:
        service.skip_preference(conn, key)
    console.print(Text(f"Skipped {key}."))


@preferences_app.command("history")
def history(ctx: typer.Context, key: KeyArgument, reveal: RevealOption = False) -> None:
    """Show every answer and skip recorded for a preference."""
    with _database(ctx) as conn:
        q = question(key)
        events = service.preference_history(conn, key)
    if not events:
        console.print(Text(f"No history for {key}."))
        return
    for event in events:
        shown = (
            "skipped"
            if event.status is PreferenceStatus.SKIPPED
            else _shown(q, event.value, reveal=reveal)
        )
        console.print(Text(f"{event.occurred_at:%Y-%m-%d %H:%M} UTC  {shown}"))
