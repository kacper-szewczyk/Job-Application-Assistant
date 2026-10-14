"""Types shared by the CV importers: located text lines, issues and the extraction result.

Extractors (pdf, docx) turn a file into `SourceLine`s; `jaa.importers.cv` turns lines into
fact candidates. Neither writes to storage.
"""

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from jaa.core.errors import ImportSourceError
from jaa.core.models import JsonObject, JsonValue

# A CV is a few pages; anything far larger is a mistake or a hostile file.
MAX_SOURCE_BYTES: Final = 25 * 1024 * 1024


class IssueSeverity(StrEnum):
    # Part of the source could not be read at all.
    ERROR = "error"
    # The source was read, but some of its content did not become facts.
    WARNING = "warning"


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportIssue:
    """Something the user should know about an import. Stored in `ImportRun.errors`."""

    code: str
    severity: IssueSeverity
    message: str = field(repr=False)
    location: str | None = None

    def to_json(self) -> JsonObject:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceLine:
    """One line of text with where it came from (e.g. `page 2`, `paragraph 14`, `header`)."""

    text: str = field(repr=False)
    location: str
    # Set by extractors that know the document structure (DOCX heading styles, numbering).
    is_heading: bool = False
    is_bullet: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedText:
    lines: tuple[SourceLine, ...]
    issues: tuple[ImportIssue, ...] = ()
    # Counts for the import summary, e.g. {"pages": 2}.
    stats: JsonObject = field(default_factory=dict[str, JsonValue])


def read_source(path: Path) -> tuple[Path, bytes]:
    """Resolve and read a user-supplied file, checking that it is a regular file of sane size."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ImportSourceError(f"file not found: {path}") from exc
    if not resolved.is_file():
        raise ImportSourceError(f"not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise ImportSourceError(f"file is empty: {resolved}")
    if size > MAX_SOURCE_BYTES:
        raise ImportSourceError(
            f"file is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB: {resolved}"
        )
    return resolved, resolved.read_bytes()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
