"""Prepare the import of a PDF or DOCX CV: detect the format, extract, parse, build the run.

The result is data only. Callers store it with `jaa.storage.profile.add_imported_facts`, which
records the `ImportRun` (also for failed imports) and adds the candidates as `unreviewed`.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from jaa.core.clock import utcnow
from jaa.core.errors import ImportSourceError
from jaa.core.models import (
    FactCandidate,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JsonValue,
    RetentionChoice,
)
from jaa.importers.base import (
    ExtractedText,
    ImportIssue,
    IssueSeverity,
    read_source,
    sha256,
)
from jaa.importers.cv import parse_cv
from jaa.importers.docx import extract_docx, is_docx
from jaa.importers.pdf import extract_pdf, is_pdf

log = logging.getLogger(__name__)

_OLE_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedImport:
    run: ImportRun
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]


def detect_type(data: bytes) -> ImportSourceType:
    """Detect PDF or DOCX from the content, not the file extension."""
    if is_pdf(data):
        return ImportSourceType.PDF
    if is_docx(data):
        return ImportSourceType.DOCX
    if data.startswith(_OLE_MAGIC):
        raise ImportSourceError(
            "this looks like a legacy Word .doc file; save it as .docx or PDF and import that"
        )
    raise ImportSourceError("unsupported file type; import a PDF or DOCX CV")


def _status(candidates: tuple[FactCandidate, ...], issues: tuple[ImportIssue, ...]) -> ImportStatus:
    if not candidates:
        return ImportStatus.FAILED
    return ImportStatus.PARTIAL if issues else ImportStatus.SUCCEEDED


def prepare_cv_import(path: Path, *, at: datetime | None = None) -> PreparedImport:
    """Read and parse a CV file.

    Raises `ImportSourceError` only when there is nothing to record: the path is unusable or
    the format is unsupported. A supported file that cannot be read yields a `failed` run with
    the reason in its errors, so the attempt is visible in the import history.
    """
    resolved, data = read_source(path)
    source_type = detect_type(data)
    at = at or utcnow()

    candidates: tuple[FactCandidate, ...] = ()
    issues: tuple[ImportIssue, ...]
    stats: dict[str, JsonValue] = {}
    try:
        extracted: ExtractedText = (
            extract_pdf(data) if source_type is ImportSourceType.PDF else extract_docx(data)
        )
    except ImportSourceError as exc:
        issues = (ImportIssue(code="unreadable", severity=IssueSeverity.ERROR, message=str(exc)),)
    else:
        parsed = parse_cv(extracted.lines)
        candidates = parsed.candidates
        issues = extracted.issues + parsed.issues
        if not candidates:
            issues += (
                ImportIssue(
                    code="no_facts",
                    severity=IssueSeverity.ERROR,
                    message="No profile facts could be extracted from this file.",
                ),
            )
        stats = {
            **extracted.stats,
            "lines": len(extracted.lines),
            "sections": dict[str, JsonValue](parsed.sections),
        }

    run = ImportRun(
        source_type=source_type,
        source_path=str(resolved),
        source_sha256=sha256(data),
        imported_at=at,
        status=_status(candidates, issues),
        summary={**stats, "candidates": len(candidates), "issues": len(issues)},
        errors=[issue.to_json() for issue in issues],
        # The importer reads the user's file in place and keeps no copy of it.
        retention=RetentionChoice.KEEP,
    )
    log.info(
        "prepared %s import status=%s candidates=%d issues=%d",
        source_type,
        run.status,
        len(candidates),
        len(issues),
    )
    return PreparedImport(run=run, candidates=candidates, issues=issues)
