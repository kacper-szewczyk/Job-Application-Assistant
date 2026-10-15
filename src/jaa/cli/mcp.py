"""`jaa mcp …`: run the MCP server that gives Claude Code typed access to jaa (T16, ADR 0019)."""

import typer

mcp_app = typer.Typer(
    help="Run the MCP server for Claude Code.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


@mcp_app.command()
def serve() -> None:
    """Serve jaa's tools over stdio. Claude Code starts this; see README for registering it."""
    # Imported here so other commands do not load the MCP SDK.
    from jaa.mcp.server import serve as run

    run()
