"""The real network `Transport`, on the standard library's urllib (ADR 0011, ADR 0013).

It performs plain GETs and adds nothing the `SourceClient` does not ask for: no cookies, no
stored credentials, no proxies from the environment beyond what urllib reads itself. Redirects
are followed only to the same scheme and host as the request. Any other redirect is handed back
with its target as the response URL, so the client sees where the source tried to send it (a
login page, a bot check) and treats it as access denied without the request ever being made.
"""

import http.client
import ssl
import urllib.error
import urllib.request
from email.message import Message
from typing import Final, override
from urllib.parse import urljoin, urlsplit

from jaa.connectors.http import HttpRequest, HttpResponse, TransportError

MAX_REDIRECTS: Final = 5


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port


class _OffOriginRedirectError(Exception):
    def __init__(self, status: int, location: str, headers: Message) -> None:
        super().__init__(location)
        self.status = status
        self.location = location
        self.headers = headers


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urljoin(req.full_url, newurl)
        if _origin(target) != _origin(req.full_url):
            close = getattr(fp, "close", None)
            if callable(close):
                close()
            raise _OffOriginRedirectError(code, target, headers)
        return super().redirect_request(req, fp, code, msg, headers, target)  # pyright: ignore[reportArgumentType] - typeshed wants IO; urllib passes the response


def _headers(message: Message) -> dict[str, str]:
    return {name.lower(): value for name, value in message.items()}


def _read(response: http.client.HTTPResponse | urllib.error.HTTPError, max_bytes: int) -> bytes:
    # One byte more than allowed, so the client can tell an oversized body from a full one.
    try:
        return response.read(max_bytes + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(f"reading the response failed: {type(exc).__name__}") from exc


class UrllibTransport:
    """Sends GET requests with urllib. Errors become `TransportError`; HTTP statuses do not."""

    def __init__(self, *, context: ssl.SSLContext | None = None) -> None:
        handlers: list[urllib.request.BaseHandler] = [
            _SameOriginRedirects(),
            urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        ]
        self._opener = urllib.request.build_opener(*handlers)

    def send(self, request: HttpRequest, *, timeout: float, max_bytes: int) -> HttpResponse:
        if urlsplit(request.url).scheme not in ("https", "http"):
            raise TransportError("only http(s) URLs can be fetched")
        req = urllib.request.Request(request.url, headers=dict(request.headers), method="GET")  # noqa: S310 - scheme checked above
        try:
            with self._opener.open(req, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    url=response.url,
                    headers=_headers(response.headers),
                    body=_read(response, max_bytes),
                )
        except _OffOriginRedirectError as exc:
            return HttpResponse(status=exc.status, url=exc.location, headers=_headers(exc.headers))
        except urllib.error.HTTPError as exc:
            with exc:
                return HttpResponse(
                    status=exc.code,
                    url=exc.url or request.url,
                    headers=_headers(exc.headers),
                    body=_read(exc, max_bytes),
                )
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            raise TransportError(f"request failed: {type(exc).__name__}") from exc
