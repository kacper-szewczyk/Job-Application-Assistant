"""Helpers shared by CLI command modules."""

import sqlite3
from typing import Final

import typer
from rich.console import Console

from jaa.config import Settings
from jaa.storage.db import open_database

# Shown instead of sensitive values in listings.
MASK: Final = "•••• (sensitive)"

# soft_wrap keeps long paths on one line so they stay copyable.
console = Console(soft_wrap=True)
err_console = Console(stderr=True)


def settings_from(ctx: typer.Context) -> Settings:
    # Subcommand groups get a child context; the settings live on the root one.
    settings = ctx.find_root().obj
    if not isinstance(settings, Settings):
        raise RuntimeError("CLI context was not initialised by the main callback")
    return settings


def open_existing(settings: Settings) -> sqlite3.Connection:
    if not settings.database_path.is_file():
        err_console.print("[red]No database yet.[/red] Run `jaa init` first.")
        raise typer.Exit(code=1)
    return open_database(settings.database_path)
