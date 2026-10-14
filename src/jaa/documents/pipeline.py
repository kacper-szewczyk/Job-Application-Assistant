"""Building a CV PDF: render, compile in a private build directory, verify, and describe it.

Returns data (paths, hashes, manifest, warnings); the CLI records `DocumentArtifact`s. Output
files hold the CV, so they are created owner-only. Logging records counts and ids only.
"""

import hashlib
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jaa.core.clock import utcnow
from jaa.core.errors import DocumentError
from jaa.core.models import JsonObject, JsonValue
from jaa.documents.content import CvContent, CvLanguage, format_period
from jaa.documents.engine import CompileResult, Engine, compile_tex
from jaa.documents.latex import CvTemplate, render

log = logging.getLogger(__name__)

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700
MAX_PHOTO_BYTES: Final = 10 * 1024 * 1024
# CVs longer than this are usually trimmed by recruiters; it is a warning, not an error.
PAGE_WARNING: Final = 2
# Hyphen, dashes and minus sign (U+2010 to U+2015, U+2212).
_DASHES: Final = re.compile(f"[{chr(0x2010)}-{chr(0x2015)}{chr(0x2212)}]")
_PHOTO_TYPES: Final = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}

type Compiler = Callable[[Engine, Path], CompileResult]


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildOptions:
    language: CvLanguage = CvLanguage.EN
    photo: Path | None = None
    # The user's own consent clause (e.g. the Polish RODO "Oświadczenie"); never a default.
    consent_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildOutput:
    pdf: Path
    tex: Path
    pdf_sha256: str
    tex_sha256: str
    pages: int
    manifest: JsonObject = field(repr=False)
    # Layout warnings from the engine plus jaa's own checks (page count).
    warnings: tuple[str, ...] = ()
    # Text from facts that could not be found in the PDF's extractable text (ATS risk).
    missing_text: tuple[str, ...] = field(default=(), repr=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _photo(path: Path) -> tuple[Path, str]:
    """Resolve and check a user-supplied photo. Returns (path, extension)."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise DocumentError(f"photo not found: {path}") from exc
    if not resolved.is_file():
        raise DocumentError(f"photo is not a regular file: {resolved}")
    if resolved.stat().st_size > MAX_PHOTO_BYTES:
        raise DocumentError(f"photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    with resolved.open("rb") as fh:
        head = fh.read(8)
    for magic, suffix in _PHOTO_TYPES.items():
        if head.startswith(magic):
            return resolved, suffix
    raise DocumentError("photo must be a JPEG or PNG file")


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _normalize(text: str) -> str:
    # Compare without whitespace and with ligatures/dashes unified: extraction splits lines and
    # turns "fi" into a ligature, but must not lose or reorder characters.
    text = unicodedata.normalize("NFKC", text)
    text = _DASHES.sub("-", text)
    return re.sub(r"\s+", "", text).casefold()


def expected_text(content: CvContent, language: CvLanguage) -> list[str]:
    """Every piece of fact text the PDF must contain, in document order."""
    texts: list[str | None] = [content.name, content.headline, content.location, content.phone]
    texts += [content.email, *(url for _, url in content.links), content.summary]
    for entry in (*content.experience, *content.education):
        texts += [entry.role, entry.organization, entry.location, *entry.items]
        texts.append(format_period(entry.dates, language, dash="-"))
    texts += list(content.skills)
    for lang in content.languages:
        texts += [lang.name, lang.level]
    for cert in content.certifications:
        texts += [cert.name, cert.authority]
    return [t for t in texts if t]


def extract_text(pdf: Path) -> tuple[str, int]:
    """All extractable text of a PDF and its page count."""
    try:
        reader = PdfReader(pdf)
        return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise DocumentError(f"the generated PDF cannot be read back: {exc}") from exc


def missing_from(text: str, expected: list[str]) -> list[str]:
    haystack = _normalize(text)
    return [t for t in expected if _normalize(t) not in haystack]


def _stem(template: CvTemplate, language: CvLanguage, at: datetime) -> str:
    return f"cv-{at:%Y%m%d-%H%M%S}-{template.id}-{language.value}"


def build_cv(
    content: CvContent,
    *,
    template: CvTemplate,
    engine: Engine,
    output_dir: Path,
    options: BuildOptions,
    profile_version_id: int | None,
    excluded_fact_ids: tuple[int, ...] = (),
    compiler: Compiler = compile_tex,
) -> BuildOutput:
    """Render, compile and verify a CV; write `<stem>.pdf` and `<stem>.tex` into `output_dir`.

    `excluded_fact_ids` are unreviewed facts the user did not approve; the manifest records
    that they were left out. Raises `DocumentError` (or a subclass) on any failure.
    """
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    photo = _photo(options.photo) if options.photo else None
    at = utcnow()
    stem = _stem(template, options.language, at)
    if (output_dir / f"{stem}.pdf").exists():
        raise DocumentError(f"{output_dir / stem}.pdf already exists; try again in a second")

    photo_name = f"photo{photo[1]}" if photo else None
    source = render(
        template,
        content,
        options.language,
        consent_text=options.consent_text,
        photo_file=photo_name,
    )
    tex_bytes = source.encode()
    build_dir = Path(tempfile.mkdtemp(prefix=".build-", dir=output_dir))
    try:
        tex = build_dir / "cv.tex"
        _write_private(tex, tex_bytes)
        if photo and photo_name:
            shutil.copyfile(photo[0], build_dir / photo_name)
        try:
            result = compiler(engine, tex)
        except DocumentError:
            log_file = tex.with_suffix(".log")
            if log_file.is_file():
                # Kept for the user to inspect; it quotes the CV, so it stays owner-only.
                kept = output_dir / f"{stem}.log"
                shutil.copyfile(log_file, kept)
                kept.chmod(FILE_MODE)
            raise
        pdf_bytes = result.pdf.read_bytes()
        pdf_out = output_dir / f"{stem}.pdf"
        tex_out = output_dir / f"{stem}.tex"
        _write_private(pdf_out, pdf_bytes)
        _write_private(tex_out, tex_bytes)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    text, pages = extract_text(pdf_out)
    expected = expected_text(content, options.language)
    missing = missing_from(text, expected)
    warnings = [p.describe() for p in result.warnings]
    if pages > PAGE_WARNING:
        warnings.append(f"the CV is {pages} pages long; recruiters usually expect 1-2")
    pdf_sha, tex_sha = _sha256(pdf_bytes), _sha256(tex_bytes)
    manifest: JsonObject = {
        "kind": "cv",
        "generated_at": at.isoformat(),
        "template": {
            "id": template.id,
            "version": template.version,
            "sha256": template.sha256,
        },
        "engine": {"name": engine.name, "version": engine.version},
        "language": options.language.value,
        "profile_version_id": profile_version_id,
        "fact_ids": list[JsonValue](content.fact_ids),
        "excluded_unreviewed_fact_ids": list[JsonValue](excluded_fact_ids),
        "photo": photo is not None,
        "consent_clause": bool(options.consent_text),
        "source": {"path": str(tex_out), "sha256": tex_sha},
        "pdf": {"path": str(pdf_out), "sha256": pdf_sha, "pages": pages},
        "text_check": {"expected": len(expected), "missing": len(missing)},
    }
    log.info(
        "built cv template=%s facts=%s pages=%s missing_text=%s",
        template.version_label,
        len(content.fact_ids),
        pages,
        len(missing),
    )
    return BuildOutput(
        pdf=pdf_out,
        tex=tex_out,
        pdf_sha256=pdf_sha,
        tex_sha256=tex_sha,
        pages=pages,
        manifest=manifest,
        warnings=tuple(warnings),
        missing_text=tuple(missing),
    )
