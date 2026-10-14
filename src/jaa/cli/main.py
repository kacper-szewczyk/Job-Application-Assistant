import logging
from typing import Annotated

import typer
from rich.console import Console

from jaa import __version__
from jaa.config import Settings, ensure_data_dir, load_settings
from jaa.core.errors import JaaError
from jaa.logging import setup_logging

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
    console.print(f"Database: {settings.database_path}")


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the local data directory with owner-only permissions."""
    settings = _settings(ctx)
    try:
        path = ensure_data_dir(settings)
    except (JaaError, OSError) as exc:
        err_console.print(f"[red]Could not create data directory:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    log.info("data directory ready (source=%s)", settings.data_dir_source)
    console.print(f"Data directory ready: {path}")
