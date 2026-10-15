import re
from pathlib import Path

import pytest

from jaa.core.errors import TemplateError
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.latex import (
    DEFAULT_TEMPLATE,
    _load,  # pyright: ignore[reportPrivateUsage] - template validation is tested directly
    available_templates,
    escape,
    get_template,
    render,
)
from tests.fixtures.cv_profile import confirmed_facts


def test_escape_neutralizes_every_special_character() -> None:
    text = "\\input{/etc/passwd} 100% $x$ & #1 a_b ^ ~ \x07line\nbreak"
    escaped = escape(text)
    assert escaped == (
        r"\textbackslash{}input\{/etc/passwd\} 100\% \$x\$ \& \#1 a\_b "
        r"\textasciicircum{} \textasciitilde{} line break"
    )
    # No command survives: every backslash starts one of our own safe macros.
    assert set(re.findall(r"\\([a-zA-Z]+)", escaped)) <= {
        "textbackslash",
        "textasciicircum",
        "textasciitilde",
    }


def test_shipped_templates_load() -> None:
    templates = available_templates()
    assert DEFAULT_TEMPLATE in templates
    template = templates[DEFAULT_TEMPLATE]
    assert template.version_label == "moderncv-classic@1"
    assert len(template.sha256) == 64
    with pytest.raises(TemplateError, match="unknown cv template"):
        get_template("nope")


_PREAMBLE = "\\documentclass{moderncv}\n%%jaa:babel%%\n%%jaa:header%%\n\\begin{document}\n"
_END = "\\end{document}\n"
_VALID = _PREAMBLE + "%%jaa:body%%\n%%jaa:consent%%\n" + _END


def _template_dir(tmp_path: Path, tex: str, family: str = "moderncv") -> Path:
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "template.json").write_text(
        f'{{"id": "broken", "version": 1, "name": "B", "family": "{family}", "license": "MIT"}}'
    )
    (folder / "template.tex").write_text(tex)
    return folder


@pytest.mark.parametrize(
    ("tex", "family", "message"),
    [
        (_PREAMBLE + "%%jaa:body%%\n" + _END, "moderncv", "missing slot line %%jaa:consent%%"),
        (_VALID + "%%jaa:x%%\n", "moderncv", "unknown slot line %%jaa:x%%"),
        (_VALID, "awesome", "TemplateFamily"),
        (_VALID, "custom", "family is moderncv"),
        (_VALID.replace("{moderncv}", "{article}"), "custom", r"nor defines \\name"),
        (
            _PREAMBLE + "%%jaa:body%%\n%%jaa:consent%%\n%%jaa:body%%\n" + _END,
            "moderncv",
            "appears 2 times",
        ),
        (
            "\\documentclass{moderncv}\n%%jaa:babel%%\n\\begin{document}\n%%jaa:header%%\n"
            "%%jaa:body%%\n%%jaa:consent%%\n" + _END,
            "moderncv",
            r"header%% must come before",
        ),
    ],
)
def test_template_validation(tmp_path: Path, tex: str, family: str, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        _load(_template_dir(tmp_path, tex, family))


def test_render_english_and_polish() -> None:
    content = build_content(confirmed_facts()).content
    template = get_template(DEFAULT_TEMPLATE)

    english = render(template, content, CvLanguage.EN)
    assert not re.search(r"^%%jaa:", english, re.MULTILINE)
    assert r"\usepackage[english]{babel}" in english
    assert r"\name{Avery}{Żółkiewska-\allowbreak{}Synthetic}" in english
    assert r"\section{Experience}" in english
    assert (
        r"\cventry{01.2021--\allowbreak{}Present}{Senior Widget Engineer}"
        r"{\textsc{Example Co}}{Wrocław}{}{}" in english
    )
    assert r"\cvlistitem{Mentored four engineers \textbackslash{}input\{/etc/hostname\}" in english
    assert (
        r"\quote{Builds 100\% synthetic widgets with \{care\} and \$budget\_limits\#.}" in english
    )
    assert "Consent" not in english
    assert r"\photo" not in english

    polish = render(
        template,
        content,
        CvLanguage.PL,
        consent_text="Wyrażam zgodę 100%.\n\nDrugi akapit.",
        photo_file="photo.jpg",
    )
    assert r"\usepackage[polish]{babel}" in polish
    assert r"\section{Doświadczenie zawodowe}" in polish
    assert "obecnie" in polish
    assert r"\textbf{Oświadczenie}" in polish
    assert r"\textit{Wyrażam zgodę 100\%.}" in polish
    assert r"\textit{Drugi akapit.}" in polish
    assert r"\photo[64pt][0.4pt]{photo.jpg}" in polish
