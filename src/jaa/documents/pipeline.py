"""Building CV and cover letter PDFs: render, compile privately, verify, and describe them.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jaa.core.clock import utcnow
from jaa.core.errors import DocumentError
from jaa.core.models import JsonObject, JsonValue, PaperSize
from jaa.documents.content import CvContent, CvLanguage, format_period
from jaa.documents.contract import PAPER_POINTS
from jaa.documents.engine import CompileResult, Engine, compile_tex
from jaa.documents.latex import CvTemplate, TemplateKind, render
from jaa.documents.letter import LetterContent, expected_letter_text, render_letter

log = logging.getLogger(__name__)

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700
MAX_PHOTO_BYTES: Final = 10 * 1024 * 1024
# CVs longer than this are usually trimmed by recruiters; it is a warning, not an error.
PAGE_WARNING: Final = 2
# How far (in points) a page may differ from the chosen paper; PDF sizes are rounded.
PAPER_TOLERANCE: Final = 2.0
# Hyphen, dashes and minus sign (U+2010 to U+2015, U+2212).
_DASHES: Final = re.compile(f"[{chr(0x2010)}-{chr(0x2015)}{chr(0x2212)}]")
_PHOTO_TYPES: Final = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}

type Compiler = Callable[[Engine, Path], CompileResult]


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildOptions:
    language: CvLanguage = CvLanguage.EN
    paper: PaperSize = PaperSize.A4
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


def paper_mismatch(pdf: Path, paper: PaperSize) -> str | None:
    """Why the PDF's pages are not `paper`, or None when every page is."""
    want_w, want_h = PAPER_POINTS[paper]
    try:
        pages = PdfReader(pdf).pages
        for number, page in enumerate(pages, start=1):
            width, height = (float(v) for v in page.mediabox.upper_right)
            width -= float(page.mediabox.left)
            height -= float(page.mediabox.bottom)
            if abs(width - want_w) > PAPER_TOLERANCE or abs(height - want_h) > PAPER_TOLERANCE:
                return f"page {number} is {width:.0f}x{height:.0f} pt, not {paper.value}"
    except (PyPdfError, OSError, ValueError) as exc:
        raise DocumentError(f"the generated PDF cannot be read back: {exc}") from exc
    return None


def missing_from(text: str, expected: list[str]) -> list[str]:
    haystack = _normalize(text)
    return [t for t in expected if _normalize(t) not in haystack]


def _stem(template: CvTemplate, language: CvLanguage, at: datetime, job_id: int | None) -> str:
    job = f"-job{job_id}" if job_id is not None else ""
    prefix = "cover-letter" if template.kind is TemplateKind.COVER_LETTER else "cv"
    return f"{prefix}-{at:%Y%m%d-%H%M%S}{job}-{template.id}-{language.value}"


def _write_assets(build_dir: Path, assets: Mapping[str, bytes]) -> None:
    for name, data in assets.items():
        target = (build_dir / name).resolve()
        # Paths were checked when the template was added; check again before writing.
        if not target.is_relative_to(build_dir.resolve()) or target == build_dir.resolve():
            raise DocumentError(f"template file {name!r} would be written outside the build")
        target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        _write_private(target, data)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Compiled:
    pdf: Path
    tex: Path
    pdf_sha256: str
    tex_sha256: str
    result: CompileResult


