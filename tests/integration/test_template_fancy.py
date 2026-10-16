"""moderncv fancy (#93, ADR 0029): long contact details stay inside the page, text reads in order.

Real PDF builds, skipped without a LaTeX engine; CI requires one (JAA_REQUIRE_LATEX=1).
"""

from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

from jaa.core.models import PaperSize
from jaa.documents.content import LABELS, CvContent, CvLanguage, build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import get_template
from jaa.documents.pipeline import BuildOptions, BuildOutput, build_cv, extract_text
from jaa.documents.sample import sample_cv
from tests.fixtures.cv_profile import confirmed_facts
from tests.fixtures.images import png

pytestmark = pytest.mark.latex

# Longer than the contact column, with and without the usual break points.
LONG_EMAIL = "aleksandra.konstantynopolitanczykiewicz@long-company-domain.example.test"
LONG_LINKS = (
    ("github", "https://github.com/example-sample"),
    ("site", "https://www.very-long-personal-homepage.example.test/portfolio/projects/2026"),
    ("blog", "https://example.test/ThisPathSegmentHasNoBreakPointsAtAllAndIsVeryLong_1234"),
)


def _content(language: CvLanguage, source: str) -> CvContent:
    if source == "fixture":
        return build_content(confirmed_facts()).content
    content = sample_cv(language)
    return replace(content, email=LONG_EMAIL, links=LONG_LINKS) if source == "long" else content


def _reading_order(content: CvContent) -> list[str]:
    """Fact text in the order an ATS meets it. fancy sets the contact details in the left
    column next to the name, and they come first in the text."""
    texts = [content.location, content.phone, content.email]
    texts += [url for _, url in content.links]
    texts += [content.name, content.headline, content.summary]
    for entry in (*content.experience, *content.education):
        texts += [entry.role, entry.organization, *entry.items]
    texts += list(content.skills)
    for cert in content.certifications:
        texts += [cert.name, cert.authority]
    for lang in content.languages:
        texts += [lang.name, lang.level]
    return [t for t in texts if t]


def _assert_in_order(text: str, expected: list[str]) -> None:
    # Contact details may break across lines at any character, so whitespace is ignored there;
    # a word split inside the body ("T est") still fails because spaces between words matter
    # for the rest of the text.
    flat = "".join(text.split())
    position = 0
    for value in expected:
        found = flat.find("".join(value.split()), position)
        assert found >= 0, f"{value!r} missing or out of order"
        position = found + len("".join(value.split()))


def _build(engine: Engine, content: CvContent, out: Path, options: BuildOptions) -> BuildOutput:
    return build_cv(
        content,
        template=get_template("moderncv-fancy"),
        engine=engine,
        output_dir=out,
        options=options,
        profile_version_id=None,
    )


@pytest.mark.parametrize("language", list(CvLanguage))
@pytest.mark.parametrize("source", ["sample", "long", "fixture"])
def test_builds_inside_the_page_in_reading_order(
    latex_engine: Engine, tmp_path: Path, language: CvLanguage, source: str
) -> None:
    content = _content(language, source)
    out = _build(latex_engine, content, tmp_path, BuildOptions(language=language))
    assert out.missing_text == ()
    # Overfull boxes are reported as warnings: none means no text runs into the margin.
    assert out.warnings == ()
    assert out.pages == 1
    text, _ = extract_text(out.pdf)
    _assert_in_order(text, _reading_order(content))
    labels = LABELS[language]
    _assert_in_order(text, [labels.experience, labels.education, labels.skills])
    # Kerning is off, so words come out whole ("Test", not "T est").
    flat = " ".join(text.split())
    for value in (content.headline, labels.education, *(e.role for e in content.experience)):
        assert value is None or " ".join(value.split()) in flat


def test_us_letter_with_a_photo(latex_engine: Engine, tmp_path: Path) -> None:
    photo = tmp_path / "photo.png"
    photo.write_bytes(png())
    content = _content(CvLanguage.PL, "long")
    options = BuildOptions(language=CvLanguage.PL, paper=PaperSize.LETTER, photo=photo)
    out = _build(latex_engine, content, tmp_path / "out", options)
    assert out.missing_text == ()
    assert out.warnings == ()
    width, height = (float(v) for v in PdfReader(out.pdf).pages[0].mediabox.upper_right)
    assert (round(width), round(height)) == (612, 792)
