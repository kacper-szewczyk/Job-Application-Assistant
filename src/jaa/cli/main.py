import logging
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from jaa import __version__
from jaa.config import Settings, ensure_data_dir, load_settings
from jaa.core.errors import JaaError
from jaa.logging import setup_logging
from jaa.storage.db import connect, open_database, schema_version
from jaa.storage.export import backup_database, export_json

log = logging.getLogger(__name__)
# soft_wrap keeps long paths on one line so they stay copyable.
console = Console(soft_wrap=True)
err_console = Console(stderr=True)

app = typer.Typer(
    name="jaa",
    help="Local-first Job Application Assistant.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals may hold CV contents or answers
)


def _settings(ctx: typer.Context) -> Settings:
    settings = ctx.obj
    if not isinstance(settings, Settings):
        raise RuntimeError("CLI context was not initialised by the main callback")
    return settings


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jaa {__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    # Handled entirely by the eager callback.
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    try:
        settings = load_settings()
    except JaaError as exc:
        err_console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    setup_logging(settings.log_level)
    ctx.obj = settings


@app.command()
def info(ctx: typer.Context) -> None:
    """Show version, data directory and database location."""
    settings = _settings(ctx)
    state = "exists" if settings.data_dir.is_dir() else "not created yet, run `jaa init`"
    console.print(f"jaa {__version__}")
    console.print(f"Data directory: {settings.data_dir} ({settings.data_dir_source}, {state})")
    if settings.database_path.is_file():
        conn = connect(settings.database_path)
        try:
            version = schema_version(conn)
        finally:
            conn.close()
        console.print(f"Database: {settings.database_path} (schema version {version})")
    else:
        console.print(f"Database: {settings.database_path} (not created yet)")


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the data directory and database, and apply pending migrations."""
    settings = _settings(ctx)
    try:
        path = ensure_data_dir(settings)
        conn = open_database(settings.database_path)
    except (JaaError, OSError) as exc:
        err_console.print(f"[red]Initialisation failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    try:
        version = schema_version(conn)
    finally:
        conn.close()
    log.info("data directory ready (source=%s)", settings.data_dir_source)
    console.print(f"Data directory ready: {path}")
    console.print(f"Database ready: {settings.database_path} (schema version {version})")


OverwriteOption = Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")]


def _open_existing(settings: Settings) -> sqlite3.Connection:
    if not settings.database_path.is_file():
        err_console.print("[red]No database yet.[/red] Run `jaa init` first.")
        raise typer.Exit(code=1)
    return open_database(settings.database_path)


@app.command()
def backup(
    ctx: typer.Context,
    dest: Annotated[Path, typer.Argument(help="Where to write the SQLite backup file.")],
    force: OverwriteOption = False,
) -> None:
    """Write a consistent copy of the database (owner-only permissions)."""
    conn = _open_existing(_settings(ctx))
    try:
        path = backup_database(conn, dest.expanduser().resolve(), overwrite=force)
    except JaaError as exc:
        err_console.print(f"[red]Backup failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    console.print(f"Backup written: {path}")
    console.print("It contains your personal data. Store it somewhere private.")


@app.command()
def export(
    ctx: typer.Context,
    dest: Annotated[Path, typer.Argument(help="Where to write the JSON export.")],
    force: OverwriteOption = False,
) -> None:
    """Export all records as JSON (see docs/export-format.md)."""
    conn = _open_existing(_settings(ctx))
    try:
        path = export_json(conn, dest.expanduser().resolve(), overwrite=force)
    except JaaError as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    console.print(f"Export written: {path}")
    console.print("It contains your personal data. Store it somewhere private.")