def _compile_into(
    source: str,
    *,
    stem: str,
    engine: Engine,
    output_dir: Path,
    compiler: Compiler,
    paper: PaperSize,
    assets: Mapping[str, bytes] | None = None,
    extra_files: dict[str, Path] | None = None,
) -> _Compiled:
    """Compile `source` in a private build folder; write `<stem>.pdf` and `<stem>.tex`.

    `assets` are the template's own files (uploaded templates), `extra_files` the user's photo.
    The PDF is saved only if its pages are `paper`: a template's own class or package could
    still fix another size (ADR 0033).
    """
    if (output_dir / f"{stem}.pdf").exists():
        raise DocumentError(f"{output_dir / stem}.pdf already exists; try again in a second")
    tex_bytes = source.encode()
    build_dir = Path(tempfile.mkdtemp(prefix=".build-", dir=output_dir))
    try:
        tex = build_dir / "document.tex"
        _write_private(tex, tex_bytes)
        _write_assets(build_dir, assets or {})
        for name, path in (extra_files or {}).items():
            shutil.copyfile(path, build_dir / name)
        try:
            result = compiler(engine, tex)
        except DocumentError:
            log_file = tex.with_suffix(".log")
            if log_file.is_file():
                # Kept for the user to inspect; it quotes the document, so it stays owner-only.
                kept = output_dir / f"{stem}.log"
                shutil.copyfile(log_file, kept)
                kept.chmod(FILE_MODE)
            raise
        if result.absolute_reads:
            # TeX engines can read any file the user can; a template must not (ADR 0029).
            raise DocumentError(
                "the template read files outside its folder, so the PDF was not saved: "
                + ", ".join(result.absolute_reads)
            )
        if mismatch := paper_mismatch(result.pdf, paper):
            raise DocumentError(
                f"the template ignored the paper size, so the PDF was not saved: {mismatch}"
            )
        pdf_bytes = result.pdf.read_bytes()
        pdf_out = output_dir / f"{stem}.pdf"
        tex_out = output_dir / f"{stem}.tex"
        _write_private(pdf_out, pdf_bytes)
        _write_private(tex_out, tex_bytes)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    return _Compiled(
        pdf=pdf_out,
        tex=tex_out,
        pdf_sha256=_sha256(pdf_bytes),
        tex_sha256=_sha256(tex_bytes),
        result=result,
    )


def _manifest(
    kind: str,
    at: datetime,
    template: CvTemplate,
    engine: Engine,
    language: CvLanguage,
    paper: PaperSize,
    compiled: _Compiled,
    pages: int,
    checked: tuple[int, int],
) -> JsonObject:
    """The manifest fields every built document shares."""
    expected, missing = checked
    return {
        "kind": kind,
        "generated_at": at.isoformat(),
        "template": {
            "id": template.id,
            "version": template.version,
            "sha256": template.sha256,
            "uploaded": template.uploaded,
        },
        "engine": {"name": engine.name, "version": engine.version},
        "language": language.value,
        "paper": paper.value,
        "source": {"path": str(compiled.tex), "sha256": compiled.tex_sha256},
        "pdf": {"path": str(compiled.pdf), "sha256": compiled.pdf_sha256, "pages": pages},
        "text_check": {"expected": expected, "missing": missing},
    }


