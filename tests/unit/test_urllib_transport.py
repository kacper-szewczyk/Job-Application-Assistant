"""The urllib transport against a server on the loopback interface; nothing leaves the machine."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, override

import pytest

from jaa.connectors.http import HttpRequest, HttpResponse, TransportError
from jaa.connectors.urllib_transport import UrllibTransport


class _Handler(BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []
    headers_seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        self.paths.append(self.path)
        self.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.path == "/json":
            self._send(200, b'{"ok": true}', "application/json")
        elif self.path == "/missing":
            self._send(404, b"not here", "text/plain")
        elif self.path == "/same-origin":
            self._redirect("/json")
        elif self.path == "/elsewhere":
            self._redirect("https://login.example.test/signin")
        elif self.path == "/big":
            self._send(200, b"x" * 1000, "text/plain")
        else:
            self._send(500, b"", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @override
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.paths.clear()
    _Handler.headers_seen.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str, max_bytes: int = 10_000) -> HttpResponse:
    request = HttpRequest(url=url, headers={"User-Agent": "jaa-test", "Accept": "application/json"})
    return UrllibTransport().send(request, timeout=5, max_bytes=max_bytes)


def test_success(server: str) -> None:
    response = _get(f"{server}/json")
    assert (response.status, response.body) == (200, b'{"ok": true}')
    assert response.headers["content-type"] == "application/json"
    assert response.url == f"{server}/json"
    assert _Handler.headers_seen[0]["user-agent"] == "jaa-test"
    assert "cookie" not in _Handler.headers_seen[0]


def test_http_errors_are_responses(server: str) -> None:
    response = _get(f"{server}/missing")
    assert (response.status, response.body) == (404, b"not here")


def test_same_origin_redirects_are_followed(server: str) -> None:
    response = _get(f"{server}/same-origin")
    assert (response.status, response.url) == (200, f"{server}/json")
    assert _Handler.paths == ["/same-origin", "/json"]


def test_other_redirects_are_reported_not_followed(server: str) -> None:
    response = _get(f"{server}/elsewhere")
    # The client sees the off-policy host and treats it as access denied.
    assert (response.status, response.url) == (302, "https://login.example.test/signin")
    assert _Handler.paths == ["/elsewhere"]


def test_body_is_read_one_byte_past_the_limit(server: str) -> None:
    assert len(_get(f"{server}/big", max_bytes=100).body) == 101


def test_connection_failures_are_transport_errors(server: str) -> None:
    closed = server.rsplit(":", 1)[0] + ":9"  # discard port; nothing listens there
    with pytest.raises(TransportError):
        _get(f"{closed}/json")


def test_only_http_urls() -> None:
    with pytest.raises(TransportError, match="only http"):
        _get("file:///etc/hostname")
