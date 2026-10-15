"""Build and run the review UI server (ADR 0022)."""

import logging
import socket
import webbrowser
from collections.abc import Callable

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from jaa.config import Settings
from jaa.ui.pages import CsrfError, Pages
from jaa.ui.security import GuardMiddleware, UiSession, check_loopback

log = logging.getLogger(__name__)


def build_app(settings: Settings, session: UiSession) -> Starlette:
    """The ASGI app for one UI session. The database must exist and be migrated."""
    pages = Pages(settings, session)

    async def refused(request: Request, exc: Exception) -> Response:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>jaa</title>"
            "<p>This form has expired. Reload the page and try again.</p>",
            status_code=403,
        )

    routes = [
        Route("/login", pages.login),
        Route("/static/app.css", pages.stylesheet),
        Route("/", pages.overview),
        Route("/profile", pages.profile),
        Route("/profile/decide", pages.decide, methods=["POST"]),
        Route("/profile/facts/{fact_id:int}", pages.fact),
        Route("/profile/facts/{fact_id:int}/correct", pages.correct, methods=["POST"]),
        Route("/jobs", pages.job_list),
        Route("/jobs/{job_id:int}", pages.job),
        Route("/jobs/{job_id:int}/answers", pages.answer, methods=["POST"]),
        Route("/jobs/{job_id:int}/questions", pages.add_question, methods=["POST"]),
        Route("/jobs/{job_id:int}/start", pages.start, methods=["POST"]),
        Route("/jobs/{job_id:int}/status", pages.status, methods=["POST"]),
        Route("/jobs/{job_id:int}/submitted", pages.submitted, methods=["POST"]),
        Route("/duplicates/{flag_id:int}", pages.duplicate, methods=["POST"]),
        Route("/applications", pages.applications),
        Route("/documents", pages.documents),
        Route("/files/{artifact_id:int}", pages.file),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(GuardMiddleware, session=session)],
        exception_handlers={CsrfError: refused},
    )


def bind(host: str, port: int) -> socket.socket:
    """Open the listening socket on a loopback address (port 0 picks a free one)."""
    check_loopback(host)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def serve(
    settings: Settings,
    sock: socket.socket,
    session: UiSession,
    *,
    open_browser: bool,
    announce: Callable[[str], None],
) -> None:
    """Serve until interrupted. `announce` shows the one-time link to the user."""
    app = build_app(settings, session)
    config = uvicorn.Config(
        app,
        # Request lines are not logged: the first one carries the login token.
        access_log=False,
        log_config=None,
        log_level="warning",
        server_header=False,
        date_header=False,
    )
    announce(session.login_url)
    if open_browser:
        webbrowser.open(session.login_url)
    log.info("ui serving port=%d", session.port)
    uvicorn.Server(config).run(sockets=[sock])
