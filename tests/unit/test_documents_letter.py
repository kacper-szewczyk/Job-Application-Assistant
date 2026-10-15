import re
from datetime import date
from pathlib import Path

import pytest

from jaa.core.errors import DocumentError, TemplateError
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import (
    DEFAULT_LETTER_TEMPLATE,
    DEFAULT_TEMPLATE,
    CvTemplate,
    TemplateKind,
    available_templates,
    get_template,
)
from jaa.documents.letter import (
    LetterContent,
    expected_letter_text,
    format_date,
    render_letter,
)
from jaa.documents.pipeline import BuildOptions, build_cover_letter, build_cv
from tests.fixtures.cv_profile import confirmed_facts

FAKE_ENGINE = Engine(name="tectonic", path=Path("/nonexistent/tectonic"), version="fake")
DAY = date(2026, 10, 15)


def _letter(**changes: object) -> LetterContent:
    fields: dict[str, object] = {
        "sender": build_content(confirmed_facts()).content,
        "recipient": ("Example Widgets", "Kraków"),
        "date": DAY,
        "paragraphs": ("I led 100% of {the} migration at $Example_Co.", "Second paragraph."),
    }
    fields |= changes
    return LetterContent(**fields)  # pyright: ignore[reportArgumentType]


def _template() -> CvTemplate:
    return get_template(DEFAULT_LETTER_TEMPLATE, TemplateKind.COVER_LETTER)


def test_letter_template_is_a_separate_kind() -> None:
    template = _template()
    assert template.version_label == "moderncv-letter@1"
    assert template.kind is TemplateKind.COVER_LETTER
    assert set(available_templates(TemplateKind.CV)) == {DEFAULT_TEMPLATE}
    with pytest.raises(TemplateError, match="unknown cv template"):
        get_template(DEFAULT_LETTER_TEMPLATE)
    with pytest.raises(TemplateError, match="unknown cover_letter template"):
        get_template(DEFAULT_TEMPLATE, TemplateKind.COVER_LETTER)


def test_render_escapes_text_and_adds_no_greeting() -> None:
    source = render_letter(_template(), _letter(), CvLanguage.EN)
    assert r"I led 100\% of \{the\} migration at \$Example\_Co." in source
    assert r"\recipient{Example Widgets}{Kraków}" in source
    assert r"\date{15 October 2026}" in source
    # No opening or closing unless the user gives one: no "Dear …", no "Sincerely".
    assert r"\opening{\mbox{}}" in source
    assert r"\closing{\mbox{}}" in source
    assert "Dear" not in source and "regards" not in source
    assert r"\name{Avery}{Żółkiewska-\allowbreak{}Synthetic}" in source
    assert r"\email{avery.cv@example.test}" in source
    # The CV's headline and summary are not part of a letter.
    assert "Team Lead" not in source and "budget" not in source
    assert not re.search(r"^%%jaa:", source, re.MULTILINE)


def test_user_opening_closing_and_polish_date() -> None:
    letter = _letter(
        opening="Szanowni Państwo,",
        closing="Z poważaniem,",
        recipient=("HR & People", "ul. Przykładowa 1", "", "00-001 Warszawa"),
    )
    source = render_letter(_template(), letter, CvLanguage.PL)
    assert r"\opening{Szanowni Państwo,}" in source
    assert r"\closing{Z poważaniem,}" in source
    assert r"\recipient{HR \& People}{ul. Przykładowa 1\\00-001 Warszawa}" in source
    assert r"\date{15 października 2026}" in source
    assert r"\usepackage[polish]{babel}" in source
    expected = expected_letter_text(letter, CvLanguage.PL)
    assert expected[0] == "Avery Żółkiewska-Synthetic"
    assert {"HR & People", "15 października 2026", "Szanowni Państwo,", "Z poważaniem,"} <= set(
        expected
    )
    assert "" not in expected


@pytest.mark.parametrize(
    ("day", "language", "text"),
    [
        (date(2026, 1, 3), CvLanguage.EN, "3 January 2026"),
        (date(2026, 9, 30), CvLanguage.PL, "30 września 2026"),
    ],
)
def test_format_date(day: date, language: CvLanguage, text: str) -> None:
    assert format_date(day, language) == text


def test_letter_needs_paragraphs_and_a_recipient() -> None:
    with pytest.raises(DocumentError, match="no paragraphs"):
        render_letter(_template(), _letter(paragraphs=()), CvLanguage.EN)
    with pytest.raises(DocumentError, match="recipient"):
        render_letter(_template(), _letter(recipient=(" ",)), CvLanguage.EN)


def test_builders_refuse_the_other_kind_of_template(tmp_path: Path) -> None:
    with pytest.raises(DocumentError, match="not a CV template"):
        build_cv(
            build_content(confirmed_facts()).content,
            template=_template(),
            engine=FAKE_ENGINE,
            output_dir=tmp_path,
            options=BuildOptions(),
            profile_version_id=1,
        )
    with pytest.raises(DocumentError, match="not a cover letter template"):
        build_cover_letter(
            _letter(),
            template=get_template(DEFAULT_TEMPLATE),
            engine=FAKE_ENGINE,
            output_dir=tmp_path,
            language=CvLanguage.EN,
            profile_version_id=1,
            job_id=1,
            fact_ids=(),
            tailoring={},
        )