def _prepare(output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    return output_dir


def build_cv(
    content: CvContent,
    *,
    template: CvTemplate,
    engine: Engine,
    output_dir: Path,
    options: BuildOptions,
    profile_version_id: int | None,
    excluded_fact_ids: tuple[int, ...] = (),
    job_id: int | None = None,
    tailoring: JsonObject | None = None,
    compiler: Compiler = compile_tex,
) -> BuildOutput:
    """Render, compile and verify a CV; write `<stem>.pdf` and `<stem>.tex` into `output_dir`.

    `excluded_fact_ids` are unreviewed facts the user did not approve; the manifest records
    that they were left out. A CV tailored for a job passes the job id and the tailoring's
    revision and changes (ADR 0016), which go into the manifest too. Raises `DocumentError`
    (or a subclass) on any failure.
    """
    if template.kind is not TemplateKind.CV:
        raise DocumentError(f"{template.version_label} is not a CV template")
    output_dir = _prepare(output_dir)
    photo = _photo(options.photo) if options.photo else None
    at = utcnow()
    photo_name = f"photo{photo[1]}" if photo else None
    source = render(
        template,
        content,
        options.language,
        consent_text=options.consent_text,
        photo_file=photo_name,
        paper=options.paper,
    )
    compiled = _compile_into(
        source,
        stem=_stem(template, options.language, at, job_id),
        engine=engine,
        output_dir=output_dir,
        compiler=compiler,
        paper=options.paper,
        assets=template.assets,
        extra_files={photo_name: photo[0]} if photo and photo_name else None,
    )

    text, pages = extract_text(compiled.pdf)
    expected = expected_text(content, options.language)
    missing = missing_from(text, expected)
    warnings = [p.describe() for p in compiled.result.warnings]
    if pages > PAGE_WARNING:
        warnings.append(f"the CV is {pages} pages long; recruiters usually expect 1-2")
    manifest = _manifest(
        "cv",
        at,
        template,
        engine,
        options.language,
        options.paper,
        compiled,
        pages,
        (len(expected), len(missing)),
    )
    manifest |= {
        "profile_version_id": profile_version_id,
        "fact_ids": list[JsonValue](content.fact_ids),
        "excluded_unreviewed_fact_ids": list[JsonValue](excluded_fact_ids),
        "photo": photo is not None,
        "consent_clause": bool(options.consent_text),
    }
    if job_id is not None:
        manifest["job_id"] = job_id
    if tailoring is not None:
        manifest["tailoring"] = tailoring
    log.info(
        "built cv template=%s facts=%s pages=%s missing_text=%s",
        template.version_label,
        len(content.fact_ids),
        pages,
        len(missing),
    )
    return BuildOutput(
        pdf=compiled.pdf,
        tex=compiled.tex,
        pdf_sha256=compiled.pdf_sha256,
        tex_sha256=compiled.tex_sha256,
        pages=pages,
        manifest=manifest,
        warnings=tuple(warnings),
        missing_text=tuple(missing),
    )


def build_cover_letter(
    letter: LetterContent,
    *,
    template: CvTemplate,
    engine: Engine,
    output_dir: Path,
    language: CvLanguage,
    profile_version_id: int | None,
    job_id: int | None,
    fact_ids: tuple[int, ...],
    tailoring: JsonObject | None,
    paper: PaperSize = PaperSize.A4,
    compiler: Compiler = compile_tex,
) -> BuildOutput:
    """Render, compile and verify a cover letter for a job (ADR 0024).

    `fact_ids` are the sender's facts plus the facts the paragraphs cite; `tailoring` holds the
    revision and each paragraph's facts for the manifest. A template preview (ADR 0029) has no
    profile version, job or tailoring. Raises `DocumentError` on failure.
    """
    if template.kind is not TemplateKind.COVER_LETTER:
        raise DocumentError(f"{template.version_label} is not a cover letter template")
    output_dir = _prepare(output_dir)
    at = utcnow()
    source = render_letter(template, letter, language, paper)
    compiled = _compile_into(
        source,
        stem=_stem(template, language, at, job_id),
        engine=engine,
        output_dir=output_dir,
        compiler=compiler,
        paper=paper,
        assets=template.assets,
    )
    text, pages = extract_text(compiled.pdf)
    expected = expected_letter_text(letter, language)
    missing = missing_from(text, expected)
    warnings = [p.describe() for p in compiled.result.warnings]
    if pages > 1:
        warnings.append(f"the cover letter is {pages} pages long; one page is usual")
    manifest = _manifest(
        "cover_letter",
        at,
        template,
        engine,
        language,
        paper,
        compiled,
        pages,
        (len(expected), len(missing)),
    )
    manifest |= {
        "profile_version_id": profile_version_id,
        "job_id": job_id,
        "fact_ids": list[JsonValue](fact_ids),
        "letter_date": letter.date.isoformat(),
        "opening": bool(letter.opening),
        "closing": bool(letter.closing),
        "tailoring": tailoring,
    }
    log.info(
        "built cover letter template=%s job=%s paragraphs=%s pages=%s missing_text=%s",
        template.version_label,
        job_id,
        len(letter.paragraphs),
        pages,
        len(missing),
    )
    return BuildOutput(
        pdf=compiled.pdf,
        tex=compiled.tex,
        pdf_sha256=compiled.pdf_sha256,
        tex_sha256=compiled.tex_sha256,
        pages=pages,
        manifest=manifest,
        warnings=tuple(warnings),
        missing_text=tuple(missing),
    )
