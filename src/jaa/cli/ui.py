"""`jaa ui`: the local review UI in the browser (T18, ADR 0022)."""

from typing import Annotated

import typer

from jaa.cli.common import console, err_console, open_existing, settings_from
from jaa.core.errors import ConfigError


def ui(
    ctx: typer.Context,
    port: Annotated[
        int, typer.Option("--port", min=0, max=65535, help="Port to use. Default: a free one.")
    ] = 0,
    host: Annotated[
        str, typer.Option("--host", help="Loopback address to bind to (127.0.0.1 or ::1).")
    ] = "127.0.0.1",
    browser: Annotated[
        bool, typer.Option("--browser/--no-browser", help="Open the UI in your browser.")
    ] = True,
) -> None:
    """Review facts, CVs, answers and applications in your browser. Stays on this computer.

    The server listens on this computer only and needs the one-time link it prints. Press
    Ctrl+C to stop it.
    """
    # Imported here so other commands do not load the web server.
    from jaa.ui.app import bind, serve
    from jaa.ui.security import UiSession

    settings = settings_from(ctx)
    open_existing(settings).close()  # creates nothing; applies pending migrations
    try:
        sock = bind(host, port)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except OSError as exc:
        err_console.print(f"[red]Cannot listen on port {port}:[/red] {exc.strerror}")
        raise typer.Exit(code=1) from exc
    session = UiSession(host=host, port=int(sock.getsockname()[1]))

    def announce(url: str) -> None:
        console.print("jaa review UI, on this computer only. Open this link (it works once):")
        console.print(url, highlight=False)
        console.print("Press Ctrl+C to stop.")

    try:
        serve(settings, sock, session, open_browser=browser, announce=announce)
    finally:
        sock.close()
