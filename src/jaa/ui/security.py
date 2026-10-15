"""Who may talk to the UI server: loopback only, the user's browser only (ADR 0022).

- The server binds to a loopback address; anything else is refused before binding.
- The `Host` header must name that address and port, which stops DNS rebinding pages.
- A one-time login token (in the link `jaa ui` prints) is swapped for an HttpOnly,
  SameSite=Strict session cookie; every page except the login and the stylesheet needs it.
- Every form post carries a CSRF token, and a cross-origin `Origin` is refused.
- Responses forbid scripts, framing by other origins, caching and referrers.
"""

import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from jaa.core.errors import ConfigError

COOKIE: Final = "jaa_ui"
LOGIN_PATH: Final = "/login"
PUBLIC_PATHS: Final = frozenset({LOGIN_PATH, "/static/app.css"})
# Generated PDFs, served for the browser's own viewer.
FILES_PREFIX: Final = "/files/"

# No scripts at all: the pages are plain HTML forms. PDFs open in a same-origin frame.
PAGE_CSP: Final = (
    "default-src 'none'; style-src 'self'; img-src 'self'; frame-src 'self'; "
    "form-action 'self'; frame-ancestors 'self'; base-uri 'none'"
)
# The browser's PDF viewer needs its own resources; the file may only be framed by the UI.
DOCUMENT_CSP: Final = "frame-ancestors 'self'"

type Endpoint = Callable[[Request], Awaitable[Response]]


def check_loopback(host: str) -> str:
    """Return `host` if it is a loopback IP address; refuse anything else.

    Names are refused too: what `localhost` resolves to is up to the system's resolver.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError(f"the UI only binds to a loopback address, not {host!r}") from exc
    if not address.is_loopback:
        raise ConfigError(f"the UI only binds to a loopback address, not {host}")
    return host


@dataclass(frozen=True, slots=True)
class Flash:
    kind: str  # "ok" or "bad"
    text: str


@dataclass(slots=True, kw_only=True)
class UiSession:
    """The secrets of one `jaa ui` run. They live in memory only and die with the server."""

    host: str
    port: int
    login_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    logged_in: bool = False
    flashes: list[Flash] = field(default_factory=list[Flash], repr=False)

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def login_url(self) -> str:
        return f"{self.origin}{LOGIN_PATH}?token={self.login_token}"

    @property
    def localhost_origin(self) -> str:
        return f"http://localhost:{self.port}"

    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({self.origin.removeprefix("http://"), f"localhost:{self.port}"})

    def redeem(self, token: str) -> bool:
        """Swap the one-time login token for the session; a second use is refused."""
        if self.logged_in or not secrets.compare_digest(token, self.login_token):
            return False
        self.logged_in = True
        return True

    def is_authenticated(self, request: Request) -> bool:
        cookie = request.cookies.get(COOKIE, "")
        return self.logged_in and secrets.compare_digest(cookie, self.session_id)

    def check_csrf(self, request: Request, token: str) -> bool:
        origin = request.headers.get("origin")
        if origin is not None and origin not in (self.origin, self.localhost_origin):
            return False
        return secrets.compare_digest(token, self.csrf)

    def flash(self, kind: str, text: str) -> None:
        self.flashes.append(Flash(kind, text))

    def take_flashes(self) -> list[Flash]:
        taken, self.flashes = self.flashes, []
        return taken


def _secure(response: Response, *, document: bool) -> Response:
    headers = response.headers
    headers["Content-Security-Policy"] = DOCUMENT_CSP if document else PAGE_CSP
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "SAMEORIGIN"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Cache-Control"] = "no-store"
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


def _refusal(status: int, text: str) -> Response:
    body = (
        "<!doctype html><meta charset='utf-8'><title>jaa</title>"
        f"<link rel='stylesheet' href='/static/app.css'><main class='refusal'><p>{text}</p></main>"
    )
    return _secure(HTMLResponse(body, status_code=status), document=False)


class GuardMiddleware(BaseHTTPMiddleware):
    """Refuses foreign hosts and requests without the session, and secures every response."""

    def __init__(self, app: object, session: UiSession) -> None:
        super().__init__(app)  # pyright: ignore[reportArgumentType] - Starlette passes the ASGI app
        self.session = session

    async def dispatch(self, request: Request, call_next: Endpoint) -> Response:
        if request.headers.get("host", "") not in self.session.allowed_hosts():
            return _refusal(400, "This address is not the jaa UI.")
        path = request.url.path
        if path not in PUBLIC_PATHS and not self.session.is_authenticated(request):
            return _refusal(401, "Open the link that <code>jaa ui</code> printed in your terminal.")
        response = await call_next(request)
        return _secure(response, document=path.startswith(FILES_PREFIX))
