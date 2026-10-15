import logging
from pathlib import Path
from typing import Annotated

import typer

from jaa import __version__
from jaa.cli.answers import answers_app
from jaa.cli.apply import apply_app
from jaa.cli.common import YesOpt, console, err_console, open_existing, settings_from
from jaa.cli.cv import cv_app
from jaa.cli.imports import import_app
from jaa.cli.jobs import jobs_app
from jaa.cli.mcp import mcp_app
from jaa.cli.preferences import onboard, preferences_app
from jaa.cli.profile import profile_app
from jaa.cli.sources import sources_app
from jaa.cli.tailor import tailor_app
from jaa.config import ensure_data_dir, load_settings
from jaa.core.errors import DatabaseNotEmptyError, JaaError
from jaa.logging import setup_logging
from jaa.storage.db import connect, open_database, schema_version
from jaa.storage.export import backup_database, export_json, restore

log = logging.getLogger(__name__)

app = typer.Typer(
    name="jaa",
    help="Local-first Job Application Assistant.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals may hold CV contents or answers
)
app.add_typer(profile_app, name="profile")
app.add_typer(preferences_app, name="preferences")
app.add_typer(import_app, name="import")
app.add_typer(sources_app, name="sources")
app.add_typer(jobs_app, name="jobs")
cv_app.add_typer(tailor_app, name="tailor")
app.add_typer(cv_app, name="cv")
app.add_typer(answers_app, name="answers")
app.add_typer(apply_app, name="apply")
app.add_typer(mcp_app, name="mcp")
app.command()(onboard)


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
    settings = settings_from(ctx)
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
    settings = settings_from(ctx)
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


@app.command()
def backup(
    ctx: typer.Context,
    dest: Annotated[Path, typer.Argument(help="Where to write the SQLite backup file.")],
    force: OverwriteOption = False,
) -> None:
    """Write a consistent copy of the database (owner-only permissions)."""
    conn = open_existing(settings_from(ctx))
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
    conn = open_existing(settings_from(ctx))
    try:
        path = export_json(conn, dest.expanduser().resolve(), overwrite=force)
    except JaaError as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    console.print(f"Export written: {path}")
    console.print("It contains your personal data. Store it somewhere private.")


@app.command("restore")
def restore_command(
    ctx: typer.Context,
    src: Annotated[Path, typer.Argument(help="A file written by `jaa backup` or `jaa export`.")],
    force: Annotated[
        bool, typer.Option("--force", help="Replace a database that already has records.")
    ] = False,
    yes: YesOpt = False,
) -> None:
    """Restore the database from a backup or export.

    A database that has records is replaced only with --force, and a copy of it is written to
    the `backups` folder in the data directory first. Stop `jaa mcp serve` before restoring.
    """
    settings = settings_from(ctx)
    if (
        force
        and not yes
        and not typer.confirm(
            "Replace your current records with the ones in this file? A copy is kept first",
            default=False,
        )
    ):
        console.print("Nothing restored.")
        raise typer.Exit(code=1)
    try:
        ensure_data_dir(settings)
        result = restore(settings.database_path, src, replace=force)
    except JaaError as exc:
        err_console.print(f"[red]Restore failed:[/red] {exc}")
        if isinstance(exc, DatabaseNotEmptyError):
            err_console.print("Pass --force to replace them.")
        raise typer.Exit(code=1) from exc
    console.print(f"Restored {result.rows} record(s) from the {result.kind}.")
    if result.previous is not None:
        console.print(f"Your previous database was saved to {result.previous}.")
