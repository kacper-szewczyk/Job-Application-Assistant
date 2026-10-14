"""Extract text lines from a PDF CV, page by page (SPEC §3: text, not layout reconstruction).

Pages without a text layer (scans) are reported, not guessed at: there is no OCR.
"""

import io
import logging
import re
from typing import Final

from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError

from jaa.core.errors import ImportSourceError
from jaa.importers.base import ExtractedText, ImportIssue, IssueSeverity, SourceLine

log = logging.getLogger(__name__)

# CVs are short; a cap bounds the work done on hostile or mistaken inputs.
MAX_PAGES: Final = 30

PDF_MAGIC: Final = b"%PDF-"

# Running page footers carry no profile content.
_PAGE_FOOTER: Final = re.compile(r"(?i)^(page\s*)?\d+\s*(/|of)\s*\d+$|^page\s+\d+$")


def is_pdf(data: bytes) -> bool:
    # The header may follow a few bytes of junk; viewers accept that, so do we.
    return PDF_MAGIC in data[:1024]


def _open(data: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many PDFs are encrypted with an empty user password only to set permissions;
            # every viewer opens those. A real password is the user's to remove.
            try:
                reader.decrypt("")
            except DependencyError as exc:
                raise ImportSourceError(
                    "the PDF is encrypted with an algorithm that needs the optional "
                    "'cryptography' package; save an unencrypted copy and import that"
                ) from exc
        _ = len(reader.pages)
    except FileNotDecryptedError as exc:
        raise ImportSourceError(
            "the PDF is password-protected; save a copy without a password and import that"
        ) from exc
    except ImportSourceError:
        raise
    # pypdf raises a wide range of exceptions on malformed files; any of them means the same
    # thing to the user, and the message carries no file contents.
    except Exception as exc:
        raise ImportSourceError(f"the PDF could not be read ({type(exc).__name__})") from exc
    return reader


def extract_pdf(data: bytes) -> ExtractedText:
    """Return the text lines of each page, located as `page N`."""
    reader = _open(data)
    total = len(reader.pages)
    lines: list[SourceLine] = []
    issues: list[ImportIssue] = []
    empty_pages = 0
    for index, page in enumerate(reader.pages[:MAX_PAGES]):
        location = f"page {index + 1}"
        try:
            text = page.extract_text()
        # Same reasoning as in _open: one broken page should not lose the others.
        except Exception as exc:
            issues.append(
                ImportIssue(
                    code="page_unreadable",
                    severity=IssueSeverity.ERROR,
                    message=f"Text on this page could not be read ({type(exc).__name__}).",
                    location=location,
                )
            )
            continue
        page_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not _PAGE_FOOTER.match(line.strip())
        ]
        if not page_lines:
            empty_pages += 1
            issues.append(
                ImportIssue(
                    code="page_without_text",
                    severity=IssueSeverity.ERROR,
                    message="This page has no extractable text (it may be a scanned image). "
                    "Nothing from it was imported.",
                    location=location,
                )
            )
        lines.extend(SourceLine(text=line, location=location) for line in page_lines)
    if total > MAX_PAGES:
        issues.append(
            ImportIssue(
                code="too_many_pages",
                severity=IssueSeverity.ERROR,
                message=f"Only the first {MAX_PAGES} of {total} pages were read.",
            )
        )
    log.info("extracted pdf pages=%d lines=%d empty_pages=%d", total, len(lines), empty_pages)
    return ExtractedText(lines=tuple(lines), issues=tuple(issues), stats={"pages": total})
