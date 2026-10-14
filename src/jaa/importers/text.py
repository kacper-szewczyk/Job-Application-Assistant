"""Import pasted text, e.g. profile sections copied from LinkedIn or a CV (SPEC §5 fallback).

The text goes through the same parser as CV files (`jaa.importers.cv.parse_cv`), so it needs
the usual section headings (Experience, Education, Skills, …) to yield more than contact
details. Lines are located as `line N`.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from jaa.core.clock import utcnow
from jaa.core.errors import ImportSourceError
from jaa.core.models import ImportRun, ImportSourceType, ImportStatus, JsonValue, RetentionChoice
from jaa.importers.base import ImportIssue, IssueSeverity, SourceLine, sha256
from jaa.importers.cv import parse_cv
from jaa.importers.cv_file import PreparedImport

log = logging.getLogger(__name__)

MAX_TEXT_CHARS: Final = 200_000


def text_lines(text: str) -> list[SourceLine]:
    return [
        SourceLine(text=line.strip(), location=f"line {number}")
        for number, line in enumerate(text.splitlines(), 1)
        if line.strip()
    ]


def prepare_text_import(
    text: str, *, source_path: Path | None = None, at: datetime | None = None
) -> PreparedImport:
    """Parse pasted or file-provided text into fact candidates and build the import run."""
    if not text.strip():
        raise ImportSourceError("no text to import")
    if len(text) > MAX_TEXT_CHARS:
        raise ImportSourceError(f"text is longer than {MAX_TEXT_CHARS} characters")
    at = at or utcnow()
    lines = text_lines(text)
    parsed = parse_cv(lines)
    issues = parsed.issues
    if not parsed.candidates:
        issues += (
            ImportIssue(
                code="no_facts",
                severity=IssueSeverity.ERROR,
                message="No profile facts could be extracted from this text.",
            ),
        )
    status = (
        ImportStatus.FAILED
        if not parsed.candidates
        else (ImportStatus.PARTIAL if issues else ImportStatus.SUCCEEDED)
    )
    run = ImportRun(
        source_type=ImportSourceType.TEXT,
        source_path=str(source_path) if source_path else None,
        source_sha256=sha256(text.encode()),
        imported_at=at,
        status=status,
        summary={
            "lines": len(lines),
            "sections": dict[str, JsonValue](parsed.sections),
            "headings": [
                {"title": title, "section": section.value} for title, section in parsed.headings
            ],
            "candidates": len(parsed.candidates),
            "issues": len(issues),
        },
        errors=[issue.to_json() for issue in issues],
        # Pasted text is not kept anywhere; a file given instead is left where it is.
        retention=RetentionChoice.KEEP,
    )
    log.info(
        "prepared text import status=%s candidates=%d issues=%d",
        status,
        len(parsed.candidates),
        len(issues),
    )
    return PreparedImport(run=run, candidates=parsed.candidates, issues=issues)
