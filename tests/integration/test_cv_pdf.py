"""Real PDF builds. Skipped without a LaTeX engine; CI requires one (JAA_REQUIRE_LATEX=1)."""

import logging
import stat
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from jaa.core.errors import DocumentError, LatexCompileError
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import (
    DEFAULT_LETTER_TEMPLATE,
    DEFAULT_TEMPLATE,
    TemplateKind,
    available_templates,
    get_template,
)
from jaa.documents.letter import LetterContent
from jaa.documents.pipeline import BuildOptions, build_cover_letter, build_cv, extract_text
from jaa.documents.sample import sample_cv, sample_letter
from tests.fixtures.cv_profile import confirmed_facts
from tests.fixtures.images import png

pytestmark = pytest.mark.latex


@pytest.mark.parametrize("language", list(CvLanguage))
def test_pdf_text_matches_source_facts(
    latex_engine: Engine, tmp_path: Path, language: CvLanguage
) -> None:
    photo = tmp_path / "photo.png"
    photo.write_bytes(png())
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
        "uploaded": False,
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


CV_TEMPLATES = sorted(available_templates(TemplateKind.CV))


def test_at_least_three_cv_templates_ship() -> None:
    assert len(CV_TEMPLATES) >= 3


@pytest.mark.parametrize("template_id", CV_TEMPLATES)
@pytest.mark.parametrize("language", list(CvLanguage))
def test_every_cv_template_renders_the_same_profile(
    latex_engine: Engine, tmp_path: Path, template_id: str, language: CvLanguage
) -> None:
    content = build_content(confirmed_facts()).content
    out = build_cv(
        content,
        template=get_template(template_id),
        engine=latex_engine,
        output_dir=tmp_path,
        options=BuildOptions(language=language),
        profile_version_id=1,
    )
    assert out.missing_text == ()
    assert out.warnings == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    # Reading order: the experience entries keep their order in the extracted text.
    assert text.index("Senior Widget Engineer") < text.index("Built gadget inventory APIs.")


@pytest.mark.parametrize("template_id", sorted(available_templates()))
@pytest.mark.parametrize("language", list(CvLanguage))
def test_previews_from_sample_data(
    latex_engine: Engine, tmp_path: Path, template_id: str, language: CvLanguage
) -> None:
    template = get_template(template_id, available_templates()[template_id].kind)
    if template.kind is TemplateKind.CV:
        out = build_cv(
            sample_cv(language),
            template=template,
            engine=latex_engine,
            output_dir=tmp_path,
            options=BuildOptions(language=language),
            profile_version_id=None,
        )
    else:
        out = build_cover_letter(
            sample_letter(language),
            template=template,
            engine=latex_engine,
            output_dir=tmp_path,
            language=language,
            profile_version_id=None,
            job_id=None,
            fact_ids=(),
            tailoring=None,
        )
    assert out.missing_text == ()
    assert out.warnings == ()
    assert out.pages == 1


def test_a_template_that_reads_outside_its_folder_is_refused(
    latex_engine: Engine, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.tex"
    secret.write_text("SECRET-CONTENT")
    shipped = get_template(DEFAULT_TEMPLATE)
    # \csname hides the \input from the static check; the build must still refuse it.
    sneaky = replace(
        shipped,
        id="user-sneaky",
        uploaded=True,
        source=shipped.source.replace(
            r"\makecvtitle", rf"\makecvtitle\csname input\endcsname{{{secret}}}"
        ),
        assets={"img/logo.png": png()},
    )
    output = tmp_path / "out"
    with pytest.raises(DocumentError):
        build_cv(
            build_content(confirmed_facts()).content,
            template=sneaky,
            engine=latex_engine,
            output_dir=output,
            options=BuildOptions(),
            profile_version_id=None,
        )
    assert not list(output.glob("*.pdf"))
