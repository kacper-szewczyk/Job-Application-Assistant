"""jaa's own template designs (#41, ADR 0030): reading order, paper sizes, page breaks.

Real PDF builds, skipped without a LaTeX engine; CI requires one (JAA_REQUIRE_LATEX=1).
"""

from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

from jaa.core.models import PaperSize, TemplateFamily, TemplateKind
from jaa.documents.content import LABELS, CvContent, CvLanguage, build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import CvTemplate, available_templates, get_template
from jaa.documents.letter import expected_letter_text
from jaa.documents.pipeline import (
    BuildOptions,
    BuildOutput,
    build_cover_letter,
    build_cv,
    extract_text,
)
from jaa.documents.sample import sample_cv, sample_letter
from tests.fixtures.cv_profile import confirmed_facts
from tests.fixtures.images import png

pytestmark = pytest.mark.latex

DESIGNS = sorted(
    t.id for t in available_templates(TemplateKind.CV).values() if t.family is TemplateFamily.CUSTOM
)


def test_at_least_two_designs_and_a_letter_ship() -> None:
    assert len(DESIGNS) >= 2
    letters = available_templates(TemplateKind.COVER_LETTER).values()
    assert any(t.family is TemplateFamily.CUSTOM for t in letters)


def _reading_order(content: CvContent) -> list[str]:
    """Fact text in the order a reader (or an ATS) meets it. Dates and entry locations are
    left out: each design places them differently around the title, and the build already
    checks that they are present."""
    texts = [content.name, content.headline, content.location, content.phone, content.email]
    texts += [url for _, url in content.links]
    texts.append(content.summary)
    for entry in (*content.experience, *content.education):
        texts += [entry.role, entry.organization, *entry.items]
    texts += list(content.skills)
    for cert in content.certifications:
        texts += [cert.name, cert.authority]
    for lang in content.languages:
        texts += [lang.name, lang.level]
    return [t for t in texts if t]


def _assert_in_order(text: str, expected: list[str]) -> None:
    # Whitespace is collapsed but not removed, so a word split by extraction ("T eam") fails.
    flat = " ".join(text.split())
    position = 0
    for value in expected:
        found = flat.find(" ".join(value.split()), position)
        assert found >= 0, f"{value!r} missing or out of order"
        position = found + len(value)


def _build(
    engine: Engine, template: CvTemplate, content: CvContent, out: Path, options: BuildOptions
) -> BuildOutput:
    return build_cv(
        content,
        template=template,
        engine=engine,
        output_dir=out,
        options=options,
        profile_version_id=None,
    )


@pytest.mark.parametrize("template_id", DESIGNS)
@pytest.mark.parametrize("language", list(CvLanguage))
@pytest.mark.parametrize("source", ["sample", "fixture"])
def test_extracted_text_follows_the_facts_in_reading_order(
    latex_engine: Engine, tmp_path: Path, template_id: str, language: CvLanguage, source: str
) -> None:
    content = (
        sample_cv(language) if source == "sample" else build_content(confirmed_facts()).content
    )
    out = _build(
        latex_engine,
        available_templates()[template_id],
        content,
        tmp_path,
        BuildOptions(language=language),
    )
    assert out.missing_text == ()
    assert out.warnings == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    _assert_in_order(text, _reading_order(content))
    labels = LABELS[language]
    # Headings may be set in capitals.
    headings = [labels.experience, labels.education, labels.skills]
    _assert_in_order(text.casefold(), [h.casefold() for h in headings])


@pytest.mark.parametrize("template_id", DESIGNS)
def test_us_letter_photo_and_consent(
    latex_engine: Engine, tmp_path: Path, template_id: str
) -> None:
    template = available_templates()[template_id]
    photo = tmp_path / "photo.png"
    photo.write_bytes(png())
    consent = "Wyrażam zgodę na przetwarzanie moich danych osobowych (tekst syntetyczny)."
    options = BuildOptions(
        language=CvLanguage.PL, paper=PaperSize.LETTER, photo=photo, consent_text=consent
    )
    out = _build(latex_engine, template, sample_cv(CvLanguage.PL), tmp_path / "out", options)
    assert out.missing_text == ()
    assert out.pages == 1
    width, height = (float(v) for v in PdfReader(out.pdf).pages[0].mediabox.upper_right)
    assert (round(width), round(height)) == (612, 792)  # US Letter in points
    text, _ = extract_text(out.pdf)
    assert "Oświadczenie" in text
    assert " ".join(consent.split()) in " ".join(text.split())


@pytest.mark.parametrize("template_id", DESIGNS)
def test_long_profiles_break_between_entries(
    latex_engine: Engine, tmp_path: Path, template_id: str
) -> None:
    sample = sample_cv(CvLanguage.EN)
    items = tuple(f"Delivered synthetic project {i} and described its outcome." for i in range(5))
    experience = tuple(
        replace(sample.experience[0], entry_id=f"e{n}", role=f"Engineer {n}", items=items)
        for n in range(8)
    )
    content = replace(sample, experience=experience)
    out = _build(
        latex_engine,
        available_templates()[template_id],
        content,
        tmp_path,
        BuildOptions(language=CvLanguage.EN),
    )
    assert out.missing_text == ()
    assert out.pages >= 2
    labels = LABELS[CvLanguage.EN]
    headings = {labels.experience, labels.education, labels.skills, labels.certifications}
    pages = [page.extract_text() or "" for page in PdfReader(out.pdf).pages]
    for page in pages[:-1]:
        last = [line.strip() for line in page.splitlines() if line.strip()][-1]
        # Neither a section heading nor an entry's title line ends a page.
        assert last.casefold() not in {h.casefold() for h in headings}
        assert not last.startswith("Engineer ")
    _assert_in_order("\n".join(pages), [f"Engineer {n}" for n in range(8)])


@pytest.mark.parametrize("language", list(CvLanguage))
def test_letter_design_reads_in_order(
    latex_engine: Engine, tmp_path: Path, language: CvLanguage
) -> None:
    letter = sample_letter(language)
    out = build_cover_letter(
        letter,
        template=get_template("jaa-minimal-letter", TemplateKind.COVER_LETTER),
        engine=latex_engine,
        output_dir=tmp_path,
        language=language,
        profile_version_id=None,
        job_id=None,
        fact_ids=(),
        tailoring=None,
    )
    assert out.missing_text == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    _assert_in_order(text, [t for t in expected_letter_text(letter, language) if t])
