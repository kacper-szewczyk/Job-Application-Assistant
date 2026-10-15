"""The template contract (ADR 0029): slots, macros, files and file access."""

import pytest

from jaa.core.models import TemplateFamily, TemplateKind
from jaa.documents.contract import REQUIRED_MACROS, check_template, defined_macros

PREAMBLE = "\\documentclass{moderncv}\n\\hyphenpenalty=10000\n%%jaa:babel%%\n%%jaa:header%%\n"
CV = PREAMBLE + "\\begin{document}\n\\makecvtitle\n%%jaa:body%%\n%%jaa:consent%%\n\\end{document}\n"


def _check(main: str, extra: dict[str, str] | None = None, assets: dict[str, int] | None = None):
    return check_template(
        "cv.tex", {"cv.tex": main, **(extra or {})}, assets or {}, TemplateKind.CV
    )


def test_moderncv_template_meets_the_contract() -> None:
    result = _check(CV, assets={"logo.png": 100, "fonts/Sans.otf": 2000})
    assert result.ok, result.problems
    assert result.family is TemplateFamily.MODERNCV
    assert result.document_class == "moderncv"
    assert result.warnings == ()


def test_custom_class_must_define_the_macros() -> None:
    article = CV.replace("{moderncv}", "{article}")
    result = _check(article)
    assert result.family is TemplateFamily.CUSTOM
    assert not result.ok
    assert "\\cventry" in result.problems[0] and "\\makecvtitle" in result.problems[0]

    definitions = "\n".join(
        [
            "\\newcommand{\\name}[2]{#1 #2}",
            "\\newcommand*\\address[3]{#1}",
            "\\providecommand{\\mobile}[1]{#1}",
            "\\def\\email#1{#1}",
            "\\NewDocumentCommand{\\extrainfo}{m}{#1}",
            "\\DeclareRobustCommand{\\photo}[2][]{}",
            "\\let\\quote\\relax",
            "\\newcommand{\\makecvtitle}{}",
            "\\newcommand{\\cventry}[6]{#2}",
            "\\newcommand{\\cvitem}[2]{#2}",
            "\\newcommand{\\cvlistitem}[1]{#1}",
            "\\newcommand{\\cvlanguage}[3]{#1}",
        ]
    )
    assert defined_macros({"x.sty": definitions}) >= set(REQUIRED_MACROS[TemplateKind.CV])
    assert _check(
        article.replace("%%jaa:header%%", "\\usepackage{cv}\n%%jaa:header%%"),
        {"cv.sty": definitions},
    ).ok


def test_commented_out_definitions_do_not_count() -> None:
    assert defined_macros({"x.sty": "% \\newcommand{\\cventry}{}\n"}) == set()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("\\directlua{io.open('/etc/hosts')}", "\\directlua is not allowed"),
        ("\\begin{luacode}\nx = 1\n\\end{luacode}", "\\luacode is not allowed"),
        ("\\immediate\\write18{ls}", "\\write18 is not allowed"),
        ("\\immediate\\openout\\f=out.txt", "\\openout is not allowed"),
        ("\\input{|ls}", "piped \\input"),
        ("\\input{/etc/hosts}", "outside the template's folder"),
        ("\\input /etc/hosts", "outside the template's folder"),
        ("\\input{../secret}", "outside the template's folder"),
        ("\\includegraphics{~/photo.png}", "outside the template's folder"),
        ("\\openin\\r=/etc/hosts", "outside the template's folder"),
        ("\\setmainfont[Path=/Library/Fonts/]{X}", "outside the template's folder"),
        ("\\input{sub/../../x}", "outside the template's folder"),
    ],
)
def test_file_access_is_refused(line: str, message: str) -> None:
    result = _check(CV.replace("\\makecvtitle", f"\\makecvtitle\n{line}"))
    assert not result.ok
    assert any(message in p for p in result.problems), result.problems


def test_file_access_is_checked_in_every_text_file() -> None:
    result = _check(CV, {"style.sty": "\\input{/etc/passwd}"})
    assert result.problems == (
        "style.sty: refers to a file outside the template's folder (an absolute, ~ or .. path)",
    )


def test_comments_and_local_paths_are_fine() -> None:
    body = (
        "% \\input{/etc/hosts}\n\\input{parts/skills}\n\\includegraphics{img/logo.png}\n50\\% off"
    )
    assert _check(CV.replace("\\makecvtitle", f"\\makecvtitle\n{body}")).ok


def test_low_level_tex_and_hyphenation_are_warnings() -> None:
    main = CV.replace("\\hyphenpenalty=10000\n", "").replace(
        "\\makecvtitle", "\\makecvtitle\n\\csname input\\endcsname{x}\\catcode`\\@=11"
    )
    result = _check(main)
    assert result.ok
    assert any("\\catcode, \\csname" in w for w in result.warnings)
    assert any("hyphenation" in w for w in result.warnings)


def test_file_names_types_and_sizes() -> None:
    result = _check(
        CV,
        assets={"document.pdf": 10, "photo.jpg": 10, "run.sh": 10, "big.png": 6 * 1024 * 1024},
    )
    problems = " | ".join(result.problems)
    assert "document.pdf: the name 'document' is used by jaa's build" in problems
    assert "photo.jpg: the name 'photo'" in problems
    assert "run.sh: unsupported file type" in problems
    assert "more than 5 MB" in problems
    many = _check(CV, assets={f"img/{i}.png": 1 for i in range(100)})
    assert any("at most 100" in p for p in many.problems)


def test_shipped_templates_skip_the_upload_checks() -> None:
    result = check_template(
        "t.tex",
        {"t.tex": CV.replace("\\hyphenpenalty=10000\n", "")},
        {},
        TemplateKind.CV,
        uploaded=False,
    )
    assert result.ok and result.warnings == ()


def test_missing_main_file() -> None:
    result = check_template("main.tex", {"cv.tex": CV}, {}, TemplateKind.CV)
    assert result.problems == ("the main file main.tex is not among the template's files",)


def test_cover_letter_slots() -> None:
    letter = (
        "\\documentclass{moderncv}\n%%jaa:babel%%\n%%jaa:header%%\n%%jaa:letter%%\n"
        "\\begin{document}\n%%jaa:body%%\n\\end{document}\n"
    )
    assert check_template("l.tex", {"l.tex": letter}, {}, TemplateKind.COVER_LETTER).ok
    as_cv = check_template("l.tex", {"l.tex": letter}, {}, TemplateKind.CV)
    assert "unknown slot line %%jaa:letter%% for a cv template" in as_cv.problems
    assert "missing slot line %%jaa:consent%%" in as_cv.problems


def test_no_document_class_or_begin_document() -> None:
    result = _check("%%jaa:babel%%\n%%jaa:header%%\n%%jaa:body%%\n%%jaa:consent%%\n")
    assert "no \\begin{document} in the main file" in result.problems
    assert "no \\documentclass in the main file" in result.problems
