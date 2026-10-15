"""Real PDF builds. Skipped without a LaTeX engine; CI requires one (JAA_REQUIRE_LATEX=1)."""

import logging
import stat
import struct
import zlib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from jaa.core.errors import LatexCompileError
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import (
    DEFAULT_LETTER_TEMPLATE,
    DEFAULT_TEMPLATE,
    TemplateKind,
    get_template,
)
from jaa.documents.letter import LetterContent
from jaa.documents.pipeline import BuildOptions, build_cover_letter, build_cv, extract_text
from tests.fixtures.cv_profile import confirmed_facts

pytestmark = pytest.mark.latex


def _png(width: int = 60, height: int = 80) -> bytes:
    """A valid grey RGB PNG, built here so its checksums are right (libpng aborts on bad ones)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("language", list(CvLanguage))
def test_pdf_text_matches_source_facts(
    latex_engine: Engine, tmp_path: Path, language: CvLanguage
) -> None:
    photo = tmp_path / "photo.png"
    photo.write_bytes(_png())
    content = build_content(confirmed_facts()).content
    out = build_cv(
        content,
        template=get_template(DEFAULT_TEMPLATE),
        engine=latex_engine,
        output_dir=tmp_path / "out",
        options=BuildOptions(
            language=language, photo=photo, consent_text="Wyrażam zgodę (tekst syntetyczny)."
        ),
        profile_version_id=7,
        excluded_fact_ids=(99,),
    )
    assert out.missing_text == ()
    assert out.warnings == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    for value in ("Żółkiewska-Synthetic", "Gadgets Sp. z o.o.", "Wrocław", "$budget_limits#"):
        assert value in text
    assert ("Doświadczenie zawodowe" in text) == (language is CvLanguage.PL)
    assert "\\input{/etc/hostname}" in text  # printed literally, never executed
    for path in (out.pdf, out.tex):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.suffix for p in out.pdf.parent.iterdir()) == [".pdf", ".tex"]

    manifest = out.manifest
    assert manifest["template"] == {
        "id": "moderncv-classic",
        "version": 1,
        "sha256": get_template(DEFAULT_TEMPLATE).sha256,
    }
    assert manifest["profile_version_id"] == 7
    assert manifest["fact_ids"] == list(content.fact_ids)
    assert manifest["excluded_unreviewed_fact_ids"] == [99]
    assert manifest["pdf"] == {"path": str(out.pdf), "sha256": out.pdf_sha256, "pages": 1}


def test_compile_errors_are_explained(latex_engine: Engine, tmp_path: Path) -> None:
    template = get_template(DEFAULT_TEMPLATE)
    broken = replace(template, source=template.source.replace(r"\makecvtitle", r"\makecvtitlex"))
    with pytest.raises(LatexCompileError) as exc_info:
        build_cv(
            build_content(confirmed_facts()).content,
            template=broken,
            engine=latex_engine,
            output_dir=tmp_path,
            options=BuildOptions(),
            profile_version_id=None,
        )
    problems = " ".join(exc_info.value.problems)
    assert "Undefined control sequence" in problems
    assert "command that is not defined" in problems


@pytest.mark.parametrize("language", list(CvLanguage))
def test_cover_letter_text_matches_its_sources(
    latex_engine: Engine, tmp_path: Path, language: CvLanguage, caplog: pytest.LogCaptureFixture
) -> None:
    paragraph = "Zarządzałam migracją 100% potoków w Example Co {bez} $problemów_#."
    letter = LetterContent(
        sender=build_content(confirmed_facts()).content,
        recipient=("Gadgets Sp. z o.o.", "Wrocław"),
        date=date(2026, 10, 15),
        paragraphs=(paragraph, "Second paragraph."),
        closing="Z poważaniem,",
    )
    with caplog.at_level(logging.DEBUG, logger="jaa"):
        out = build_cover_letter(
            letter,
            template=get_template(DEFAULT_LETTER_TEMPLATE, TemplateKind.COVER_LETTER),
            engine=latex_engine,
            output_dir=tmp_path / "out",
            language=language,
            profile_version_id=7,
            job_id=3,
            fact_ids=(1, 8),
            tailoring={"revision": 2},
        )
    assert out.missing_text == ()
    assert out.warnings == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    assert ("15 października 2026" in text) == (language is CvLanguage.PL)
    assert "Example Co {bez} $problemów_#." in text
    for path in (out.pdf, out.tex):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert out.manifest["kind"] == "cover_letter"
    assert out.manifest["job_id"] == 3
    assert out.manifest["fact_ids"] == [1, 8]
    assert out.manifest["letter_date"] == "2026-10-15"
    for value in ("Zarządzałam", "Gadgets", "Avery", "poważaniem"):
        assert value not in caplog.text
