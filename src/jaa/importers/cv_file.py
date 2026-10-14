"""Prepare the import of a PDF, DOCX or LaTeX CV: detect the format, extract, parse, build the run.

The result is data only. Callers store it with `jaa.storage.profile.add_imported_facts`, which
records the `ImportRun` (also for failed imports) and adds the candidates as `unreviewed`. A
LaTeX import also carries a `TemplateCandidate` that callers may register (T08).
"""

import json
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
    TemplateCandidate,
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
from jaa.importers.latex import (
    LatexProject,
    LatexResult,
    extract_latex,
    is_latex,
    is_latex_fragment,
    read_latex_project,
)
from jaa.importers.pdf import extract_pdf, is_pdf

log = logging.getLogger(__name__)

_OLE_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedImport:
    run: ImportRun
    candidates: tuple[FactCandidate, ...]
    issues: tuple[ImportIssue, ...]
    # LaTeX sources only: the files, ready to be kept as a template candidate if the user asks.
    template: TemplateCandidate | None = None


def detect_type(data: bytes) -> ImportSourceType:
    """Detect PDF, DOCX or LaTeX from the content, not the file extension."""
    if is_pdf(data):
        return ImportSourceType.PDF
    if is_docx(data):
        return ImportSourceType.DOCX
    if is_latex(data):
        return ImportSourceType.LATEX
    if data.startswith(_OLE_MAGIC):
        raise ImportSourceError(
            "this looks like a legacy Word .doc file; save it as .docx or PDF and import that"
        )
    if is_latex_fragment(data):
        raise ImportSourceError(
            "this looks like part of a LaTeX CV (it has no \\documentclass); import the main "
            ".tex file; the files it includes are read with it"
        )
    raise ImportSourceError("unsupported file type; import a PDF, DOCX or LaTeX (.tex) CV")


def _template(project: LatexProject, result: LatexResult, at: datetime) -> TemplateCandidate:
    files = dict[str, JsonValue](project.files)
    canonical = json.dumps(files, sort_keys=True, ensure_ascii=False).encode()
    return TemplateCandidate(
        main_file=project.main,
        files=files,
        document_class=result.document_class,
        packages=list[JsonValue](result.packages),
        content_sha256=sha256(canonical),
        registered_at=at,
    )


def _extract(
    source_type: ImportSourceType, path: Path, data: bytes, at: datetime
) -> tuple[ExtractedText, TemplateCandidate | None]:
    if source_type is ImportSourceType.PDF:
        return extract_pdf(data), None
    if source_type is ImportSourceType.DOCX:
        return extract_docx(data), None
    project = read_latex_project(path, data)
    result = extract_latex(project)
    return result.extracted, _template(project, result, at)


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
    template: TemplateCandidate | None = None
    issues: tuple[ImportIssue, ...]
    stats: dict[str, JsonValue] = {}
    try:
        extracted, template = _extract(source_type, resolved, data, at)
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
            "headings": [
                {"title": title, "section": section.value} for title, section in parsed.headings
            ],
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
    return PreparedImport(run=run, candidates=candidates, issues=issues, template=template)
