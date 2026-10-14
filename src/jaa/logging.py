"""Logging setup with scrubbing.

The primary rule is to log IDs and counts, never values (CLAUDE.md). Scrubbing is a
safety net for mistakes: it redacts values wrapped in `Sensitive` and common
personal-data and secret patterns from every formatted log line, including tracebacks.
"""

import logging
import re
import sys
from dataclasses import dataclass
from types import TracebackType
from typing import Final, TextIO, override

type OptExcInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
)

REDACTED: Final = "[REDACTED]"

_PATTERNS: Final = (
    # Email addresses
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # Bearer/basic auth headers
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # key=value style secrets
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|cookie)\b\s*[:=]\s*\S+"),
    # Prefixed tokens (GitHub, Anthropic, OpenAI-style, Slack, AWS access keys)
    re.compile(
        r"\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
    ),
)

# Phone number candidates; only redacted when they contain enough digits (see _redact_phone),
# so dates like 2026-10-14 and small counts stay readable.
_PHONE_CANDIDATE: Final = re.compile(r"(?<![\w.])\+?\d[\d \-().]{7,}\d(?![\w.])")
_PHONE_MIN_DIGITS: Final = 9


def _redact_phone(match: re.Match[str]) -> str:
    digits = sum(ch.isdigit() for ch in match.group())
    return REDACTED if digits >= _PHONE_MIN_DIGITS else match.group()


@dataclass(frozen=True, slots=True)
class Sensitive[T]:
    """Wraps a value so it never renders in logs, reprs or f-strings."""

    value: T

    def reveal(self) -> T:
        return self.value

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return f"Sensitive({REDACTED})"

    def __format__(self, format_spec: str) -> str:
        return REDACTED


def scrub(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _PHONE_CANDIDATE.sub(_redact_phone, text)


class ScrubbingFormatter(logging.Formatter):
    """Scrubs the message, traceback and stack, but not the timestamp/level prefix."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        # Work on a copy so other handlers still see the original record.
        record = logging.makeLogRecord(record.__dict__)
        record.msg = scrub(record.getMessage())
        record.args = None
        record.exc_text = None
        return super().format(record)

    @override
    def formatException(self, ei: OptExcInfo) -> str:
        return scrub(super().formatException(ei))

    @override
    def formatStack(self, stack_info: str) -> str:
        return scrub(super().formatStack(stack_info))


LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str, stream: TextIO | None = None) -> logging.Handler:
    """Configure the `jaa` logger tree to write scrubbed lines to stderr."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(ScrubbingFormatter(LOG_FORMAT))

    logger = logging.getLogger("jaa")
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handler
