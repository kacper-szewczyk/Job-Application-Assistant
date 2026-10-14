import io
import logging

import pytest

from jaa.logging import REDACTED, Sensitive, scrub, setup_logging


@pytest.mark.parametrize(
    "secret",
    [
        "jane.doe@example.com",
        "+48 601 234 567",
        "(555) 123-4567",
        "Bearer abcdefghijklmnop123",
        "api_key=abc123secret",
        "password: hunter2",
        "ghp_" + "a" * 36,
        "sk-ant-" + "b" * 30,
        "AKIA" + "C" * 16,
    ],
)
def test_scrub_redacts_sensitive_patterns(secret: str) -> None:
    out = scrub(f"value is {secret} here")
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    ["imported 12 facts", "posted 2026-10-14", "job 4821 status saved", "version 0.1.0"],
)
def test_scrub_keeps_ordinary_text(text: str) -> None:
    assert scrub(text) == text


def test_sensitive_never_renders() -> None:
    value = Sensitive("Senior Engineer at Acme")
    assert "Acme" not in str(value)
    assert "Acme" not in repr(value)
    assert "Acme" not in f"{value}"
    assert value.reveal() == "Senior Engineer at Acme"


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    setup_logging("DEBUG", stream=stream)
    return stream


def test_log_lines_are_scrubbed(log_stream: io.StringIO) -> None:
    log = logging.getLogger("jaa.test")
    log.info("contact %s, phone %s", "jane.doe@example.com", "+48 601 234 567")
    log.info("title %s", Sensitive("Staff Engineer at Acme"))
    out = log_stream.getvalue()
    assert "jane.doe@example.com" not in out
    assert "601 234 567" not in out
    assert "Acme" not in out
    assert out.count(REDACTED) == 3


def test_timestamp_prefix_is_kept(log_stream: io.StringIO) -> None:
    logging.getLogger("jaa.test").warning("hello")
    first = log_stream.getvalue().split()[0]
    assert first[:2] == "20"
    assert REDACTED not in log_stream.getvalue()


def test_tracebacks_are_scrubbed(log_stream: io.StringIO) -> None:
    log = logging.getLogger("jaa.test")
    try:
        raise ValueError("bad email jane.doe@example.com")
    except ValueError:
        log.exception("import failed")
    out = log_stream.getvalue()
    assert "Traceback" in out
    assert "jane.doe@example.com" not in out


def test_setup_logging_is_idempotent() -> None:
    setup_logging("INFO", stream=io.StringIO())
    setup_logging("INFO", stream=io.StringIO())
    assert len(logging.getLogger("jaa").handlers) == 1
